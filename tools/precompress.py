"""Precompress large static payloads for the web app.

Writes `<file>.br` (brotli, quality 10) and `<file>.gz` (gzip, level 9)
siblings next to each input file. web/app.py's CompressedStaticFiles serves
these siblings (with the original Content-Type) when the browser advertises
br/gzip in Accept-Encoding.

Usage:
    python tools/precompress.py <file-or-glob> [<file-or-glob> ...]

e.g.
    python tools/precompress.py web/static/replay3d/pkg/viewer_bg.wasm ^
        web/static/replay3d/pkg/viewer.js "web/static/replay3d/assets/maps/*/world.glb"

A sibling is skipped when it already exists and is newer than its source
(mtime), so re-running after a rebuild only recompresses what changed.
Requires `pip install brotli` (build-time only); without it, only .gz is
emitted (with a warning).
"""

import glob
import gzip
import os
import sys
import time
from pathlib import Path

try:
    import brotli
except ImportError:
    brotli = None

BR_QUALITY = 10
GZ_LEVEL = 9


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:,.1f} GB"


def iter_inputs(patterns):
    """Expand globs; keep order, drop duplicates and .br/.gz siblings."""
    seen = set()
    for pat in patterns:
        matches = sorted(glob.glob(pat, recursive=True)) or [pat]
        for m in matches:
            p = Path(m)
            if p.suffix.lower() in (".br", ".gz"):
                continue                      # never compress a compressed sibling
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen:
                continue
            seen.add(key)
            yield p


def is_fresh(src, sibling):
    """True when `sibling` exists and is newer than `src` (skip recompression)."""
    return sibling.exists() and sibling.stat().st_mtime >= src.stat().st_mtime


def write_whole(path, data):
    """Write through a temporary name. A sibling cut short by a restart would look fresh
    to is_fresh, stay there on the next run, and the site would serve it."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def compress_one(src):
    """Emit .br/.gz siblings for `src`; returns a result row for the table."""
    data = None

    def read():
        nonlocal data
        if data is None:
            data = src.read_bytes()
        return data

    row = {"file": src, "orig": src.stat().st_size, "br": None, "gz": None}

    br_path = src.with_name(src.name + ".br")
    if brotli is not None:
        if is_fresh(src, br_path):
            row["br"] = br_path.stat().st_size
        else:
            t0 = time.perf_counter()
            write_whole(br_path, brotli.compress(read(), quality=BR_QUALITY))
            row["br"] = br_path.stat().st_size
            print(f"  {br_path.name}  ({time.perf_counter() - t0:.1f}s)")

    gz_path = src.with_name(src.name + ".gz")
    if is_fresh(src, gz_path):
        row["gz"] = gz_path.stat().st_size
    else:
        t0 = time.perf_counter()
        write_whole(gz_path, gzip.compress(read(), compresslevel=GZ_LEVEL, mtime=0))
        row["gz"] = gz_path.stat().st_size
        print(f"  {gz_path.name}  ({time.perf_counter() - t0:.1f}s)")

    return row


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    if brotli is None:
        print("WARNING: the `brotli` module is not installed (pip install brotli); "
              "emitting only .gz siblings.", file=sys.stderr)

    rows, missing = [], []
    for src in iter_inputs(argv):
        if not src.is_file():
            missing.append(src)
            print(f"skip (not found): {src}", file=sys.stderr)
            continue
        print(f"{src}")
        rows.append(compress_one(src))

    if rows:
        name_w = max(len(str(r["file"])) for r in rows)
        print()
        print(f"{'file':<{name_w}}  {'orig':>12}  {'br':>12}  {'':>6}  {'gz':>12}  {'':>6}")
        for r in rows:
            br = f"{human(r['br']):>12}  {r['br'] / r['orig']:>5.0%}" if r["br"] else f"{'-':>12}  {'-':>5} "
            gz = f"{human(r['gz']):>12}  {r['gz'] / r['orig']:>5.0%}" if r["gz"] else f"{'-':>12}  {'-':>5} "
            print(f"{str(r['file']):<{name_w}}  {human(r['orig']):>12}  {br}  {gz}")
    return 1 if missing and not rows else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
