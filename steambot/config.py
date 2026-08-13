"""Configuration for the Steam-bot PoC. Everything comes from env / .env.

Nothing secret is ever hardcoded. Mirrors the dotenv pattern already used by
``web/config.py`` and ``src/coach.py`` -- the .env file lives at the repo root.

Two distinct identities are involved, do not confuse them:

  * The BOT account (STEAMBOT_USERNAME/PASSWORD) -- a throwaway Steam account
    that logs in to the Game Coordinator. It needs to have launched CS2 once
    (CS2 is free), but it does NOT need to be friends with you.

  * The TARGET account (you) -- identified by STEAMBOT_TARGET_STEAMID plus your
    per-account Match Authentication Code (STEAMBOT_MATCH_AUTH_CODE). That code
    is NOT your Steam password; you generate it from CS2 / the Steam help wizard
    and it only authorises reading your match-sharing history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
DEFAULT_DOWNLOAD_DIR = REPO_ROOT / "demos" / "auto"

load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"{name} is not set. Add it to your .env at the repo root "
            f"(see steambot/README.md for how to obtain each value)."
        )
    return val


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _opt_int(name: str, default: int) -> int:
    raw = _opt(name)
    if not raw:  # absent OR present-but-blank
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid integer")


@dataclass
class BotConfig:
    # --- Bot Steam account (logs into the GC) ---
    bot_username: str
    bot_password: str
    bot_shared_secret: str  # optional: base64 TOTP secret for auto 2FA codes

    # --- Steam Web API (sharecode chain; no login needed for this call) ---
    web_api_key: str

    # --- Target player (you) ---
    target_steamid: str
    match_auth_code: str
    known_sharecode: str

    # --- Behaviour ---
    poll_interval_s: int
    download_dir: Path

    @classmethod
    def load(cls) -> "BotConfig":
        return cls(
            bot_username=_req("STEAMBOT_USERNAME"),
            bot_password=_req("STEAMBOT_PASSWORD"),
            bot_shared_secret=_opt("STEAMBOT_SHARED_SECRET"),
            web_api_key=_req("STEAM_API_KEY"),
            target_steamid=_req("STEAMBOT_TARGET_STEAMID"),
            match_auth_code=_req("STEAMBOT_MATCH_AUTH_CODE"),
            known_sharecode=_req("STEAMBOT_KNOWN_SHARECODE"),
            poll_interval_s=_opt_int("STEAMBOT_POLL_INTERVAL_S", 90),
            download_dir=Path(_opt("STEAMBOT_DOWNLOAD_DIR", str(DEFAULT_DOWNLOAD_DIR))),
        )
