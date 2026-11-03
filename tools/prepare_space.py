"""Which parts of a map are solid, for the 3D replay's smoke, fire and bullet hits.

    python tools/prepare_space.py <world.c2map> <map>.c2space

A grenade's smoke in CS2 floods the open space around it and stops at walls, so a
smoke in a corridor runs further down it than one in the open. The viewer does the same
over this grid: a cell is solid when an opaque surface of the textured map passes
through it. A wall thinner than a cell still closes every path through it, because the
viewer only steps between cells that share a face.

Glass, leaves, fences and decals do not count: they are see-through, and the game's
smoke goes where players can see.

Layout, little-endian, in the map file's own frame (Hammer units, y up, z = -Hammer y):

    4 bytes  b"C2SP"
    u32      version (1)
    f32 x3   origin of cell (0, 0, 0)
    f32      cell side
    u32 x3   cells along x, y, z
    bits     one per cell, index (x * ny + y) * nz + z, lowest bit first
"""

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

MAGIC = b"C2SP"
VERSION = 1
# Half a player's width. Smaller would close fewer gaps a player cannot see through;
# larger would block doorways.
CELL = 16.0
_MAP_MAGIC = b"C2MAP2\x00\x00"


def read_world(path):
    """(header, [(positions (n, 3) float32, triangles (m, 3) int64, material)])."""
    data = Path(path).read_bytes()
    if data[:8] != _MAP_MAGIC:
        raise SystemExit(f"{path} is not a textured map file")
    head_len, binary_at = struct.unpack("<II", data[8:16])
    header = json.loads(data[16:16 + head_len])
    chunks = []
    for c in header["chunks"]:
        at = binary_at + c["o"]
        v, i = c["v"], c["i"]
        pos = np.frombuffer(data, np.float32, v * 3, at).reshape(-1, 3)
        # position, normal i8x4, uv f32x2, light uv u16x2, colour u8x4: each padded to 4
        at += v * 12 + v * 4 + v * 8 + v * 4 + v * 4
        idx = np.frombuffer(data, np.uint16, i, at).astype(np.int64).reshape(-1, 3)
        chunks.append((pos, idx, c["m"]))
    return header, chunks


def blocks(material):
    """Does this surface stop smoke? Only opaque, non-decal surfaces do."""
    return material.get("alpha", 0) == 0 and not material.get("decal", False)


def surface_points(pos, tri, cell):
    """Corners plus points spread over each triangle at under half a cell apart, so a
    big wall made of two triangles still fills every cell it crosses."""
    v0, v1, v2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    e1, e2 = v1 - v0, v2 - v0
    longest = np.maximum(np.linalg.norm(e1, axis=1), np.linalg.norm(e2, axis=1))
    # points along each edge direction: enough that neighbours sit under half a cell apart
    steps = np.clip(np.ceil(longest / (cell * 0.4)), 1, 400).astype(np.int64)
    out = [v0, v1, v2]
    for k in np.unique(steps):
        if k <= 1:
            continue
        m = steps == k
        a = np.linspace(0.0, 1.0, k + 1, dtype=np.float32)
        u, w = np.meshgrid(a, a, indexing="ij")
        keep = (u + w) <= 1.0
        u, w = u[keep][None, :, None], w[keep][None, :, None]
        pts = v0[m][:, None] + e1[m][:, None] * u + e2[m][:, None] * w
        out.append(pts.reshape(-1, 3))
    return np.concatenate(out)


def build(header, chunks, cell=CELL):
    """(origin, dims, solid bool array (nx, ny, nz))."""
    materials = header["materials"]
    solid_chunks = [(p, t) for p, t, m in chunks if blocks(materials[m])]
    if not solid_chunks:
        raise SystemExit("no opaque surfaces")
    lo = np.min([p.min(0) for p, _ in solid_chunks], axis=0) - cell
    hi = np.max([p.max(0) for p, _ in solid_chunks], axis=0) + cell
    dims = np.ceil((hi - lo) / cell).astype(np.int64)
    solid = np.zeros(tuple(dims), bool)
    for pos, tri in solid_chunks:
        pts = surface_points(pos.astype(np.float32), tri, cell)
        ijk = np.clip(((pts - lo) / cell).astype(np.int64), 0, dims - 1)
        solid[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True
    return lo.astype(np.float32), dims, solid


def write(path, origin, dims, solid, cell=CELL):
    bits = np.packbits(solid.reshape(-1), bitorder="little")
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", VERSION))
        f.write(struct.pack("<3f", *[float(x) for x in origin]))
        f.write(struct.pack("<f", cell))
        f.write(struct.pack("<3I", *[int(x) for x in dims]))
        f.write(bits.tobytes())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("world", help="a textured map's world.c2map")
    ap.add_argument("out", help="the .c2space file to write")
    args = ap.parse_args(argv)
    header, chunks = read_world(args.world)
    origin, dims, solid = build(header, chunks)
    write(args.out, origin, dims, solid)
    print(f"{args.out}: {dims.tolist()} cells of {CELL:g}, {solid.mean() * 100:.1f}% solid, "
          f"{Path(args.out).stat().st_size / 1e3:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
