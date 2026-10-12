"""Tests for the terrain of the 3D view: the heightmap reader
(blitzreview/dava.py), the shipped terrain files (assets/terrain, written by
tools/wotb_extract_assets.py) and gamedata.terrain_heights.

The heightmaps are stored tile by tile, and a wrong guess about that -- or
about which way the rows run -- still yields a plausible-looking landscape,
just one that has nothing to do with the map. The check that cannot be
fooled is the tanks: a tank stands on the ground, so its height in the
replay must be the terrain's height under it. That holds to a few
centimetres on every fixture and fails by metres for any other layout.
"""

import importlib.util
import json
import struct
from pathlib import Path

from blitzreview import arena, dava, gamedata
from blitzreview.container import open_replay
from blitzreview.packets import positions, read_stream

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
ROOT = Path(__file__).resolve().parent.parent
CLIENT_MAPS = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz\Data\3d\Maps")


def _tool():
    spec = importlib.util.spec_from_file_location("wotb_extract_assets",
                                                  ROOT / "tools" / "wotb_extract_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiled(size, tile, value):
    """A heightmap file whose untiled point (row j, column i) is value(j, i)."""
    out = [struct.pack("<II", size, tile)]
    for block_row in range(size // tile):
        for block_col in range(size // tile):
            for y in range(tile):
                row = block_row * tile + y
                out.append(struct.pack(f"<{tile}H", *(value(row, block_col * tile + x)
                                                      for x in range(tile))))
    return b"".join(out)


def test_a_tiled_heightmap_reads_back_row_by_row():
    size, values = dava.read_heightmap(_tiled(8, 4, lambda j, i: 100 * j + i))
    assert size == 8 and values == [100 * j + i for j in range(8) for i in range(8)]


def test_broken_heightmaps_fail_loudly():
    good = _tiled(8, 4, lambda j, i: 0)
    for bad in (good[:5], good[:-2], good + b"\0\0", struct.pack("<II", 8, 3) + good[8:]):
        try:
            dava.read_heightmap(bad)
        except dava.DavaError:
            continue
        raise AssertionError("no DavaError")


def test_resampling_keeps_the_edges_and_straight_slopes_straight():
    tool = _tool()
    ramp = [float(i + 2 * j) for j in range(5) for i in range(5)]
    out = tool.resample(5, ramp, 9)
    assert out[0] == 0 and out[8] == 4 and out[-1] == 12      # all four corners kept
    assert all(abs(out[j * 9 + i] - (i / 2 + j)) < 1e-9 for j in range(9) for i in range(9))


def _height_at(n, heights, world, x, z):
    x0, z0, x1, z1 = world
    u, v = (x - x0) / (x1 - x0) * (n - 1), (z - z0) / (z1 - z0) * (n - 1)
    i, j = min(max(int(u), 0), n - 2), min(max(int(v), 0), n - 2)
    fu, fv = u - i, v - j
    a = heights[j * n + i] * (1 - fu) + heights[j * n + i + 1] * fu
    b = heights[(j + 1) * n + i] * (1 - fu) + heights[(j + 1) * n + i + 1] * fu
    return a * (1 - fv) + b * fv


def test_every_tank_stands_on_the_shipped_terrain():
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        files = open_replay(path)
        key = files.meta["mapName"]
        n, heights = gamedata.terrain_heights(key)
        world = gamedata.minimap(key)["world"]
        stream = read_stream(files.data)
        roster = arena.parse(stream)["roster"]
        gaps = sorted(abs(p[2] - _height_at(n, heights, world, p[1], p[3]))
                      for eid, track in positions(stream).items() if eid in roster
                      for p in track[::10])
        assert len(gaps) > 500, path.name
        # the rest stand on bridges and rocks the terrain does not have
        assert gaps[len(gaps) // 2] < 0.3 and gaps[len(gaps) * 9 // 10] < 0.5, (path.name, gaps[::200])


def test_every_map_with_a_minimap_has_terrain():
    maps = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]
    for key in maps:
        if gamedata.minimap(key) is None:
            continue
        n, heights = gamedata.terrain_heights(key)
        assert n == 160 and len(heights) == n * n, key
        assert 0 <= min(heights) < max(heights) <= 200, (key, min(heights), max(heights))
    assert gamedata.terrain("no_such_map") is None and gamedata.terrain_heights(None) is None


def test_the_reader_agrees_with_the_shipped_terrain():
    path = CLIENT_MAPS / "25_canyon_ca" / "landscape" / "canyon_height.heightmap.dvpl"
    if not path.is_file():
        import unittest
        raise unittest.SkipTest("World of Tanks Blitz client not installed here")
    from blitzreview import dvpl
    size, values = dava.read_heightmap(dvpl.read(path))
    tool = _tool()
    expected = [v / 65535 * 100 for v in tool.resample(size, values, 160)]  # canyon's z: 0..100 m
    n, heights = gamedata.terrain_heights("canyon")
    assert max(abs(a - b) for a, b in zip(expected, heights)) <= 0.051
