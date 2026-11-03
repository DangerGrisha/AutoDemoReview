"""The 3D replay's effects: which ones the page asks for (web/replay3d.py, replay3d.html),
and the two build tools behind them (tools/prepare_fx.py, tools/prepare_space.py).

What a user would notice: a fresh clone without the never-committed CS2 particle textures
breaking smoke and fire instead of keeping the simple shapes; REPLAY3D_CS2_FX=0 not turning
them off; or a smoke that leaks through walls because the solid-space grid was built wrong.
"""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-replay3d-fx-tests-0123456789")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

try:
    import prepare_fx
    import prepare_space
except ImportError as exc:  # the analysis suite must still run without the image stack
    raise unittest.SkipTest(f"needs the image stack: {exc}")

try:
    from jinja2 import Environment, FileSystemLoader

    from web import replay3d
except ImportError as exc:
    raise unittest.SkipTest(f"needs the web stack: {exc}")

TEMPLATES = ROOT / "web" / "templates"


class _Fx:
    """replay3d looking at a temporary fx folder holding the atlases `sheets` and the
    grids of `spaces`."""

    def __init__(self, sheets=None, spaces=(), flag=None):
        self.sheets = replay3d.FX_SHEETS if sheets is None else sheets
        self.spaces, self.flag = spaces, flag

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for name in self.sheets:
            (root / f"{name}.ktx2").write_bytes(b"ktx2")
        (root / "space").mkdir()
        for name in self.spaces:
            (root / "space" / f"{name}.c2space").write_bytes(b"C2SP")
        self._dir = replay3d.FX_DIR
        replay3d.FX_DIR = root
        self._env = os.environ.pop("REPLAY3D_CS2_FX", None)
        if self.flag is not None:
            os.environ["REPLAY3D_CS2_FX"] = self.flag
        return self

    def __exit__(self, *exc):
        replay3d.FX_DIR = self._dir
        os.environ.pop("REPLAY3D_CS2_FX", None)
        if self._env is not None:
            os.environ["REPLAY3D_CS2_FX"] = self._env
        self._tmp.cleanup()


def test_the_site_knows_every_atlas_the_viewer_loads():
    names = [s["name"] for s in prepare_fx.read_sheet_list()]
    assert list(replay3d.FX_SHEETS) == names
    assert {"smoke", "fire", "fireball", "blast_smoke", "blood", "dust", "glow"} <= set(names)


def test_effects_are_used_when_every_atlas_is_there():
    with _Fx():
        assert replay3d.cs2_fx_enabled()


def test_one_missing_atlas_keeps_the_simple_shapes():
    with _Fx(sheets=[n for n in replay3d.FX_SHEETS if n != "glow"]):
        assert not replay3d.cs2_fx_enabled()
    with _Fx(sheets=[]):
        assert not replay3d.cs2_fx_enabled()


def test_the_flag_turns_the_effects_off_without_touching_the_files():
    with _Fx(flag="0"):
        assert not replay3d.cs2_fx_enabled()
    with _Fx(flag="1"):
        assert replay3d.cs2_fx_enabled()


def test_a_map_gets_its_grid_only_with_the_effects():
    with _Fx(spaces=["de_mirage"]):
        assert replay3d.space_rel("de_mirage") == "fx_v1/space/de_mirage.c2space"
        assert replay3d.space_rel("de_nuke") is None
        assert replay3d.space_rel("") is None
    with _Fx(spaces=["de_mirage"], flag="0"):
        assert replay3d.space_rel("de_mirage") is None


def test_the_page_tells_the_viewer_about_the_effects_and_the_grid():
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    page = env.get_template("replay3d.html")
    common = dict(match_id=7, map_name="de_mirage", demo_name="m.dem", has_replay=True,
                  map_asset="maps/de_mirage/world.glb", cs2_models=True, wasm_size=1, map_size=1)
    on = page.render(cs2_fx=True, space_asset="fx_v1/space/de_mirage.c2space", **common)
    assert "const CS2_FX = true;" in on
    assert 'const SPACE_ASSET = "fx_v1/space/de_mirage.c2space";' in on
    off = page.render(**common)
    assert "const CS2_FX = false;" in off
    assert 'const SPACE_ASSET = "";' in off


def test_grids_go_out_compressed():
    # nothing looks wrong when they don't: de_vertigo's grid is 25 MB raw, 50 KB in brotli
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from web.app import CompressedStaticFiles

    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "de_mirage.c2space").write_bytes(b"the whole grid")
        (Path(root) / "de_mirage.c2space.br").write_bytes(b"small")
        client = TestClient(Starlette(routes=[Mount("/s", CompressedStaticFiles(directory=root))]))
        # HEAD, so the client doesn't try to unpack the fake brotli
        got = client.head("/s/de_mirage.c2space", headers={"Accept-Encoding": "br"})
        assert got.status_code == 200
        assert got.headers["content-encoding"] == "br"
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["content-length"] == str(len(b"small"))


# --- tools/prepare_fx.py ---------------------------------------------------------------

def test_an_atlas_is_a_power_of_two_both_ways():
    assert prepare_fx.layout(1) == (1, 1)
    assert prepare_fx.layout(4) == (4, 1)
    assert prepare_fx.layout(32) == (8, 4)
    assert prepare_fx.layout(48) == (8, 8), "6 rows round up to 8"
    for n in range(1, 70):
        columns, rows = prepare_fx.layout(n)
        assert columns * rows >= n
        assert columns & (columns - 1) == 0 and rows & (rows - 1) == 0


def test_a_short_sequence_repeats_frames_to_last_as_long():
    frames = [f"f{i}" for i in range(15)]
    picked = prepare_fx.pick(frames, 16)
    assert len(picked) == 16
    assert picked[0] == "f0" and picked[-1] == "f14"
    assert picked == sorted(picked, key=lambda f: int(f[1:])), "still in order"
    assert prepare_fx.pick(frames, 1) == ["f0"]
    long = [f"f{i}" for i in range(64)]
    assert prepare_fx.pick(long, 16)[-1] == "f63"


def test_the_sheet_list_reads_sequences_and_plain_textures():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sheets.txt"
        path.write_text("# comment\nsmoke a/b 0,2 16 128\nglow c+d - 1 256\n", encoding="utf-8")
        smoke, glow = prepare_fx.read_sheet_list(path)
    assert smoke == {"name": "smoke", "textures": ["a/b"], "sequences": [0, 2], "frames": 16, "cell": 128}
    assert glow["textures"] == ["c", "d"] and glow["sequences"] is None


def test_a_frame_is_premultiplied_in_linear_light():
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.png"
        pixels = np.zeros((4, 4, 4), np.uint8)
        pixels[..., :3] = 255
        pixels[..., 3] = [[0, 64, 128, 255]] * 4
        Image.fromarray(pixels, "RGBA").save(path)
        frame = prepare_fx.premultiplied_frame(path, 4)
    assert frame.shape == (4, 4, 4)
    assert np.allclose(frame[..., 0], frame[..., 3], atol=0.02), "white times its alpha"
    assert frame[0, 0, 3] < 0.01 and frame[0, 3, 3] > 0.99


# --- tools/prepare_space.py ------------------------------------------------------------

def _wall(x, material):
    """A square wall across y and z at `x`, 64 units a side, as two triangles."""
    pos = np.array([[x, 0, 0], [x, 64, 0], [x, 64, 64], [x, 0, 64]], np.float32)
    tri = np.array([[0, 1, 2], [0, 2, 3]], np.int64)
    return pos, tri, material


def test_an_opaque_wall_fills_every_cell_it_crosses():
    header = {"materials": [{"alpha": 0}, {"alpha": 2}, {"alpha": 0, "decal": True}]}
    origin, dims, solid = prepare_space.build(header, [_wall(32.0, 0), _wall(96.0, 1), _wall(128.0, 2)])
    cell = prepare_space.CELL
    column = int((32.0 - origin[0]) // cell)
    plane = solid[column]
    inside = plane[1:-1, 1:-1]
    assert inside[:4, :4].all(), "no gap a smoke could leak through"
    others = np.delete(solid, column, axis=0)
    assert not others.any(), "glass and decals stop nothing"
    assert tuple(dims) == solid.shape


def test_a_map_without_opaque_surfaces_is_refused():
    header = {"materials": [{"alpha": 2}]}
    try:
        prepare_space.build(header, [_wall(0.0, 0)])
    except SystemExit:
        return
    raise AssertionError("built a grid of nothing")


def test_the_grid_file_is_what_the_viewer_reads():
    solid = np.zeros((3, 2, 5), bool)
    solid[1, 0, 2] = True
    solid[2, 1, 4] = True
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.c2space"
        prepare_space.write(path, np.array([-8.0, 4.0, 2.0], np.float32), np.array(solid.shape), solid, cell=16.0)
        data = path.read_bytes()
    assert data[:4] == b"C2SP"
    version, = struct.unpack("<I", data[4:8])
    origin = struct.unpack("<3f", data[8:20])
    cell, = struct.unpack("<f", data[20:24])
    dims = struct.unpack("<3I", data[24:36])
    assert (version, origin, cell, dims) == (1, (-8.0, 4.0, 2.0), 16.0, (3, 2, 5))
    bits = np.unpackbits(np.frombuffer(data[36:], np.uint8), bitorder="little")
    ny, nz = 2, 5
    on = {int(i) for i in np.flatnonzero(bits[:30])}
    assert on == {(1 * ny + 0) * nz + 2, (2 * ny + 1) * nz + 4}, "index (x * ny + y) * nz + z, lowest bit first"
