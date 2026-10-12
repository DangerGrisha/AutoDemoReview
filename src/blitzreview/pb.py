"""Minimal protobuf wire-format reader (no schema, no dependency).

Wargaming ships battle results and most in-battle messages as protobuf but no
.proto files, so every field number we use was worked out by hand and lives
next to its meaning in battle_results.py / arena.py. This module only knows the
wire format: varints, fixed 32/64-bit values and length-delimited bytes.

A Message is split into {field: [(wire_type, value), ...]} once; nested
messages stay bytes until an accessor asks for them, so a malformed
sub-message only fails the code that actually reads it.

Signed integers: Wargaming uses plain int32/int64 (two's complement, a
negative number is a 10-byte varint), not zigzag sint32 -- a destroyed
vehicle's "hit points left" of -3 arrives as 2**64 - 3. `sint` undoes that.
"""

import struct

VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5

_MAX_VARINT_BYTES = 10


class PbError(ValueError):
    """Bytes that are not valid protobuf (or not the shape a field expects)."""


def read_varint(buf, pos):
    """Decode one base-128 varint at `pos`; return (value, next_pos)."""
    result = 0
    for i in range(_MAX_VARINT_BYTES):
        if pos >= len(buf):
            raise PbError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return result, pos
    raise PbError("varint longer than 10 bytes")


class Message:
    """One decoded protobuf message. Scalars follow proto rules: last value wins."""

    __slots__ = ("_fields",)

    def __init__(self, buf):
        buf = bytes(buf)
        fields = {}
        pos, end = 0, len(buf)
        while pos < end:
            key, pos = read_varint(buf, pos)
            tag, wire = key >> 3, key & 7
            if tag == 0:
                raise PbError("field number 0")
            if wire == VARINT:
                value, pos = read_varint(buf, pos)
            elif wire == LEN:
                size, pos = read_varint(buf, pos)
                if pos + size > end:
                    raise PbError(f"field {tag}: length {size} overruns the buffer")
                value = buf[pos:pos + size]
                pos += size
            elif wire == FIXED32:
                if pos + 4 > end:
                    raise PbError(f"field {tag}: truncated fixed32")
                value = buf[pos:pos + 4]
                pos += 4
            elif wire == FIXED64:
                if pos + 8 > end:
                    raise PbError(f"field {tag}: truncated fixed64")
                value = buf[pos:pos + 8]
                pos += 8
            else:  # 3/4 are deprecated groups, 6/7 do not exist
                raise PbError(f"field {tag}: unsupported wire type {wire}")
            fields.setdefault(tag, []).append((wire, value))
        self._fields = fields

    def has(self, tag):
        return tag in self._fields

    def tags(self):
        return sorted(self._fields)

    def _last(self, tag, wire):
        values = self._fields.get(tag)
        if not values:
            return None
        got, value = values[-1]
        if got != wire:
            raise PbError(f"field {tag}: wire type {got}, expected {wire}")
        return value

    def uint(self, tag, default=0):
        value = self._last(tag, VARINT)
        return default if value is None else value

    def sint(self, tag, default=0):
        """int32/int64 field: two's complement, NOT zigzag."""
        value = self._last(tag, VARINT)
        if value is None:
            return default
        return value - (1 << 64) if value >= 1 << 63 else value

    def float32(self, tag, default=None):
        value = self._last(tag, FIXED32)
        return default if value is None else struct.unpack("<f", value)[0]

    def double(self, tag, default=None):
        value = self._last(tag, FIXED64)
        return default if value is None else struct.unpack("<d", value)[0]

    def raw(self, tag, default=None):
        value = self._last(tag, LEN)
        return default if value is None else value

    def string(self, tag, default=""):
        value = self._last(tag, LEN)
        return default if value is None else value.decode("utf-8", "replace")

    def message(self, tag):
        value = self._last(tag, LEN)
        return None if value is None else Message(value)

    def messages(self, tag):
        """Every occurrence of a repeated message field, in wire order."""
        out = []
        for wire, value in self._fields.get(tag, ()):
            if wire != LEN:
                raise PbError(f"field {tag}: wire type {wire}, expected {LEN}")
            out.append(Message(value))
        return out
