"""Tests for the 3D view's buildings and rocks: the geometry reader
(dava.read_geometry, triangles, meshes, place), the simplification in
tools/wotb_extract_assets.py and the shipped assets/objects files.

A wrong axis, a wrong quaternion order or a mirrored map still draws a
plausible city -- just in the wrong place. The check that cannot be fooled
is the ground: the scene's objects stand on its terrain, so their lowest
points must sit near the terrain under them, far nearer than under the same
objects mirrored. The file format is decoded here the way the page's script
reads it (parseObjects), and must account for every byte.
"""

import importlib.util
import json
import statistics
import struct
import zlib
from pathlib import Path

from blitzreview import dava, gamedata, render

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
CLIENT_MAPS = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz\Data\3d\Maps")


def _tool():
    spec = importlib.util.spec_from_file_location("wotb_extract_assets",
                                                  ROOT / "tools" / "wotb_extract_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _string(text):
    data = text.encode("utf-8")
    return b"\x04" + struct.pack("<I", len(data)) + data


def _group(gid, points, indices):
    """One polygon group as the client writes it: an old-format keyed archive."""
    vertices = b"".join(struct.pack("<3f2f", *p, 0.0, 0.0) for p in points)  # position + texture coords
    items = [("##name", _string("PolygonGroup")),
             ("#id", b"\x06" + struct.pack("<I", 8) + struct.pack("<Q", gid)),
             ("vertexFormat", b"\x02" + struct.pack("<i", 1 | 8)),
             ("vertexCount", b"\x02" + struct.pack("<i", len(points))),
             ("vertices", b"\x06" + struct.pack("<I", len(vertices)) + vertices),
             ("indexFormat", b"\x02" + struct.pack("<i", 0)),
             ("indexCount", b"\x02" + struct.pack("<i", len(indices))),
             ("indices", b"\x06" + struct.pack("<I", 2 * len(indices)) + struct.pack(f"<{len(indices)}H", *indices)),
             ("rhi_primitiveType", b"\x02" + struct.pack("<i", 1))]
    return b"KA\x01\x00" + struct.pack("<I", len(items)) + b"".join(_string(k) + v for k, v in items)


def _geometry(*groups):
    return b"SCPG" + struct.pack("<III", 1, len(groups), len(groups)) + b"".join(groups)


def decode_objects(blob):
    """[(points, triangles)] of an objects file, read the way parseObjects in
    the page's script reads it; asserts every byte is accounted for."""
    raw = zlib.decompress(blob)
    assert raw[:4] == b"BOB1"
    (count,) = struct.unpack_from("<I", raw, 4)
    heads = [struct.unpack_from("<HH", raw, 8 + 4 * i) for i in range(count)]
    va = 8 + 4 * count
    ta = va + 6 * sum(nv for nv, _ in heads)
    out = []
    for nv, nt in heads:
        points, x, y, z = [], 0, 0, 0
        for _ in range(nv):
            dx, dy, dz = struct.unpack_from("<3h", raw, va)
            va += 6
            x, y, z = x + dx, y + dy, z + dz
            points.append((x / 10, y / 10, z / 10))
        fresh, faces = 0, []
        for _ in range(nt):
            face = []
            for _ in range(3):
                value, shift = 0, 0
                while True:
                    byte = raw[ta]
                    ta += 1
                    value |= (byte & 127) << shift
                    shift += 7
                    if not byte & 128:
                        break
                assert value <= fresh  # a corner names the next new vertex or an older one
                face.append(fresh - value)
                fresh += value == 0
            faces.append(tuple(face))
        assert fresh == nv  # every vertex used
        out.append((points, faces))
    assert ta == len(raw)
    return out


def _height(n, heights, world, x, z):
    x0, z0, x1, z1 = world
    u, v = (x - x0) / (x1 - x0) * (n - 1), (z - z0) / (z1 - z0) * (n - 1)
    i, j = min(max(int(u), 0), n - 2), min(max(int(v), 0), n - 2)
    fu, fv = min(max(u - i, 0), 1), min(max(v - j, 0), 1)
    a = heights[j * n + i] * (1 - fu) + heights[j * n + i + 1] * fu
    b = heights[(j + 1) * n + i] * (1 - fu) + heights[(j + 1) * n + i + 1] * fu
    return a * (1 - fv) + b * fv


# --------------------------------------------------------------------------- #
# the reader (synthetic)
# --------------------------------------------------------------------------- #

def test_a_geometry_file_reads_back():
    square = [(0, 0, 0), (2, 0, 0), (2, 3, 0), (0, 3, 0)]
    groups = dava.read_geometry(_geometry(_group(166, square, [0, 1, 2, 0, 2, 3]),
                                          _group(2712, square[:3], [0, 1, 2])))
    assert sorted(groups) == [166, 2712]
    points, indices = dava.triangles(groups[166])
    assert points == [tuple(float(c) for c in p) for p in square] and indices == (0, 1, 2, 0, 2, 3)


def test_broken_geometry_fails_loudly():
    good = _geometry(_group(1, [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [0, 1, 2]))
    for bad in (b"SCPX" + good[4:], good[:-3], good + b"\0"):
        try:
            dava.read_geometry(bad)
        except dava.DavaError:
            continue
        raise AssertionError("no DavaError")
    past_the_end = dava.read_geometry(_geometry(_group(1, [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [0, 1, 3])))
    try:
        dava.triangles(past_the_end[1])
    except dava.DavaError:
        pass
    else:
        raise AssertionError("an index past the last vertex was accepted")


def test_place_scales_rotates_then_moves_like_the_scene():
    quarter = (0.0, 0.0, 2 ** -0.5, 2 ** -0.5)  # 90 degrees about z (up): east turns north
    mesh = {"translation": (100.0, -50.0, 20.0), "rotation": quarter, "scale": (2.0, 1.0, 1.0)}
    x, y, z = dava.place(mesh, (1.0, 0.0, 3.0))
    assert abs(x - 100) < 1e-9 and abs(y - (-48)) < 1e-9 and abs(z - 23) < 1e-9


def test_simplify_keeps_a_house_and_drops_a_barrel():
    tool = _tool()

    def box(x, y, w, d, h):
        corners = [(x + dx * w, y + dy * d, dz * h) for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
        quads = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
        return [tuple(corners[i] for i in tri) for a, b, c, d_ in quads for tri in ((a, b, c), (a, c, d_))]

    points, faces = tool.simplify(box(0, 0, 12, 8, 6))
    assert len(faces) == 12 and len(points) == 8                      # a box stays a box
    seen = -1
    for face in faces:  # vertices numbered in the order the triangles first use them
        for i in face:
            assert i <= seen + 1
            seen = max(seen, i)
    assert tool.simplify(box(0, 0, 1, 1, 1)) is None                   # a barrel
    assert tool.simplify(box(0, 0, 30, 0.3, 1.2)) is None              # a low fence


# --------------------------------------------------------------------------- #
# the shipped files
# --------------------------------------------------------------------------- #

def test_every_map_with_terrain_ships_its_objects():
    maps = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]
    for key, entry in maps.items():
        if "terrain" not in entry:
            continue
        found = gamedata.objects(key)
        assert found is not None and 0 < len(found["data"]) <= 400_000, key
    assert gamedata.objects("no_such_map") is None


def test_the_objects_decode_and_stand_on_the_ground():
    for key in ("canyon", "himmelsdorf", "savanna", "rift"):
        found = gamedata.objects(key)
        objects = decode_objects(found["data"])
        assert sum(len(f) for _, f in objects) == found["tris"], key
        n, heights = gamedata.terrain_heights(key)
        world = gamedata.minimap(key)["world"]
        gaps, mirrored = [], []
        for points, _faces in objects:
            cx = sum(p[0] for p in points) / len(points)
            cz = sum(p[1] for p in points) / len(points)
            if not (world[0] < cx < world[2] and world[1] < cz < world[3]):
                continue  # the far mountains
            low = min(points, key=lambda p: p[2])
            gaps.append(abs(low[2] - _height(n, heights, world, low[0], low[1])))
            mirrored.append(abs(low[2] - _height(n, heights, world, -low[0], low[1])))
        assert len(gaps) > 200, key
        assert statistics.median(gaps) < 3.2, (key, statistics.median(gaps))
        assert statistics.median(gaps) < 0.7 * statistics.median(mirrored), (key, statistics.median(mirrored))


def test_the_scene_reader_agrees_with_the_shipped_objects():
    scene_dir = CLIENT_MAPS / "25_canyon_ca"
    if not scene_dir.is_dir():
        import unittest
        raise unittest.SkipTest("World of Tanks Blitz client not installed here")
    from blitzreview import dvpl
    groups = dava.read_geometry(dvpl.read(scene_dir / "25_canyon_ca.scg.dvpl"))
    meshes = dava.meshes(dava.read_scene(dvpl.read(scene_dir / "25_canyon_ca.sc2.dvpl")))
    assert len(meshes) > 2000 and all(g in groups for m in meshes for g in m["groups"])
    assert len(decode_objects(gamedata.objects("canyon")["data"])) > 400


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

def _replay_json(page):
    start = page.index('<script type="application/json" id="replay-data">') + 49
    return json.loads(page[start:page.index("</script>", start)])


def test_the_page_carries_the_objects_or_fetches_them_from_the_site():
    from blitzreview import battle
    b = battle.load(FIXTURES / "v11_20_canyon_results.wotbreplay")
    found = gamedata.objects("canyon")
    inside = _replay_json(render.render_battle_html(b))["objects"]
    assert zlib.decompress(__import__("base64").b64decode(inside["dz"])) == zlib.decompress(found["data"])
    linked = _replay_json(render.render_battle_html(b, objects_base="/blitz/objects/"))["objects"]
    assert linked == {"url": f"/blitz/objects/{found['file']}.bin", "tris": found["tris"]}


def test_only_plain_file_names_are_served():
    assert gamedata.objects_file("25_canyon_ca") == (gamedata.ASSETS / "objects" / "25_canyon_ca.bin").read_bytes()
    for bad in ("../maps", "..", "25_canyon_ca.bin", "a/b", "", None, "maps.json", "25_canyon_ca\\x"):
        assert gamedata.objects_file(bad) is None, bad
