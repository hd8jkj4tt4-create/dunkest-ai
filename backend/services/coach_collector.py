"""Collect head coaches from several public sources and score them.

Sources (compared and merged):
- Dunkest listone (official Google Sheet published by Dunkest) -> credits price
- ESPN standings API                          -> W-L, PPG for/against
- Basketball-Reference coaches page           -> W-L cross-check, career record
- NBA.com stats API (stats.nba.com)           -> optional cross-check, may be
  unreachable from some networks and is skipped gracefully when so.

Coach scoring follows the official Dunkest coach rules (docs.dunkest.com):
result + margin bonuses/penalties + points-for/against bonuses, with the
opponent-difficulty coefficient left at 1.0 (unknown before the matchday).
"""
import csv
import io
import os
import re
from urllib.request import Request, urlopen
from urllib.error import URLError

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


def expected_coach_points(win_pct, ppg_for, ppg_against, coefficient=1.0):
    """Expected Dunkest coach points per game from a team's season averages."""
    win_pct = max(0.0, min(1.0, _float(win_pct)))
    ppg_for = _float(ppg_for)
    ppg_against = _float(ppg_against)
    margin = abs(ppg_for - ppg_against)
    base = win_pct * _margin_bucket(margin, WIN_BUCKETS) + \
        (1.0 - win_pct) * _margin_bucket(margin, LOSS_BUCKETS)
    bonus = 0.0
    if ppg_for >= 120:
        bonus += 5
    elif ppg_for < 95:
        bonus -= 3
    if ppg_against <= 95:
        bonus += 5
    elif ppg_against > 120:
        bonus -= 3
    score = (base + bonus) * coefficient
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
            name = str(team.get("displayName", "")).strip()
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
    rows = []
    for coach in listone:
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
            "projection": expected_coach_points(win_pct, ppg_for, ppg_against),
            "source": "+".join(sources),
        })
    return rows


def fetch_coaches():
    """Fetch every source, compare them and return validated coach rows."""
    listone = fetch_dunkest_listone()
    if len(listone) < 20:
        raise ValueError(f"Listone Dunkest non valido: {len(listone)} allenatori trovati.")
    try:
        espn = fetch_espn_records()
    except Exception:
        espn = {}
    try:
        bbref = fetch_bballref_records()
    except Exception:
        bbref = {}
    nba = fetch_nba_records()
    rows = merge_sources(listone, espn, bbref, nba)
    rows = [r for r in rows if r["price"] > 0]
    if len(rows) < 20:
        raise ValueError(f"Fonti incomplete: {len(rows)} allenatori validi.")
    if not espn and not bbref and not nba:
        raise ValueError(
            "Nessuna fonte di risultati raggiungibile (ESPN, Basketball-Reference, NBA.com): "
            "impossibile stimare il punteggio degli allenatori."
        )
    seen = set()
    for row in rows:
        candidate, suffix = row["id"], 2
        while candidate in seen:
            candidate = f"{row['id']}-{suffix}"
            suffix += 1
        row["id"] = candidate
        seen.add(candidate)
    return rows
