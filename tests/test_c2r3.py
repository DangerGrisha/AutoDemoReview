"""Tests for the pure-Python C2R3 reader (demoreview/c2r3.py).

The reader is a mirror of the authoritative Rust writer (rust/codec), so a
round-trip against itself would prove nothing -- a decoder with a flipped
zigzag round-trips perfectly against an encoder with the same flip. Two
independent checks are used instead:

  * a REFERENCE ENCODER written here, straight from the format spec in the
    module docstring rather than from the decoder's code, so the byte layout
    itself is under test (this is what catches a wrong zigzag, a missed yaw
    wrap, or a change list that forward-fills the wrong way);
  * a PARITY check against a real .3dr produced by the Rust binary, skipped on
    a machine that has never parsed a demo.
"""

import json
import math
import struct
import unittest
from pathlib import Path

from demoreview import c2r3

ROOT = Path(__file__).resolve().parent.parent

# The channel order is part of the on-disk format, so it is restated here from
# the spec instead of imported -- a silent reordering in the module must fail.
GEOM = (("x", False), ("y", False), ("z", False), ("yaw", True), ("pitch", False))
DISCRETE = ("flags", "health", "weapon", "flash")


# --------------------------------------------------------------------------- #
# reference encoder -- written from the format spec, not from the decoder
# --------------------------------------------------------------------------- #

def _varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _zigzag(v):
    return (v << 1) ^ (v >> 63)


def _encode_geom(values, is_ang):
    out = struct.pack("<H" if is_ang else "<h", values[0])
    prev = values[0]
    for v in values[1:]:
        delta = ((v - prev + 32768) % 65536) - 32768 if is_ang else v - prev
        out += _varint(_zigzag(delta))
        prev = v
    return out


def _encode_changes(changes):
    """`changes` is [(sample_index, value)]; the first index is absolute."""
    out = _varint(len(changes))
    prev_idx = 0
    for i, (idx, value) in enumerate(changes):
        out += _varint(idx if i == 0 else idx - prev_idx) + bytes([value])
        prev_idx = idx
    return out


def _build(header, players, *, version=None, codec=None, header_len=None):
    """A complete .3dr byte string. `players` is one dict per player block."""
    blocks = []
    for p in players:
        block = b"".join(_encode_geom(p[name], is_ang) for name, is_ang in GEOM)
        block += b"".join(_encode_changes(p.get(name, [])) for name in DISCRETE)
        blocks.append(block)

    n = len(blocks)
    pos = 4 + 4 * n
    offsets = []
    for block in blocks:
        offsets.append(pos)
        pos += len(block)
    blob = struct.pack("<I", n) + struct.pack(f"<{n}I", *offsets) + b"".join(blocks)

    raw = json.dumps(header).encode("utf-8")
    return (c2r3.MAGIC
            + bytes([c2r3.VERSION if version is None else version,
                     c2r3.CODEC_VARINT_CHANGELIST if codec is None else codec])
            + b"\x00\x00"
            + struct.pack("<I", len(raw) if header_len is None else header_len)
            + raw + blob)


def _player(sid="1", name="p", team=2, slot=0):
    return {"sid": sid, "name": name, "team": team, "slot": slot}


def _header(n_samples, players, **extra):
    head = {
        "round": 7,
        "nSamples": n_samples,
        "sampleRate": 16.0,
        "startTick": 1000,
        "sampleStride": 4,
        "origin": [0.0, 0.0, 0.0],
        "scaleXY": 1.0,
        "scaleZ": 1.0,
        "players": players,
    }
    head.update(extra)
    return head


def _channels(n, x=None, y=None, z=None, yaw=None, pitch=None, **discrete):
    """One player block with sensible flat defaults for anything unspecified."""
    block = {
        "x": x if x is not None else [0] * n,
        "y": y if y is not None else [0] * n,
        "z": z if z is not None else [0] * n,
        "yaw": yaw if yaw is not None else [0] * n,
        "pitch": pitch if pitch is not None else [0] * n,
    }
    for name in DISCRETE:
        block[name] = discrete.get(name, [])
    return block


# --------------------------------------------------------------------------- #
# format constants
# --------------------------------------------------------------------------- #

def test_channel_order_matches_the_spec():
    assert c2r3._GEOM == GEOM, "geometry channel order is part of the file format"
    assert c2r3._DISCRETE == DISCRETE, "discrete channel order is part of the format"


def test_quantisation_constants():
    # pitch spans -180..180 over the signed 16-bit range; yaw uses the full u16.
    assert c2r3.ANG_SCALE == 32768.0 / 180.0
    assert c2r3.YAW_UNITS == 65536.0
    assert (c2r3.F_PRESENT, c2r3.F_ALIVE, c2r3.F_CROUCH) == (0x01, 0x02, 0x04)


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

def test_positions_survive_negative_and_large_deltas():
    """Zigzag varints must carry deltas of either sign, of any size."""
    xs = [0, -1, -700, 4000, 3999, -32768, 32767]
    n = len(xs)
    data = _build(_header(n, [_player()]), [_channels(n, x=xs)])

    track = c2r3.decode_round(data).tracks[0]
    assert [round(v) for v in track["x"]] == xs


def test_dequantisation_applies_origin_and_scale():
    """x = origin.x + q/scaleXY and z = origin.z + q/scaleZ, exactly."""
    xs, zs = [0, 800, -400], [0, 320, -64]
    header = _header(3, [_player()], origin=[-2000.5, 1000.0, 64.0],
                     scaleXY=8.0, scaleZ=32.0)
    data = _build(header, [_channels(3, x=xs, z=zs)])

    track = c2r3.decode_round(data).tracks[0]
    assert [math.isclose(got, -2000.5 + q / 8.0)
            for got, q in zip(track["x"], xs)] == [True] * 3
    assert [math.isclose(got, 64.0 + q / 32.0)
            for got, q in zip(track["z"], zs)] == [True] * 3


def test_yaw_wraps_through_zero():
    """Yaw is modulo 2^16: turning right past 360 must not fly off to 720."""
    yaws = [65500, 30, 100, 65000]
    data = _build(_header(4, [_player()]), [_channels(4, yaw=yaws)])

    got = c2r3.decode_round(data).tracks[0]["yaw"]
    assert all(0.0 <= v < 360.0 for v in got), got
    assert [round(v / 360.0 * 65536.0) for v in got] == yaws


def test_pitch_is_signed_degrees():
    pitches = [0, 5461, -5461]          # ~0, +30, -30 degrees
    data = _build(_header(3, [_player()]), [_channels(3, pitch=pitches)])

    got = c2r3.decode_round(data).tracks[0]["pitch"]
    assert math.isclose(got[0], 0.0)
    assert math.isclose(got[1], 30.0, abs_tol=0.01), got[1]
    assert math.isclose(got[2], -30.0, abs_tol=0.01), got[2]


# --------------------------------------------------------------------------- #
# change lists
# --------------------------------------------------------------------------- #

def test_change_list_forward_fills_to_the_next_change():
    alive = c2r3.F_PRESENT | c2r3.F_ALIVE
    data = _build(_header(8, [_player()]),
                  [_channels(8, flags=[(0, alive), (5, c2r3.F_PRESENT)])])

    flags = c2r3.decode_round(data).tracks[0]["flags"]
    assert flags == [alive] * 5 + [c2r3.F_PRESENT] * 3


def test_change_list_gaps_after_the_first_are_relative():
    """First gap is an absolute index; the rest are deltas from the previous."""
    data = _build(_header(8, [_player()]),
                  [_channels(8, weapon=[(2, 1), (5, 2), (6, 3)])])

    assert c2r3.decode_round(data).tracks[0]["weapon"] == [0, 0, 1, 1, 1, 2, 3, 3]


def test_samples_before_the_first_change_are_zero():
    data = _build(_header(5, [_player()]), [_channels(5, health=[(3, 100)])])

    assert c2r3.decode_round(data).tracks[0]["health"] == [0, 0, 0, 100, 100]


def test_empty_change_list_fills_the_whole_channel():
    data = _build(_header(4, [_player()]), [_channels(4)])

    assert c2r3.decode_round(data).tracks[0]["flash"] == [0, 0, 0, 0]


# --------------------------------------------------------------------------- #
# Round helpers
# --------------------------------------------------------------------------- #

def test_round_metadata_and_time_conversion():
    data = _build(_header(160, [_player()]), [_channels(160)])
    rnd = c2r3.decode_round(data)

    assert rnd.number == 7
    assert rnd.n_samples == 160
    assert rnd.sample_rate == 16.0
    assert math.isclose(rnd.time_at(32), 2.0)      # 32 samples at 16 Hz


def test_sample_of_tick_is_clamped_to_the_round():
    data = _build(_header(10, [_player()]), [_channels(10)])
    rnd = c2r3.decode_round(data)               # startTick 1000, stride 4

    assert rnd.sample_of_tick(1000) == 0
    assert rnd.sample_of_tick(1020) == 5
    assert rnd.sample_of_tick(0) == 0           # before the round -> first sample
    assert rnd.sample_of_tick(999_999) == 9     # after it -> last sample


def test_track_of_and_alive_at_follow_the_roster():
    alive = c2r3.F_PRESENT | c2r3.F_ALIVE
    roster = [_player(sid="A", team=2, slot=0), _player(sid="B", team=3, slot=1)]
    data = _build(_header(6, roster), [
        _channels(6, x=[10] * 6, flags=[(0, alive)]),               # A alive all round
        _channels(6, x=[20] * 6, flags=[(0, alive), (3, c2r3.F_PRESENT)]),  # B dies at 3
    ])
    rnd = c2r3.decode_round(data)

    assert rnd.track_of("A")["x"][0] == 10.0
    assert rnd.track_of("B")["x"][0] == 20.0
    assert rnd.track_of("nobody") is None
    assert rnd.alive_at(0) == {"A": 2, "B": 3}
    assert rnd.alive_at(4) == {"A": 2}           # B is gone and stays gone


def test_read_header_returns_the_side_channels_without_decoding_ticks():
    utility = [{"type": "flash", "thrower": "A", "detSample": 40,
                "affected": [{"sid": "B", "blindDuration": 1.9}]}]
    header = _header(4, [_player(sid="A")], utility=utility,
                     shots=[{"p": 0, "sample": 12}])
    got = c2r3.read_header(_build(header, [_channels(4)]))

    assert got["utility"] == utility
    assert got["shots"] == [{"p": 0, "sample": 12}]
    assert got["round"] == 7


# --------------------------------------------------------------------------- #
# malformed input -- every one of these must be a clean C2R3Error
# --------------------------------------------------------------------------- #

def _raises(fn, message_part):
    try:
        fn()
    except c2r3.C2R3Error as exc:
        assert message_part in str(exc), f"expected {message_part!r} in {exc!r}"
        return
    raise AssertionError(f"expected C2R3Error mentioning {message_part!r}")


def test_rejects_foreign_and_future_files():
    good = _build(_header(2, [_player()]), [_channels(2)])

    _raises(lambda: c2r3.decode_round(b"NOPE" + good[4:]), "bad magic")
    _raises(lambda: c2r3.decode_round(b"\x00" * 8), "bad magic")
    _raises(lambda: c2r3.decode_round(
        _build(_header(2, [_player()]), [_channels(2)], version=99)), "version")
    _raises(lambda: c2r3.decode_round(
        _build(_header(2, [_player()]), [_channels(2)], codec=0x7F)), "codec")


def test_rejects_truncated_and_inconsistent_files():
    _raises(lambda: c2r3.decode_round(
        _build(_header(2, [_player()]), [_channels(2)], header_len=10_000)),
        "truncated header")
    # A change list pointing past the end of the round is corruption, not a
    # value to clamp: silently trimming it would fabricate player state.
    _raises(lambda: c2r3.decode_round(
        _build(_header(4, [_player()]), [_channels(4, flags=[(9, 3)])])),
        "out of range")
    # Roster and blocks must agree, or tracks line up with the wrong players.
    _raises(lambda: c2r3.decode_round(
        _build(_header(2, [_player(sid="A"), _player(sid="B")]), [_channels(2)])),
        "mismatch")


# --------------------------------------------------------------------------- #
# parity with the real Rust writer
# --------------------------------------------------------------------------- #

def _find_artifact():
    base = ROOT / "web" / "data" / "replay3d"
    if not base.is_dir():
        return None
    return next(iter(sorted(base.glob("*/r*.3dr"))), None)


def test_parity_with_a_real_extract3d_artifact():
    """The reader must agree with the Rust writer on a file it actually wrote."""
    path = _find_artifact()
    if path is None:
        raise unittest.SkipTest("no 3D artifacts on this machine "
                                "(web/data/replay3d is gitignored)")
    rnd = c2r3.decode_round(path)

    assert rnd.n_samples > 0
    assert len(rnd.tracks) == len(rnd.players) > 0
    for player, track in zip(rnd.players, rnd.tracks):
        for channel in ("x", "y", "z", "yaw", "pitch", *DISCRETE):
            assert len(track[channel]) == rnd.n_samples, (player["sid"], channel)
        assert all(0.0 <= v < 360.0 for v in track["yaw"])
        assert all(-90.1 <= v <= 90.1 for v in track["pitch"])
        assert all(0 <= v <= 100 for v in track["health"])
        # Hammer units: any real map fits well inside +-20000 on every axis.
        assert all(abs(v) < 20_000 for v in track["x"] + track["y"] + track["z"])


def test_a_dead_player_never_comes_back_in_the_same_round():
    """`alive_at` underpins every trade and spacing metric in tactics.py."""
    path = _find_artifact()
    if path is None:
        raise unittest.SkipTest("no 3D artifacts on this machine")
    rnd = c2r3.decode_round(path)

    for player, track in zip(rnd.players, rnd.tracks):
        alive = [bool(f & c2r3.F_ALIVE) for f in track["flags"]]
        if True in alive and False in alive[alive.index(True):]:
            first_dead = alive.index(False, alive.index(True))
            assert not any(alive[first_dead:]), (
                f"{player['sid']} is alive again after sample {first_dead}")
