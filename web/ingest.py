"""Turning a .dem file on disk into a stored match — the one path into the DB.

Both entry points use this: the manual `/upload` form and the Steam bot's
auto-download worker. Keeping it in one place matters because the steps are
order-sensitive and each has a trap:

  * dedup happens on the demo's sha256 BEFORE the ~1-minute parse, and a match
    already in the DB must not be re-parsed (nor leaked to someone who isn't in
    it);
  * the 3D-replay extract needs the raw .dem, so it has to run before the caller
    deletes the temp file — the raw demo is deliberately never kept;
  * a concurrent identical ingest loses a check-then-insert race against the
    global UNIQUE(dem_sha256) index, which is a duplicate, not an error.

The caller owns the file (and deletes it); nothing here removes it.
"""

import hashlib
import sqlite3
from dataclasses import dataclass

from . import db, participants, pipeline, replay3d


class IngestError(RuntimeError):
    """The demo could not be turned into a match (bad file, no rounds, parse failure)."""


@dataclass
class Ingested:
    """Outcome of an ingest: which match, and whether it already existed.

    `visible` is False only in the duplicate case where this viewer neither
    uploaded the match nor appears in it — the caller must not link them to it.
    """
    match_id: int
    duplicate: bool
    visible: bool = True


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def looks_like_cs2_demo(path):
    """CS2 demos start with the PBDEMS2 magic. Cheap guard before the real parse."""
    try:
        with open(path, "rb") as f:
            return f.read(8)[:7] == b"PBDEMS2"
    except OSError:
        return False


def ingest_demo(dem_path, *, owner_steamid, filename, digest=None):
    """Store the demo at `dem_path` as a match owned by `owner_steamid`.

    Returns an `Ingested`. Raises IngestError when the file isn't a usable CS2
    demo. `digest` may be passed when the caller already hashed the stream.
    """
    digest = digest or sha256_file(dem_path)

    existing = db.find_match_by_hash(digest)
    if existing is not None:
        if not db.can_access(existing, owner_steamid):
            return Ingested(match_id=existing, duplicate=True, visible=False)
        # Backfill: matches analyzed before the 3D-replay feature existed gain
        # their artifacts now, since we happen to hold the raw .dem again.
        if not replay3d.has_replay(existing):
            try:
                replay3d.extract(dem_path, existing)
            except Exception:  # noqa: BLE001 -- 3D is always best-effort
                pass
        return Ingested(match_id=existing, duplicate=True)

    if not looks_like_cs2_demo(dem_path):
        raise IngestError("That doesn't look like a CS2 demo (bad file header).")

    summary, rounds, meta = pipeline.parse_demo_file(dem_path, owner_steamid)
    if not summary:
        raise IngestError("No scored rounds found in that demo "
                          "(warm-up only or unsupported).")

    match = {
        "uploader_steamid": owner_steamid,
        "original_filename": filename,
        "map_name": meta.get("map_name"),
        "my_score": summary["my_final"],
        "opp_score": summary["opp_final"],
        "won": 1 if summary["won_match"] else 0,
        "n_rounds": summary["n_rounds"],
        "ref_name": meta.get("ref_name"),
        "ref_sid": meta.get("ref_sid"),
        "ref_kills": summary["ref_kills"],
        "ref_deaths": summary["ref_deaths"],
        "found": 1 if meta.get("found") else 0,
        "dem_sha256": digest,
        "data_json": pipeline.dumps_enriched(summary, rounds, meta),
    }
    try:
        match_id = db.insert_match_with_participants(
            match=match, participants=participants.extract_participants(summary))
    except sqlite3.IntegrityError:
        # Lost the check-then-insert race with a concurrent identical ingest.
        existing = db.find_match_by_hash(digest)
        if existing is None:
            raise
        return Ingested(match_id=existing, duplicate=True,
                        visible=db.can_access(existing, owner_steamid))

    try:
        replay3d.extract(dem_path, match_id)
    except Exception:  # noqa: BLE001
        pass
    return Ingested(match_id=match_id, duplicate=False)
