"""Standalone Phase-A CLI: extract + encode the tick-level 3D replay, report real sizes.

    python src/demoreview/build3d.py demos/match.dem [--selfcheck] [--no-write]

Parses the demo once, encodes every round into the C2R3 binary format, writes per-round
sidecars to output/<stem>/rNN.3dr, and prints a size table comparing the codec against a
naive-JSON equivalent (raw / gzip / base64). `--selfcheck` decodes each round back and
asserts every value is within quantization tolerance, reporting the total clamp count.

This is a measurement/experiment harness kept entirely separate from the shipping 2D
report (cli.py). The .3dr sidecars are a Phase-A artifact; Phase B will base64-inline the
bytes into the HTML (the report opens via file://, which blocks sidecar fetch).
"""

import base64
import gzip
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from demoreview import binfmt, replay3d  # noqa: E402

try:
    from demoparser2 import DemoParser
except ImportError:
    sys.exit("demoparser2 not installed. Activate the venv and: pip install -r requirements.txt")


def _fmt(n):
    return "{:,}".format(n)


def _tolerances(dec):
    return (0.5 / dec["scaleXY"] + 1e-6, 0.5 / dec["scaleZ"] + 1e-6,
            0.5 * 360.0 / binfmt.YAW_UNITS + 1e-6, 0.5 / binfmt.ANG_SCALE + 1e-6)


def _circ(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _selfcheck_round(model, data):
    """Decode and verify within tolerance. Returns (ok, worst_pos_err)."""
    dec = binfmt.decode_round(data)
    tol_xy, tol_z, tol_ang, tol_pitch = _tolerances(dec)
    n = model["nSamples"]
    worst = 0.0
    for a, b in zip(model["tracks"], dec["tracks"]):
        for i in range(n):
            ex = abs(a["X"][i] - b["X"][i]); ey = abs(a["Y"][i] - b["Y"][i])
            ez = abs(a["Z"][i] - b["Z"][i]); ep = abs(a["pitch"][i] - b["pitch"][i])
            ea = _circ(a["yaw"][i], b["yaw"][i])
            worst = max(worst, ex, ey, ez)
            if ex > tol_xy or ey > tol_xy or ez > tol_z or ep > tol_pitch or ea > tol_ang:
                return False, worst
            for ch in ("flags", "health", "weapon", "flash"):
                if a[ch][i] != b[ch][i]:
                    return False, worst
    return True, worst


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if not args:
        sys.exit("usage: build3d.py <demo.dem> [--selfcheck] [--no-write]")
    demo_path = args[0]
    do_selfcheck = "--selfcheck" in flags
    do_write = "--no-write" not in flags

    stem = os.path.splitext(os.path.basename(demo_path))[0]
    out_dir = os.path.join("output", stem)

    print("Parsing %s ..." % demo_path)
    parser = DemoParser(demo_path)
    models, meta = replay3d.build_replay3d(parser)

    print("map=%s  tickRate=%d (detected)  stride=%d  sampleRate=%.2f Hz  rounds=%d  mapSupported=%s"
          % (meta["map"], meta["tickRate"], meta["sampleStride"], meta["sampleRate"],
             meta["nRounds"], meta["mapSupported"]))
    if do_write:
        os.makedirs(out_dir, exist_ok=True)

    print()
    hdr = "%4s %6s %5s | %10s %10s %10s | %11s %11s | %6s %6s" % (
        "rnd", "nSamp", "plyrs", "codec", "gzip", "base64", "naiveJSON", "naive_gz",
        "ratio", "clamp")
    print(hdr)
    print("-" * len(hdr))

    tot = {"codec": 0, "gz": 0, "b64": 0, "naive": 0, "naive_gz": 0, "clamps": 0}
    tot_ok = True
    for model in models:
        data, clamps = binfmt.encode_round(model)
        naive = binfmt.naive_json_bytes(model)
        c_gz = len(gzip.compress(data, 6))
        n_gz = len(gzip.compress(naive, 6))
        b64 = ((len(data) + 2) // 3) * 4
        ratio = len(naive) / len(data) if data else 0.0

        tot["codec"] += len(data); tot["gz"] += c_gz; tot["b64"] += b64
        tot["naive"] += len(naive); tot["naive_gz"] += n_gz; tot["clamps"] += clamps

        note = ""
        if do_selfcheck:
            ok, _worst = _selfcheck_round(model, data)
            tot_ok = tot_ok and ok
            note = "ok" if ok else "FAIL"

        print("%4d %6d %5d | %10s %10s %10s | %11s %11s | %5.1fx %6d %s" % (
            model["round"], model["nSamples"], len(model["players"]),
            _fmt(len(data)), _fmt(c_gz), _fmt(b64), _fmt(len(naive)), _fmt(n_gz),
            ratio, clamps, note))

        if do_write:
            with open(os.path.join(out_dir, "r%02d.3dr" % model["round"]), "wb") as f:
                f.write(data)

    print("-" * len(hdr))
    ratio_tot = tot["naive"] / tot["codec"] if tot["codec"] else 0.0
    print("%4s %6s %5s | %10s %10s %10s | %11s %11s | %5.1fx %6d" % (
        "TOT", "", "", _fmt(tot["codec"]), _fmt(tot["gz"]), _fmt(tot["b64"]),
        _fmt(tot["naive"]), _fmt(tot["naive_gz"]), ratio_tot, tot["clamps"]))

    print()
    print("Match totals:")
    print("  codec raw      : %s bytes" % _fmt(tot["codec"]))
    print("  codec base64   : %s bytes   <- predicts Phase-B inlined HTML size" % _fmt(tot["b64"]))
    print("  naive JSON     : %s bytes" % _fmt(tot["naive"]))
    print("  base64(codec) vs naive JSON : %.1fx smaller"
          % (tot["naive"] / tot["b64"] if tot["b64"] else 0.0))
    print("  raw(codec)    vs naive JSON : %.1fx smaller" % ratio_tot)
    print("  total clamps   : %d %s" % (tot["clamps"],
          "(expected 0)" if tot["clamps"] == 0 else "<-- INVESTIGATE"))
    if do_write:
        print("  wrote %d .3dr sidecars to %s/" % (len(models), out_dir))
    if do_selfcheck:
        print("  self-check     : %s" % ("PASS" if tot_ok else "FAIL"))
        if not tot_ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
