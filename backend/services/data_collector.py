"""Import Dunkest's public statistics table and normalize its player rows.

The public statistics page currently exposes season_id=29 (NBA 2026/27).
The endpoint is the same JSON route used by the public table page. No login,
private account data, or browser-cookie automation is used.
"""
import csv
import io
import os
import re
from datetime import date
from urllib.parse import urlencode

import requests

BASE_URL = "https://www.dunkest.com"
STATS_PAGE = f"{BASE_URL}/it/nba/statistiche"
DEFAULT_SEASON_ID = "29"  # confirmed by the public 2026/27 statistics page


def _first(row, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _float(value):
    if value is None or value == "":
        return 0.0
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _role(value):
    raw = str(value or "").strip().upper()
    aliases = {
        "C": "C", "CENTER": "C", "CENTRE": "C", "CENTRO": "C", "3": "C",
        "F": "F", "FORWARD": "F", "ALA": "F", "2": "F",
        "G": "G", "GUARD": "G", "GUARDIA": "G", "1": "G",
    }
    # Position IDs in Dunkest's public filter are 1=G, 2=F, 3=C.
    return aliases.get(raw, "")


def normalize(row, source="dunkest_live"):
    # The table API has used both flat and nested player/team payloads.
    if not isinstance(row, dict):
        return None
    player = row.get("player") if isinstance(row.get("player"), dict) else {}
    team_obj = row.get("team") if isinstance(row.get("team"), dict) else {}
    first = _first(row, "first_name", "firstname", default=_first(player, "first_name", "firstname", default=""))
    last = _first(row, "last_name", "lastname", default=_first(player, "last_name", "lastname", default=""))
    name = _first(row, "name", "full_name", "player_name", default=_first(player, "name", "full_name", default=f"{first} {last}".strip()))
    if not name:
        return None

    role_value = _first(row, "role", "position", "position_name", "position_id", "pos", default=_first(player, "position", "position_name", "position_id"))
    role = _role(role_value)
    price = _float(_first(row, "price", "cr", "credits", "value", default=_first(player, "price", "cr")))
    # Requested with stats_type=avg, so "pdk" is already a per-game average and
    # must NOT be divided by games played again. Only a real cumulative total
    # (pdk_tot/total_pdk) is converted to a per-game figure.
    pdk = _float(_first(row, "pdk", "fantasy_points", "points"))
    pdk_avg = _float(_first(row, "avg_points", "pdk_avg", "avg_pdk"))
    pdk_total = _float(_first(row, "pdk_tot", "total_pdk", "pdk_total"))
    games = _float(_first(row, "gp", "games_played", "games", default=1)) or 1
    avg_points = pdk_avg or (pdk_total / games if pdk_total else pdk)
    team = _first(row, "team_code", "team_name", "team_abbr", default=_first(team_obj, "code", "name", "abbreviation", default=team_obj if isinstance(team_obj, str) else ""))
    pid = _first(row, "id", "player_id", "athlete_id", default=_first(player, "id", "player_id", default=re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")))
    status = str(_first(row, "status", "availability", default="unknown")).strip().lower()
    return {
        "id": str(pid), "name": str(name).strip(), "team": str(team or ""),
        "role": role, "price": price,
        # Public Dunkest stats table does not publish a reliable future
        # projection; use its per-game PDK as a clearly labelled proxy.
        "projection": _float(_first(row, "projection", "projected_points", "fantasy_projection")),
        "avg_points": avg_points,
        "last5": _float(_first(row, "last5_avg", "last5", "last_5_avg", "last_5")),
        "status": status, "source": source,
    }


def _extract_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("players", "data", "table", "rows", "results", "items", "response", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                found = _extract_rows(value)
                if found:
                    return found
    return []


# NBA weeks already played/known: unknown or future week numbers are ignored
# by the API, so a wide fixed range keeps the request valid all season long.
MAX_WEEKS = 40


def _public_table_params():
    """Build query parameters in the format expected by Dunkest's PHP table API.

    The endpoint expects repeated array keys (positions[]=1), not indexed keys
    (positions[0]=1). Indexed keys can be silently ignored and return an empty
    or incomplete table.

    In dunkest mode the API also requires ``weeks[]``: without it the response
    is an empty list even though every other parameter is correct.
    """
    params = [
        ("season_id", os.getenv("DUNKEST_SEASON_ID", DEFAULT_SEASON_ID)),
        ("mode", "dunkest"),
        ("noadv", "no"),
        ("player_search", ""),
        ("min_cr", "4"),
        ("max_cr", "35"),
        ("sort_by", "pdk"),
        ("sort_order", "desc"),
        # The public table's per-game view is the useful input for the optimizer.
        ("stats_type", "avg"),
        ("iframe", "yes"),
        ("positions[]", "1"),
        ("positions[]", "2"),
        ("positions[]", "3"),
    ]
    params.extend(("weeks[]", str(week)) for week in range(1, MAX_WEEKS + 1))
    return params


def fetch_live():
    """Fetch and validate the live player list from Dunkest's public table."""
    url = os.getenv("DUNKEST_DATA_URL") or f"{BASE_URL}/api/stats/table"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": STATS_PAGE,
        "Origin": BASE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    params = _public_table_params() if "DUNKEST_DATA_URL" not in os.environ else None
    try:
        response = requests.get(url, params=params, headers=headers, timeout=(10, 40))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Impossibile raggiungere l'API pubblica Dunkest ({exc}). Controlla la connessione o riprova.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        preview = re.sub(r"\\s+", " ", response.text[:180])
        raise ValueError(
            f"Dunkest ha restituito una pagina HTML invece dei dati JSON "
            f"(HTTP {response.status_code}). Anteprima: {preview}"
        ) from exc

    raw_rows = _extract_rows(payload)
    rows = [normalize(r) for r in raw_rows]
    rows = [r for r in rows if r and r["name"] and r["price"] > 0 and r["role"] in {"C", "F", "G"}]
    if len(rows) < 10:
        # Do not write partial/invalid data into the local database.
        message = payload.get("message") if isinstance(payload, dict) else None
        raise ValueError(
            f"La risposta Dunkest non contiene un listone valido: "
            f"{len(rows)} giocatori validi su {len(raw_rows)} righe. "
            f"{message or 'Verifica stagione e struttura della risposta API.'}"
        )

    # Ensure IDs remain unique even when the API omits an ID or repeats it.
    seen = set()
    for player in rows:
        original = player["id"]
        candidate = original
        suffix = 2
        while candidate in seen:
            candidate = f"{original}-{player['team'] or player['role']}-{suffix}"
            suffix += 1
        player["id"] = candidate
        seen.add(candidate)
    return rows


def import_csv_bytes(data: bytes):
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = [normalize(r, source="csv") for r in reader]
    return [r for r in rows if r and r["id"] and r["name"] and r["role"] in {"C", "F", "G"} and r["price"] > 0]
