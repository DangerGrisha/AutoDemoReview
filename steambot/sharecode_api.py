"""Walk a player's match-sharing-code history via the Steam Web API.

This layer needs NO Steam login and NO Game Coordinator -- just a Web API key,
the target's SteamID64, their Match Authentication Code, and a seed sharecode.
It is the reliable, well-documented half of the pipeline (Leetify / csstats /
csgostats.gg all rely on this same endpoint).

Endpoint:
    GET https://api.steampowered.com/ICSGOPlayers_730/GetNextMatchSharingCode/v1/
    params: key, steamid, steamidkey, knowncode

Response semantics (confirmed against current CS2 behaviour):
    200 + {"result": {"nextcode": "CSGO-..."}}  -> a newer match exists
    202 + {"result": {"nextcode": "n/a"}}        -> caught up (no newer match yet)
    412 Precondition Failed                       -> knowncode invalid / not owned
    403 Forbidden                                 -> bad api key or bad auth code
"""

from __future__ import annotations

from typing import Optional

import httpx

from .logutil import StepLogger

_ENDPOINT = "https://api.steampowered.com/ICSGOPlayers_730/GetNextMatchSharingCode/v1/"
_TIMEOUT = httpx.Timeout(20.0)


class ShareCodeAuthError(RuntimeError):
    """Bad Web API key or bad Match Authentication Code (HTTP 403)."""


class ShareCodeInvalidKnownCode(RuntimeError):
    """knowncode is malformed, not owned by the player, or too old (HTTP 412)."""


class ShareCodeTransientError(RuntimeError):
    """A transient failure (network error, 429 rate-limit, 5xx) -- safe to retry."""


def get_next_sharecode(
    *,
    web_api_key: str,
    steamid: str,
    match_auth_code: str,
    known_code: str,
    log: StepLogger,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Return the sharecode of the match immediately after ``known_code``.

    Returns None when the player is caught up (API says ``n/a`` / HTTP 202) --
    i.e. no new match has been played since ``known_code``.

    Raises ShareCodeAuthError (403) or ShareCodeInvalidKnownCode (412) so the
    caller can tell a transient "nothing new yet" apart from a misconfiguration.
    """
    params = {
        "key": web_api_key,
        "steamid": steamid,
        "steamidkey": match_auth_code,
        "knowncode": known_code,
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        resp = client.get(_ENDPOINT, params=params)
    except httpx.HTTPError as exc:
        # Connection reset, DNS, timeout, etc. -- transient; caller can retry.
        raise ShareCodeTransientError(f"network error calling sharecode API: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code == 403:
        raise ShareCodeAuthError(
            "HTTP 403 -- Steam rejected the Web API key or the Match "
            "Authentication Code (steamidkey). Re-check STEAM_API_KEY and "
            "STEAMBOT_MATCH_AUTH_CODE."
        )
    if resp.status_code == 412:
        raise ShareCodeInvalidKnownCode(
            f"HTTP 412 -- knowncode {known_code!r} is invalid, not owned by this "
            "SteamID, or too old (Valve only walks matches from ~the last month). "
            "Seed STEAMBOT_KNOWN_SHARECODE with a recent sharecode."
        )
    # Only 200 (new code) and 202 (caught up) carry the JSON we expect. Anything
    # else -- 429 rate-limit, 5xx gateway pages -- is transient; retry later.
    if resp.status_code not in (200, 202):
        raise ShareCodeTransientError(
            f"HTTP {resp.status_code} from sharecode API (retryable): {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except ValueError:
        raise ShareCodeTransientError(
            f"non-JSON response ({resp.status_code}) from sharecode API: {resp.text[:200]}"
        )

    nextcode = (body.get("result") or {}).get("nextcode")
    if not nextcode or nextcode == "n/a":
        log.info(f"caught up (HTTP {resp.status_code}, nextcode=n/a) -- no new match")
        return None

    log.ok(f"new sharecode from chain: {nextcode}")
    return nextcode
