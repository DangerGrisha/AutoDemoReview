"""Web-app configuration: load .env once and expose validated settings.

Mirrors the ad-hoc dotenv pattern used by src/coach.py, but centralised so the
app has a single source of truth and fails fast on a missing session secret.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent
DATA_DIR = WEB_DIR / "data"
TMP_DIR = DATA_DIR / "tmp"

# .env lives at the repo root (shared with the CLI/coach layer).
load_dotenv(REPO_ROOT / ".env")

_PLACEHOLDER_SECRET = "change-me-to-a-long-random-string"

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
if not SESSION_SECRET or SESSION_SECRET == _PLACEHOLDER_SECRET:
    raise RuntimeError(
        "SESSION_SECRET is not set (or is still the placeholder). Add a long "
        "random value to .env -- see .env.example. Generate one with:\n"
        '    python -c "import secrets; print(secrets.token_hex(32))"'
    )

# Public base URL the browser uses to reach the app; drives the Steam OpenID
# return_to / realm. For LAN use, set this to the host's LAN IP in .env.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

# Optional Steam Web API key -> real persona names + avatars. Falls back to
# showing the SteamID64 when unset.
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "").strip() or None

# SQLite database path (default web/data/app.db). Relative paths resolve against
# the repo root.
_db = Path(os.environ.get("APP_DB_PATH", str(DATA_DIR / "app.db")))
DB_PATH = _db if _db.is_absolute() else (REPO_ROOT / _db)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "800"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PARSE_CONCURRENCY = max(1, int(os.environ.get("PARSE_CONCURRENCY", "1")))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Ensure runtime dirs exist (gitignored).
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
