"""The 3D replay's "as in CS2" look and its HUD: whose eyes it opens in (web/replay3d.py,
replay3d.html), and the places where the page, the audio and the viewer must agree on the
frame report's layout.

What a user would notice: a replay opening in a stranger's eyes instead of their own;
footsteps and the direction of every sound going wrong because a header grew by one float
(it happened once, sfx.js read the players one float off); or the radar drawing grenades
from the wrong floats.
"""

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-replay3d-hud-tests-0123456789")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import unittest  # noqa: E402

try:
    from jinja2 import Environment, FileSystemLoader

    from web import replay3d
except ImportError as exc:
    raise unittest.SkipTest(f"needs the web stack: {exc}")

TEMPLATES = ROOT / "web" / "templates"
PAGE_SOURCE = (TEMPLATES / "replay3d.html").read_text("utf-8")
SFX_SOURCE = (ROOT / "web" / "static" / "replay3d" / "sfx.js").read_text("utf-8")
VIEWER = ROOT / "rust" / "viewer" / "src"


def _render(**extra):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    common = dict(match_id=7, map_name="de_mirage", demo_name="m.dem", has_replay=True,
                  map_asset="maps/de_mirage/world.glb", wasm_size=1, map_size=1)
    return env.get_template("replay3d.html").render(**common, **extra)


def test_the_viewer_sees_through_their_own_eyes_first():
    assert replay3d.pov_sids("765611980001", "765611980002") == ["765611980001", "765611980002"]
    assert replay3d.pov_sids("765611980001", "765611980001") == ["765611980001"], "once is enough"
    assert replay3d.pov_sids("765611980001", None) == ["765611980001"]
    assert replay3d.pov_sids(None, "") == []


def test_the_page_is_told_whose_eyes_to_open_in():
    page = _render(pov_sids=["765611980001", "765611980002"])
    assert 'const POV_SIDS = ["765611980001", "765611980002"];' in page
    assert "const POV_SIDS = [];" in _render(), "an old caller still gets a working page"


def test_sound_reads_the_players_after_the_header_the_page_gives():
    assert "SFX.frame(d, nP, HDR)" in PAGE_SOURCE
    # a number before "+ slot * 10" is a header length written down twice
    assert not re.search(r"d\[\s*\d+\s*\+\s*\w+\s*\*\s*10", SFX_SOURCE)
    assert not re.search(r"=\s*\d+\s*\+\s*i\s*\*\s*10", SFX_SOURCE)
    hdr = int(re.search(r"const HDR = (\d+);", PAGE_SOURCE).group(1))
    assert int(re.search(r"let HDR = (\d+);", SFX_SOURCE).group(1)) == hdr, "the same default"


def test_the_radar_reads_marks_as_the_viewer_writes_them():
    radar = (VIEWER / "radar.rs").read_text("utf-8")
    stride = int(re.search(r"pub const MARK_STRIDE: usize = (\d+);", radar).group(1))
    assert int(re.search(r"const MSTRIDE = (\d+);", PAGE_SOURCE).group(1)) == stride
    # the kinds the page draws
    kinds = dict(re.findall(r"(\w+) = (\d+),", radar.split("pub enum Mark")[1].split("}")[0]))
    assert kinds == {"Flying": "1", "Working": "2", "Bomb": "3"}
    for kind in kinds.values():
        assert f"kind==={kind}" in PAGE_SOURCE or f"kind!=={kind}" in PAGE_SOURCE


def test_the_round_timer_ends_where_the_replay_does():
    extract = (ROOT / "rust" / "extract" / "src" / "lib.rs").read_text("utf-8")
    pad = int(re.search(r"pub const PAD_SECONDS: i64 = (\d+);", extract).group(1))
    assert int(re.search(r"const ROUND_PAD = (\d+);", PAGE_SOURCE).group(1)) == pad


def test_keys_work_on_any_keyboard_layout():
    # e.key is what a key types: on a Russian layout W types "ц" and nothing flies
    handler = PAGE_SOURCE.split("addEventListener('keydown'")[1].split("addEventListener('blur'")[0]
    assert "e.key" not in handler
    assert "e.key ===" not in SFX_SOURCE, "the sound's own key too"
    for code in ("KeyW", "KeyA", "KeyS", "KeyD", "KeyF", "KeyC", "KeyX", "KeyV", "Tab"):
        assert f"'{code}'" in handler or f"{code}:" in PAGE_SOURCE


def test_the_page_imports_every_camera_control_it_calls():
    imported = PAGE_SOURCE.split("import init, {")[1].split("}")[0]
    lib = (VIEWER / "lib.rs").read_text("utf-8")
    for name in ("set_camera_orbit", "set_camera_fly", "set_camera_overview", "set_fly_input", "set_analysis_look"):
        assert name in imported
        assert f"pub fn {name}(" in lib
