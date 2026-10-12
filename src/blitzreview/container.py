"""Open a `.wotbreplay` file safely.

A replay is a zip with three members:

    meta.json           client version, map, the recorder's tank and account
    battle_results.dat  final results of all players -- MISSING when the
                        recorder quit to the hangar before the results arrived
    data.wotreplay      the packet stream (positions, roster, kill feed, ...)

These files come from strangers through the upload form, so everything is read
in memory -- never extracted, so no path traversal -- and every member is
bounded twice: by the size its zip header declares and by a capped read, since
a forged header is exactly how a zip bomb lies about its size. Members with
names we do not know are ignored, never read.
"""

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 30 * 1024 * 1024

# Real members are far smaller (meta ~330 B, results ~25 KB, stream <= ~5 MB);
# the caps leave headroom for long battles and future versions.
MAX_MEMBER_BYTES = {
    "meta.json": 64 * 1024,
    "battle_results.dat": 4 * 1024 * 1024,
    "data.wotreplay": 64 * 1024 * 1024,
}


class ReplayError(ValueError):
    """The file is not a usable World of Tanks Blitz replay."""


@dataclass(frozen=True)
class ReplayFiles:
    meta: dict
    battle_results: bytes | None
    data: bytes | None


def _as_bytes(src, limit):
    if isinstance(src, (bytes, bytearray, memoryview)):
        raw = bytes(src)
    else:
        path = Path(src)
        if path.stat().st_size > limit:
            raise ReplayError(f"file is larger than {limit // (1024 * 1024)} MB")
        raw = path.read_bytes()
    if len(raw) > limit:
        raise ReplayError(f"file is larger than {limit // (1024 * 1024)} MB")
    return raw


def _read_member(zf, info, limit):
    if info.file_size > limit:
        raise ReplayError(f"{info.filename} is too large ({info.file_size} bytes)")
    try:
        with zf.open(info) as fh:
            data = fh.read(limit + 1)
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError) as exc:
        raise ReplayError(f"{info.filename} cannot be read: {exc}") from exc
    if len(data) > limit:
        raise ReplayError(f"{info.filename} unpacks beyond {limit} bytes")
    return data


def open_replay(src, *, max_file_bytes=MAX_FILE_BYTES, max_member_bytes=None):
    """Read a replay from a path or bytes into a ReplayFiles."""
    limits = dict(MAX_MEMBER_BYTES, **(max_member_bytes or {}))
    raw = _as_bytes(src, max_file_bytes)
    if not raw.startswith(b"PK\x03\x04"):
        raise ReplayError("not a .wotbreplay (no zip signature)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ReplayError(f"broken zip: {exc}") from exc

    with zf:
        members = {}
        for info in zf.infolist():
            if info.filename in limits and info.filename not in members:
                members[info.filename] = info
        if "meta.json" not in members:
            raise ReplayError("meta.json is missing -- not a Blitz replay")

        found = {name: _read_member(zf, info, limits[name])
                 for name, info in members.items()}

    try:
        meta = json.loads(found["meta.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError("meta.json is not valid JSON") from exc
    if not isinstance(meta, dict):
        raise ReplayError("meta.json is not an object")

    return ReplayFiles(meta=meta,
                       battle_results=found.get("battle_results.dat"),
                       data=found.get("data.wotreplay"))
