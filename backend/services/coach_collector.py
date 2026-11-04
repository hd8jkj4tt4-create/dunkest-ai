"""Collect head coaches from the live Dunkest API and public sources.

Sources:
- Dunkest live API (fantaking-api.dunkest.com): current coach roster, price,
  matchday, opponent and actual fantasy points. The token is only required to
  discover coach IDs; profile/match-log endpoints are public.
- Dunkest listone (official Google Sheet): fallback price/name/team when the
  live API is unavailable or the token has expired.
- ESPN standings API: W-L, PPG for/against for the model.
- Basketball-Reference coaches page: W-L cross-check, career record.
- NBA.com stats API: optional cross-check, skipped gracefully when blocked.

Coach scoring follows the official Dunkest coach rules (docs.dunkest.com):
result + margin bonuses/penalties + points-for/against bonuses. The opponent
coefficient uses the real opponent coach price for the current matchday; if
the opponent is unknown it falls back to the average over all coach prices.
The live projection blends actual Dunkest scoring (60%) with the model (40%).
"""
import csv
import io
import json
import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.config import (
    DUNKEST_EMAIL,
    DUNKEST_GAME_ID,
    DUNKEST_PASSWORD,
    DUNKEST_TOKEN,
)

logger = logging.getLogger(__name__)

DUNKEST_API_BASE = "https://fantaking-api.dunkest.com/api/v1"
DUNKEST_LEAGUE_ID = int(os.getenv("DUNKEST_LEAGUE_ID", "1"))
DUNKEST_PLAYERS_LIST_ID = int(os.getenv("DUNKEST_PLAYERS_LIST_ID", "1"))
LIVE_WEIGHT = 0.6
MODEL_WEIGHT = 0.4

DUNKEST_LISTONE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1k-zD9sws6VZLLdgSNRoUS2_cQ_3N1W6znaJcF-hGSRU/export?format=csv"
)
ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
BBREF_COACHES_URL = "https://www.basketball-reference.com/leagues/NBA_2027_coaches.html"
NBA_STATS_URL = (
    "https://stats.nba.com/stats/leaguestandingsv3"
    "?LeagueID=00&Season=2026-27&SeasonType=Regular%20Season&Stage=0"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BBREF_ABBR_TO_TEAM = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BRK": "Brooklyn Nets",
    "CHO": "Charlotte Hornets", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets", "DET": "Detroit Pistons", "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets", "IND": "Indiana Pacers", "LAC": "Los Angeles Clippers",
    "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers", "PHO": "Phoenix Suns", "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}

# Official Dunkest coach table: (max_margin, points) for wins and losses.
WIN_BUCKETS = [(5, 15), (10, 18), (15, 20), (20, 23), (25, 25),
               (30, 28), (35, 30), (40, 33), (float("inf"), 35)]
LOSS_BUCKETS = [(5, -7), (10, -10), (15, -13), (20, -15), (25, -18),
                (30, -20), (35, -23), (40, -25), (float("inf"), -30)]

# Official Dunkest coefficient table: (min_abs_diff, higher_price, lower_price).
COEFF_TABLE = [(16.0, 0.70, 1.60), (12.0, 0.75, 1.50), (8.0, 0.79, 1.41),
               (6.0, 0.83, 1.32), (4.0, 0.87, 1.23), (2.0, 0.91, 1.14),
               (1.0, 0.95, 1.05), (0.0, 1.00, 1.00)]

# Sources disagree on a few display names (ESPN vs Dunkest listone).
TEAM_NAME_ALIASES = {"LA Clippers": "Los Angeles Clippers"}


def normalize_team_name(name):
    s = str(name or "").strip()
    return TEAM_NAME_ALIASES.get(s, s)


def pair_coefficient(price_self, price_other):
    """Official coefficient applied to one coach's score against another."""
    price_self, price_other = _float(price_self), _float(price_other)
    diff = abs(price_self - price_other)
    for minimum, higher, lower in COEFF_TABLE:
        if diff >= minimum:
            return higher if price_self >= price_other else lower
    return 1.00


def expected_coefficient(price, other_prices):
    """Average (win, loss) coefficients for a coach over the listone prices.

    The real coefficient needs the matchday opponent, which is unknown before
    the round: averaging it over every other coach's price is the closest
    unbiased estimate. Wins multiply by the table value, losses divide by it,
    so the two expected multipliers differ.
    """
    price = _float(price)
    others = [_float(p) for p in other_prices if _float(p) > 0]
    if not others:
        return 1.0, 1.0
    wins = []
    losses = []
    for other in others:
        value = pair_coefficient(price, other)
        wins.append(value)
        losses.append(1.0 / value)
    return round(sum(wins) / len(wins), 4), round(sum(losses) / len(losses), 4)


def _http_get(url, timeout=(10, 40), headers=None):
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    req = Request(url, headers=merged)
    with urlopen(req, timeout=timeout[1]) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _float(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_token_cache = {"value": DUNKEST_TOKEN}


def _token_from_payload(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("token", "access_token", "accessToken"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("token", "access_token", "accessToken"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _login_token():
    """Log in with optional DUNKEST_EMAIL/DUNKEST_PASSWORD credentials."""
    if not DUNKEST_EMAIL or not DUNKEST_PASSWORD:
        return ""
    try:
        game_id = int(DUNKEST_GAME_ID)
    except (TypeError, ValueError):
        game_id = 1
    body = json.dumps({
        "email": DUNKEST_EMAIL,
        "password": DUNKEST_PASSWORD,
        "game_id": game_id,
    }).encode()
    request = Request(
        f"{DUNKEST_API_BASE}/login", data=body, method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, OSError, ValueError):
        return ""
    return _token_from_payload(payload)


def _current_token(force_login=False):
    if force_login or not _token_cache["value"]:
        token = _login_token()
        if token:
            _token_cache["value"] = token
    return _token_cache["value"]


def _api_get(path, token=None, timeout=40):
    url = path if path.startswith("http") else f"{DUNKEST_API_BASE}{path}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _ref_from_list_row(row):
    team = row.get("team") or {}
    return {
        "dunkest_id": int(row["id"]),
        "name": f"{row.get('first_name', '')} {row.get('last_name', '')}".strip(),
        "team": team.get("name") or "",
        "team_abbr": team.get("abbreviation") or "",
        "list_price": _float(row.get("quotation")),
        "is_injured": bool(row.get("is_injured")),
    }


def _coach_refs_from_list(matchday, token):
    """Head coaches of one matchday from the authenticated players list."""
    data = _api_get(
        f"/players-lists/{DUNKEST_PLAYERS_LIST_ID}/matchdays/{matchday}/players"
        "?per_page=1000&page=1",
        token,
    )
    rows = data.get("data") if isinstance(data, dict) else data
    refs = []
    for row in rows or []:
        if (row.get("position") or {}).get("id") != 4:
            continue
        ref = _ref_from_list_row(row)
        if ref["dunkest_id"] and ref["name"] and ref["team"]:
            refs.append(ref)
    return refs


def _discover_coach_refs():
    """Discover coach IDs with the token, retrying once after a fresh login."""
    token = _current_token()
    if not token:
        return []
    for attempt in range(2):
        try:
            for matchday in range(1, 21):
                try:
                    refs = _coach_refs_from_list(matchday, token)
                except HTTPError as exc:
                    if exc.code in (401, 403):
                        raise
                    continue
                if len(refs) >= 20:
                    return refs
            return []
        except HTTPError as exc:
            if exc.code not in (401, 403) or attempt:
                return []
            token = _current_token(force_login=True)
            if not token:
                return []
    return []


def _persisted_coach_refs():
    """Coach IDs saved by a previous sync: allow live updates without a token."""
    try:
        from backend.database import connect
        with connect() as con:
            rows = con.execute(
                "SELECT dunkest_id,name,team FROM coaches "
                "WHERE dunkest_id IS NOT NULL AND dunkest_id > 0"
            ).fetchall()
    except Exception:
        return []
    return [{
        "dunkest_id": int(row["dunkest_id"]),
        "name": row["name"] or "",
        "team": row["team"] or "",
        "team_abbr": "",
        "list_price": 0.0,
        "is_injured": False,
    } for row in rows]


def _coach_log(coach_id):
    data = _api_get(
        f"/basketball/leagues/{DUNKEST_LEAGUE_ID}/players/{coach_id}/match-log"
    )
    if isinstance(data, dict):
        return data.get("data") or []
    return data or []


def _fetch_logs(refs):
    def work(ref):
        coach_id = ref["dunkest_id"]
        try:
            return coach_id, _coach_log(coach_id)
        except Exception:
            return coach_id, []

    logs = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for coach_id, log in pool.map(work, refs):
            if log:
                logs[coach_id] = log
    return logs


def _mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 2) if values else 0.0


def _current_matchday(logs):
    counts = Counter()
    for log in logs.values():
        for row in log:
            number = row.get("matchday_number")
            if number:
                counts[number] += 1
                break
    return counts.most_common(1)[0][0] if counts else None


def _row_for_matchday(log, matchday):
    for row in log:
        if row.get("matchday_number") == matchday:
            return row
    return None


def blend_live_projection(live_avg, model_projection, has_history=False):
    """Blend actual Dunkest points (60%) with the regulation model (40%)."""
    live_avg = _float(live_avg)
    model_projection = _float(model_projection)
    has_live = bool(has_history) or live_avg != 0
    if has_live and model_projection != 0:
        return round(LIVE_WEIGHT * live_avg + MODEL_WEIGHT * model_projection, 2)
    if has_live:
        return round(live_avg, 2)
    return round(model_projection, 2)


def _sheet_price_index(listone):
    by_name, by_team = {}, {}
    for coach in listone or []:
        price = _float(coach.get("price"))
        if price <= 0:
            continue
        name = str(coach.get("name", "")).strip().lower()
        team = normalize_team_name(coach.get("team")).lower()
        if name:
            by_name[name] = price
        if team:
            by_team[team] = price
    return {"name": by_name, "team": by_team}


def _sheet_price(ref, index):
    price = index["name"].get(str(ref.get("name", "")).strip().lower())
    if price:
        return _float(price)
    return _float(index["team"].get(normalize_team_name(ref.get("team")).lower()))


def fetch_dunkest_live_coaches(espn=None, bbref=None, nba=None, listone=None):
    """Build coach rows from the live Dunkest API.

    The authenticated list is only used to discover coach IDs. All the data
    used here (match log: opponent, price, fantasy points) is public, so the
    sync keeps working without a token after the first successful run.
    """
    espn, bbref, nba = espn or {}, bbref or {}, nba or {}
    refs = _discover_coach_refs()
    if len(refs) < 20:
        refs = _persisted_coach_refs()
    if len(refs) < 20:
        raise ValueError(
            "token Dunkest assente/scaduto e nessun ID allenatore salvato: "
            "imposta DUNKEST_TOKEN o DUNKEST_EMAIL/DUNKEST_PASSWORD."
        )

    logs = _fetch_logs(refs)
    if len(logs) < 20:
        raise ValueError(f"match-log Dunkest non disponibile per abbastanza allenatori ({len(logs)}).")
    current_matchday = _current_matchday(logs)
    if not current_matchday:
        raise ValueError("matchday corrente Dunkest non rilevato dal match-log.")

    # With a working token, refresh the current roster from the official list
    # so mid-season coach changes are picked up automatically.
    token = _current_token()
    if token:
        try:
            current_refs = _coach_refs_from_list(current_matchday, token)
            if len(current_refs) >= 20:
                missing = [ref for ref in current_refs if ref["dunkest_id"] not in logs]
                if missing:
                    logs.update(_fetch_logs(missing))
                refs = current_refs
        except Exception:
            pass

    sheet_prices = _sheet_price_index(listone)
    prices, price_source, current_rows = {}, {}, {}
    for ref in refs:
        coach_id = ref["dunkest_id"]
        log = logs.get(coach_id, [])
        row = _row_for_matchday(log, current_matchday)
        current_rows[coach_id] = row
        price = _float(row.get("quotation")) if row else 0.0
        source = "dunkest_live"
        if price <= 0:
            price = _float(ref.get("list_price"))
        if price <= 0:
            price = _sheet_price(ref, sheet_prices)
            source = "dunkest_listone"
        if price > 0:
            prices[coach_id] = price
            price_source[coach_id] = source

    prices_by_team = {}
    coach_by_team = {}
    for ref in refs:
        team = normalize_team_name(ref.get("team"))
        if team:
            coach_by_team[team] = ref.get("name", "")
            if prices.get(ref["dunkest_id"]):
                prices_by_team[team] = prices[ref["dunkest_id"]]

    all_prices = [prices[ref["dunkest_id"]] for ref in refs if ref["dunkest_id"] in prices]
    rows = []
    for index, ref in enumerate(refs):
        coach_id = ref["dunkest_id"]
        price = prices.get(coach_id, 0.0)
        if price <= 0:
            continue
        log = logs.get(coach_id, [])
        row = current_rows.get(coach_id) or {}
        team = ref.get("team") or ""
        record = espn.get(normalize_team_name(team), {})
        cross = bbref.get(team, {})
        nba_rec = nba.get(team, {})

        wins = int(record.get("wins", 0))
        losses = int(record.get("losses", 0))
        ppg_for = _float(record.get("ppg_for"))
        ppg_against = _float(record.get("ppg_against"))
        if record:
            win_pct = _float(record.get("win_pct"))
            if not win_pct and wins + losses:
                win_pct = round(wins / (wins + losses), 4)
        elif cross:
            wins, losses = int(cross.get("wins", 0)), int(cross.get("losses", 0))
            win_pct = round(wins / (wins + losses), 4) if wins + losses else 0.0
        elif nba_rec:
            wins, losses = int(nba_rec.get("wins", 0)), int(nba_rec.get("losses", 0))
            win_pct = round(wins / (wins + losses), 4) if wins + losses else 0.0
            ppg_for = _float(nba_rec.get("ppg_for"))
        else:
            win_pct = 0.0

        record_match = None
        if record and cross:
            record_match = (wins == cross.get("wins") and losses == cross.get("losses"))

        opponent = (row.get("opponent_team") or {})
        opponent_name = opponent.get("name") or ""
        opponent_price = prices_by_team.get(normalize_team_name(opponent_name), 0.0)
        if opponent_price > 0:
            coefficient = pair_coefficient(price, opponent_price)
            loss_coefficient = round(1.0 / coefficient, 4) if coefficient else 1.0
        else:
            others = all_prices[:index] + all_prices[index + 1:]
            coefficient, loss_coefficient = expected_coefficient(price, others)

        model_projection = 0.0
        if record or cross or nba_rec:
            model_projection = expected_coach_points(
                win_pct, ppg_for, ppg_against, coefficient, loss_coefficient)

        past = [
            entry for entry in log
            if entry.get("matchday_number") is not None
            and entry.get("matchday_number") < current_matchday
            and entry.get("fantasy_pts") is not None
        ]
        past.sort(key=lambda entry: entry["matchday_number"], reverse=True)
        live_avg = _mean([entry["fantasy_pts"] for entry in past])
        last5 = _mean([entry["fantasy_pts"] for entry in past[:5]])

        sources = [price_source.get(coach_id, "dunkest_live")]
        if record:
            sources.append("espn")
        if cross:
            sources.append("basketball_reference")
        if nba_rec:
            sources.append("nba.com")

        rows.append({
            "id": re.sub(r"[^a-z0-9]+", "-", f"{ref.get('name', '')}-{team}".lower()).strip("-"),
            "name": ref.get("name", ""),
            "team": team,
            "price": price,
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "ppg_for": round(ppg_for, 2),
            "ppg_against": round(ppg_against, 2),
            "diff": _float(record.get("diff")),
            "career_wins": int(cross.get("career_wins", 0)),
            "career_losses": int(cross.get("career_losses", 0)),
            "opponent_coach": coach_by_team.get(normalize_team_name(opponent_name), ""),
            "record_match": record_match,
            "projection": blend_live_projection(live_avg, model_projection, bool(past)),
            "coefficient_win": coefficient,
            "coefficient_loss": loss_coefficient,
            "source": "+".join(sources),
            "dunkest_id": coach_id,
            "matchday": current_matchday,
            "opponent": opponent_name,
            "opponent_price": opponent_price,
            "live_avg": live_avg,
            "last5": last5,
            "model_projection": round(model_projection, 2),
            "is_injured": int(bool(ref.get("is_injured"))),
        })
    return [row for row in rows if row["price"] > 0]


def _int(value):
    return int(round(_float(value)))


def _clean_coach_name(first, last):
    # The published sheet escapes dots in initials as commas: "J,B," + "Bickerstaff".
    name = f"{first} {last}".replace(",", ".").strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\.{2,}", ".", name)
    return name.strip(" .")


def _margin_bucket(margin, buckets):
    margin = abs(margin)
    for limit, points in buckets:
        if margin <= limit:
            return points
    return buckets[-1][1]


def expected_coach_points(win_pct, ppg_for, ppg_against, coefficient=1.0, loss_coefficient=None):
    """Expected Dunkest coach points per game from a team's season averages."""
    if loss_coefficient is None:
        loss_coefficient = coefficient
    win_pct = max(0.0, min(1.0, _float(win_pct)))
    ppg_for = _float(ppg_for)
    ppg_against = _float(ppg_against)
    has_ppg = ppg_for > 0 and ppg_against > 0
    if has_ppg:
        margin = abs(ppg_for - ppg_against)
    else:
        # No scoring data: estimate the margin from the record alone instead
        # of treating the missing 0 PPG as a real (and rewarding) value.
        margin = abs(20.0 * (2 * win_pct - 1))
    win_part = win_pct * _margin_bucket(margin, WIN_BUCKETS)
    loss_part = (1.0 - win_pct) * _margin_bucket(margin, LOSS_BUCKETS)
    bonus = 0.0
    if has_ppg:
        # Official rules: >=120 scored +5, <=95 scored -3,
        # <=95 conceded +5, >=120 conceded -3.
        if ppg_for >= 120:
            bonus += 5
        elif ppg_for <= 95:
            bonus -= 3
        if ppg_against <= 95:
            bonus += 5
        elif ppg_against >= 120:
            bonus -= 3
    score = win_part * coefficient + loss_part * loss_coefficient + \
        bonus * (win_pct * coefficient + (1.0 - win_pct) * loss_coefficient)
    return round(score, 2)


def fetch_dunkest_listone(url=None):
    """Official Dunkest listone: coach name, team and credits."""
    url = url or os.getenv("DUNKEST_LISTONE_URL", DUNKEST_LISTONE_URL)
    text = _http_get(url)
    coaches = []
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("Position", "")).strip() != "Head Coach":
            continue
        name = _clean_coach_name(row.get("First Name", ""), row.get("Last Name", ""))
        team = str(row.get("Team", "")).strip()
        price = _float(row.get("Cr 26/27") or row.get("Cr"))
        if not name or not team or price <= 0:
            continue
        coaches.append({"name": name, "team": team, "price": price})
    return coaches


def fetch_espn_records():
    """Team records (W-L, PPG) from ESPN's public standings API."""
    import json
    data = json.loads(_http_get(ESPN_STANDINGS_URL))
    records = {}
    for conference in data.get("children", []):
        for entry in conference.get("standings", {}).get("entries", []):
            team = entry.get("team", {})
            name = normalize_team_name(team.get("displayName", ""))
            stats = {s.get("name"): s.get("value") for s in entry.get("stats", [])}
            wins = _int(stats.get("wins"))
            losses = _int(stats.get("losses"))
            games = wins + losses
            records[name] = {
                "wins": wins, "losses": losses,
                "win_pct": round(wins / games, 4) if games else 0.0,
                "ppg_for": round(_float(stats.get("avgPointsFor")), 2),
                "ppg_against": round(_float(stats.get("avgPointsAgainst")), 2),
                "diff": round(_float(stats.get("differential")), 2),
            }
    return records


def fetch_bballref_records():
    """Current and career W-L per team from Basketball-Reference."""
    html = _http_get(BBREF_COACHES_URL)
    table = re.search(r'<table[^>]*id="NBA_coaches".*?</table>', html, re.S)
    if not table:
        raise ValueError("Basketball-Reference: tabella allenatori non trovata.")
    records = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(0), re.S):
        cells = dict(re.findall(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', tr, re.S))

        def text(key):
            return re.sub(r"<[^>]+>", "", cells.get(key, "")).strip()

        abbr = text("team")
        if abbr not in BBREF_ABBR_TO_TEAM:
            continue
        records[BBREF_ABBR_TO_TEAM[abbr]] = {
            "coach": text("coach"),
            "wins": _int(text("cur_w")), "losses": _int(text("cur_l")),
            "career_wins": _int(text("car_w")), "career_losses": _int(text("car_l")),
        }
    return records


def fetch_nba_records():
    """Optional NBA.com cross-check; returns {} when the API is unreachable."""
    import json
    try:
        data = json.loads(_http_get(
            NBA_STATS_URL,
            headers={
                "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
                "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
                "Accept": "application/json",
            },
        ))
    except (URLError, ValueError, OSError):
        return {}
    rows = (data.get("resultSets") or [{}])[0].get("rowSet") or []
    headers = (data.get("resultSets") or [{}])[0].get("headers") or []
    index = {name: i for i, name in enumerate(headers)}

    def cell(row, *names):
        for name in names:
            if name in index:
                return row[index[name]]
        return 0

    abbr_key = next((k for k in ("TeamAbbreviation", "TEAM_ABBREVIATION") if k in index), None)
    if not abbr_key:
        return {}
    records = {}
    for row in rows:
        team = BBREF_ABBR_TO_TEAM.get(str(row[index[abbr_key]]))
        if not team:
            continue
        wins = _int(cell(row, "Wins", "W"))
        losses = _int(cell(row, "Losses", "L"))
        records[team] = {
            "wins": wins, "losses": losses,
            "ppg_for": round(_float(cell(row, "PointsPerGame", "PTS_PG", "PPG")), 2),
        }
    return records


def merge_sources(listone, espn, bbref, nba=None):
    """Join every source on the team name; price comes from Dunkest."""
    nba = nba or {}
    espn = {normalize_team_name(k): v for k, v in espn.items()}
    all_prices = [coach["price"] for coach in listone]
    rows = []
    for idx, coach in enumerate(listone):
        team = coach["team"]
        record = espn.get(team, {})
        cross = bbref.get(team, {})
        nba_rec = nba.get(team, {})
        wins, losses = record.get("wins", 0), record.get("losses", 0)
        win_pct = record.get("win_pct") or (round(wins / (wins + losses), 4) if wins + losses else 0.0)
        ppg_for, ppg_against = record.get("ppg_for", 0.0), record.get("ppg_against", 0.0)
        if not record and cross:
            # ESPN missing: fall back to Basketball-Reference (no PPG available).
            wins, losses = cross.get("wins", 0), cross.get("losses", 0)
            win_pct = round(wins / (wins + losses), 4) if wins + losses else 0.0
        record_match = None
        if record and cross:
            record_match = (wins == cross.get("wins") and losses == cross.get("losses"))
        sources = ["dunkest_listone"]
        if record:
            sources.append("espn")
        if cross:
            sources.append("basketball_reference")
        if nba_rec:
            sources.append("nba.com")
        m_win, m_loss = expected_coefficient(
            coach["price"], all_prices[:idx] + all_prices[idx + 1:])
        rows.append({
            "id": re.sub(r"[^a-z0-9]+", "-", f"{coach['name']}-{team}".lower()).strip("-"),
            "name": coach["name"], "team": team, "price": coach["price"],
            "wins": wins, "losses": losses, "win_pct": win_pct,
            "ppg_for": ppg_for, "ppg_against": ppg_against,
            "diff": record.get("diff", 0.0),
            "career_wins": cross.get("career_wins", 0),
            "career_losses": cross.get("career_losses", 0),
            "opponent_coach": cross.get("coach", ""),
            "record_match": record_match,
            "projection": expected_coach_points(
                win_pct, ppg_for, ppg_against, m_win, m_loss),
            "coefficient_win": m_win,
            "coefficient_loss": m_loss,
            "source": "+".join(sources),
        })
    return rows


def _safe_fetch(func, default):
    try:
        return func()
    except Exception as exc:
        logger.warning("%s non disponibile: %s", func.__name__, exc)
        return default


def _ensure_unique_ids(rows):
    seen = set()
    for row in rows:
        candidate, suffix = row["id"], 2
        while candidate in seen:
            candidate = f"{row['id']}-{suffix}"
            suffix += 1
        row["id"] = candidate
        seen.add(candidate)
    return rows


def fetch_coaches():
    """Prefer live Dunkest data; fall back to the published listone."""
    espn = _safe_fetch(fetch_espn_records, {})
    bbref = _safe_fetch(fetch_bballref_records, {})
    nba = fetch_nba_records()
    listone = _safe_fetch(fetch_dunkest_listone, [])

    try:
        live_rows = fetch_dunkest_live_coaches(espn, bbref, nba, listone)
        if len(live_rows) >= 20:
            return _ensure_unique_ids(live_rows)
        logger.warning("Dunkest live: solo %s allenatori validi", len(live_rows))
    except Exception as exc:
        logger.warning("Dunkest live non disponibile, uso il listone: %s", exc)

    if len(listone) < 20:
        raise ValueError(
            f"Nessuna fonte allenatori valida: live non disponibile e listone con "
            f"{len(listone)} allenatori."
        )
    rows = merge_sources(listone, espn, bbref, nba)
    rows = [row for row in rows if row["price"] > 0]
    if len(rows) < 20:
        raise ValueError(f"Fonti incomplete: {len(rows)} allenatori validi.")
    if not espn and not bbref and not nba:
        raise ValueError(
            "Nessuna fonte di risultati raggiungibile (ESPN, Basketball-Reference, NBA.com): "
            "impossibile stimare il punteggio degli allenatori."
        )
    return _ensure_unique_ids(rows)
