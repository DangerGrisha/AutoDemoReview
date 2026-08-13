"""Resolve a decoded sharecode to a demo URL via the CS2 Game Coordinator.

------------------------------------------------------------------------------
HONEST STATUS -- READ THIS.
------------------------------------------------------------------------------
This is the genuinely uncertain layer. It uses the pure-Python ValvePython stack
(`steam` + `csgo`) because that keeps the project in one language, and because
the library still *installs and imports* cleanly on Python 3.13 and still
exposes `request_full_match_info(matchid, outcomeid, token)` -- the exact GC
message CS2 uses (`EMsgGCCStrike15_v2_MatchListRequestFullGameInfo`, unchanged
from CS:GO).

BUT -- and this is the honest headline -- the research strongly indicates this
specific path is BROKEN for CS2. ValvePython/csgo's last commit is 2021-02-03
(~2.5 years before CS2), and it has an OPEN, unresolved issue #62 "Can't connect
to GC" (Feb 2024) plus #63 "Outdated Protobufs" where a contributor tried and
FAILED to update them for CS2. The same Feb-2024 GC break was promptly fixed in
the maintained tooling (Node `node-globaloffensive`, akiver's C++
`boiler-writter`) but never in this Python package. Current Python demo tools
delegate the GC step to `boiler-writter` rather than use this library.

So the *protocol* is alive (node-globaloffensive resolves sharecodes today); the
*pure-Python client* most likely is not. Expect this module to fail at either:
  (a) login (Steam auth changes since 2023), or
  (b) the GC handshake -- `wait_event("ready")` times out (issue #62), or
  (c) decoding the MatchList reply (frozen protobufs, issue #63).

We still ship it because it is the in-stack option and because "does it actually
fail?" is a real result worth having from your own account. The logging is loud
so a failure tells you WHICH step broke. When it does fail, the proven fallback
(see steambot/README.md) is to run ONLY this GC step through Node
`node-globaloffensive` or akiver's `boiler-writter` -- everything else in this
package (sharecode chain, download) stays pure Python. This module is isolated
behind `resolve_demo_url()` so swapping the backend touches only this file.
------------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Optional

from .logutil import StepLogger
from .sharecode import MatchId

# 730 = CS2 (formerly CS:GO). The bot needs this (free) license to enter the GC.
_APPID_CS2 = 730
_GC_READY_TIMEOUT = 30
_MATCH_INFO_TIMEOUT = 30


class GCError(RuntimeError):
    """Any failure in the login -> GC -> match-info chain."""


def _totp(shared_secret: str) -> Optional[str]:
    """Generate a Steam mobile 2FA code from a base64 shared_secret, if given."""
    if not shared_secret:
        return None
    import base64

    from steam.guard import generate_twofactor_code

    return generate_twofactor_code(base64.b64decode(shared_secret))


class GCClient:
    """Blocking wrapper around the ValvePython Steam + CS2 GC clients.

    Usage:
        with GCClient(cfg, log) as gc:
            url = gc.resolve_demo_url(match_id)
    """

    def __init__(self, cfg, log: StepLogger, credential_dir: str = ".steambot_creds") -> None:
        self.cfg = cfg
        self.log = log
        self.credential_dir = credential_dir
        self._client = None
        self._cs = None

    # -- context management -------------------------------------------------
    def __enter__(self) -> "GCClient":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # best-effort; we're tearing down
                pass

    # -- login --------------------------------------------------------------
    def login(self) -> None:
        # Imported lazily so the rest of steambot (sharecode chain, download)
        # works even if steam/csgo aren't installed.
        from steam.client import SteamClient
        from steam.enums import EResult
        from csgo.client import CSGOClient

        self.log.step("Logging into Steam (bot account)")
        client = SteamClient()
        # Assign immediately so close() can tear down the socket + greenlets even
        # if login/GC-handshake fails partway (the expected CS2 failure path).
        self._client = client
        client.set_credential_location(self.credential_dir)

        if client.relogin_available:
            self.log.info("stored credentials found -> unattended relogin")
            result = client.relogin()
        else:
            self.log.info(
                "no stored credentials -> full login. On a fresh account Steam "
                "Guard may require a code; run `python -m steambot login` once "
                "interactively to seed credentials for unattended polling."
            )
            result = client.login(
                username=self.cfg.bot_username,
                password=self.cfg.bot_password,
                two_factor_code=_totp(self.cfg.bot_shared_secret),
            )

        if result != EResult.OK:
            raise GCError(
                f"Steam login failed: {result!r}. If this is AccountLogonDenied / "
                "TwoFactor, run `python -m steambot login` interactively first, or "
                "set STEAMBOT_SHARED_SECRET for automatic 2FA codes."
            )
        self.log.ok(f"logged in as {self.cfg.bot_username} (steamid {client.steam_id})")

        # A fresh F2P bot still needs the 730 license to reach the GC.
        self.log.step("Ensuring the bot owns the free CS2 license (appid 730)")
        try:
            client.request_free_license([_APPID_CS2])
            self.log.ok("requested/confirmed free CS2 license")
        except Exception as exc:
            self.log.warn(f"request_free_license failed ({exc}); continuing -- "
                          "may already be licensed")

        self.log.step("Connecting to the CS2 Game Coordinator")
        cs = CSGOClient(client)
        cs.launch()  # sends games_played([730]) + ClientHello until GC replies
        ready = cs.wait_event("ready", timeout=_GC_READY_TIMEOUT)
        if ready is None:
            raise GCError(
                f"GC did not become ready within {_GC_READY_TIMEOUT}s. This is the "
                "classic ValvePython-vs-CS2 failure (frozen protobufs / handshake). "
                "See the fallback note in steambot/README.md."
            )
        self.log.ok("Game Coordinator session established (haveGCSession)")
        self._cs = cs  # self._client was assigned up-front for cleanup safety

    # -- the one thing this layer exists to do ------------------------------
    def resolve_demo_url(self, match: MatchId) -> Optional[str]:
        """Ask the GC for full match info and pull out the .dem.bz2 URL.

        Returns the URL string, or None if the GC replied but carried no demo
        link (e.g. the demo has already been pruned by Valve).
        """
        if self._cs is None:
            raise GCError("not connected -- call login() first")

        self.log.step(f"Requesting full match info from GC for {match}")
        self._cs.request_full_match_info(match.matchid, match.outcomeid, match.token)
        args = self._cs.wait_event("full_match_info", timeout=_MATCH_INFO_TIMEOUT)
        if args is None:
            raise GCError(
                f"no full_match_info reply within {_MATCH_INFO_TIMEOUT}s. Either the "
                "match id is stale or the frozen protobufs can't parse the CS2 reply."
            )

        message = args[0]
        matches = getattr(message, "matches", [])
        self.log.info(f"GC returned {len(matches)} match record(s)")
        if not matches:
            self.log.warn("GC returned an empty match list (match too old? >8 back?)")
            return None

        # The demo URL is overloaded into the `map` field of the LAST round-stats
        # entry of the match info (a Valve quirk, same in CS:GO and CS2).
        for m in matches:
            rounds = getattr(m, "roundstatsall", None)
            if not rounds:
                continue
            url = getattr(rounds[-1], "map", "") or ""
            if url.startswith("http"):
                self.log.ok(f"demo URL: {url}")
                return url
            if url:
                self.log.warn(f"roundstatsall[-1].map is not a URL: {url!r}")

        self.log.warn("no demo URL in match info -- likely expired/pruned by Valve")
        return None
