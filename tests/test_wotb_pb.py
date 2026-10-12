"""Tests for the protobuf wire reader (blitzreview/pb.py).

Everything Blitz sends is protobuf without a schema, so this reader is the one
place where a wire-format slip would corrupt EVERY number downstream -- and
silently, because a misread varint still yields a plausible integer. The cases
that matter are the ones real data exercises: 10-byte varints (negative int32
such as "hp left: -3"), unknown fields mixed in, and garbage that must raise
instead of decoding into nonsense.
"""

from blitzreview.pb import FIXED32, LEN, VARINT, Message, PbError, read_varint


def _varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(tag, wire, payload):
    key = _varint((tag << 3) | wire)
    if wire == VARINT:
        return key + _varint(payload)
    if wire == LEN:
        return key + _varint(len(payload)) + payload
    return key + payload


def _raises(fn):
    try:
        fn()
    except PbError:
        return
    raise AssertionError("expected PbError")


def test_varints_round_trip_including_the_ten_byte_maximum():
    for value in (0, 1, 127, 128, 300, 16587784, 2**63, 2**64 - 1):
        assert read_varint(_varint(value), 0) == (value, len(_varint(value)))


def test_a_negative_int32_comes_back_negative():
    """Destroyed vehicles report hp_left = -3: a 10-byte two's-complement varint."""
    msg = Message(_field(1, VARINT, -3))
    assert msg.sint(1) == -3
    assert msg.uint(1) == 2**64 - 3


def test_truncated_and_overlong_varints_raise():
    _raises(lambda: read_varint(b"\x80\x80", 0))
    _raises(lambda: read_varint(b"\xff" * 11, 0))


def test_scalars_keep_the_last_value_and_repeats_keep_wire_order():
    buf = (_field(5, VARINT, 1) + _field(5, VARINT, 2)
           + _field(9, LEN, _field(1, VARINT, 10))
           + _field(9, LEN, _field(1, VARINT, 20)))
    msg = Message(buf)
    assert msg.uint(5) == 2
    assert [m.uint(1) for m in msg.messages(9)] == [10, 20]


def test_missing_fields_fall_back_to_defaults():
    msg = Message(b"")
    assert msg.uint(1) == 0 and msg.sint(1) == 0
    assert msg.string(1) == "" and msg.message(1) is None and msg.messages(1) == []
    assert msg.float32(1) is None


def test_strings_floats_and_nested_messages():
    buf = (_field(1, LEN, "Каньон".encode("utf-8"))
           + _field(2, FIXED32, b"\x00\x00\x40\x40")
           + _field(3, LEN, _field(7, VARINT, 42)))
    msg = Message(buf)
    assert msg.string(1) == "Каньон"
    assert msg.float32(2) == 3.0
    assert msg.message(3).uint(7) == 42


def test_reading_a_field_with_the_wrong_wire_type_raises():
    msg = Message(_field(1, LEN, b"abc"))
    _raises(lambda: msg.uint(1))


def test_malformed_buffers_raise_instead_of_decoding_garbage():
    _raises(lambda: Message(_varint((1 << 3) | LEN) + _varint(50) + b"short"))
    _raises(lambda: Message(_varint((1 << 3) | 3)))       # deprecated group
    _raises(lambda: Message(_varint(0 << 3) + b"\x01"))   # field number 0
    _raises(lambda: Message(_varint((1 << 3) | FIXED32) + b"\x00\x00"))
