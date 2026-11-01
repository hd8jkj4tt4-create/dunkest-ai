from fastapi import APIRouter, UploadFile, File, HTTPException
from backend.database import connect
from backend.services.data_collector import import_csv_bytes, fetch_live
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


@router.get("")
def list_players(q: str = "", role: str = "", sort: str = "projection"):
    allowed = {"projection", "price", "avg_points", "last5", "name"}
    if sort not in allowed: sort = "projection"
    with connect() as con:
        rows = con.execute("SELECT * FROM players WHERE name LIKE ? AND role LIKE ? ORDER BY " + sort + " DESC", (f"%{q}%", f"%{role.upper()}%")).fetchall()
    return {"players": [dict(r) for r in rows], "count": len(rows), "synced_at": synced_at("players")}


@router.post("/import-csv")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"): raise HTTPException(400, "Carica un file CSV.")
    try: rows = import_csv_bytes(await file.read())
    except Exception as e: raise HTTPException(400, str(e))
    if len(rows) < 10: raise HTTPException(400, f"CSV non valido: trovati solo {len(rows)} giocatori con ruolo e crediti validi.")
    save(rows)
    return {"imported": len(rows)}


@router.post("/sync")
def sync():
    def work():
        rows = fetch_live()
        # Save only after a complete, validated response. A failed scan never
        # destroys the last known-good local database.
        save(rows, replace_sources=LIVE_SOURCES)
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
