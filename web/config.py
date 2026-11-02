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

# 3D replay: per-match extract3d artifacts (manifest.json + rNN.3dr) live here.
REPLAY3D_DIR = DATA_DIR / "replay3d"

# Path to the extract3d release binary. 3D replay is best-effort: when the
# binary is missing the upload still succeeds, just without a 3D replay.
_extract3d_default = REPO_ROOT / "rust" / "target" / "release" / (
    "extract3d.exe" if os.name == "nt" else "extract3d")
_extract3d = os.environ.get("EXTRACT3D_BIN", "").strip()
EXTRACT3D_BIN = Path(_extract3d) if _extract3d else _extract3d_default

# A 300-500 MB demo takes ~1 min to extract; leave generous headroom.
EXTRACT3D_TIMEOUT_S = int(os.environ.get("EXTRACT3D_TIMEOUT_S", "900"))

# Gzipped copies of processed demos, one per match. A stored match can only gain
# what a newer extractor learns to read if its demo is still around, and Valve
# prunes its own copies within weeks. KEEP_DEMOS=0 turns this off (the upload and
# /connect pages then say the demo is discarded, so keep the two in step).
DEMOS_DIR = DATA_DIR / "demos"
KEEP_DEMOS = os.environ.get("KEEP_DEMOS", "1").strip() != "0"

# --- Steam bot (auto-import of finished matches) ---
# The bot's own profile: the /connect page links players at it so THEY send the
# friend invite (the bot only accepts). Absent -> the page shows the auto-import
# setup but hides the Steam-chat half.
STEAMBOT_PROFILE_URL = os.environ.get("STEAMBOT_PROFILE_URL", "").strip() or None
STEAMBOT_PERSONA = os.environ.get("STEAMBOT_PERSONA", "").strip() or "our Steam bot"
# Where the Node Steam agent (GC resolve + chat) listens. Loopback only.
STEAMBOT_AGENT_URL = os.environ.get("STEAMBOT_AGENT_URL", "http://127.0.0.1:8790").rstrip("/")
# Shared secret between the worker and the agent. The agent binds to loopback, so
# this guards against other local processes driving the bot, not the network.
STEAMBOT_AGENT_TOKEN = os.environ.get("STEAMBOT_AGENT_TOKEN", "").strip()
# How often a subscription is polled for a new match, and how many per sweep.
# Two speeds on purpose: the idle poll hits Steam's Web API (rate-limited, and
# pointless to hammer -- a match can't appear faster than it is played), while
# waiting for an already-detected match only asks the Game Coordinator and the
# replay CDN, which we own the pace of. So the wait is checked much more often:
# that window is the only part of the delay we can actually shorten.
STEAMBOT_POLL_INTERVAL_S = int(os.environ.get("STEAMBOT_POLL_INTERVAL_S", "120"))
STEAMBOT_PENDING_INTERVAL_S = int(os.environ.get("STEAMBOT_PENDING_INTERVAL_S", "45"))
STEAMBOT_BATCH = int(os.environ.get("STEAMBOT_BATCH", "25"))
# Auto-downloaded demos land here and this copy is deleted right after ingest.
STEAMBOT_DOWNLOAD_DIR = TMP_DIR / "steambot"

# Ensure runtime dirs exist (gitignored).
DATA_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
REPLAY3D_DIR.mkdir(parents=True, exist_ok=True)
DEMOS_DIR.mkdir(parents=True, exist_ok=True)
STEAMBOT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
