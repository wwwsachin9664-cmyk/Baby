import os
from pathlib import Path

from .base import *

DEBUG = False
SECRET_KEY = "insecure"

# Only allow connections from local IPs
ALLOWED_CIDR_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

# Persistent SQLite fallback: if no database URL env var is set (e.g. after a remix),
# fall back to the local SQLite file so catch dates and trade history are never lost.
_db_url = os.environ.get("CRICSTARBOT_DB_URL") or os.environ.get("DATABASE_URL")
if not _db_url:
    _SQLITE_PATH = Path(__file__).resolve().parent.parent.parent / "db.sqlite3"
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _SQLITE_PATH,
        }
    }
