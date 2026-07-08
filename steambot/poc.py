"""End-to-end proof-of-concept orchestration.

Ties the three layers together with loud, honest logging:
    1. sharecode chain   (Web API, pure Python, reliable)
    2. GC demo-URL resolve (ValvePython -- the uncertain layer)
    3. download + decompress (pure Python, reliable)

Nothing here touches the parsing pipeline / DB / Steam chat -- by design, this
pass only proves detection + raw download for a single account.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from . import sharecode
from .config import BotConfig
from .downloader import download_demo
from .logutil import StepLogger
from .sharecode_api import (
    ShareCodeAuthError,
    ShareCodeInvalidKnownCode,
    ShareCodeTransientError,
    get_next_sharecode,
)


def check_next_sharecode(cfg: BotConfig, log: StepLogger) -> Optional[str]:
    """Layer 1 only: one call to the sharecode chain. No Steam login, no GC."""
    log.banner("Layer 1 -- sharecode chain (Steam Web API, no login)")
    log.step("Calling GetNextMatchSharingCode")
    log.info(f"steamid={cfg.target_steamid}  seed knowncode={cfg.known_sharecode}")
    nxt = get_next_sharecode(
        web_api_key=cfg.web_api_key,
        steamid=cfg.target_steamid,
        match_auth_code=cfg.match_auth_code,
        known_code=cfg.known_sharecode,
        log=log,
    )
    if nxt is None:
        log.ok("chain reachable; no NEW match since the seed (this is expected if "
               "you haven't played since). Play a match, wait ~5-10 min, retry.")
    else:
        log.ok(f"a newer match exists: {nxt}")
    return nxt


def resolve_and_download(
    cfg: BotConfig, share: str, log: StepLogger, gc=None
) -> Optional[Path]:
    """Layers 2+3 for a single sharecode: decode -> GC URL -> download.

    Accepts an already-connected GCClient (gc) or creates a temporary one.
    """
    log.banner("Layer 2 -- decode + GC demo-URL resolve")
    log.step(f"Decoding sharecode {share}")
    match = sharecode.decode(share)
    log.ok(f"decoded -> {match}")

    own_gc = gc is None
    if own_gc:
        from .gc_client import GCClient  # lazy: needs steam+csgo installed

        gc = GCClient(cfg, log)
    try:
        if own_gc:
            gc.login()  # inside try so close() runs even if the handshake fails
        url = gc.resolve_demo_url(match)
    finally:
        if own_gc:
            gc.close()

    if not url:
        log.fail("no demo URL returned (expired, still processing, or GC path broken)")
        return None

    log.banner("Layer 3 -- download + decompress (pure Python)")
    try:
        dem_path = download_demo(url, cfg.download_dir, log)
    except (OSError, RuntimeError) as exc:
        # 404 = Valve pruned the demo (routine); RuntimeError wraps httpx failures.
        log.fail(f"download failed: {exc}")
        return None
    log.ok(f"RAW DEMO READY: {dem_path}")
    return dem_path


def poll_loop(cfg: BotConfig, log: StepLogger, max_iterations: int = 0) -> None:
    """Full PoC loop: watch the chain, and on each new match resolve+download.

    max_iterations=0 means run forever. The sharecode chain advances one match
    per successful call, so we keep walking until 'n/a', then sleep and re-poll.
    """
    log.banner("PoC poll loop -- detect new match, then download its demo")
    log.info(f"poll interval: {cfg.poll_interval_s}s   download dir: {cfg.download_dir}")

    # The GC connection (Layer 2) is expensive to set up, so connect once and
    # reuse it across iterations. If the GC layer is broken (likely, per README),
    # this raises here and the loop never starts -- which is itself the result.
    from .gc_client import GCClient, GCError

    known = cfg.known_sharecode
    iterations = 0
    gc = None
    try:
        while True:
            iterations += 1
            log.step(f"[iter {iterations}] walking sharecode chain from {known}")
            try:
                nxt = get_next_sharecode(
                    web_api_key=cfg.web_api_key,
                    steamid=cfg.target_steamid,
                    match_auth_code=cfg.match_auth_code,
                    known_code=known,
                    log=log,
                )
            except (ShareCodeAuthError, ShareCodeInvalidKnownCode) as exc:
                log.fail(f"chain stopped: {exc}")
                return
            except ShareCodeTransientError as exc:
                # Momentary blip (network / 429 / 5xx) -- ride it out, don't die.
                log.warn(f"transient API error: {exc}")
                if max_iterations and iterations >= max_iterations:
                    return
                time.sleep(cfg.poll_interval_s)
                continue

            if nxt is None:
                log.info(f"caught up; sleeping {cfg.poll_interval_s}s")
                if max_iterations and iterations >= max_iterations:
                    log.ok("reached max_iterations; stopping")
                    return
                time.sleep(cfg.poll_interval_s)
                continue

            # New match detected -> resolve + download. Connect GC lazily on first
            # real need so Layer-1-only testing never requires a working GC.
            if gc is None:
                gc = GCClient(cfg, log)
                gc.login()
            try:
                resolve_and_download(cfg, nxt, log, gc=gc)
            except GCError as exc:
                log.fail(f"GC step failed for {nxt}: {exc}")
                log.warn("advancing the chain anyway so we don't re-hit the same match")

            known = nxt  # advance regardless, so we make forward progress
            if max_iterations and iterations >= max_iterations:
                log.ok("reached max_iterations; stopping")
                return
    finally:
        if gc is not None:
            gc.close()
