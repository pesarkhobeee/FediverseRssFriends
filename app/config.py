import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RSSRADAR_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("RSSRADAR_DATABASE_URL", f"sqlite:///{DATA_DIR / 'rssradar.db'}")
SESSION_SECRET = os.getenv("RSSRADAR_SESSION_SECRET", "dev-only-change-me")
DEBUG = os.getenv("RSSRADAR_DEBUG", "0") == "1"
