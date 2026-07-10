import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("RSSRADAR_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("RSSRADAR_DATABASE_URL", f"sqlite:///{DATA_DIR / 'rssradar.db'}")
SESSION_SECRET = os.getenv("RSSRADAR_SESSION_SECRET", "dev-only-change-me")
DEBUG = os.getenv("RSSRADAR_DEBUG", "0") == "1"

BASE_URL = os.getenv("RSSRADAR_BASE_URL", "http://localhost:8000")
MAGIC_LINK_TTL_MINUTES = int(os.getenv("RSSRADAR_MAGIC_LINK_TTL_MINUTES", "15"))
MAGIC_LINK_MIN_INTERVAL_SECONDS = int(os.getenv("RSSRADAR_MAGIC_LINK_MIN_INTERVAL", "60"))

# SMTP — if SMTP_HOST is unset, magic links are printed to stdout (dev mode).
SMTP_HOST = os.getenv("RSSRADAR_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("RSSRADAR_SMTP_PORT", "587"))
SMTP_USER = os.getenv("RSSRADAR_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("RSSRADAR_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("RSSRADAR_SMTP_FROM", "rssRadar <no-reply@rssradar.local>")
SMTP_STARTTLS = os.getenv("RSSRADAR_SMTP_STARTTLS", "1") == "1"

# Background feed refresh
ENABLE_SCHEDULER = os.getenv("RSSRADAR_ENABLE_SCHEDULER", "1") == "1"
REFRESH_INTERVAL_MINUTES = int(os.getenv("RSSRADAR_REFRESH_INTERVAL_MINUTES", "15"))
REFRESH_BATCH_SIZE = int(os.getenv("RSSRADAR_REFRESH_BATCH_SIZE", "300"))
REFRESH_CONCURRENCY = int(os.getenv("RSSRADAR_REFRESH_CONCURRENCY", "10"))
FEED_ITEM_KEEP = int(os.getenv("RSSRADAR_FEED_ITEM_KEEP", "100"))  # max items stored per feed
