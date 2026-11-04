import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from backend.database import init_db
from backend.config import FRONTEND
from backend.routes import players, optimizer, coaches
from backend.services.sync_state import synced_at

logger = logging.getLogger("dunkest")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# Keep the public instance fresh without any click: refresh in the background
# on boot when the local snapshot is older than these thresholds.
STALE_AFTER_SECONDS = {"players": 3 * 3600, "coaches": 12 * 3600}


def _auto_refresh():
    from backend.services import sync_state
    from backend.services.data_collector import fetch_live
    from backend.services.coach_collector import fetch_coaches

    def work_players():
        rows = fetch_live()
        players.save(rows, replace_sources=players.LIVE_SOURCES)
        # Stats replace rows (and their injury flags), so re-apply injuries.
        try:
            players.sync_injuries()
        except (ValueError, RuntimeError) as exc:
            logger.warning("auto-refresh infortuni non riuscito: %s", exc)
        return rows

    def work_coaches():
        rows = fetch_coaches()
        coaches.save(rows)
        return rows

    for kind, work in (("players", work_players), ("coaches", work_coaches)):
        age = sync_state.age_seconds(kind)
        if age is not None and age < STALE_AFTER_SECONDS[kind]:
            continue
        try:
            rows, cached = sync_state.run_sync(kind, work)
            if not cached:
                logger.info("auto-refresh %s: %s righe aggiornate", kind, len(rows))
        except Exception as exc:
            # Never block boot: the seeded database keeps serving the UI.
            logger.warning("auto-refresh %s non riuscito: %s", kind, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_auto_refresh, daemon=True).start()
    yield


app = FastAPI(title="Dunkest AI Points Maximizer", version="0.4.0", lifespan=lifespan)
init_db()
app.include_router(players.router)
app.include_router(optimizer.router)
app.include_router(coaches.router)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/")
def home(): return FileResponse(FRONTEND / "index.html")

@app.get("/favicon.ico")
def favicon(): return FileResponse(FRONTEND / "favicon.svg", media_type="image/svg+xml")

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": "Dunkest AI Points Maximizer",
        "sync": {"players": synced_at("players"), "coaches": synced_at("coaches")},
    }
