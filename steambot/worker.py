"""Auto-import worker: turn finished CS2 matches into stored reports.

One sweep per subscription in `steam_links`:

    1. GetNextMatchSharingCode  -- has this player finished a newer match?
    2. Steam agent /resolve     -- sharecode -> demo URL (Game Coordinator)
    3. download + bunzip2       -- the raw .dem
    4. web.ingest.ingest_demo   -- the SAME path the manual upload uses
    5. Steam agent /message     -- "your report is ready" + link (opt-in, friends only)

Three distinct outcomes have to stay distinct, and conflating them is how this
kind of loop breaks:

  * **caught up** — no newer match. Not an event; just stamp the poll.
  * **waiting** — the match exists but Valve hasn't published its demo yet (this
    takes minutes, sometimes ~an hour). NOT a failure: the cursor must not move
    and the failure streak must not grow, or a normal delay would eventually park
    the subscription. Bounded by `MAX_PENDING_TRIES` so a demo that never appears
    (Valve prunes after ~1-2 weeks) can't block the cursor forever.
  * **broken** — revoked auth code, sharecode not owned by the player. Counts as
    a failure; after enough of them the subscription parks itself and the user is
    told to paste a fresh code on /connect.

Run it as its own process (it holds no HTTP server):

    python -m steambot.worker              # loop forever
    python -m steambot.worker --once       # a single sweep (cron-friendly)
    python -m steambot.worker --steamid 76561198…   # just one subscription
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import config as webconfig  # noqa: E402
from web import db, ingest  # noqa: E402

from . import sharecode_api  # noqa: E402
from .downloader import DemoNotReady, download_demo  # noqa: E402
from .logutil import StepLogger  # noqa: E402

log = logging.getLogger("steambot.worker")

# A demo usually lands within minutes; at a 5-minute poll this waits ~2 hours
# before skipping the match as never-published.
# How long to keep waiting for a demo that has been detected but not published.
# Expressed in TIME, not tries: the pending poll interval is tunable, and a
# try-count silently became "18 minutes" instead of "2 hours" the moment that
# interval was shortened.
MAX_PENDING_WAIT_S = 2 * 3600
MAX_PENDING_TRIES = max(3, MAX_PENDING_WAIT_S // max(1, webconfig.STEAMBOT_PENDING_INTERVAL_S))
# Valve keeps matchmaking demos ~1-2 weeks (no published SLA). Past this, a
# missing file is gone for good, so the match is skipped immediately instead of
# being retried for hours. Generous on purpose: skipping a match we COULD have
# fetched is worse than a few wasted retries.
DEMO_RETENTION_DAYS = 21
# Cap on how many matches one subscription may chew through per sweep. A player
# reconnecting after a break can have a long backlog of expired matches, and
# walking it one-per-sweep would take days.
MAX_MATCHES_PER_SWEEP = 10
AGENT_TIMEOUT = httpx.Timeout(connect=5.0, read=90.0, write=15.0, pool=5.0)


class AgentError(RuntimeError):
    """The Steam agent is unreachable or refused the request."""


# ---- Steam agent (Node: GC resolve + chat) ----

def _agent_post(path, payload):
    url = f"{webconfig.STEAMBOT_AGENT_URL}{path}"
    headers = {}
    if webconfig.STEAMBOT_AGENT_TOKEN:
        headers["X-Agent-Token"] = webconfig.STEAMBOT_AGENT_TOKEN
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=AGENT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise AgentError(f"Steam agent unreachable at {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentError(f"Steam agent {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AgentError(f"Steam agent {path} returned non-JSON") from exc


def resolve_demo_url(share_code):
    """Turn a sharecode into (demo URL, match timestamp) via the agent.

    The timestamp matters: the replay CDN answers **502, not 404**, both for a
    demo that isn't published yet and for one Valve pruned weeks ago, so the age
    of the match is the only way to tell "wait" from "never coming".
    """
    body = _agent_post("/resolve", {"sharecode": share_code})
    return body.get("url") or None, body.get("matchtime") or None


def _too_old(matchtime):
    """True when a match predates Valve's demo retention, so waiting is futile."""
    if not matchtime:
        return False
    age_days = (time.time() - float(matchtime)) / 86400.0
    return age_days > DEMO_RETENTION_DAYS


def send_chat(steamid, text):
    _agent_post("/message", {"steamid": str(steamid), "text": text})


def sync_friends():
    """Let the agent accept invites from players who opted into chat.

    The allow-list is the point: accepting anyone would fill the bot's 250-friend
    cap with strangers. Returns the SteamIDs that are now friends.
    """
    allow = db.steam_links_awaiting_friend()
    body = _agent_post("/friends/sync", {"allow": allow})
    friends = [str(s) for s in (body.get("friends") or [])]
    for sid in friends:
        db.set_steam_link_flags(sid, friend_state="friends")
    pending = [str(s) for s in (body.get("pending") or [])]
    for sid in pending:
        if sid not in friends:
            db.set_steam_link_flags(sid, friend_state="pending")
    return friends


# ---- one subscription ----

def _match_url(match_id):
    return f"{webconfig.BASE_URL}/matches/{match_id}"


def process_link(row, *, steps=None):
    """Advance one subscription by at most one match. Returns a short status word."""
    sid = row["steamid64"]
    steps = steps or StepLogger(name="steambot.worker", verbose=False)

    # A match already detected on an earlier sweep is retried before asking for a
    # newer one -- matches must be imported in order, and the cursor only moves
    # once the demo is actually stored.
    share_code = row["pending_sharecode"]
    if not share_code:
        try:
            share_code = sharecode_api.get_next_sharecode(
                web_api_key=webconfig.STEAM_API_KEY, steamid=sid,
                match_auth_code=row["match_auth_code"],
                known_code=row["last_sharecode"], log=steps)
        except sharecode_api.ShareCodeAuthError as exc:
            db.steam_link_checked(sid, error=f"Steam rejected the authentication code: {exc}")
            return "auth-error"
        except sharecode_api.ShareCodeInvalidKnownCode as exc:
            db.steam_link_checked(sid, error=f"Stored sharecode not accepted: {exc}")
            return "cursor-error"
        except sharecode_api.ShareCodeTransientError as exc:
            db.steam_link_checked(sid, error=f"Steam Web API unavailable: {exc}")
            return "transient"
        if not share_code:
            db.steam_link_checked(sid)
            return "caught-up"

    try:
        url, matchtime = resolve_demo_url(share_code)
    except AgentError as exc:
        db.steam_link_pending(sid, share_code)      # keep the find for the next sweep
        db.steam_link_checked(sid, error=str(exc))
        return "agent-down"

    if _too_old(matchtime):
        # Played before the retention window: the demo is gone, don't wait on it.
        db.steam_link_skip_pending(sid, share_code)
        db.steam_link_checked(sid)
        log.info("skipped %s for %s: match is older than %d days, demo pruned",
                 share_code, sid, DEMO_RETENTION_DAYS)
        return "expired"

    if not url:
        return _wait_or_skip(sid, share_code, "the Game Coordinator has no demo URL yet")

    dem_path = None
    try:
        dem_path = download_demo(url, webconfig.STEAMBOT_DOWNLOAD_DIR, steps)
        res = ingest.ingest_demo(dem_path, owner_steamid=sid,
                                 filename=f"{share_code}.dem")
    except FileNotFoundError:
        # 404 -- published-but-pruned, or not published yet.
        return _wait_or_skip(sid, share_code, "Valve has no demo file at that URL")
    except DemoNotReady as exc:
        # 5xx from the replay server: routine right after a match ends.
        return _wait_or_skip(sid, share_code, str(exc))
    except ingest.IngestError as exc:
        # A demo we can't parse must not wedge the cursor: skip past it.
        db.steam_link_skip_pending(sid, share_code)
        db.steam_link_checked(sid, error=f"Demo could not be parsed, skipped: {exc}")
        return "unparsable"
    except Exception as exc:  # noqa: BLE001 -- network/disk; retry next sweep
        db.steam_link_pending(sid, share_code)
        db.steam_link_checked(sid, error=f"Download failed: {exc}")
        return "download-error"
    finally:
        if dem_path is not None:
            try:
                Path(dem_path).unlink(missing_ok=True)      # never keep the raw .dem
                Path(str(dem_path) + ".bz2").unlink(missing_ok=True)
            except OSError:
                pass

    db.steam_link_advance(sid, share_code)
    db.steam_link_checked(sid)
    log.info("imported match %s for %s (%s)", res.match_id, sid,
             "already known" if res.duplicate else "new")

    if row["chat_optin"] and row["friend_state"] == "friends" and not res.duplicate:
        try:
            send_chat(sid, f"Your match report is ready: {_match_url(res.match_id)}")
        except AgentError as exc:
            log.warning("could not message %s: %s", sid, exc)   # report is stored anyway
    return "imported"


def _wait_or_skip(sid, share_code, why):
    """Park a detected match, or give up on it once it's clearly never coming."""
    row = db.get_steam_link(sid)
    tries = (row["pending_tries"] or 0) if row else 0
    if row and row["pending_sharecode"] == share_code and tries + 1 >= MAX_PENDING_TRIES:
        db.steam_link_skip_pending(sid, share_code)
        db.steam_link_checked(sid, error=f"Gave up on {share_code}: {why}")
        return "gave-up"
    db.steam_link_pending(sid, share_code)
    db.steam_link_checked(sid)          # waiting is not a failure
    return "waiting"


# ---- sweeps ----

def sweep(*, steamid=None):
    """One pass over the due subscriptions. Returns {status: count}."""
    if not webconfig.STEAM_API_KEY:
        log.error("STEAM_API_KEY is not set -- the sharecode chain cannot run.")
        return {"misconfigured": 1}

    # Friend sync FIRST: it writes friend_state, and the rows we read below carry
    # that value into the chat-delivery check. Reading rows first meant a player
    # who had just been accepted still looked like 'none' for this sweep, and
    # their first report was stored without the Steam message.
    try:
        sync_friends()
    except AgentError as exc:
        log.warning("friend sync skipped: %s", exc)

    if steamid:
        row = db.get_steam_link(steamid)
        rows = [row] if row else []
    else:
        rows = db.list_due_steam_links(
            webconfig.STEAMBOT_POLL_INTERVAL_S,
            limit=webconfig.STEAMBOT_BATCH,
            pending_age_s=webconfig.STEAMBOT_PENDING_INTERVAL_S)
    if not rows:
        return {}

    tally = {}
    for row in rows:
        # Keep walking this player's chain while matches keep resolving: a
        # backlog (or a run of expired matches) would otherwise need one sweep
        # each. Stops as soon as there's nothing new, or we're waiting.
        for _ in range(MAX_MATCHES_PER_SWEEP):
            try:
                status = process_link(row)
            except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the sweep
                log.exception("subscription %s failed", row["steamid64"])
                db.steam_link_checked(row["steamid64"], error=f"Unexpected: {exc}")
                status = "crashed"
            tally[status] = tally.get(status, 0) + 1
            if status not in ("imported", "expired", "unparsable"):
                break
            row = db.get_steam_link(row["steamid64"])   # cursor moved; re-read it
            if row is None:
                break
    return tally


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="a single sweep, then exit")
    ap.add_argument("--steamid", help="only this subscription (ignores the due filter)")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between sweeps in loop mode (default 60)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    db.init_schema()

    while True:
        tally = sweep(steamid=args.steamid)
        if tally:
            log.info("sweep: %s", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        if args.once or args.steamid:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
