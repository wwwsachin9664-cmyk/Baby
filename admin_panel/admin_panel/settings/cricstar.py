import os
import secrets
from pathlib import Path

from .base import *  # noqa: F403

# Fix MEDIA_ROOT to use absolute path so card images resolve correctly
# BASE_DIR = admin_panel/ directory, media files live in admin_panel/media/
MEDIA_ROOT = Path(__file__).resolve().parent.parent.parent / "media"

DEBUG = False

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", secrets.token_hex(32))

ALLOWED_HOSTS = ["*"]
ALLOWED_CIDR_NETS = ["0.0.0.0/0"]

CSRF_TRUSTED_ORIGINS = ["http://localhost", "http://127.0.0.1"]
