"""The textured map bake (tools/prepare_map_textured.py) on small synthetic files.

What a user would notice: a map the players walk beside, light from the wrong spot in the
atlas, a map the viewer cannot read, or leaves that vanish in the distance.
"""

import importlib.util
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:
    raise unittest.SkipTest(f"needs numpy and Pillow: {exc}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import prepare_map_textured as pmt  # noqa: E402


def _exr_bytes(rgb):
    """A float EXR the way Source2Viewer writes it: ZIP, A B G R, 16 lines a block."""
    h, w = rgb.shape[:2]
    chans = b"".join(n.encode() + b"\0" + struct.pack("<i", 2) + bytes(4) + struct.pack("<ii", 1, 1) for n in "ABGR") + b"\0"

    def attr(name, typ, val):
        return name.encode() + b"\0" + typ.encode() + b"\0" + struct.pack("<I", len(val)) + val

    head = struct.pack("<II", 20000630, 2)
    head += attr("channels", "chlist", chans)
    head += attr("compression", "compression", bytes([3]))
    head += attr("dataWindow", "box2i", struct.pack("<4i", 0, 0, w - 1, h - 1))
    head += b"\0"
    blocks = []
    for y in range(0, h, 16):
        lines = rgb[y:y + 16]
        planes = [np.ones(lines.shape[:2], "<f4"), lines[..., 2], lines[..., 1], lines[..., 0]]
        raw = b"".join(np.asarray(p[i], "<f4").tobytes() for i in range(len(lines)) for p in planes)
        d = np.frombuffer(raw, np.uint8)
        inter = np.concatenate((d[0::2], d[1::2]))
        delta = np.empty_like(inter)
        delta[0] = inter[0]
        delta[1:] = ((inter[1:].astype(np.int16) - inter[:-1].astype(np.int16) + 128) & 255).astype(np.uint8)
        comp = zlib.compress(delta.tobytes())
        blocks.append(struct.pack("<iI", y, len(comp)) + comp)
    table_at = len(head)
    off = table_at + 8 * len(blocks)
    table = b""
    for b in blocks:
        table += struct.pack("<Q", off)
        off += len(b)
    return head + table + b"".join(blocks)


def test_exr_round_trip_and_half_size():
    rgb = np.arange(40 * 32 * 3, dtype=np.float32).reshape(40, 32, 3) / 100
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "x.exr"
        p.write_bytes(_exr_bytes(rgb))
        assert np.allclose(pmt.read_exr_rgb(p), rgb)
        half = pmt.read_exr_rgb(p, 2)
    assert half.shape == (20, 16, 3)
    assert np.allclose(half[3, 4], rgb[6:8, 8:10].mean((0, 1)))


def test_exr_with_other_compression_is_refused_not_misread():
    data = bytearray(_exr_bytes(np.zeros((16, 16, 3), np.float32)))
    at = data.index(b"compression\0compression\0") + len(b"compression\0compression\0") + 4
    data[at] = 4  # PIZ
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "x.exr"
        p.write_bytes(bytes(data))
        try:
            pmt.read_exr_rgb(p)
        except ValueError as e:
            assert "compression" in str(e)
        else:
            raise AssertionError("read a compression it cannot decode")


def _read_ktx2(path):
    d = Path(path).read_bytes()
    assert d[:12] == bytes([0xAB, 0x4B, 0x54, 0x58, 0x20, 0x32, 0x30, 0xBB, 0x0D, 0x0A, 0x1A, 0x0A])
    fmt, type_size, w, h, depth, layers, faces, levels, sc = struct.unpack_from("<9I", d, 12)
    dfd_at, dfd_len = struct.unpack_from("<II", d, 48)
    index = [struct.unpack_from("<QQQ", d, 80 + 24 * i) for i in range(levels)]
    return {"fmt": fmt, "w": w, "h": h, "depth": depth, "layers": layers, "faces": faces, "levels": levels,
            "sc": sc, "type_size": type_size, "dfd": (dfd_at, dfd_len), "index": index, "bytes": d}


def test_ktx2_layout_is_what_the_spec_asks():
    levels = [bytes([1]) * 32, bytes([2]) * 8]
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "a.ktx2"
        pmt.write_ktx2(p, pmt.BC1_SRGB, 8, 8, levels)
        k = _read_ktx2(p)
    assert (k["fmt"], k["w"], k["h"], k["levels"]) == (pmt.BC1_SRGB[0], 8, 8, 2)
    assert k["sc"] == 0 and k["faces"] == 1
    # largest level comes first in the index, but last in the file
    assert k["index"][0][1] == 32 and k["index"][1][1] == 8
    assert k["index"][1][0] < k["index"][0][0]
    # aligned to the block size
    assert k["index"][0][0] % 8 == 0 and k["index"][1][0] % 8 == 0
    d = k["bytes"]
    assert d[k["index"][0][0]:k["index"][0][0] + 32] == levels[0]
    assert d[k["index"][1][0]:k["index"][1][0] + 8] == levels[1]
    dfd_at, dfd_len = k["dfd"]
    assert struct.unpack_from("<I", d, dfd_at)[0] == dfd_len
    assert dfd_at == 80 + 24 * 2
    assert d[dfd_at + 12] == 128  # BC1 colour model
    assert d[dfd_at + 14] == 2  # sRGB transfer


def test_ktx2_bc3_describes_both_blocks():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "a.ktx2"
        pmt.write_ktx2(p, pmt.BC3_UNORM, 4, 4, [bytes(16)])
        k = _read_ktx2(p)
    d = k["bytes"]
    dfd_at, dfd_len = k["dfd"]
    assert d[dfd_at + 12] == 130
    assert d[dfd_at + 14] == 1  # linear: the lightmap decodes its own sqrt in the shader
    assert dfd_len == 4 + 24 + 16 * 2


def test_cut_out_textures_keep_their_coverage_down_the_mips():
    # thin scattered leaves: plain averaging loses them past the first mips
    alpha = np.random.default_rng(3).random((64, 64)).astype(np.float32) ** 3
    target = float((alpha >= 0.5).mean())
    small = pmt._half(pmt._half(pmt._half(alpha[..., None])))[..., 0]
    assert float((small >= 0.5).mean()) < target / 2
    kept = pmt._keep_coverage(small, 0.5, target)
    assert abs(float((kept >= 0.5).mean()) - target) < 0.05


def test_textures_stored_twice_in_cs2_are_stored_once():
    pytest_etcpak = importlib.util.find_spec("etcpak")
    if pytest_etcpak is None:
        raise unittest.SkipTest("needs etcpak")
    img = np.zeros((16, 16, 4), np.uint8)
    img[..., 0] = 200
    img[..., 3] = 255
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        Image.fromarray(img).save(t / "a.png")
        Image.fromarray(img).save(t / "b.png")
        out = t / "out"
        n1, mean = pmt.encode_texture(t / "a.png", out, 2048, False)
        n2, _ = pmt.encode_texture(t / "b.png", out, 2048, False)
        assert n1 == n2
        assert len(list((out / "tex").glob("*.ktx2"))) == 1
        k = _read_ktx2(out / n1)
    assert k["fmt"] == pmt.BC1_SRGB[0]
    assert k["levels"] == 3  # 16, 8, 4
    assert mean[0] > 0.5 and mean[1] < 0.01


def test_texture_with_alpha_only_gets_bc3_when_its_material_cuts_out():
    if importlib.util.find_spec("etcpak") is None:
        raise unittest.SkipTest("needs etcpak")
    img = np.full((8, 8, 4), 128, np.uint8)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        Image.fromarray(img).save(t / "a.png")
        out = t / "out"
        opaque, _ = pmt.encode_texture(t / "a.png", out, 2048, False)
        cut, _ = pmt.encode_texture(t / "a.png", out, 2048, True)
        assert _read_ktx2(out / opaque)["fmt"] == pmt.BC1_SRGB[0]
        assert _read_ktx2(out / cut)["fmt"] == pmt.BC3_SRGB[0]


def test_uv_scale_comes_from_the_block_that_has_lightmaps():
    dump = """m_bakedLightingInfo =
    {
        m_vLightmapUvScale = [ 1.0, 1.0 ]
        m_bHasLightmaps = false
        m_lightMaps = [  ]
    }
    m_worldLightingInfo =
    {
        m_vLightmapUvScale = [ 1.14284, 1.14284 ]
        m_bHasLightmaps = true
        m_lightMaps =
    }"""
    assert pmt.uv_scale_from_dump(dump) == (1.14284, 1.14284)


def test_frame_lands_on_the_viewer_contract():
    # a Source point (Sx, Sy, Sz) is exported as 0.0254 * (Sy, Sz, Sx), and the viewer wants (Sx, Sz, -Sy)
    sx, sy, sz = 100.0, -250.0, 64.0
    exported = np.array([sy, sz, sx, 1.0 / 0.0254]) * 0.0254
    out = pmt.EXPORT_TO_VIEWER @ exported
    assert np.allclose(out[:3], [sx, sz, -sy])
    assert np.linalg.det(pmt.EXPORT_TO_VIEWER[:3, :3]) > 0  # no mirroring


def _accessor(g, blob, arr, comp, typ):
    off = len(blob)
    blob += arr.tobytes()
    g["bufferViews"].append({"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes})
    a = {"bufferView": len(g["bufferViews"]) - 1, "componentType": comp, "count": len(arr), "type": typ}
    if typ == "VEC3":
        a["min"] = arr.min(0).tolist()
        a["max"] = arr.max(0).tolist()
    g["accessors"].append(a)
    return len(g["accessors"]) - 1


def _vmat(shader="csgo_lightmappedgeneric.vfx", name="materials/x.vmat", ints=None):
    return {"vmat": {"Name": name, "ShaderName": shader, "IntParams": ints or {}, "TextureParams": {}}}


def _scene():
    """Two world triangles, a prop that reuses TEXCOORD_1, a mirrored prop and a tool brush.
    Positions are in the export's frame: metres, (Sy, Sz, Sx)."""
    g = {"accessors": [], "bufferViews": [], "buffers": [{"uri": "world.bin"}],
         "materials": [{"name": "wall", "extras": _vmat()},
                       {"name": "toolsclip", "extras": _vmat(name="materials/tools/toolsclip.vmat")}],
         "meshes": [], "nodes": [], "scenes": [{"nodes": []}], "scene": 0}
    blob = bytearray()
    # the pool has more vertices than this primitive uses: the unused one must not survive
    pos = np.array([[0, 0, 0], [0, 0, 1], [1, 0, 0], [9, 9, 9]], "<f4") * 0.0254 * 100
    nrm = np.array([[0, 1, 0]] * 4, "<f4")
    uv = np.array([[0, 0], [1, 0], [0, 1], [5, 5]], "<f4")
    lm = np.array([[0.5, 0.25], [0.75, 0.25], [0.5, 0.5], [0, 0]], "<f4")
    idx = np.array([0, 1, 2], "<u2")
    a = {"POSITION": _accessor(g, blob, pos, 5126, "VEC3"),
         "NORMAL": _accessor(g, blob, nrm, 5126, "VEC3"),
         "TEXCOORD_0": _accessor(g, blob, uv, 5126, "VEC2"),
         "TEXCOORD_1": _accessor(g, blob, lm, 5126, "VEC2")}
    i = _accessor(g, blob, idx, 5123, "SCALAR")
    g["meshes"].append({"primitives": [{"attributes": a, "indices": i, "material": 0}]})
    g["meshes"].append({"primitives": [{"attributes": a, "indices": i, "material": 1}]})
    identity = np.eye(4).reshape(-1).tolist()
    mirrored = np.diag([-1.0, 1, 1, 1]).reshape(-1).tolist()
    g["nodes"] = [
        {"name": "n0_lr0_agg_merge_wall_0", "mesh": 0, "matrix": identity},
        {"name": "some_prop.body", "mesh": 0, "matrix": identity},
        {"name": "n0_lr0_mirrored", "mesh": 0, "matrix": mirrored},
        {"name": "n0_lr0_clip", "mesh": 1, "matrix": identity},
        {"name": "retake_asite_2.hammer_mesh_0", "mesh": 0, "matrix": identity},
    ]
    g["scenes"][0]["nodes"] = list(range(len(g["nodes"])))
    return g, bytes(blob)


def test_gather_bakes_frames_flags_light_and_drops_what_never_shows():
    g, blob = _scene()
    soups = pmt.gather(g, blob, np.array([2.0, 2.0]))
    assert list(soups) == [0]  # the tool brush's material is gone
    s = soups[0]
    # world, prop and mirrored kept, the retake barrier dropped
    assert len(s.tri) == 3
    # the pool's unused vertex is gone: three per instance, not four
    assert len(s.pos) == 9
    # export (a, b, c) metres -> viewer (c, b, -a) units
    assert np.allclose(s.pos[1], [100.0, 0.0, 0.0], atol=1e-3)
    assert np.allclose(s.pos[2], [0.0, 0.0, -100.0], atol=1e-3)
    # world nodes read the lightmap with the map's scale, the prop's second UV means nothing
    assert s.col[0, 3] == 1.0 and np.allclose(s.lm[0], [1.0, 0.5])
    assert s.col[3, 3] == 0.0 and np.allclose(s.lm[3], [0.0, 0.0])
    # a mirrored instance flips its winding, or its faces would point inward
    assert np.array_equal(s.tri[2] - s.tri[2].min(), [0, 2, 1])
    assert np.array_equal(s.tri[0] - s.tri[0].min(), [0, 1, 2])


def test_sun_points_up_and_keeps_its_brightness():
    g = {"extensions": {"KHR_lights_punctual": {"lights": [{"color": [1, 0.5, 0.25], "intensity": 2049, "type": "directional"}]}},
         "nodes": [{"name": "light_environment", "extensions": {"KHR_lights_punctual": {"light": 0}},
                    # column 2 is the node's +Z: back towards the sun, in the export's frame
                    "matrix": [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]}],
         "scenes": [{"nodes": [0]}], "scene": 0}
    direction, rgb = pmt.find_sun(g)
    assert direction[1] > 0.99
    assert np.allclose(rgb, [3.0, 1.5, 0.75])


def test_split_keeps_every_triangle_once_and_leaves_small_scattered_ones_alone():
    # a thousand triangles spread over a huge area: not worth a draw per area
    n = 1000
    pos = np.random.default_rng(1).random((n * 3, 3)) * 8000
    tri = np.arange(n * 3).reshape(-1, 3)
    assert len(pmt.split_triangles(pos, tri, 2000.0, 65000)) == 1
    # the same spread with many triangles is split, and nothing is lost or doubled
    n = 20000
    pos = np.random.default_rng(2).random((n * 3, 3)) * 8000
    tri = np.arange(n * 3).reshape(-1, 3)
    parts = pmt.split_triangles(pos, tri, 2000.0, 65000)
    assert len(parts) > 4
    assert sorted(np.concatenate(parts).tolist()) == list(range(n))
    # the vertex limit holds whatever the size says
    parts = pmt.split_triangles(pos, tri, 1e9, 3000)
    assert all(len(np.unique(tri[p])) <= 3000 for p in parts)


def test_grid_finds_a_floors_light_and_tells_sun_from_shade():
    # two floors at different spots: one lit by the sun, one in shade, read from the atlas
    irradiance = np.full((4, 4, 3), 0.5, np.float32)
    visibility = np.zeros((4, 4), np.float32)
    visibility[:2, :] = 1.0  # the top half of the atlas is in the sun
    s = pmt.Soup()
    s.pos = np.array([[0, 0, 0], [0, 0, 64], [64, 0, 0], [640, 0, 0], [640, 0, 64], [704, 0, 0]], np.float32)
    s.nrm = np.tile(np.array([0, 1, 0], np.float32), (6, 1))
    s.uv = np.zeros((6, 2), np.float32)
    s.lm = np.array([[.1, .1], [.2, .1], [.1, .2], [.1, .9], [.2, .9], [.1, .8]], np.float32)
    s.col = np.zeros((6, 4), np.float32)
    s.col[:, 3] = 1.0
    s.tri = np.array([[0, 1, 2], [3, 4, 5]])
    lo, dims, grid = pmt.build_light_grid({0: s}, irradiance, visibility, [0, 1, 0], [1.0, 1.0, 1.0])
    def at(p):
        return grid[tuple(np.clip(((np.array(p) - lo) / pmt.GRID_CELL).astype(int), 0, dims - 1))]
    sunny, shady = at([20, 2, 20]), at([660, 2, 20])
    assert np.allclose(sunny, 1.5, atol=0.01)  # irradiance + the sun
    assert np.allclose(shady, 0.5, atol=0.01)
    # above a floor the same light holds: a player mid-jump or a tree top
    assert np.allclose(at([20, 200, 20]), 1.5, atol=0.01)


def test_props_without_a_lightmap_get_their_light_in_the_vertex_colour():
    s = pmt.Soup()
    s.pos = np.array([[0, 0, 0], [0, 0, 0]], np.float32)
    s.nrm = np.array([[0, 1, 0], [0, -1, 0]], np.float32)
    s.col = np.zeros((2, 4), np.float32)
    lo, dims = np.array([-64.0, -96.0, -64.0]), np.array([2, 2, 2])
    grid = np.full((2, 2, 2, 3), 1.0)
    pmt.bake_props({0: s}, lo, dims, grid, [0, 1, 0], [1, 1, 1])
    lit = (s.col[:, 0] ** 2) * pmt.LIGHT_RANGE
    assert np.isclose(lit[0], 1.0, atol=0.01)  # facing the sun
    assert lit[1] < 0.5  # facing away, but not black
    assert (s.col[:, 3] == 0).all()  # and still flagged as not reading the lightmap


def _triangles(*items):
    """A soup of small triangles, each given as (height, normal y, lightmap u, v)."""
    s = pmt.Soup()
    s.pos, s.nrm, s.uv, s.lm = [], [], [], []
    for y, ny, u, v in items:
        s.pos += [[0, y, 0], [8, y, 0], [0, y, 8]]
        s.nrm += [[0, ny, 0]] * 3
        s.uv += [[0, 0]] * 3
        s.lm += [[u, v]] * 3
    s.pos, s.nrm, s.lm = (np.array(x, np.float32) for x in (s.pos, s.nrm, s.lm))
    s.uv = np.array(s.uv, np.float32)
    s.col = np.zeros((len(s.pos), 4), np.float32)
    s.col[:, 3] = 1.0
    s.tri = np.arange(len(s.pos)).reshape(-1, 3)
    return s


def test_chart_edges_spread_over_the_empty_texels_and_no_further():
    rgb = np.zeros((1, 8, 3), np.float32)
    rgb[0, 3] = 1.0
    vis = np.zeros((1, 8), np.float32)
    vis[0, 3] = 0.5
    rgb, vis = pmt.dilate_charts(rgb, vis, passes=2)
    assert rgb[0, :, 0].tolist() == [0, 1, 1, 1, 1, 1, 0, 0]
    assert vis[0, 1] == 0.5 and vis[0, 5] == 0.5


def _lightmap_with_sun_at(row):
    """An atlas with the sun in one row of texels, and a floor whose UV lands there."""
    vis = np.zeros((8, 8), np.float32)
    vis[row] = 1.0
    return np.full((8, 8, 3), 0.5, np.float32), vis


def test_the_lightmap_scale_is_the_one_that_finds_the_sun():
    tops = (np.array([0.0, 0.0]), np.array([1, 1]), np.zeros((1, 1)))
    s = _triangles((0, 1, .5, .5))
    # as exported the floor lands on row 4, scaled by 1.5 on row 6
    irr, vis = _lightmap_with_sun_at(4)
    assert pmt.choose_lightmap_scale({0: s}, tops, irr, vis, [0, 1, 0], (1.5, 1.5)).tolist() == [1.0, 1.0]
    irr, vis = _lightmap_with_sun_at(6)
    assert pmt.choose_lightmap_scale({0: s}, tops, irr, vis, [0, 1, 0], (1.5, 1.5)).tolist() == [1.5, 1.5]


def test_a_lightmap_that_fits_no_scale_stops_the_bake():
    tops = (np.array([0.0, 0.0]), np.array([1, 1]), np.zeros((1, 1)))
    s = _triangles((0, 1, .5, .5))
    irr, vis = _lightmap_with_sun_at(1)
    try:
        pmt.choose_lightmap_scale({0: s}, tops, irr, vis, [0, 1, 0], (1.5, 1.5))
    except ValueError as e:
        assert "wrong surfaces" in str(e)
    else:
        raise AssertionError("no error")


def test_an_outer_wall_counts_as_open_when_the_column_in_front_of_it_is():
    lo, dims = np.array([0.0, 0.0]), np.array([4, 1])
    top = np.array([[0.0], [0.0], [500.0], [500.0]])  # empty street, then a building
    pos = np.array([[128, 300, 0], [128, 300, 0]], np.float32)
    nrm = np.array([[-1, 0, 0], [1, 0, 0]], np.float32)  # towards the street, into the building
    assert pmt.faces_open_sky(pos, nrm, (lo, dims, top)).tolist() == [True, False]


def test_column_tops_see_a_roof_made_of_one_huge_triangle():
    s = pmt.Soup()
    s.pos = np.array([[0, 300, 0], [2000, 300, 0], [0, 300, 2000], [500, 0, 500], [510, 0, 500], [500, 0, 510]], np.float32)
    s.tri = np.array([[0, 1, 2], [3, 4, 5]])
    tops = pmt.column_tops({0: s}, {0})
    up = np.array([[0, 1, 0]], np.float32)
    # the middle of the roof is far from any of its corners, and the floor below is not open
    assert not pmt.faces_open_sky(np.array([[505, 0, 505]], np.float32), up, tops)[0]
    assert pmt.faces_open_sky(np.array([[505, 300, 505]], np.float32), up, tops)[0]
    # leaves do not make what is under them indoors
    assert pmt.column_tops({0: s}, set())[2].max() == 0.0


def test_tone_comes_from_the_maps_files_and_has_a_default(tmp_path=None):
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d)
        assert pmt.read_tone(raw) == {"exposure": pmt.DEFAULT_EXPOSURE, "white": pmt.DEFAULT_WHITE_POINT,
                                      "curve": list(pmt.DEFAULT_TONE_CURVE)}
        (raw / "ents.txt").write_text("enableexposure = true\nminexposure = 0.5\nmaxexposure = 1.5\n", encoding="utf-8")
        (raw / "post.txt").write_text(
            "m_flExposureBias = 1.0\nm_flShoulderStrength = 0.15\nm_flLinearStrength = 0.5\n"
            "m_flLinearAngle = 0.1\nm_flToeStrength = 0.2\nm_flToeNum = 0.02\nm_flToeDenom = 0.3\n"
            "m_flWhitePoint = 4.0\n", encoding="utf-8")
        assert pmt.read_tone(raw) == {"exposure": 2.0, "white": 4.0, "curve": [0.15, 0.5, 0.1, 0.2, 0.02, 0.3]}


def test_the_suns_shadow_channel_is_the_one_light_environment_names():
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d)
        assert pmt.sun_shadow_channel(raw) == 0
        (raw / "ents.txt").write_text(
            'keyValues3Data = { values = { bakedshadowindex = "3" classname = "light_omni2" } }\n'
            'keyValues3Data = { values = { bakedshadowindex = "2" color = [ 1, 1, 1 ] classname = "light_environment" } }\n'
            'keyValues3Data = { values = { bakedshadowindex = "1" classname = "light_rect" } }\n',
            encoding="utf-8")
        assert pmt.sun_shadow_channel(raw) == 2
        (raw / "ents.txt").write_text('keyValues3Data = { values = { classname = "light_environment" } }', encoding="utf-8")
        assert pmt.sun_shadow_channel(raw) == 0


def test_sun_visibility_reads_only_the_suns_channel():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "shadows.png"
        Image.fromarray(np.full((2, 2, 4), (255, 0, 51, 255), np.uint8), "RGBA").save(path)
        assert np.allclose(pmt.read_visibility(path, 0, (2, 2)), 0.0)
        assert np.allclose(pmt.read_visibility(path, 1, (2, 2)), 1.0)
        assert np.allclose(pmt.read_visibility(path, 2, (2, 2)), 0.8)
        # a greyscale file has one channel, whatever the entities say
        Image.fromarray(np.full((2, 2), 51, np.uint8), "L").save(path)
        assert np.allclose(pmt.read_visibility(path, 2, (2, 2)), 0.8)
        # and a smaller atlas comes out at the size asked for
        assert pmt.read_visibility(path, 0, (1, 1)).shape == (1, 1)


def test_the_lightmap_is_the_last_uv_set_that_stays_on_the_atlas():
    inside, outside = np.array([[0.1, 0.9]]), np.array([[-3.0, 7.0]])

    def sets(force=False, **attrs):
        return pmt.texture_and_lightmap_sets(attrs, force, lambda a: a)

    assert sets(TEXCOORD_0=outside, TEXCOORD_1=inside) == ("TEXCOORD_0", "TEXCOORD_1")
    # the material's own second UV, even when it happens to stay inside: most of de_ancient
    assert sets(TEXCOORD_0=outside, TEXCOORD_1=inside, TEXCOORD_2=inside) == ("TEXCOORD_0", "TEXCOORD_2")
    assert sets(TEXCOORD_0=outside, TEXCOORD_1=outside, TEXCOORD_2=inside) == ("TEXCOORD_0", "TEXCOORD_2")
    # no lightmap at all
    assert sets(TEXCOORD_0=inside, TEXCOORD_1=outside) == ("TEXCOORD_0", None)
    assert sets(TEXCOORD_0=inside) == ("TEXCOORD_0", None)


def test_force_uv2_moves_the_texture_to_the_second_set_and_leaves_the_lightmap_alone():
    inside, outside = np.array([[0.1, 0.9]]), np.array([[-3.0, 7.0]])
    read = lambda a: a  # noqa: E731
    attrs = {"TEXCOORD_0": inside, "TEXCOORD_1": outside, "TEXCOORD_2": inside}
    assert pmt.texture_and_lightmap_sets(attrs, True, read) == ("TEXCOORD_1", "TEXCOORD_2")
    attrs = {"TEXCOORD_0": inside, "TEXCOORD_1": outside}
    assert pmt.texture_and_lightmap_sets(attrs, True, read) == ("TEXCOORD_1", None)
    # a texture can't be asked to read a set the primitive doesn't have
    assert pmt.texture_and_lightmap_sets({"TEXCOORD_0": inside}, True, read) == ("TEXCOORD_0", None)


def test_world_file_layout_is_what_the_viewer_reads():
    chunk = {"material": 0,
             "pos": np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], "<f4"),
             "nrm": np.array([[0, 127, 0, 0]] * 3, "i1"),
             "uv": np.array([[0, 0], [1, 0], [0, 1]], "<f4"),
             "lm": np.array([[0, 0], [65535, 0], [0, 65535]], "<u2"),
             "col": np.array([[0, 0, 0, 255]] * 3, "u1"),
             "idx": np.array([0, 1, 2], "<u2")}
    head = {"version": pmt.FORMAT_VERSION, "materials": [], "grid": {"dims": [1, 1, 1]}}
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "world.c2map"
        pmt.write_world(p, head, [chunk], bytes([1, 2, 3]))
        d = p.read_bytes()
    assert d[:8] == pmt.MAGIC
    json_len, binary_at = struct.unpack_from("<II", d, 8)
    assert binary_at == 16 + json_len and binary_at % 4 == 0
    h = json.loads(d[16:16 + json_len])
    c = h["chunks"][0]
    assert (c["v"], c["i"], c["o"]) == (3, 3, 0)
    assert c["lo"] == [0, 0, 0] and c["hi"] == [1, 1, 0]
    b = d[binary_at:]
    assert struct.unpack_from("<3f", b, 12) == (1.0, 0.0, 0.0)  # second position
    idx_at = 3 * (12 + 4 + 8 + 4 + 4)
    assert struct.unpack_from("<3H", b, idx_at) == (0, 1, 2)
    # three u16 indices pad to eight bytes, then the grid
    assert h["grid"]["offset"] == idx_at + 8
    assert b[h["grid"]["offset"]:] == bytes([1, 2, 3])


def test_a_new_frame_convention_stops_the_bake():
    s = pmt.Soup()
    s.pos = np.array([[0.0, 0.0, 0.0], [100.0, 10.0, 100.0]])
    with tempfile.TemporaryDirectory() as t:
        repo, pmt.REPO_ROOT = pmt.REPO_ROOT, Path(t)
        try:
            plain = Path(t) / "web" / "static" / "replay3d" / "assets" / "maps" / "de_x"
            plain.mkdir(parents=True)
            gltf = {"accessors": [{"min": [-4000, -400, -2000], "max": [3000, 1200, 3800]}],
                    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                    "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}], "scene": 0}
            js = json.dumps(gltf).encode()
            (plain / "world.glb").write_bytes(b"glTF" + struct.pack("<II", 2, 0) + struct.pack("<I", len(js)) + b"JSON" + js)
            try:
                pmt.check_against_plain_map({0: s}, "de_x")
            except ValueError as e:
                assert "frame" in str(e)
            else:
                raise AssertionError("a map 39 times too small went through")
            # no plain map to compare with is not an error
            assert pmt.check_against_plain_map({0: s}, "de_missing") is None
        finally:
            pmt.REPO_ROOT = repo
