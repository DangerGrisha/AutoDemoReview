"""Steam OpenID 2.0 login + persona lookup.

Steam only supports OpenID 2.0 with identifier_select. Three primitives:
  * build_login_url  -- the redirect the user's browser follows to Steam
  * verify_callback  -- verify Steam's assertion, return the SteamID64
  * fetch_persona    -- optional display name + avatar (needs STEAM_API_KEY)
"""

import re
import urllib.parse

import httpx

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
_ID_RE = re.compile(r"^https://steamcommunity\.com/openid/id/(\d+)$")
_NS = "http://specs.openid.net/auth/2.0"
_IDENTIFIER_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"


def build_login_url(base_url):
    """The Steam OpenID checkid_setup URL to redirect the browser to."""
    params = {
        "openid.ns": _NS,
        "openid.mode": "checkid_setup",
        "openid.return_to": f"{base_url}/auth/steam/callback",
        "openid.realm": f"{base_url}/",
        "openid.identity": _IDENTIFIER_SELECT,
        "openid.claimed_id": _IDENTIFIER_SELECT,
    }
    return STEAM_OPENID_URL + "?" + urllib.parse.urlencode(params)


def verify_callback(params, expected_return_to=None):
    """Verify the OpenID assertion in `params` (dict of the callback query).

    Returns the SteamID64 string on success, or None. Confirms the claimed_id
    shape, (optionally) that return_to matches ours, and re-checks the signature
    with Steam via a check_authentication POST (defeats a forged assertion).
    """
    if params.get("openid.mode") != "id_res":
        return None
    claimed = params.get("openid.claimed_id", "")
    m = _ID_RE.match(claimed or "")
    if not m:
        return None
    if expected_return_to and params.get("openid.return_to") != expected_return_to:
        return None

    # Echo every openid.* param back with mode=check_authentication.
    verify = {k: v for k, v in params.items() if k.startswith("openid.")}
    verify["openid.mode"] = "check_authentication"
    try:
        resp = httpx.post(STEAM_OPENID_URL, data=verify, timeout=15.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or "is_valid:true" not in resp.text:
        return None
    return m.group(1)


def fetch_persona(steamid64, api_key):
    """Return (persona_name, avatar_url).

    Falls back to (steamid64, None) when no API key is configured or the call
    fails -- login still works, the user is just shown by their SteamID64.
    """
    if not api_key:
        return steamid64, None
    try:
        resp = httpx.get(STEAM_SUMMARIES_URL,
                         params={"key": api_key, "steamids": steamid64},
                         timeout=15.0)
        resp.raise_for_status()
        players = resp.json().get("response", {}).get("players", [])
        if players:
            p = players[0]
            return (p.get("personaname") or steamid64), p.get("avatarfull")
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return steamid64, None
