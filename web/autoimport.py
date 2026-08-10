"""Auto-import subscriptions: validate a player's CS2 codes and store the link.

The web layer's half of the Steam bot. Two values come from the player (both from
one Steam help page):

  * the **match-history authentication code** -- a scoped, revocable token that
    only authorises reading their match-sharing history. NOT a Steam password,
    but still a secret: never logged, never rendered back in full (`mask`).
  * a recent **sharecode**, which seeds the cursor the bot walks forward from.

Validation is deliberately a LIVE call: the two codes are only meaningful
together (Steam answers 403 for a bad key/auth code and 412 for a sharecode that
isn't this player's), so local regexes alone would happily accept a pair that can
never work and the failure would surface hours later in a background worker.
"""

import re

from steambot import sharecode as sharecode_codec
from steambot import sharecode_api
from steambot.logutil import StepLogger

from . import config, db

# Steam prints the auth code as three dash-separated alphanumeric groups
# (e.g. ABCD-EFGH2-IJKL). Kept loose on purpose -- the live call is the real
# check, and a format tweak by Valve shouldn't lock users out of the feature.
_AUTH_RE = re.compile(r"^[A-Za-z0-9]{4,6}(?:-[A-Za-z0-9]{4,6}){2,3}$")

HELP_URL = ("https://help.steampowered.com/en/wizard/HelpWithGameIssue/"
            "?appid=730&issueid=128")


class SetupError(ValueError):
    """The submitted codes can't be used; the message is shown to the user."""


def mask(code):
    """Render a secret as `ABCD-…-IJKL` for display."""
    if not code:
        return ""
    code = str(code)
    return code if len(code) <= 9 else f"{code[:4]}…{code[-4:]}"


def normalize_auth_code(raw):
    code = (raw or "").strip().upper()
    if not code:
        raise SetupError("Paste your match history authentication code.")
    if not _AUTH_RE.match(code):
        raise SetupError("That doesn't look like a match history authentication code "
                         "(it looks like ABCD-EFGH2-IJKL).")
    return code


def normalize_sharecode(raw):
    code = (raw or "").strip()
    if not code:
        raise SetupError("Paste the sharecode of your most recent match.")
    if not sharecode_codec.is_valid(code):
        raise SetupError("That doesn't look like a match sharecode "
                         "(it looks like CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx).")
    sharecode_codec.decode(code)      # raises only if the regex lied
    return code


def verify_with_steam(steamid, auth_code, share_code):
    """Ask Steam whether this (key, auth code, sharecode) triple actually works.

    Returns the next sharecode when Steam already has a newer match, or None when
    the player is caught up -- both mean the codes are GOOD. Raises SetupError
    with a user-facing message otherwise.
    """
    if not config.STEAM_API_KEY:
        raise SetupError("Auto-import isn't configured on the server yet "
                         "(no Steam Web API key). Ask the admin to set STEAM_API_KEY.")
    log = StepLogger(name="web.autoimport", verbose=False)
    try:
        return sharecode_api.get_next_sharecode(
            web_api_key=config.STEAM_API_KEY, steamid=steamid,
            match_auth_code=auth_code, known_code=share_code, log=log)
    except sharecode_api.ShareCodeAuthError:
        raise SetupError("Steam rejected the authentication code. Re-generate it on "
                         "the Steam help page and paste it again.")
    except sharecode_api.ShareCodeInvalidKnownCode:
        raise SetupError("Steam says that sharecode isn't yours (or is too old). "
                         "Use the sharecode of one of your recent matches.")
    except sharecode_api.ShareCodeTransientError as exc:
        raise SetupError(f"Steam didn't answer just now -- try again in a minute. ({exc})")


def connect(steamid, auth_code_raw, share_code_raw, *, chat_optin=True):
    """Validate and store a subscription. Returns the stored sharecode cursor.

    The cursor stays at the sharecode the player gave us even when Steam already
    reports a newer match: the worker imports that newer one on its first sweep,
    so a match played between setup and the first poll is never skipped.
    """
    auth_code = normalize_auth_code(auth_code_raw)
    share_code = normalize_sharecode(share_code_raw)
    verify_with_steam(steamid, auth_code, share_code)
    db.upsert_steam_link(steamid, auth_code, share_code)
    db.set_steam_link_flags(steamid, chat_optin=chat_optin)
    return share_code


def status(steamid):
    """View-model for the /connect page (never leaks the full auth code)."""
    row = db.get_steam_link(steamid)
    if row is None:
        return {"linked": False, "help_url": HELP_URL,
                "bot_url": config.STEAMBOT_PROFILE_URL,
                "bot_name": config.STEAMBOT_PERSONA}
    return {
        "linked": True,
        "help_url": HELP_URL,
        "bot_url": config.STEAMBOT_PROFILE_URL,
        "bot_name": config.STEAMBOT_PERSONA,
        "auth_code_masked": mask(row["match_auth_code"]),
        "last_sharecode": row["last_sharecode"],
        "enabled": bool(row["enabled"]),
        "chat_optin": bool(row["chat_optin"]),
        "friend_state": row["friend_state"],
        "last_checked_at": row["last_checked_at"],
        "last_match_at": row["last_match_at"],
        "matches_found": row["matches_found"],
        "last_error": row["last_error"],
        "paused": bool(row["fail_count"] >= 10),
    }
