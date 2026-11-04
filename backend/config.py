import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "dunkest.db"
FRONTEND = ROOT / "frontend"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency). Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")
DUNKEST_TOKEN = os.environ.get("DUNKEST_TOKEN", "")

