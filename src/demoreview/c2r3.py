"""Pure-Python reader for the C2R3 round format written by extract3d.

The Rust codec (rust/codec/src/lib.rs) is the authoritative writer; this is a
read-only mirror so the coaching layer can use the per-round positional data
WITHOUT re-parsing the .dem. Every match that has a 3D replay already has this
data on disk, so a coaching report costs a few hundred milliseconds of file
reading instead of a 20-second demo parse.

Layout (must stay in sync with `encode_round`):

    magic "C2R3" | version u8 | codec u8 | reserved u16 | headerLen u32le
    header JSON (headerLen bytes)
    blob: nPlayers u32le | nPlayers x offset u32le | player blocks

Each player block holds five delta-coded geometry channels (x, y, z, yaw,
pitch) followed by four run-length "change list" channels (flags, health,
weapon, flash).

Reading the header alone is cheap and already carries the utility timeline,
the shot list and the roster -- `read_header` exists so callers that only need
those never pay for the tick streams.
"""

import json
import struct
from pathlib import Path

MAGIC = b"C2R3"
VERSION = 1
CODEC_VARINT_CHANGELIST = 0x01

ANG_SCALE = 32768.0 / 180.0
YAW_UNITS = 65536.0

F_PRESENT = 0x01
F_ALIVE = 0x02
F_CROUCH = 0x04
F_SCOPED = 0x08
F_DEFUSING = 0x10
F_WALKING = 0x20
F_AIRBORNE = 0x40

# Geometry channels in encode order; yaw is the one wrapped modulo 2^16.
_GEOM = (("x", False), ("y", False), ("z", False), ("yaw", True), ("pitch", False))
_DISCRETE = ("flags", "health", "weapon", "flash")


class C2R3Error(Exception):
    """Malformed or unsupported round blob."""


def _as_bytes(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    return Path(src).read_bytes()


def _split(data):
    """Return (header_dict, blob_bytes) after validating the fixed prologue."""
    if len(data) < 12 or data[:4] != MAGIC:
        raise C2R3Error("not a C2R3 blob (bad magic)")
    if data[4] != VERSION:
        raise C2R3Error(f"unsupported version {data[4]}")
    if data[5] != CODEC_VARINT_CHANGELIST:
        raise C2R3Error(f"unsupported codec {data[5]:#04x}")
    (header_len,) = struct.unpack_from("<I", data, 8)
    raw = data[12:12 + header_len]
    if len(raw) != header_len:
        raise C2R3Error("truncated header")
    return json.loads(raw), data[12 + header_len:]


def read_header(src):
    """Parse just the JSON header -- roster, utility, shots, quantisation."""
    return _split(_as_bytes(src))[0]


def _read_varint(buf, pos):
    """LEB128 unsigned varint -> (value, next_pos)."""
    result = 0
    shift = 0
    while True:
        try:
            byte = buf[pos]
        except IndexError:
            raise C2R3Error("truncated varint") from None
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise C2R3Error("varint too long")


def _decode_block(blob, pos, n):
    """Decode one player block; geometry stays quantised (ints)."""
    geom = {}
    for name, is_ang in _GEOM:
        vals = []
        if n:
            fmt = "<H" if is_ang else "<h"
            (keyframe,) = struct.unpack_from(fmt, blob, pos)
            pos += 2
            vals.append(keyframe)
            prev = keyframe
            for _ in range(n - 1):
                zz, pos = _read_varint(blob, pos)
                # zigzag: even -> +n/2, odd -> -(n+1)/2
                delta = (zz >> 1) ^ -(zz & 1)
                prev = (prev + delta) & 0xFFFF if is_ang else prev + delta
                vals.append(prev)
        geom[name] = vals

    discrete = {}
    for name in _DISCRETE:
        n_changes, pos = _read_varint(blob, pos)
        arr = [0] * n
        cur_idx = 0
        cur_val = 0
        first = True
        for _ in range(n_changes):
            gap, pos = _read_varint(blob, pos)
            try:
                val = blob[pos]
            except IndexError:
                raise C2R3Error("truncated change list") from None
            pos += 1
            # The first gap is an absolute sample index; the rest are deltas.
            idx = gap if first else cur_idx + gap
            if idx > n:
                raise C2R3Error("change-list index out of range")
            for i in range(cur_idx, idx):
                arr[i] = cur_val
            cur_idx, cur_val, first = idx, val, False
        for i in range(cur_idx, n):
            arr[i] = cur_val
        discrete[name] = arr

    return geom, discrete, pos


class Round:
    """One decoded round: header fields plus per-player sample channels.

    `tracks` is index-aligned with `header['players']`. Geometry is dequantised
    back into world units (the same units the .dem carries), so positions can be
    compared against grenade `pos` entries from the header directly.
    """

    __slots__ = ("header", "players", "tracks", "n_samples", "sample_rate",
                 "start_tick", "sample_stride")

    def __init__(self, header, tracks):
        self.header = header
        self.players = header.get("players", [])
        self.tracks = tracks
        self.n_samples = int(header.get("nSamples", 0))
        self.sample_rate = float(header.get("sampleRate", 16.0)) or 16.0
        self.start_tick = int(header.get("startTick", 0))
        self.sample_stride = int(header.get("sampleStride", 1))

    @property
    def number(self):
        return int(self.header.get("round", 0))

    def time_at(self, sample):
        """Seconds since freeze-end for a sample index."""
        return sample / self.sample_rate

    def sample_of_tick(self, tick):
        """Clamped sample index for an absolute demo tick."""
        if self.sample_stride <= 0 or self.n_samples <= 0:
            return 0
        s = (tick - self.start_tick) // self.sample_stride
        return max(0, min(self.n_samples - 1, s))

    def track_of(self, sid):
        """The track for a steamid64 string, or None when the player sat out."""
        for i, p in enumerate(self.players):
            if p.get("sid") == sid:
                return self.tracks[i]
        return None

    def alive_at(self, sample):
        """steamid64 -> team for everyone alive at `sample`."""
        out = {}
        for p, tr in zip(self.players, self.tracks):
            flags = tr["flags"]
            if sample < len(flags) and flags[sample] & F_ALIVE:
                out[p["sid"]] = p["team"]
        return out


def decode_round(src):
    """Fully decode a .3dr file into a `Round` (geometry in world units)."""
    header, blob = _split(_as_bytes(src))
    n = int(header.get("nSamples", 0))
    if len(blob) < 4:
        raise C2R3Error("truncated blob")
    (n_players,) = struct.unpack_from("<I", blob, 0)
    offsets = struct.unpack_from(f"<{n_players}I", blob, 4)

    ox, oy, oz = header.get("origin", [0.0, 0.0, 0.0])
    sxy = float(header.get("scaleXY", 1.0)) or 1.0
    sz = float(header.get("scaleZ", 1.0)) or 1.0

    tracks = []
    for off in offsets:
        geom, discrete, _ = _decode_block(blob, off, n)
        track = {
            "x": [ox + v / sxy for v in geom["x"]],
            "y": [oy + v / sxy for v in geom["y"]],
            "z": [oz + v / sz for v in geom["z"]],
            "yaw": [(v / YAW_UNITS) * 360.0 for v in geom["yaw"]],
            "pitch": [v / ANG_SCALE for v in geom["pitch"]],
        }
        track.update(discrete)
        tracks.append(track)

    if len(tracks) != len(header.get("players", [])):
        raise C2R3Error("player/track count mismatch")
    return Round(header, tracks)
