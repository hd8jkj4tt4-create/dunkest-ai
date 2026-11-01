import os, sys, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
subprocess.run([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload"], check=True)
