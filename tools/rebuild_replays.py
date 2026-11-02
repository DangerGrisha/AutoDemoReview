"""Bring 3D replays up to what today's extractor gives.

    python tools/rebuild_replays.py                    stale replays of matches with a kept demo
    python tools/rebuild_replays.py --force            those matches, stale or not
    python tools/rebuild_replays.py demos/a.dem ...    the match each of these demos belongs to

A replay is stale when its manifest says less than replay3d.DATA_VERSION. A failed rebuild
leaves the old replay in place. Takes about 40 s and 2 GB of memory per match, which is why
the server does not do this by itself. A demo given by hand is also kept from then on.
"""

import argparse
import gzip
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import config, db, ingest, replay3d  # noqa: E402


def rebuild_from_demo(path):
    """Rebuild the match this demo's bytes belong to, and no other. True on success."""
    match_id = db.find_match_by_hash(ingest.sha256_file(path))
    if match_id is None:
        print(f"{path}: no stored match came from this demo")
        return False
    ingest.keep_demo(path, match_id)
    return report(match_id, replay3d.extract(path, match_id))


def rebuild_kept(force=False):
    """Rebuild from the gzipped copies in config.DEMOS_DIR. True when none failed."""
    all_ok = True
    for copy in sorted(config.DEMOS_DIR.glob("*.dem.gz")):
        name = copy.name.split(".")[0]
        if not name.isdigit() or not (force or replay3d.is_stale(int(name))):
            continue
        try:
            with tempfile.TemporaryDirectory(dir=str(config.TMP_DIR)) as tmp:
                demo = Path(tmp) / "demo.dem"
                with gzip.open(copy, "rb") as src, open(demo, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                all_ok = report(int(name), replay3d.extract(demo, int(name))) and all_ok
        except (OSError, EOFError) as exc:
            print(f"match {name}: could not unpack {copy.name}: {exc}")
            all_ok = False
    return all_ok


def report(match_id, ok):
    print(f"match {match_id}: {'rebuilt' if ok else 'FAILED, the old replay stays'}")
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("demos", nargs="*", type=Path, help="raw .dem files of stored matches")
    parser.add_argument("--force", action="store_true", help="rebuild kept demos' replays even if current")
    args = parser.parse_args(argv)
    if args.demos:
        results = [rebuild_from_demo(path) for path in args.demos]
        return 0 if all(results) else 1
    return 0 if rebuild_kept(args.force) else 1


if __name__ == "__main__":
    sys.exit(main())
