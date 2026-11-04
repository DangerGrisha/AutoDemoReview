"""The 3D replay's settings panel (web/static/replay3d/settings.js) and crosshairs
(crosshair.js): what is kept in the browser, the limits the viewer is given, and the share
codes CS2 writes into demos.

What a user would notice: a crosshair code from the game turning into the wrong crosshair, or
an accepted code that is not one; a page that breaks because something old or broken sits in
localStorage; a slider that offers a value the viewer then quietly cuts off.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "web" / "static" / "replay3d"
SETTINGS_SOURCE = (STATIC / "settings.js").read_text("utf-8")
PAGE_SOURCE = (ROOT / "web" / "templates" / "replay3d.html").read_text("utf-8")
VIEWER = ROOT / "rust" / "viewer" / "src"
CODES = ROOT / "tests" / "data" / "crosshair_codes.json"
NODE = shutil.which("node")


def _node(script: str) -> dict:
    """Run an ES module that imports the page's modules by URL and prints one JSON line."""
    if not NODE:
        raise unittest.SkipTest("needs node to run the page's modules")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "check.mjs"
        f.write_text(script, encoding="utf-8")
        out = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _url(path: Path) -> str:
    return path.resolve().as_uri()


def _range(key: str):
    """min, max, step, default of one `range('key', ...)` in settings.js."""
    m = re.search(rf"range\('{key}', '[^']+', (-?[\d.]+), (-?[\d.]+), ([\d.]+), (-?[\d.]+)", SETTINGS_SOURCE)
    assert m, key
    return tuple(float(v) for v in m.groups())


def _rust_pair(src: str, name: str):
    m = re.search(rf"pub const {name}: \(f32, f32\) = \((-?[\d.]+), (-?[\d.]+)\);", src)
    assert m, name
    return float(m.group(1)), float(m.group(2))


def _rust_vec3(src: str, name: str):
    m = re.search(rf"const {name}: Vec3 = Vec3::new\((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\);", src)
    assert m, name
    return tuple(float(v) for v in m.groups())


def test_crosshair_codes_decode_as_the_reference_says():
    got = _node(f"""
import {{ readFileSync }} from 'node:fs';
const x = await import('{_url(STATIC / "crosshair.js")}');
const data = JSON.parse(readFileSync({json.dumps(str(CODES))}, 'utf8'));
const wrong = [];
for (const {{ code, want }} of data.samples) {{
  const got = x.decodeShareCode(code);
  if (!got) {{ wrong.push(code + ': not read'); continue; }}
  for (const k of Object.keys(got)) if (JSON.stringify(got[k]) !== JSON.stringify(want[k])) wrong.push(code + ' ' + k);
}}
const accepted = data.invalid.filter(c => x.decodeShareCode(c));
const demo = data.demo.map(c => [x.decodeShareCode(c), x.settingsFromCode(c)]);
console.log(JSON.stringify({{ n: data.samples.length, wrong, accepted, demo,
  formats: [...new Set(data.samples.map(s => s.want.format))].sort() }}));
""")
    assert got["n"] >= 18 and not got["wrong"], got["wrong"]
    assert got["formats"] == ["cs2-v1", "legacy-v1", "legacy-v3", "legacy-v4"], "every layout CS2 has written"
    assert got["accepted"] == [], "bad checksums, unknown versions and a zero screen height are refused"
    for decoded, settings in got["demo"]:
        assert decoded["format"] == "legacy-v1", "what the three local demos hold"
        assert settings and 0 <= settings["length"] <= 60 and settings["thickness"] >= 1


def test_a_code_becomes_the_same_crosshair_at_any_window_height():
    got = _node(f"""
const x = await import('{_url(STATIC / "crosshair.js")}');
// a pixel code made on a 720-line screen, drawn on 1440 lines, is twice as big: CS2's rule
const c = {{ format: 'legacy-v4', style: 4, centerDotEnabled: false, tStyleEnabled: true, red: 1, green: 2,
  blue: 3, alpha: 255, gap: 3, length: 8, thickness: 2, screenHeight: 720, outlineMode: 0 }};
const xh = x.fromDecoded(c);
const s = x.settingsFromCode('CSGO-6AcC5-PcSww-cOozD-6ZRd6-YBr7C');
console.log(JSON.stringify({{ ref: xh.ref, length: xh.length, t: xh.t, shape: xh.shape,
  mine: x.fromSettings({{ shape: 'cross', color: 1, r: 0, g: 0, b: 0, alpha: 99, length: 7, thickness: 2,
    gap: 4, dot: false, t: false, outline: 1, outlineAlpha: 160 }}), s }}));
""")
    assert got["ref"] == 720 and got["length"] == 8 and got["t"] and got["shape"] == "cross"
    assert got["mine"]["rgba"] == [50, 250, 50, 99], "a colour preset is CS2's own green"
    assert got["mine"]["ref"] == 1080, "the page's own sizes are for 1080 lines"


def test_settings_survive_whatever_the_browser_kept():
    got = _node(f"""
let stored = null, throws = false;
globalThis.localStorage = {{
  getItem() {{ if (throws) throw new Error('blocked'); return stored; }},
  setItem(k, v) {{ if (throws) throw new Error('blocked'); stored = v; }},
}};
const S = await import('{_url(STATIC / "settings.js")}');
const out = {{}};
stored = '{{not json'; out.broken = S.load();
stored = JSON.stringify({{ radar: {{ scale: 99, rotate: 'yes', utility: false }}, viewmodel: [1, 2], crosshair: {{ shape: 'star' }} }});
out.odd = S.load();
throws = true; out.blocked = S.load(); S.save(out.blocked);
throws = false; stored = null;
const v = S.load(); S.assign(v, 'viewmodel', {{ fov: 30, nonsense: 1 }}); S.save(v);
out.saved = JSON.parse(stored);
out.defaults = Object.fromEntries(Object.keys(S.SCHEMA).map(t => [t, S.defaults(t)]));
console.log(JSON.stringify(out));
""")
    d = got["defaults"]
    assert got["broken"] == d and got["blocked"] == d, "broken JSON or blocked storage: the defaults"
    odd = got["odd"]
    assert odd["radar"]["scale"] == 1, "a number past the range is brought into it"
    assert odd["radar"]["rotate"] is True, "a value of the wrong kind gives the default"
    assert odd["radar"]["utility"] is False, "a good value is kept"
    assert odd["viewmodel"] == d["viewmodel"] and odd["crosshair"]["shape"] == "cross"
    assert got["saved"]["viewmodel"]["fov"] == 54 and "nonsense" not in got["saved"]["viewmodel"]


def test_the_sliders_offer_what_the_viewer_takes():
    vm = (VIEWER / "viewmodel.rs").read_text("utf-8")
    orbit = (VIEWER / "orbit.rs").read_text("utf-8")
    fov = _range("fov")
    assert fov[:2] == _rust_pair(vm, "VIEWMODEL_FOV_RANGE")
    lo, hi = _rust_vec3(vm, "VIEWMODEL_OFFSET_MIN"), _rust_vec3(vm, "VIEWMODEL_OFFSET_MAX")
    default = _rust_vec3(vm, "VIEWMODEL_OFFSET")
    for i, key in enumerate("xyz"):
        r = _range(key)
        assert r[:2] == (lo[i], hi[i]), key
        assert r[3] == default[i], f"{key}: the slider starts where the arms are without settings"
    assert fov[3] == float(re.search(r"const VIEWMODEL_FOV_4_3: f32 = ([\d.]+);", vm).group(1))
    assert _range("flySpeed")[:2] == _rust_pair(orbit, "SPEED_RANGE")
    assert _range("sensitivity")[:2] == _rust_pair(orbit, "SENSITIVITY_RANGE")


def test_the_defaults_change_nothing_on_screen():
    # the radar's default zoom is the 2x the page always used
    whole = float(re.search(r"const RADAR_SCALE_WHOLE = ([\d.]+);", PAGE_SOURCE).group(1))
    assert _range("scale")[3] / whole == 2
    assert _range("dim")[3] == 0.32 and _range("hudScale")[3] == 1 and _range("iconScale")[3] == 1
    assert _range("autoNextDelay")[3] == 2.5, "the move to a teammate after a death"
    assert _range("renderScale")[3] == 1


def test_the_page_calls_only_what_the_viewer_exports():
    imported = PAGE_SOURCE.split("import init, {")[1].split("}")[0]
    lib = (VIEWER / "lib.rs").read_text("utf-8")
    for name in ("set_camera_feel", "set_viewmodel", "set_player_shadows"):
        assert name in imported, name
        assert f"pub fn {name}(" in lib, name
    for module in ("settings.js", "crosshair.js"):
        assert f"/static/replay3d/{module}?v=" in PAGE_SOURCE
        build = (ROOT / "tools" / "build_replay3d.ps1").read_text("utf-8")
        assert f"replay3d\\{module}" in build, f"{module} is compressed by the build"
