from fastapi import APIRouter, UploadFile, File, HTTPException
from backend.database import connect
from backend.services.data_collector import import_csv_bytes, fetch_live
from backend.services.injury_collector import fetch_injuries, normalize_key
from backend.services.sync_state import run_sync, synced_at
router = APIRouter(prefix="/api/players", tags=["players"])


LIVE_SOURCES = ("dunkest_live", "dunkest_api")


def save(rows, replace_sources=()):
    with connect() as con:
        if replace_sources:
            # Replace those sources' previous listone in the same transaction, so
            # players removed from the live table cannot linger with stale data.
            con.execute(
                "DELETE FROM players WHERE source IN (%s)" % ",".join("?" * len(replace_sources)),
                replace_sources,
            )
        for p in rows:
            con.execute('''INSERT INTO players(id,name,team,role,price,projection,avg_points,last5,status,source,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,team=excluded.team,role=excluded.role,
            price=excluded.price,projection=excluded.projection,avg_points=excluded.avg_points,
            last5=excluded.last5,status=excluded.status,source=excluded.source,updated_at=CURRENT_TIMESTAMP''',
            (p["id"],p["name"],p["team"],p["role"],p["price"],p["projection"],p["avg_points"],p["last5"],p["status"],p["source"]))


def apply_injuries(injuries):
    """Mark injured players; clear previous injury marks first.

    The injuries endpoint uses different IDs than the stats table, so rows are
    matched by normalized name, preferring the same team abbreviation.
    """
    with connect() as con:
        # Previous injury marks are cleared; their status returns to 'unknown'.
        con.execute("""UPDATE players SET injured=0, status_detail='', status='unknown'
            WHERE injured=1""")
        by_name = {}
        by_last_team = {}
        for r in con.execute("SELECT id, name, team FROM players"):
            key = normalize_key(r["name"])
            by_name.setdefault(key, []).append(r)
            parts = key.split()
            if len(parts) > 1:
                by_last_team.setdefault((parts[-1], str(r["team"] or "").upper()), []).append(r)
        matched = 0
        for inj in injuries:
            candidates = by_name.get(normalize_key(inj["name"]), [])
            if not candidates:
                # Variant first names (e.g. "Alex" vs "Alexandre"): fall back to
                # a unique last-name + team match.
                parts = normalize_key(inj["name"]).split()
                if len(parts) > 1:
                    candidates = by_last_team.get((parts[-1], inj.get("team", "").upper()), [])
                    if len(candidates) != 1:
                        continue
            team = inj.get("team", "").upper()
            row = next((c for c in candidates if str(c["team"] or "").upper() == team), None) \
                or (candidates[0] if len(candidates) == 1 else None)
            if row is None:
                continue
            con.execute("""UPDATE players SET injured=1, status=?, status_detail=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (inj["status"], inj.get("detail", ""), row["id"]))
            matched += 1
    return matched


def sync_injuries():
    injuries = fetch_injuries()
    matched = apply_injuries(injuries)
    return injuries, matched


@router.get("")
def list_players(q: str = "", role: str = "", sort: str = "projection", order: str = "desc"):
    allowed = {"projection", "price", "avg_points", "last5", "name"}
    if sort not in allowed: sort = "projection"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    if sort == "name" and order == "":
        direction = "ASC"
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM players WHERE name LIKE ? AND role LIKE ? ORDER BY " + sort + " " + direction,
            (f"%{q}%", f"%{role.upper()}%"),
        ).fetchall()
    return {"players": [dict(r) for r in rows], "count": len(rows), "synced_at": synced_at("players")}


@router.post("/import-csv")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"): raise HTTPException(400, "Carica un file CSV.")
    try: rows = import_csv_bytes(await file.read())
    except Exception as e: raise HTTPException(400, str(e))
    if len(rows) < 10: raise HTTPException(400, f"CSV non valido: trovati solo {len(rows)} giocatori con ruolo e crediti validi.")
    save(rows)
    return {"imported": len(rows)}


@router.post("/injuries-sync")
def injuries_sync():
    try:
        injuries, matched = sync_injuries()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(503, str(e))
    return {"flagged": len(injuries), "matched": matched,
            "synced_at": synced_at("players")}


@router.post("/sync")
def sync():
    def work():
        rows = fetch_live()
        # Save only after a complete, validated response. A failed scan never
        # destroys the last known-good local database.
        save(rows, replace_sources=LIVE_SOURCES)
        # Stats replace rows (and their injury flags), so re-apply injuries.
        try:
            sync_injuries()
        except (ValueError, RuntimeError):
            pass
        return rows
    try:
        rows, cached = run_sync("players", work)
    except Exception as e:
        raise HTTPException(503, f"Scansione live Dunkest non riuscita: {e}")
    if cached:
        with connect() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
    else:
        count = len(rows)
    return {"updated": count, "source": "Dunkest public stats", "season": "2026/27",
            "synced_at": synced_at("players"), "cached": cached}
