"""Download a Valve matchmaking demo and decompress it.

The GC hands back a URL like
``http://replay123.valve.net/730/003600000000_1234567890.dem.bz2``. The file is
bzip2-compressed. We stream it to disk, then decompress to a plain ``.dem`` that
the existing parser could later consume (not wired up in this PoC).
"""

from __future__ import annotations

import bz2
from pathlib import Path

import httpx

from .logutil import StepLogger

# Valve replay servers are plain HTTP and can be slow; give them room.
_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)
_CHUNK = 1 << 16


class DemoNotReady(RuntimeError):
    """The replay server answered 5xx -- the demo isn't published yet, or the
    server is busy. Seen in the wild as a plain 502 minutes after a match ends,
    so callers must treat it as "come back later", never as a hard failure."""


def download_demo(url: str, dest_dir: Path, log: StepLogger) -> Path:
    """Download ``url`` (a .dem.bz2) into ``dest_dir`` and decompress it.

    Returns the path to the decompressed ``.dem``. Raises on HTTP error or if the
    URL has already expired (Valve prunes MM demos after a retention window --
    typically a few weeks -- after which this 404s).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = url.rstrip("/").split("/")[-1]  # e.g. 003600..._1234.dem.bz2
    if not name.endswith(".dem.bz2"):
        log.warn(f"unexpected demo filename (no .dem.bz2 suffix): {name!r}")
    bz2_path = dest_dir / name
    dem_path = dest_dir / name[: -len(".bz2")] if name.endswith(".bz2") else dest_dir / (name + ".dem")

    log.info(f"GET {url}")
    total = 0
    try:
        with httpx.stream("GET", url, timeout=_TIMEOUT, follow_redirects=True) as resp:
            if resp.status_code == 404:
                raise FileNotFoundError(
                    f"demo not found (404) -- likely expired/pruned by Valve: {url}"
                )
            if 500 <= resp.status_code < 600:
                raise DemoNotReady(
                    f"replay server returned {resp.status_code} -- the demo is most "
                    f"likely still being published: {url}"
                )
            resp.raise_for_status()
            declared = int(resp.headers.get("content-length", 0))
            with open(bz2_path, "wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK):
                    fh.write(chunk)
                    total += len(chunk)
            if declared and total != declared:
                log.warn(f"size mismatch: got {total} bytes, header said {declared}")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"download failed: {exc}") from exc

    log.ok(f"downloaded {total / 1_048_576:.1f} MB -> {bz2_path.name}")

    log.info(f"decompressing bz2 -> {dem_path.name}")
    decompressed = 0
    with bz2.open(bz2_path, "rb") as src, open(dem_path, "wb") as dst:
        while True:
            block = src.read(_CHUNK)
            if not block:
                break
            dst.write(block)
            decompressed += len(block)
    log.ok(f"decompressed {decompressed / 1_048_576:.1f} MB -> {dem_path.name}")

    return dem_path
