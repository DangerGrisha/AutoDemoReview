"""DVPL: the packed-file wrapper of the Blitz client (`Data/**/*.dvpl`).

    payload | original_size u32le | packed_size u32le | crc32(payload) u32le
            | kind u32le | b"DVPL"

kind 0 = stored as-is (the minimap webps), 1 = LZ4 block, 2 = LZ4-HC block
(same decoder). Only the offline asset tool reads client files; the replay
parser never does. LZ4 is decoded here in pure Python rather than adding the
`lz4` package for a one-off extraction step.
"""

import struct
import zlib
from pathlib import Path

_FOOTER = struct.Struct("<IIII4s")
MAGIC = b"DVPL"
KIND_STORED, KIND_LZ4, KIND_LZ4HC = 0, 1, 2


class DvplError(ValueError):
    """Not a DVPL file, or its payload does not decode."""


def _extended_length(src, pos, value):
    """LZ4 length continuation: add 255-bytes until one is not 255."""
    while True:
        if pos >= len(src):
            raise DvplError("truncated LZ4 length")
        byte = src[pos]
        pos += 1
        value += byte
        if byte != 255:
            return value, pos


def lz4_block_decompress(src, size):
    """Decode one raw LZ4 block (no frame header) of known output `size`."""
    dst = bytearray()
    pos, end = 0, len(src)
    while pos < end:
        token = src[pos]
        pos += 1
        literals = token >> 4
        if literals == 15:
            literals, pos = _extended_length(src, pos, literals)
        if pos + literals > end:
            raise DvplError("LZ4 literals overrun the input")
        dst += src[pos:pos + literals]
        pos += literals
        if pos >= end:  # the last sequence carries literals only
            break
        if pos + 2 > end:
            raise DvplError("truncated LZ4 match offset")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(dst):
            raise DvplError(f"bad LZ4 match offset {offset}")
        match = token & 15
        if match == 15:
            match, pos = _extended_length(src, pos, match)
        match += 4
        start = len(dst) - offset
        if offset >= match:
            dst += dst[start:start + match]
        else:  # overlapping copy: the last `offset` bytes repeat
            pattern = bytes(dst[start:])
            dst += (pattern * (match // offset + 1))[:match]
        if len(dst) > size:
            raise DvplError("LZ4 output exceeds the declared size")
    if len(dst) != size:
        raise DvplError(f"LZ4 output is {len(dst)} bytes, expected {size}")
    return bytes(dst)


def unpack(raw):
    """Return the original bytes of one .dvpl file's content."""
    if len(raw) < _FOOTER.size:
        raise DvplError("too short for a DVPL footer")
    original, packed, crc, kind, magic = _FOOTER.unpack_from(raw, len(raw) - _FOOTER.size)
    if magic != MAGIC:
        raise DvplError("no DVPL magic")
    body = raw[:-_FOOTER.size]
    if len(body) != packed:
        raise DvplError(f"payload is {len(body)} bytes, footer says {packed}")
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        raise DvplError("crc32 mismatch")
    if kind == KIND_STORED:
        if original != packed:
            raise DvplError("stored payload with a different original size")
        return bytes(body)
    if kind in (KIND_LZ4, KIND_LZ4HC):
        return lz4_block_decompress(body, original)
    raise DvplError(f"unknown DVPL kind {kind}")


def read(path):
    return unpack(Path(path).read_bytes())
