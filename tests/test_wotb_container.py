"""Tests for opening uploaded replay files (blitzreview/container.py).

Replays come from strangers through the upload form, so the failure that
matters is not "a good replay fails to open" but "a hostile file gets further
than it should": a zip bomb that unpacks to gigabytes, a forged size header, a
file that is not a replay at all. Each must stop at ReplayError, in memory,
before anything else sees the bytes.
"""

import io
import json
import struct
import zipfile

from blitzreview.container import ReplayError, open_replay

META = {"version": "11.20.0", "mapName": "canyon", "dbid": "636604639"}


def _zip(members, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _raises(fn, needle=""):
    try:
        fn()
    except ReplayError as exc:
        assert needle in str(exc), f"{needle!r} not in {exc!r}"
        return
    raise AssertionError("expected ReplayError")


def _lie_about_size(raw, name, declared):
    """Rewrite the uncompressed size a member's headers claim."""
    out = bytearray(raw)
    encoded = name.encode()
    pos = 0
    while (pos := out.find(b"PK\x03\x04", pos)) != -1:     # local file header
        if out[pos + 30:pos + 30 + len(encoded)] == encoded:
            struct.pack_into("<I", out, pos + 22, declared)
        pos += 4
    pos = 0
    while (pos := out.find(b"PK\x01\x02", pos)) != -1:     # central directory
        if out[pos + 46:pos + 46 + len(encoded)] == encoded:
            struct.pack_into("<I", out, pos + 24, declared)
        pos += 4
    return bytes(out)


def test_a_minimal_replay_opens_and_missing_members_are_none():
    files = open_replay(_zip({"meta.json": json.dumps(META)}))
    assert files.meta["mapName"] == "canyon"
    assert files.battle_results is None and files.data is None


def test_all_three_members_are_returned_and_unknown_ones_ignored():
    raw = _zip({"meta.json": json.dumps(META), "battle_results.dat": b"br",
                "data.wotreplay": b"stream", "../../evil.txt": b"never read"})
    files = open_replay(raw)
    assert files.battle_results == b"br" and files.data == b"stream"


def test_not_a_zip():
    _raises(lambda: open_replay(b"MZ\x90\x00 definitely an exe"), "zip")
    _raises(lambda: open_replay(b"PK\x03\x04 but broken"), "zip")


def test_meta_json_is_required_and_must_be_an_object():
    _raises(lambda: open_replay(_zip({"data.wotreplay": b"x"})), "meta.json")
    _raises(lambda: open_replay(_zip({"meta.json": b"{not json"})), "JSON")
    _raises(lambda: open_replay(_zip({"meta.json": b"[1, 2]"})), "object")


def test_an_oversized_member_is_rejected_from_its_header():
    raw = _zip({"meta.json": json.dumps(META), "data.wotreplay": bytes(5000)})
    _raises(lambda: open_replay(raw, max_member_bytes={"data.wotreplay": 1000}), "too large")


def test_a_zip_bomb_lying_about_its_size_still_stops():
    """Forged header says 100 bytes; the deflate stream holds 200 000 zeros."""
    raw = _zip({"meta.json": json.dumps(META), "data.wotreplay": bytes(200_000)})
    forged = _lie_about_size(raw, "data.wotreplay", 100)
    _raises(lambda: open_replay(forged, max_member_bytes={"data.wotreplay": 1000}))


def test_the_whole_file_size_is_capped():
    raw = _zip({"meta.json": json.dumps(META), "data.wotreplay": bytes(50_000)},
               compression=zipfile.ZIP_STORED)
    _raises(lambda: open_replay(raw, max_file_bytes=10_000), "larger")
