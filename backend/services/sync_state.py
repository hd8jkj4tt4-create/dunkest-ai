"""Single-flight sync with cooldown and persisted timestamps.

Public deployments must not let visitors stampede the upstream sites: one
sync per kind at a time, at most once every COOLDOWN_SECONDS. The last
successful sync time lives in the settings table so it survives restarts.
"""
import threading
import time
from datetime import datetime, timezone

from backend.database import connect

COOLDOWN_SECONDS = 300

_locks = {"players": threading.Lock(), "coaches": threading.Lock()}
_last_success = {"players": 0.0, "coaches": 0.0}


def _setting_key(kind):
    return f"{kind}_synced_at"


def mark_synced(kind):
    now = datetime.now(timezone.utc)
    with connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_setting_key(kind), now.isoformat()),
        )
    _last_success[kind] = time.time()


def synced_at(kind):
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (_setting_key(kind),)).fetchone()
    return row["value"] if row else None


def age_seconds(kind):
    iso = synced_at(kind)
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def run_sync(kind, work):
    """Run `work()` (must return the fetched rows) for `kind`.

    Returns (rows, cached). Raises RuntimeError when another sync is running;
    returns (None, True) when the cooldown is still active.
    """
    if _last_success[kind] and time.time() - _last_success[kind] < COOLDOWN_SECONDS:
        return None, True
    age = age_seconds(kind)
    if age is not None and age < COOLDOWN_SECONDS:
        return None, True
    if not _locks[kind].acquire(blocking=False):
        raise RuntimeError("Sincronizzazione già in corso: riprova tra qualche secondo.")
    try:
        rows = work()
        mark_synced(kind)
        return rows, False
    finally:
        _locks[kind].release()
