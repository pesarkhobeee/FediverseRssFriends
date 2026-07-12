import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RSSFRIENDS_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("RSSFRIENDS_DATABASE_URL", f"sqlite:///{DATA_DIR / 'rssfriends.db'}")
SESSION_SECRET = os.getenv("RSSFRIENDS_SESSION_SECRET", "dev-only-change-me")
DEBUG = os.getenv("RSSFRIENDS_DEBUG", "0") == "1"

BASE_URL = os.getenv("RSSFRIENDS_BASE_URL", "http://localhost:8000")
MAGIC_LINK_TTL_MINUTES = int(os.getenv("RSSFRIENDS_MAGIC_LINK_TTL_MINUTES", "15"))
MAGIC_LINK_MIN_INTERVAL_SECONDS = int(os.getenv("RSSFRIENDS_MAGIC_LINK_MIN_INTERVAL", "60"))

# SMTP — if SMTP_HOST is unset, magic links are printed to stdout (dev mode).
SMTP_HOST = os.getenv("RSSFRIENDS_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("RSSFRIENDS_SMTP_PORT", "587"))
SMTP_USER = os.getenv("RSSFRIENDS_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("RSSFRIENDS_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("RSSFRIENDS_SMTP_FROM", "rssFriends <no-reply@rssfriends.local>")
SMTP_STARTTLS = os.getenv("RSSFRIENDS_SMTP_STARTTLS", "1") == "1"

# Background feed refresh
ENABLE_SCHEDULER = os.getenv("RSSFRIENDS_ENABLE_SCHEDULER", "1") == "1"
REFRESH_INTERVAL_MINUTES = int(os.getenv("RSSFRIENDS_REFRESH_INTERVAL_MINUTES", "15"))
REFRESH_BATCH_SIZE = int(os.getenv("RSSFRIENDS_REFRESH_BATCH_SIZE", "300"))
REFRESH_CONCURRENCY = int(os.getenv("RSSFRIENDS_REFRESH_CONCURRENCY", "10"))
FEED_ITEM_KEEP = int(os.getenv("RSSFRIENDS_FEED_ITEM_KEEP", "100"))  # max items stored per feed
