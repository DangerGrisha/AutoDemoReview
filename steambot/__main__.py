"""CLI for the Steam-bot proof-of-concept.

Subcommands are deliberately granular so you can test each layer in isolation
and see exactly where the chain works or breaks:

    python -m steambot selftest           # offline: codec + config sanity, no network
    python -m steambot next               # Layer 1 only: one sharecode-chain call
    python -m steambot login              # one-time interactive Steam login (seeds creds)
    python -m steambot resolve <CSGO-...> # Layers 2+3: GC resolve + download one match
    python -m steambot poll               # full loop: detect new match -> download

Run `python -m steambot <cmd> -h` for options.
"""

from __future__ import annotations

import argparse
import sys

from .config import BotConfig, ConfigError
from .logutil import StepLogger


def _cmd_selftest(args, log: StepLogger) -> int:
    """Offline sanity: prove the codec against a known Valve vector; load config."""
    from . import sharecode

    log.banner("selftest -- offline (no network, no Steam)")
    vector = "CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK"
    expect = (3230642215713767580, 3230647599455273103, 55788)
    log.step(f"decoding known vector {vector}")
    got = sharecode.decode(vector)
    if (got.matchid, got.outcomeid, got.token) == expect:
        log.ok(f"codec correct -> {got}")
    else:
        log.fail(f"codec MISMATCH -> {got} (expected {expect})")
        return 1

    log.step("loading configuration from .env")
    try:
        cfg = BotConfig.load()
    except ConfigError as exc:
        log.warn(f"config incomplete: {exc}")
        log.info("that's fine for a pure codec check, but 'next'/'poll' need it.")
        return 0
    log.ok("config loaded (all required env vars present)")
    log.info(f"bot={cfg.bot_username}  target_steamid={cfg.target_steamid}")
    log.info(f"seed sharecode={cfg.known_sharecode}")
    return 0


def _cmd_next(args, log: StepLogger) -> int:
    """Layer 1 only: call the sharecode chain once. No Steam login, no GC."""
    from .poc import check_next_sharecode

    cfg = BotConfig.load()
    check_next_sharecode(cfg, log)
    return 0


def _cmd_login(args, log: StepLogger) -> int:
    """One-time interactive Steam login to seed credentials for unattended polling."""
    from steam.client import SteamClient
    from steam.enums import EResult
    from csgo.client import CSGOClient

    cfg = BotConfig.load()
    log.banner("interactive login -- seeds stored credentials, verifies GC")
    client = SteamClient()
    client.set_credential_location(".steambot_creds")
    log.step("cli_login (you may be prompted for a Steam Guard code)")
    result = client.cli_login(cfg.bot_username, cfg.bot_password)
    if result != EResult.OK:
        log.fail(f"login failed: {result!r}")
        return 1
    log.ok(f"logged in as {cfg.bot_username} (steamid {client.steam_id})")
    log.info("credentials stored in ./.steambot_creds -- future runs are unattended")

    log.step("requesting free CS2 license (appid 730)")
    try:
        client.request_free_license([730])
        log.ok("done")
    except Exception as exc:
        log.warn(f"request_free_license: {exc}")

    log.step("verifying Game Coordinator connection (this is the risky part)")
    cs = CSGOClient(client)
    cs.launch()
    ready = cs.wait_event("ready", timeout=30)
    if ready is None:
        log.fail("GC did NOT become ready in 30s -- consistent with ValvePython "
                 "issue #62 (can't connect to CS2 GC). See steambot/README.md fallback.")
        client.disconnect()
        return 2
    log.ok("GC session established -- the pure-Python GC path WORKS on your setup!")
    client.disconnect()
    return 0


def _cmd_resolve(args, log: StepLogger) -> int:
    """Layers 2+3 for one sharecode: decode -> GC URL -> download."""
    from . import sharecode
    from .poc import resolve_and_download

    if not sharecode.is_valid(args.sharecode):
        log.fail(f"not a valid sharecode: {args.sharecode!r} "
                 "(expected CSGO-xxxxx-xxxxx-xxxxx-xxxxx-xxxxx)")
        return 2
    cfg = BotConfig.load()
    path = resolve_and_download(cfg, args.sharecode, log)
    return 0 if path else 3


def _cmd_poll(args, log: StepLogger) -> int:
    """Full loop: detect new matches on the chain, resolve + download each."""
    from .poc import poll_loop

    cfg = BotConfig.load()
    poll_loop(cfg, log, max_iterations=args.max_iterations)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="steambot", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="offline codec + config sanity check")
    sub.add_parser("next", help="Layer 1: one sharecode-chain call (no login)")
    sub.add_parser("login", help="one-time interactive Steam login + GC check")

    p_res = sub.add_parser("resolve", help="Layers 2+3: resolve + download one sharecode")
    p_res.add_argument("sharecode", help="a CSGO-xxxxx-... match sharing code")

    p_poll = sub.add_parser("poll", help="full loop: detect new match -> download")
    p_poll.add_argument("--max-iterations", type=int, default=0,
                        help="stop after N loop iterations (0 = forever)")

    args = parser.parse_args(argv)
    log = StepLogger()

    handlers = {
        "selftest": _cmd_selftest,
        "next": _cmd_next,
        "login": _cmd_login,
        "resolve": _cmd_resolve,
        "poll": _cmd_poll,
    }
    try:
        return handlers[args.cmd](args, log)
    except ConfigError as exc:
        log.fail(str(exc))
        return 1
    except RuntimeError as exc:
        # GCError / ShareCode* errors all derive from RuntimeError; show cleanly.
        log.fail(str(exc))
        return 4
    except ModuleNotFoundError as exc:
        if exc.name in ("steam", "csgo") or (exc.name or "").startswith(("steam.", "csgo.")):
            log.fail(f"missing dependency {exc.name!r} -- this command needs the GC "
                     "stack. Install it with:  pip install -r requirements-steambot.txt")
            return 1
        raise
    except KeyboardInterrupt:
        log.warn("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
