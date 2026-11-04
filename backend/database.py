import sqlite3
from backend.config import DB_PATH

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS players (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, team TEXT DEFAULT '',
            role TEXT NOT NULL, price REAL NOT NULL, projection REAL NOT NULL DEFAULT 0,
            avg_points REAL DEFAULT 0, last5 REAL DEFAULT 0, status TEXT DEFAULT 'unknown',
            injured INTEGER DEFAULT 0, status_detail TEXT DEFAULT '',
            source TEXT DEFAULT 'manual', updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        # Migrate databases created before the injury columns existed.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(players)").fetchall()}
        if "injured" not in cols:
            con.execute("ALTER TABLE players ADD COLUMN injured INTEGER DEFAULT 0")
        if "status_detail" not in cols:
            con.execute("ALTER TABLE players ADD COLUMN status_detail TEXT DEFAULT ''")
        con.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS coaches (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, team TEXT DEFAULT '',
            price REAL NOT NULL, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            win_pct REAL DEFAULT 0, ppg_for REAL DEFAULT 0, ppg_against REAL DEFAULT 0,
            diff REAL DEFAULT 0, career_wins INTEGER DEFAULT 0, career_losses INTEGER DEFAULT 0,
            opponent_coach TEXT DEFAULT '', record_match INTEGER,
            projection REAL DEFAULT 0, source TEXT DEFAULT 'merged',
            dunkest_id INTEGER, matchday INTEGER, opponent TEXT DEFAULT '',
            opponent_price REAL DEFAULT 0, live_avg REAL DEFAULT 0, last5 REAL DEFAULT 0,
            model_projection REAL DEFAULT 0, coefficient_win REAL DEFAULT 0,
            coefficient_loss REAL DEFAULT 0, is_injured INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        # Migrate databases created before live Dunkest coach columns existed.
        coach_cols = {r["name"] for r in con.execute("PRAGMA table_info(coaches)").fetchall()}
        for column, declaration in {
            "dunkest_id": "INTEGER", "matchday": "INTEGER",
            "opponent": "TEXT DEFAULT ''", "opponent_price": "REAL DEFAULT 0",
            "live_avg": "REAL DEFAULT 0", "last5": "REAL DEFAULT 0",
            "model_projection": "REAL DEFAULT 0", "coefficient_win": "REAL DEFAULT 0",
            "coefficient_loss": "REAL DEFAULT 0", "is_injured": "INTEGER DEFAULT 0",
        }.items():
            if column not in coach_cols:
                con.execute(f"ALTER TABLE coaches ADD COLUMN {column} {declaration}")
