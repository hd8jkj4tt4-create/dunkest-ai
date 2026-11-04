"""Fetch Dunkest's injury report and normalize it.

Requires a logged-in Dunkest session token in the DUNKEST_TOKEN env var
(read from .env). IDs from this endpoint do NOT match the public stats
table, so matching happens by player name + team.
"""
import os
import re

import requests

from backend.config import DUNKEST_TOKEN

INJURIES_URL = "https://fantaking-api.dunkest.com/api/v1/leagues/1/injuries?lang=it"

OUT_TOKENS = ("indisponibile", "out", "riposo", "operazion", "frattura",
              "sospeso", "non disponibile", "fuori")


def _status(injury: dict) -> str:
    text = f"{injury.get('reason', '')} {injury.get('description', '')}".lower()
    if any(t in text for t in OUT_TOKENS):
        return "out"
    # GTD / "da valutare" / unknown: still on the injury report, so the player
    # is flagged and excluded from team picking until cleared.
    return "injured"


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def normalize(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    first = str(row.get("first_name") or "").strip()
    last = str(row.get("last_name") or "").strip()
    name = f"{first} {last}".strip() or str(row.get("name") or "").strip()
    if not name:
        return None
    injury = row.get("injury") if isinstance(row.get("injury"), dict) else {}
    team = row.get("team") if isinstance(row.get("team"), dict) else {}
    detail = " ".join(
        str(x).strip() for x in (injury.get("reason"), injury.get("description")) if x
    )
    return {
        "id": str(row.get("id") or ""),
        "name": name,
        "team": str(team.get("abbreviation") or team.get("name") or "").strip(),
        "status": _status(injury),
        "detail": detail,
        "date": str(injury.get("date") or ""),
    }


def fetch_injuries() -> list[dict]:
    token = (DUNKEST_TOKEN or os.getenv("DUNKEST_TOKEN") or "").strip()
    if not token:
        raise ValueError("DUNKEST_TOKEN mancante: aggiungilo al file .env per sincronizzare gli infortuni.")
    headers = {
        "Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130 Safari/537.36",
    }
    try:
        response = requests.get(INJURIES_URL, headers=headers, timeout=(10, 40))
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Impossibile recuperare il listone infortuni Dunkest: {exc}") from exc

    players = payload.get("data", {}).get("players") if isinstance(payload, dict) else None
    if not isinstance(players, list):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise ValueError(f"Risposta infortuni non valida: {message or payload}")
    rows = [normalize(r) for r in players]
    return [r for r in rows if r]
