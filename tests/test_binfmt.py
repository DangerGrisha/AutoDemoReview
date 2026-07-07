"""Round-trip, edge-case, and size tests for demoreview.binfmt (C2R3 v1).

Plain-runnable, no pytest dependency:  python tests/test_binfmt.py
(Functions are named test_* so pytest *could* collect them later, but nothing requires it.)
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from demoreview import binfmt  # noqa: E402

# Flag bits (mirror binfmt docstring)
PRESENT, ALIVE, CROUCH, SCOPED, DEFUSING = 0x01, 0x02, 0x04, 0x08, 0x10


# --------------------------------------------------------------------------- #
# tiny assert harness
# --------------------------------------------------------------------------- #
_FAILURES = []


def check(cond, msg):
    if not cond:
        _FAILURES.append(msg)
        print("  FAIL:", msg)


def circ_deg_diff(a, b):
    d = abs((a - b + 180.0) % 360.0 - 180.0)
    return d


# --------------------------------------------------------------------------- #
# synthetic builders
# --------------------------------------------------------------------------- #
def make_track(n, alive_from=0, alive_to=None, x0=0.0, y0=0.0, z0=-200.0,
               vx=15.0, vy=-9.0, yaw0=-55.0, dyaw=3.0, pitch0=2.0,
               weapon=1, hp0=100):
    """Build one player track of length n with freeze-on-death applied.

    Alive during [alive_from, alive_to). Before/after, geometry is frozen at the
    nearest alive value and flags drop the ALIVE bit.
    """
    if alive_to is None:
        alive_to = n
    X = [0.0] * n
    Y = [0.0] * n
    Z = [0.0] * n
    yaw = [0.0] * n
    pitch = [0.0] * n
    flags = [0] * n
    health = [0] * n
    weap = [255] * n
    flash = [0] * n

    last = None
    for i in range(n):
        alive = alive_from <= i < alive_to
        if alive:
            X[i] = x0 + vx * (i - alive_from)
            Y[i] = y0 + vy * (i - alive_from)
            Z[i] = z0 + math.sin(i * 0.3) * 40.0
            yaw[i] = ((yaw0 + dyaw * i + 180.0) % 360.0) - 180.0
            pitch[i] = pitch0 + math.sin(i * 0.2) * 5.0
            flags[i] = PRESENT | ALIVE | (CROUCH if i % 7 == 0 else 0)
            health[i] = max(1, hp0 - (i - alive_from))
            weap[i] = weapon
            flash[i] = 200 if (i - alive_from) in (2, 3) else 0
            last = (X[i], Y[i], Z[i], yaw[i], pitch[i])
        else:
            fx = last if last is not None else (x0, y0, z0, yaw0, pitch0)
            X[i], Y[i], Z[i], yaw[i], pitch[i] = fx
            flags[i] = 0            # absent/dead: not present&alive
            health[i] = 0
            weap[i] = 255
            flash[i] = 0
    return {"X": X, "Y": Y, "Z": Z, "yaw": yaw, "pitch": pitch,
            "flags": flags, "health": health, "weapon": weap, "flash": flash}


def make_model(n, tracks, rnd=5):
    players = [{"slot": i, "sid": str(76561190000000000 + i), "name": "P%d" % i,
                "team": 2 if i < len(tracks) // 2 else 3}
               for i in range(len(tracks))]
    return {
        "map": "de_mirage", "round": rnd, "tickRate": 64, "sampleStride": 4,
        "sampleRate": 16.0, "nSamples": n, "startTick": 1000, "endTick": 1000 + n * 4,
        "players": players, "weaponTable": ["", "AK", "AWP", "USP", "🔪", "💣"],
        "utility": [{"type": "smoke", "thrower": players[0]["sid"], "throwTick": 1100,
                     "detTick": 1150, "throwSample": 25, "detSample": 37,
                     "pos": [100.0, 200.0, -150.0], "radius": 144, "duration": 18.0}],
        "tracks": tracks,
    }


def assert_roundtrip(model, label):
    data, clamps = binfmt.encode_round(model)
    dec = binfmt.decode_round(data)
    n = model["nSamples"]
    sxy = dec["scaleXY"]
    sz = dec["scaleZ"]
    tol_xy = 0.5 / sxy + 1e-6
    tol_z = 0.5 / sz + 1e-6
    tol_ang = 0.5 * 360.0 / binfmt.YAW_UNITS + 1e-6
    tol_pitch = 0.5 / binfmt.ANG_SCALE + 1e-6

    check(dec["nSamples"] == n, "%s: nSamples preserved" % label)
    check(len(dec["tracks"]) == len(model["tracks"]), "%s: player count preserved" % label)
    check(dec["players"] == model["players"], "%s: player metadata preserved" % label)
    check(dec["weaponTable"] == model["weaponTable"], "%s: weaponTable preserved" % label)
    check(dec["utility"] == model["utility"], "%s: utility preserved" % label)

    worst = 0.0
    for si, (a, b) in enumerate(zip(model["tracks"], dec["tracks"])):
        for i in range(n):
            ex = abs(a["X"][i] - b["X"][i])
            ey = abs(a["Y"][i] - b["Y"][i])
            ez = abs(a["Z"][i] - b["Z"][i])
            ep = abs(a["pitch"][i] - b["pitch"][i])
            ea = circ_deg_diff(a["yaw"][i], b["yaw"][i])
            worst = max(worst, ex, ey, ez)
            check(ex <= tol_xy, "%s p%d s%d X err %.4f > %.4f" % (label, si, i, ex, tol_xy))
            check(ey <= tol_xy, "%s p%d s%d Y err %.4f > %.4f" % (label, si, i, ey, tol_xy))
            check(ez <= tol_z, "%s p%d s%d Z err %.4f > %.4f" % (label, si, i, ez, tol_z))
            check(ep <= tol_pitch, "%s p%d s%d pitch err %.5f" % (label, si, i, ep))
            check(ea <= tol_ang, "%s p%d s%d yaw err %.5f" % (label, si, i, ea))
            for ch in ("flags", "health", "weapon", "flash"):
                check(a[ch][i] == b[ch][i],
                      "%s p%d s%d %s %d!=%d" % (label, si, i, ch, a[ch][i], b[ch][i]))
    return data, clamps


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_varint_roundtrip():
    for v in [0, 1, 127, 128, 255, 16383, 16384, 65535, 131071, 2097151]:
        buf = bytearray()
        binfmt.write_varint(buf, v)
        got, pos = binfmt.read_varint(buf, 0)
        check(got == v and pos == len(buf), "varint %d roundtrip (got %d)" % (v, got))


def test_zigzag_roundtrip():
    for n in [0, 1, -1, 2, -2, 32767, -32767, -32768, 32768, 100000, -100000]:
        check(binfmt.zigzag_decode(binfmt.zigzag_encode(n)) == n, "zigzag %d" % n)


def test_basic_roundtrip():
    tracks = [make_track(120, x0=100 + 200 * i, y0=-300 + 150 * i, weapon=1 + (i % 4))
              for i in range(10)]
    data, clamps = assert_roundtrip(make_model(120, tracks), "basic10p")
    check(clamps == 0, "basic10p: no clamps (got %d)" % clamps)
    print("  basic10p: %d samples x10p -> %d bytes, %d clamps" % (120, len(data), clamps))


def test_dead_and_absent():
    tracks = [
        make_track(80, alive_from=0, alive_to=40),     # dies mid-round
        make_track(80, alive_from=20, alive_to=80),    # absent early then alive
        make_track(80, alive_from=0, alive_to=0),      # never alive (all dead)
        make_track(80, alive_from=0, alive_to=80),     # alive whole round
    ]
    assert_roundtrip(make_model(80, tracks), "dead_absent")


def test_single_sample():
    assert_roundtrip(make_model(1, [make_track(1), make_track(1)]), "single_sample")


def test_zero_samples():
    tracks = [{"X": [], "Y": [], "Z": [], "yaw": [], "pitch": [],
               "flags": [], "health": [], "weapon": [], "flash": []} for _ in range(3)]
    assert_roundtrip(make_model(0, tracks), "zero_samples")


def test_angle_wrap():
    n = 12
    yaws = [170, 175, 179, 180, -179, -175, -170, -180, 178, -178, 0, 90]
    tr = make_track(n)
    tr["yaw"] = [float(y) for y in yaws]
    assert_roundtrip(make_model(n, [tr]), "angle_wrap")
    # The ±180 crossing must cost the same as small non-crossing steps: build a control
    # whose yaw makes equal-magnitude moves that never cross the seam, and compare size.
    wrap_size = len(binfmt.encode_round(make_model(n, [tr]))[0])
    ctrl = make_track(n)
    ctrl["yaw"] = [float((i * 3) % 60) for i in range(n)]   # small, no seam crossing
    ctrl_size = len(binfmt.encode_round(make_model(n, [ctrl]))[0])
    check(wrap_size <= ctrl_size + 4,
          "angle_wrap: wrap size %d not ~= control %d (modular delta failed)"
          % (wrap_size, ctrl_size))


def test_clamp_counting():
    # a *dead* sample carrying a wild coordinate is outside the alive-only bbox and
    # must clamp safely (and be counted) rather than wrap.
    tr = make_track(30, alive_from=0, alive_to=20)
    tr["X"][25] = 1e9      # dead sample, absurd coord
    _data, clamps = binfmt.encode_round(make_model(30, [tr]))
    check(clamps >= 1, "clamp_counting: wild dead coord was clamped (got %d)" % clamps)


def test_size_vs_naive():
    tracks = [make_track(200, x0=100 + 180 * i, y0=-400 + 120 * i, weapon=1 + (i % 4))
              for i in range(10)]
    model = make_model(200, tracks)
    data, _ = binfmt.encode_round(model)
    naive = binfmt.naive_json_bytes(model)
    ratio = len(naive) / len(data)
    print("  size_vs_naive: codec=%d B  naive_json=%d B  ratio=%.1fx"
          % (len(data), len(naive), ratio))
    check(len(data) < len(naive), "size_vs_naive: codec smaller than naive JSON")
    check(ratio > 3.0, "size_vs_naive: ratio >3x (got %.1fx)" % ratio)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print("Running %d test groups...\n" % len(tests))
    for t in tests:
        print(t.__name__)
        t()
    print()
    if _FAILURES:
        print("FAILED: %d assertion(s)" % len(_FAILURES))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
