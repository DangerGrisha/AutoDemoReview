"""Compact binary codec for tick-level 3D replay rounds  (format ``C2R3`` v1).

This module is the single source of truth for the on-disk / on-wire format that the
3D replay is built on. It is **pure** (no demoparser / pandas / numpy import) so it can
be unit-tested in isolation and mirrored exactly by ``web/replay3d_decoder.mjs``.

The public surface is :func:`encode_round` / :func:`decode_round`, which round-trip a
neutral in-memory ``round_model`` dict (see below). Quantization happens inside the codec
so a round-trip is self-contained and the exact same rounding is applied on encode and on
the (Python-mirror) decode.

round_model  (the contract between extraction and this codec)
-------------------------------------------------------------
    {
      "map": str, "round": int, "tickRate": int, "sampleStride": int,
      "sampleRate": float, "nSamples": int, "startTick": int, "endTick": int,
      "players": [ {"slot": int, "sid": str, "name": str, "team": int}, ... ],  # slot order
      "weaponTable": [str, ...],           # weapon index -> short tag
      "utility": [ {...}, ... ],           # opaque metadata, carried verbatim in the header
      "tracks": [                          # one per player, slot order, each list len == nSamples
        { "X":[float], "Y":[float], "Z":[float], "yaw":[float], "pitch":[float],
          "flags":[int], "health":[int], "weapon":[int], "flash":[int] }, ...
      ],
    }

Extraction is expected to have already applied *freeze-on-death*: for any sample where a
player is not present-and-alive, its X/Y/Z/yaw/pitch equal the last present-and-alive
value (so geometry deltas stay ~0 across death), and the ``flags`` bit tells the renderer
to hide them. The codec then treats geometry as a smooth stream and never needs the
dead/alive semantics — except that it computes the quantization bounding box over
*alive* samples only (dead frozen coords are inside that box anyway, but this keeps the
scale honest even if extraction ever leaves a stray dead coordinate in).

Container layout (little-endian for all fixed multi-byte fields; varints are LEB128)
------------------------------------------------------------------------------------
    off 0  : 4  magic       "C2R3"
    off 4  : 1  version     1
    off 5  : 1  codecFlags  bit0=1 -> varint+changelist (this spec). other bits reserved.
    off 6  : 2  reserved    0
    off 8  : 4  headerLen   uint32 LE
    off 12 :    header      headerLen bytes of UTF-8 minified JSON
    ...    :    tickStream  binary blob

JSON header keys: v, map, round, tickRate, sampleStride, sampleRate, nSamples, startTick,
endTick, origin[cx,cy,cz] (per-round bbox center), scaleXY, scaleZ, angScale (=32768/180),
players[{slot,sid,name,team}], weaponTable[...], utility[...].

Tick stream blob:
    uint32 nPlayers
    uint32 offset[nPlayers]   # byte offset from blob start to each player's block
    per player (slot order), channels in this fixed order:
      geometry: X, Y, Z, yaw, pitch
        - sample 0 keyframe: int16 (X,Y,Z,pitch) or uint16 (yaw brad), LE
        - samples 1..n-1: zigzag-varint delta from previous sample (<=3 bytes each)
          yaw uses the *minimal modular* delta so the +180/-180 wrap costs 1 byte.
      discrete: FLAGS, HEALTH, WEAPON, FLASH
        - varint nChanges, then nChanges * ( varint gap, uint8 value )
          gap = samples since the previous change; sample 0 is always emitted.
          value holds until the next change or nSamples (fill-forward on decode).

Quantization:
    X_q = clamp_i16(round((X-cx)*scaleXY));  Y_q likewise;  Z_q uses scaleZ.
    pitch_q = clamp_i16(round(pitch*angScale)).
    yaw_q  = round((yaw % 360)/360 * 65536) & 0xFFFF   (uint16, wraps; no clamp).
    -32768 is reserved; clamp target is [-32767, 32767]. Clamp count is surfaced by callers.

FLAGS bitfield (uint8): bit0 present, bit1 alive, bit2 crouch, bit3 scoped, bit4 defusing.
HEALTH uint8 0-100. WEAPON uint8 index into weaponTable (255 = none). FLASH uint8
(0 = not flashed .. 255 ~= 5.1s).
"""

import json
import struct

MAGIC = b"C2R3"
VERSION = 1
CODEC_VARINT_CHANGELIST = 0x01
ANG_SCALE = 32768.0 / 180.0          # quant units per degree (fixed); yaw uses brad below
YAW_UNITS = 65536                     # yaw brad resolution
I16_MAX = 32767
I16_MIN_RESERVED = -32768            # reserved sentinel; encoder never emits it
POS_MARGIN = 1.02                    # widen the bbox slightly before deriving scale
SCALE_TARGET = 32000.0              # map half-span to ~this many quant units (leaves headroom)

# Geometry channels in fixed wire order. kind: "lin" (linear int16) / "ang" (modular uint16).
GEOM_CHANNELS = (("X", "lin"), ("Y", "lin"), ("Z", "lin"), ("yaw", "ang"), ("pitch", "lin"))
DISCRETE_CHANNELS = ("flags", "health", "weapon", "flash")


# --------------------------------------------------------------------------- #
# primitive helpers (mirrored exactly by the JS decoder)
# --------------------------------------------------------------------------- #
def zigzag_encode(n):
    return (n << 1) ^ (n >> 63)      # n fits in int32 here; >>63 gives -1 for negatives


def zigzag_decode(u):
    return (u >> 1) ^ -(u & 1)


def write_varint(buf, value):
    """Append LEB128 unsigned varint (value >= 0) to bytearray `buf`."""
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def read_varint(buf, pos):
    """Return (value, new_pos). Reads an LEB128 unsigned varint from `buf` at `pos`."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _clamp_i16(v, counter):
    """Round to int, clamp to [-32767, 32767]; bump counter[0] on clamp."""
    q = int(round(v))
    if q > I16_MAX:
        counter[0] += 1
        return I16_MAX
    if q < -I16_MAX:                  # keep -32768 reserved
        counter[0] += 1
        return -I16_MAX
    return q


def _yaw_brad(deg):
    return int(round((deg % 360.0) / 360.0 * YAW_UNITS)) & 0xFFFF


def _mod_delta(cur, prev):
    """Minimal signed delta in (-32768, 32768] for two uint16 angle brads."""
    return ((cur - prev + 32768) & 0xFFFF) - 32768


# --------------------------------------------------------------------------- #
# scale derivation
# --------------------------------------------------------------------------- #
def derive_transform(tracks):
    """Compute per-round (origin, scaleXY, scaleZ) over present-and-alive samples.

    Returns (cx, cy, cz, scaleXY, scaleZ). Falls back to a unit scale for degenerate
    (empty / zero-span) input so encoding never divides by zero.
    """
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    seen = False
    for tr in tracks:
        flags = tr["flags"]
        xs, ys, zs = tr["X"], tr["Y"], tr["Z"]
        for i, fl in enumerate(flags):
            if not (fl & 0x02):          # alive bit
                continue
            seen = True
            for axis, arr in ((0, xs), (1, ys), (2, zs)):
                v = arr[i]
                if v < lo[axis]:
                    lo[axis] = v
                if v > hi[axis]:
                    hi[axis] = v
    if not seen:
        return 0.0, 0.0, 0.0, 1.0, 1.0
    cx = (lo[0] + hi[0]) / 2.0
    cy = (lo[1] + hi[1]) / 2.0
    cz = (lo[2] + hi[2]) / 2.0
    half_xy = max(hi[0] - cx, hi[1] - cy, 1e-6) * POS_MARGIN
    half_z = max(hi[2] - cz, 1e-6) * POS_MARGIN
    scale_xy = SCALE_TARGET / half_xy
    scale_z = SCALE_TARGET / half_z
    return cx, cy, cz, scale_xy, scale_z


# --------------------------------------------------------------------------- #
# per-player block encode / decode
# --------------------------------------------------------------------------- #
def _encode_player_block(tr, n, cx, cy, cz, scale_xy, scale_z, clamp_counter):
    buf = bytearray()

    # geometry: quantize whole channel, then keyframe + zigzag-varint deltas
    quant = {}
    quant["X"] = [_clamp_i16((v - cx) * scale_xy, clamp_counter) for v in tr["X"]]
    quant["Y"] = [_clamp_i16((v - cy) * scale_xy, clamp_counter) for v in tr["Y"]]
    quant["Z"] = [_clamp_i16((v - cz) * scale_z, clamp_counter) for v in tr["Z"]]
    quant["pitch"] = [_clamp_i16(v * ANG_SCALE, clamp_counter) for v in tr["pitch"]]
    quant["yaw"] = [_yaw_brad(v) for v in tr["yaw"]]

    for name, kind in GEOM_CHANNELS:
        q = quant[name]
        if n == 0:
            continue
        if kind == "ang":
            buf += struct.pack("<H", q[0])
        else:
            buf += struct.pack("<h", q[0])
        prev = q[0]
        for i in range(1, n):
            cur = q[i]
            d = _mod_delta(cur, prev) if kind == "ang" else (cur - prev)
            zz = zigzag_encode(d)
            start = len(buf)
            write_varint(buf, zz)
            assert len(buf) - start <= 3, "geometry varint exceeded 3 bytes"
            prev = cur

    # discrete: change-lists
    for name in DISCRETE_CHANNELS:
        arr = tr[name]
        changes = []           # (sample_index, value)
        if n > 0:
            changes.append((0, arr[0] & 0xFF))
            for i in range(1, n):
                v = arr[i] & 0xFF
                if v != changes[-1][1]:
                    changes.append((i, v))
        write_varint(buf, len(changes))
        last = 0
        for idx, val in changes:
            write_varint(buf, idx - last)
            buf.append(val)
            last = idx
    return bytes(buf)


def _decode_player_block(blob, pos, n):
    tr = {"X": [], "Y": [], "Z": [], "yaw": [], "pitch": [],
          "flags": [], "health": [], "weapon": [], "flash": []}
    quant = {}
    for name, kind in GEOM_CHANNELS:
        vals = []
        if n > 0:
            if kind == "ang":
                (kf,) = struct.unpack_from("<H", blob, pos)
                pos += 2
            else:
                (kf,) = struct.unpack_from("<h", blob, pos)
                pos += 2
            vals.append(kf)
            prev = kf
            for _ in range(1, n):
                zz, pos = read_varint(blob, pos)
                d = zigzag_decode(zz)
                cur = ((prev + d) & 0xFFFF) if kind == "ang" else (prev + d)
                vals.append(cur)
                prev = cur
        quant[name] = vals

    for name in DISCRETE_CHANNELS:
        n_changes, pos = read_varint(blob, pos)
        arr = [0] * n
        cur_idx = 0
        cur_val = 0
        first = True
        for _ in range(n_changes):
            gap, pos = read_varint(blob, pos)
            val = blob[pos]
            pos += 1
            idx = gap if first else cur_idx + gap
            # fill-forward from previous change up to this one
            for j in range(cur_idx, idx):
                arr[j] = cur_val
            cur_idx = idx
            cur_val = val
            first = False
        for j in range(cur_idx, n):
            arr[j] = cur_val
        tr[name] = arr
    return tr, pos, quant


def _dequantize(tr, quant_by_slot, slot, cx, cy, cz, scale_xy, scale_z):
    q = quant_by_slot[slot]
    tr["X"] = [cx + v / scale_xy for v in q["X"]]
    tr["Y"] = [cy + v / scale_xy for v in q["Y"]]
    tr["Z"] = [cz + v / scale_z for v in q["Z"]]
    tr["pitch"] = [v / ANG_SCALE for v in q["pitch"]]
    tr["yaw"] = [(v / YAW_UNITS) * 360.0 for v in q["yaw"]]


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def encode_round(model):
    """Encode a round_model dict into ``C2R3`` v1 bytes.

    Returns ``(data: bytes, clamp_count: int)``.
    """
    n = int(model["nSamples"])
    tracks = model["tracks"]
    cx, cy, cz, scale_xy, scale_z = derive_transform(tracks)
    clamp_counter = [0]

    blocks = [_encode_player_block(tr, n, cx, cy, cz, scale_xy, scale_z, clamp_counter)
              for tr in tracks]

    # blob = nPlayers + offset table + concatenated blocks
    n_players = len(blocks)
    table_bytes = 4 + 4 * n_players
    offsets = []
    running = table_bytes
    for b in blocks:
        offsets.append(running)
        running += len(b)
    blob = bytearray()
    blob += struct.pack("<I", n_players)
    for off in offsets:
        blob += struct.pack("<I", off)
    for b in blocks:
        blob += b

    header = {
        "v": VERSION, "map": model["map"], "round": model["round"],
        "tickRate": model["tickRate"], "sampleStride": model["sampleStride"],
        "sampleRate": model["sampleRate"], "nSamples": n,
        "startTick": model["startTick"], "endTick": model["endTick"],
        "origin": [cx, cy, cz], "scaleXY": scale_xy, "scaleZ": scale_z,
        "angScale": ANG_SCALE,
        "players": model["players"], "weaponTable": model["weaponTable"],
        "utility": model.get("utility", []),
    }
    header_bytes = json.dumps(header, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")

    out = bytearray()
    out += MAGIC
    out.append(VERSION)
    out.append(CODEC_VARINT_CHANGELIST)
    out += struct.pack("<H", 0)                      # reserved
    out += struct.pack("<I", len(header_bytes))
    out += header_bytes
    out += blob
    return bytes(out), clamp_counter[0]


def read_header(data):
    """Parse just the JSON header from a ``C2R3`` blob (no tick-stream decode).

    Cheap metadata access for building a round manifest without expanding tracks.
    """
    if data[:4] != MAGIC:
        raise ValueError("bad magic: not a C2R3 blob")
    (header_len,) = struct.unpack_from("<I", data, 8)
    return json.loads(data[12:12 + header_len].decode("utf-8"))


def decode_round(data):
    """Decode ``C2R3`` v1 bytes into a round_model dict (geometry dequantized to floats)."""
    if data[:4] != MAGIC:
        raise ValueError("bad magic: not a C2R3 blob")
    version = data[4]
    if version != VERSION:
        raise ValueError("unsupported version %d" % version)
    codec = data[5]
    if codec != CODEC_VARINT_CHANGELIST:
        raise ValueError("unsupported codecFlags 0x%02x" % codec)
    (header_len,) = struct.unpack_from("<I", data, 8)
    hstart = 12
    header = json.loads(data[hstart:hstart + header_len].decode("utf-8"))
    blob = memoryview(data)[hstart + header_len:]

    n = header["nSamples"]
    cx, cy, cz = header["origin"]
    scale_xy, scale_z = header["scaleXY"], header["scaleZ"]

    (n_players,) = struct.unpack_from("<I", blob, 0)
    offsets = [struct.unpack_from("<I", blob, 4 + 4 * i)[0] for i in range(n_players)]

    tracks = []
    quant_by_slot = []
    for slot in range(n_players):
        tr, _pos, quant = _decode_player_block(blob, offsets[slot], n)
        quant_by_slot.append(quant)
        tracks.append(tr)
    for slot in range(n_players):
        _dequantize(tracks[slot], quant_by_slot, slot, cx, cy, cz, scale_xy, scale_z)

    model = {
        "map": header["map"], "round": header["round"], "tickRate": header["tickRate"],
        "sampleStride": header["sampleStride"], "sampleRate": header["sampleRate"],
        "nSamples": n, "startTick": header["startTick"], "endTick": header["endTick"],
        "players": header["players"], "weaponTable": header["weaponTable"],
        "utility": header.get("utility", []), "tracks": tracks,
        "origin": [cx, cy, cz], "scaleXY": scale_xy, "scaleZ": scale_z,
    }
    return model


# --------------------------------------------------------------------------- #
# naive JSON baseline (for the size comparison in tests / build3d)
# --------------------------------------------------------------------------- #
def naive_json_bytes(model):
    """A fair 'if we just dumped JSON' baseline carrying the same information.

    Positions rounded to 0.1u, angles to 0.1deg (finer than we render), minified.
    """
    n = model["nSamples"]
    players = []
    for p, tr in zip(model["players"], model["tracks"]):
        samples = []
        for i in range(n):
            samples.append([
                round(tr["X"][i], 1), round(tr["Y"][i], 1), round(tr["Z"][i], 1),
                round(tr["yaw"][i], 1), round(tr["pitch"][i], 1),
                tr["flags"][i], tr["health"][i], tr["weapon"][i], tr["flash"][i],
            ])
        players.append({"slot": p["slot"], "sid": p["sid"], "name": p["name"],
                        "team": p["team"], "samples": samples})
    obj = {
        "map": model["map"], "round": model["round"], "tickRate": model["tickRate"],
        "sampleStride": model["sampleStride"], "sampleRate": model["sampleRate"],
        "nSamples": n, "startTick": model["startTick"], "endTick": model["endTick"],
        "players": players, "weaponTable": model["weaponTable"],
        "utility": model.get("utility", []),
    }
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
