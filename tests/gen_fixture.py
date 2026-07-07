"""Generate a deterministic cross-validation fixture for the Python<->JS decoders.

Builds a small synthetic round (no demo needed), encodes it with demoreview.binfmt,
and writes tests/fixtures/synthetic_round.json containing:
  - b64:        base64 of the encoded C2R3 bytes (the identical bytes both decoders read)
  - header:     the decoded JSON header (metadata parity check)
  - expected:   the Python decode_round() output (dequantized tracks) both must match
  - tolerances: float tolerances the JS side asserts within (discrete channels are exact)

Run:  python tests/gen_fixture.py
"""

import base64
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from demoreview import binfmt  # noqa: E402

PRESENT, ALIVE, CROUCH, SCOPED, DEFUSING = 0x01, 0x02, 0x04, 0x08, 0x10

N = 24  # samples


def det_track(seed, alive_from, alive_to):
    """Fully deterministic per-player track (no RNG), with freeze-on-death."""
    X = [0.0] * N
    Y = [0.0] * N
    Z = [0.0] * N
    yaw = [0.0] * N
    pitch = [0.0] * N
    flags = [0] * N
    health = [0] * N
    weapon = [255] * N
    flash = [0] * N
    last = None
    for i in range(N):
        alive = alive_from <= i < alive_to
        if alive:
            k = i - alive_from
            X[i] = -1200.0 + seed * 300.0 + 14.0 * k
            Y[i] = 900.0 - seed * 220.0 - 8.5 * k
            Z[i] = -240.0 + 30.0 * math.sin(0.35 * i + seed)
            # deliberately sweep yaw across the +/-180 seam for some seeds
            yaw[i] = ((150.0 + seed * 40.0 + 7.0 * k + 180.0) % 360.0) - 180.0
            pitch[i] = -3.0 + 4.0 * math.sin(0.2 * i)
            flags[i] = PRESENT | ALIVE | (CROUCH if k % 5 == 0 else 0) \
                | (SCOPED if (seed == 1 and 6 <= k <= 9) else 0)
            health[i] = max(1, 100 - 3 * k)
            weapon[i] = 1 + (seed % 4)
            flash[i] = 180 if k in (3, 4, 5) else 0
            last = (X[i], Y[i], Z[i], yaw[i], pitch[i])
        else:
            fx = last if last is not None else (-1200.0 + seed * 300.0, 900.0, -240.0, 150.0, 0.0)
            X[i], Y[i], Z[i], yaw[i], pitch[i] = fx
    return {"X": X, "Y": Y, "Z": Z, "yaw": yaw, "pitch": pitch,
            "flags": flags, "health": health, "weapon": weapon, "flash": flash}


def build_model():
    tracks = [
        det_track(0, 0, N),        # alive whole round
        det_track(1, 0, 15),       # dies at sample 15
        det_track(2, 8, N),        # connects/alive from sample 8
    ]
    players = [{"slot": 0, "sid": "76561198000000001", "name": "Alpha", "team": 2},
               {"slot": 1, "sid": "76561198000000002", "name": "Bravo", "team": 2},
               {"slot": 2, "sid": "76561198000000003", "name": "César⚡", "team": 3}]
    return {
        "map": "de_mirage", "round": 7, "tickRate": 64, "sampleStride": 4,
        "sampleRate": 16.0, "nSamples": N, "startTick": 5000, "endTick": 5000 + N * 4,
        "players": players, "weaponTable": ["", "AK", "AWP", "USP", "Deagle"],
        "utility": [
            {"type": "smoke", "thrower": players[0]["sid"], "throwTick": 5040,
             "detTick": 5080, "throwSample": 10, "detSample": 20,
             "pos": [123.0, -456.0, -200.0], "radius": 144, "duration": 18.0},
            {"type": "flash", "thrower": players[2]["sid"], "detTick": 5060,
             "detSample": 15, "pos": [200.0, 100.0, -180.0], "radius": 120,
             "affected": [{"sid": players[0]["sid"], "blindDuration": 2.35}]},
        ],
        "tracks": tracks,
    }


def main():
    model = build_model()
    data, clamps = binfmt.encode_round(model)
    assert clamps == 0, "fixture should not clamp (got %d)" % clamps
    dec = binfmt.decode_round(data)

    tol = {
        "pos": 0.5 / dec["scaleXY"] + 1e-6,
        "z": 0.5 / dec["scaleZ"] + 1e-6,
        "ang": 0.5 * 360.0 / binfmt.YAW_UNITS + 1e-6,
        "pitch": 0.5 / binfmt.ANG_SCALE + 1e-6,
    }
    fixture = {
        "b64": base64.b64encode(data).decode("ascii"),
        "byteLength": len(data),
        "header": {"map": dec["map"], "round": dec["round"], "nSamples": dec["nSamples"],
                   "origin": dec["origin"], "scaleXY": dec["scaleXY"],
                   "scaleZ": dec["scaleZ"], "players": dec["players"],
                   "weaponTable": dec["weaponTable"], "utility": dec["utility"]},
        "expected": {"nSamples": dec["nSamples"], "tracks": dec["tracks"]},
        "tolerances": tol,
    }
    out_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "synthetic_round.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)
    print("wrote %s (%d encoded bytes, %d players x %d samples, %d clamps)"
          % (out_path, len(data), len(dec["tracks"]), dec["nSamples"], clamps))


if __name__ == "__main__":
    main()
