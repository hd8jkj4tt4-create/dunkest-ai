from fastapi import APIRouter, HTTPException
from backend.database import connect
from backend.services.coach_collector import fetch_coaches
from backend.services.sync_state import run_sync, synced_at

router = APIRouter(prefix="/api/coaches", tags=["coaches"])

COLUMNS = ("id,name,team,price,wins,losses,win_pct,ppg_for,ppg_against,diff,"
           "career_wins,career_losses,opponent_coach,record_match,projection,source")


def save(rows):
    with connect() as con:
        # Coaches are always replaced as a whole: a partial listone would
        # otherwise keep dismissed coaches with stale prices around.
        con.execute("DELETE FROM coaches")
        for c in rows:
            con.execute(
                f'''INSERT INTO coaches({COLUMNS},updated_at)
                VALUES({",".join("?" * 16)},CURRENT_TIMESTAMP)''',
                tuple(c.get(k) for k in COLUMNS.split(",")),
            )


@router.get("")
def list_coaches(sort: str = "projection"):
    allowed = {"projection", "price", "win_pct", "wins", "ppg_for", "name"}
    if sort not in allowed:
        sort = "projection"
    with connect() as con:
        rows = con.execute("SELECT * FROM coaches ORDER BY " + sort + " DESC").fetchall()
    return {"coaches": [dict(r) for r in rows], "count": len(rows), "synced_at": synced_at("coaches")}


@router.post("/sync")
def sync():
    def work():
        rows = fetch_coaches()
        save(rows)
        return rows
    try:
        rows, cached = run_sync("coaches", work)
    except Exception as e:
        raise HTTPException(503, f"Scansione allenatori non riuscita: {e}")
    if cached:
        with connect() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM coaches").fetchone()["n"]
    else:
        count = len(rows)
    return {
        "updated": count,
        "sources": ["Dunkest listone", "ESPN", "Basketball-Reference", "NBA.com (opzionale)"],
        "synced_at": synced_at("coaches"),
        "cached": cached,
    }
