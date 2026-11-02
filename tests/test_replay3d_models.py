"""Which player model the 3D replay page asks for (web/replay3d.py, replay3d.html).

What a user would notice: a fresh clone without the never-committed CS2 models showing
no players at all, or REPLAY3D_CS2_MODELS=0 not actually turning those models off.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-replay3d-model-tests-0123456789")

try:
    from jinja2 import Environment, FileSystemLoader

    from web import replay3d
except ImportError as exc:  # the analysis suite must still run without the web stack
    raise unittest.SkipTest(f"needs the web stack: {exc}")

TEMPLATES = Path(__file__).resolve().parent.parent / "web" / "templates"


class _Models:
    """replay3d looking at a temporary models folder holding `files`."""

    def __init__(self, files, flag=None):
        self.files, self.flag = files, flag

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        for name in self.files:
            (Path(self._tmp.name) / name).write_bytes(b"glb")
        self._dir = replay3d.CS2_MODELS_DIR
        replay3d.CS2_MODELS_DIR = Path(self._tmp.name)
        self._env = os.environ.pop("REPLAY3D_CS2_MODELS", None)
        if self.flag is not None:
            os.environ["REPLAY3D_CS2_MODELS"] = self.flag
        return self

    def __exit__(self, *exc):
        replay3d.CS2_MODELS_DIR = self._dir
        os.environ.pop("REPLAY3D_CS2_MODELS", None)
        if self._env is not None:
            os.environ["REPLAY3D_CS2_MODELS"] = self._env
        self._tmp.cleanup()


def _render(cs2_models):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    return env.get_template("replay3d.html").render(
        match_id=7, map_name="de_mirage", demo_name="m.dem", has_replay=True,
        map_asset="maps/de_mirage/world.glb", cs2_models=cs2_models, wasm_size=1, map_size=1)


def test_models_are_used_when_both_files_are_there():
    with _Models(["t.glb", "ct.glb"]):
        assert replay3d.cs2_models_enabled()


def test_a_fresh_clone_without_the_files_gets_the_soldier():
    with _Models([]):
        assert not replay3d.cs2_models_enabled()


def test_one_missing_file_gets_the_soldier():
    # a half-built folder would leave one whole side without bodies
    with _Models(["t.glb"]):
        assert not replay3d.cs2_models_enabled()


def test_the_flag_turns_the_models_off_without_touching_the_files():
    with _Models(["t.glb", "ct.glb"], flag="0"):
        assert not replay3d.cs2_models_enabled()


def test_any_other_flag_value_leaves_them_on():
    with _Models(["t.glb", "ct.glb"], flag="1"):
        assert replay3d.cs2_models_enabled()


def test_the_page_passes_the_choice_to_the_viewer():
    assert "const CS2_MODELS = true;" in _render(True)
    assert "const CS2_MODELS = false;" in _render(False)
    assert "run('#canvas', '/static/replay3d/assets', CS2_MODELS)" in _render(True)


def test_the_page_only_warm_fetches_models_it_will_use():
    # the fetch is behind the flag, so a clone without the files logs no 404s
    assert "CS2_MODELS\n    ? Promise.all" in _render(False)


def test_wasm_cache_buster_is_the_same_everywhere_on_the_page():
    import re

    tokens = set(re.findall(r"viewer(?:_bg\.wasm|\.js)\?v=(\d+)", _render(True)))
    assert len(tokens) == 1, f"a stale wasm would break run()'s new signature: {tokens}"
