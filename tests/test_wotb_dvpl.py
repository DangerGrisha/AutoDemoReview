"""Tests for the client's DVPL/LZ4 unpacker (blitzreview/dvpl.py).

Only the offline asset tool uses it, but every tank name, tier and minimap the
analyzer shows passes through it once, so a decoder bug becomes wrong data in
committed files. LZ4's overlapping match copy (offset < length) is the classic
place to get wrong, so it is tested explicitly, on hand-assembled blocks built
from the spec rather than from our own encoder.
"""

import struct
import unittest
import zlib
from pathlib import Path

from blitzreview import dvpl
from blitzreview.dvpl import DvplError

CLIENT_DATA = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz\Data")


def _wrap(body, original, kind):
    return body + struct.pack("<IIII4s", original, len(body), zlib.crc32(body), kind, b"DVPL")


def _raises(fn):
    try:
        fn()
    except DvplError:
        return
    raise AssertionError("expected DvplError")


def test_literal_only_block():
    assert dvpl.lz4_block_decompress(b"\x50hello", 5) == b"hello"


def test_overlapping_match_repeats_the_pattern():
    # "abc" then a match of 9 bytes at offset 3: the copy overlaps itself.
    block = bytes([(3 << 4) | (9 - 4)]) + b"abc" + b"\x03\x00"
    assert dvpl.lz4_block_decompress(block, 12) == b"abcabcabcabc"


def test_extended_literal_and_match_lengths():
    data = bytes(range(256)) + bytes(24)                    # 280 literals
    block = bytes([0xF0]) + bytes([255, 280 - 15 - 255]) + data
    assert dvpl.lz4_block_decompress(block, 280) == data
    # one literal, then a 300-byte run via an extended match length
    run = bytes([0x1F]) + b"z" + b"\x01\x00" + bytes([255, 300 - 4 - 15 - 255])
    assert dvpl.lz4_block_decompress(run, 301) == b"z" * 301


def test_bad_offsets_and_sizes_raise():
    _raises(lambda: dvpl.lz4_block_decompress(bytes([0x10]) + b"a" + b"\x05\x00", 10))
    _raises(lambda: dvpl.lz4_block_decompress(b"\x50hello", 6))


def test_dvpl_footer_stored_and_lz4():
    assert dvpl.unpack(_wrap(b"payload", 7, dvpl.KIND_STORED)) == b"payload"
    block = bytes([(3 << 4) | 5]) + b"abc" + b"\x03\x00"
    assert dvpl.unpack(_wrap(block, 12, dvpl.KIND_LZ4HC)) == b"abcabcabcabc"


def test_dvpl_rejects_bad_magic_crc_and_kind():
    good = _wrap(b"payload", 7, dvpl.KIND_STORED)
    _raises(lambda: dvpl.unpack(good[:-4] + b"XXXX"))
    _raises(lambda: dvpl.unpack(b"Payload" + good[7:]))      # crc no longer matches
    _raises(lambda: dvpl.unpack(_wrap(b"payload", 7, 9)))
    _raises(lambda: dvpl.unpack(b"tiny"))


def test_a_real_client_file_unpacks():
    path = CLIENT_DATA / "maps.yaml.dvpl"
    if not path.is_file():
        raise unittest.SkipTest("Blitz client not installed here")
    assert dvpl.read(path).startswith(b"maps:")
