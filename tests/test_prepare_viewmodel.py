"""The first-person rigs (tools/prepare_viewmodel.py), on tiny synthetic exports.

What a user would notice: a weapon that keeps the still model in first person because the
clip list forgot it, a gun with holes, a magazine that does not leave the gun, or a weapon
that floats away from the hands.
"""

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:  # the analysis suite must still run without the tool stack
    raise unittest.SkipTest(f"needs the tool stack: {exc}")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import prepare_viewmodel as pv  # noqa: E402
from prepare_map import _read_accessor  # noqa: E402

R = [-0.5, -0.5, -0.5, 0.5]
FOLDER = "rifle/rifle_x"
KINDS = {"draw": f"{FOLDER}/draw_x", "idle": f"{FOLDER}/idle_x", "shoot": f"{FOLDER}/shoot1_x"}
_FLOAT, _USHORT, _UBYTE = 5126, 5123, 5121


class _Gltf:
    def __init__(self):
        self.blob = bytearray()
        self.g = {"accessors": [], "bufferViews": [], "buffers": [{"byteLength": 0}], "nodes": [],
                  "meshes": [], "materials": [{"name": "gun"}], "scenes": [{"nodes": []}], "scene": 0}

    def accessor(self, array, component, kind):
        array = np.asarray(array)
        self.g["bufferViews"].append({"buffer": 0, "byteOffset": len(self.blob), "byteLength": array.nbytes})
        self.blob.extend(array.tobytes())
        self.g["accessors"].append({"bufferView": len(self.g["bufferViews"]) - 1, "componentType": component,
                                    "count": len(array), "type": kind})
        return len(self.g["accessors"]) - 1

    def node(self, name, parent=None, **fields):
        self.g["nodes"].append({"name": name, **fields})
        index = len(self.g["nodes"]) - 1
        if parent is None:
            self.g["scenes"][0]["nodes"].append(index)
        else:
            self.g["nodes"][parent].setdefault("children", []).append(index)
        return index


def _translate(x, y, z):
    """A column-major inverse bind matrix for a bone at (x, y, z) with no rotation."""
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -x, -y, -z, 1]


def _weapon_export():
    """A gun with two parts: the body on `weapon_offset` and a magazine on `clip`, at (1, 2, 3)."""
    e = _Gltf()
    root = e.node("weapons\\models\\x\\x.vmdl_c")
    weapon = e.node("weapon", root, translation=[0.5, 0.5, 0.5], rotation=R)
    offset = e.node("weapon_offset", weapon)
    clip = e.node("clip", offset, translation=[1.0, 2.0, 3.0])
    points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0],             # the body
                       [1, 2, 3], [2, 2, 3], [1, 3, 3]], np.float32)  # the magazine
    count = len(points)
    prim = {"attributes": {
        "POSITION": e.accessor(points, _FLOAT, "VEC3"),
        "NORMAL": e.accessor(np.tile([0.0, 1.0, 0.0], (count, 1)).astype(np.float32), _FLOAT, "VEC3"),
        "TANGENT": e.accessor(np.tile([1.0, 0.0, 0.0, 1.0], (count, 1)).astype(np.float32), _FLOAT, "VEC4"),
        "TEXCOORD_0": e.accessor(np.tile([0.5, 0.5], (count, 1)).astype(np.float32), _FLOAT, "VEC2"),
        "JOINTS_0": e.accessor(np.array([[1, 0, 0, 0]] * 3 + [[2, 0, 0, 0]] * 3, np.uint16), _USHORT, "VEC4"),
        "WEIGHTS_0": e.accessor(np.tile([1.0, 0.0, 0.0, 0.0], (count, 1)).astype(np.float32), _FLOAT, "VEC4"),
    }, "indices": e.accessor(np.array([0, 1, 2, 3, 4, 5], np.uint16), _USHORT, "SCALAR"), "material": 0}
    e.g["meshes"].append({"primitives": [prim]})
    ibm = e.accessor(np.array([_translate(0.5, 0.5, 0.5), _translate(0.5, 0.5, 0.5), _translate(1.0, 2.0, 3.0)],
                              np.float32).reshape(-1, 16), _FLOAT, "MAT4")
    e.g["skins"] = [{"joints": [weapon, offset, clip], "inverseBindMatrices": ibm}]
    mesh_node = e.node("weapons\\models\\x\\x.vmdl_c.body_hd", mesh=0, skin=0)
    return e, [(e.g["nodes"][mesh_node], prim)]


def _write_clip(clips_dir, name, moves_clip=True):
    """A clip's export on disk: both skeletons, as scene roots, and tracks for their bones."""
    e = _Gltf()
    arm_root = e.node(pv._ARMS_SKELETON)
    motion = e.node("root_motion", arm_root, rotation=R)
    wpn = e.node("wpn", motion, translation=[0.4, 0.0, 0.0])
    e.node("wpnTip", wpn, translation=[0.1, 0.0, 0.0])
    lower = e.node("arm_lower_L", motion, translation=[0.2, 0.1, 0.0])
    e.node("hand_L", lower, translation=[0.3, 0.0, 0.0])
    gun_root = e.node(f"{pv._WEAPON_SKELETONS}x.vnmskel")
    weapon = e.node("weapon", gun_root, rotation=R)
    offset = e.node("weapon_offset", weapon)
    clip_bone = e.node("clip", offset, translation=[1.0, 2.0, 3.0])

    times = e.accessor(np.array([0.0, 0.5, 1.0], np.float32), _FLOAT, "SCALAR")
    one_time = e.accessor(np.array([0.0], np.float32), _FLOAT, "SCALAR")
    samplers, channels = [], []

    def track(node, path, values):
        values = np.asarray(values, np.float32)
        kind = "VEC4" if path == "rotation" else "VEC3"
        samplers.append({"input": times if len(values) == 3 else one_time, "output": e.accessor(values, _FLOAT, kind)})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": path}})

    track(motion, "rotation", [R])
    track(motion, "translation", [[0.0, 0.0, 0.0]])
    track(motion, "scale", [[1.0, 1.0, 1.0]])
    track(wpn, "translation", [[0.4, 0.0, 0.0], [0.3, 0.0, 0.0], [0.4, 0.0, 0.0]])
    track(wpn, "rotation", [[0, 0, 0, 1]])
    track(lower, "rotation", [[0, 0, 0, 1]])
    track(lower, "translation", [[0.2, 0.1, 0.0]])
    track(weapon, "rotation", [R])
    track(weapon, "translation", [[0.0, 0.0, 0.0]])
    track(offset, "rotation", [[0, 0, 0, 1]])
    track(offset, "translation", [[0.0, 0.0, 0.0]])
    moved = [[1.0, 2.0, 3.0], [1.0, 2.0, 1.0], [1.0, 2.0, 3.0]] if moves_clip else [[1.0, 2.0, 3.0]]
    track(clip_bone, "translation", moved)
    track(clip_bone, "rotation", [[0, 0, 0, 1]])
    e.g["animations"] = [{"channels": channels, "samplers": samplers}]

    path = pv.clip_file(clips_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".bin").write_bytes(bytes(e.blob))
    e.g["buffers"][0]["uri"] = path.with_suffix(".bin").name
    e.g["buffers"][0]["byteLength"] = len(e.blob)
    path.write_text(json.dumps(e.g), encoding="utf-8")


def _write_clips(clips_dir):
    for name in KINDS.values():
        _write_clip(clips_dir, name)


def _build(clips_dir, kinds=None):
    e, primitives = _weapon_export()
    return pv.build_viewmodel_weapon(e.g, bytes(e.blob), primitives, (None, {0}), clips_dir, kinds or KINDS)


def _nodes(result):
    return {n["name"]: n for n in result["nodes"]}


def _path_to(result, name):
    parents = {c: i for i, n in enumerate(result["nodes"]) for c in n.get("children", [])}
    index = next(i for i, n in enumerate(result["nodes"]) if n["name"] == name)
    path = [name]
    while index in parents:
        index = parents[index]
        path.append(result["nodes"][index]["name"])
    return path[::-1]


class ClipListTest(unittest.TestCase):
    def test_every_weapon_the_game_can_show_has_clips(self):
        models = REPO / "rust" / "viewer" / "src" / "weapon_models.txt"
        files = {line.split()[1] for line in models.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")}
        self.assertEqual(set(pv.read_clip_list()), files)

    def test_a_kind_the_viewer_does_not_know_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.txt"
            path.write_text("ak47 rifle/rifle_ak draw=a idle=b inspect=c\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                pv.read_clip_list(path)

    def test_a_weapon_with_no_idle_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.txt"
            path.write_text("ak47 rifle/rifle_ak draw=a\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                pv.read_clip_list(path)


class WeaponRigTest(unittest.TestCase):
    def test_every_triangle_of_the_gun_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, _, stats = _build(tmp)
        drawn = sum(result["accessors"][m["primitives"][0]["indices"]]["count"] for m in result["meshes"])
        self.assertEqual(drawn, 6)
        self.assertEqual(stats["parts"], {"clip": 3, "weapon_offset": 3})

    def test_a_part_is_stored_in_its_own_bones_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, blob, _ = _build(tmp)
        mesh = result["meshes"][_nodes(result)["clip"]["mesh"]]
        at = _read_accessor(result, blob, mesh["primitives"][0]["attributes"]["POSITION"])
        np.testing.assert_allclose(at, [[0, 0, 0], [1, 0, 0], [0, 1, 0]], atol=1e-6)

    def test_the_gun_hangs_from_the_weapon_bone_at_its_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, _, _ = _build(tmp)
        self.assertEqual(_path_to(result, "clip"),
                         ["viewmodel", "root_motion", "wpn", "weapon", "weapon_offset", "clip"])
        weapon = _nodes(result)["weapon"]
        self.assertNotIn("translation", weapon)
        self.assertNotIn("rotation", weapon)

    def test_the_guns_root_gets_no_track_or_the_frame_swap_would_come_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, _, _ = _build(tmp)
        index = next(i for i, n in enumerate(result["nodes"]) if n["name"] == "weapon")
        for animation in result["animations"]:
            self.assertFalse([c for c in animation["channels"] if c["target"]["node"] == index])

    def test_the_magazine_moves_and_clips_carry_no_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, _, _ = _build(tmp)
        index = next(i for i, n in enumerate(result["nodes"]) if n["name"] == "clip")
        shoot = next(a for a in result["animations"] if a["name"] == "shoot")
        moving = [c for c in shoot["channels"] if c["target"]["node"] == index and c["target"]["path"] == "translation"]
        self.assertEqual(len(moving), 1)
        for animation in result["animations"]:
            self.assertFalse([c for c in animation["channels"] if c["target"]["path"] == "scale"])

    def test_bones_that_stand_still_keep_a_track_or_the_last_clip_would_stick(self):
        # Bevy leaves a bone with no track where the previous clip put it.
        with tempfile.TemporaryDirectory() as tmp:
            _write_clip(tmp, KINDS["draw"])
            _write_clip(tmp, KINDS["idle"], moves_clip=False)
            _write_clip(tmp, KINDS["shoot"])
            result, _, _ = _build(tmp)
        index = next(i for i, n in enumerate(result["nodes"]) if n["name"] == "clip")
        idle = next(a for a in result["animations"] if a["name"] == "idle")
        self.assertTrue([c for c in idle["channels"] if c["target"]["node"] == index])

    def test_a_clip_with_nothing_moving_still_has_a_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            still = pv.clip_file(tmp, KINDS["idle"])
            g = json.loads(still.read_text(encoding="utf-8"))
            for sampler in g["animations"][0]["samplers"]:
                sampler["input"] = 1  # the one-key times
            for accessor_index, count in ((a["output"], 1) for a in g["animations"][0]["samplers"]):
                g["accessors"][accessor_index]["count"] = count
            still.write_text(json.dumps(g), encoding="utf-8")
            result, blob, _ = _build(tmp)
        idle = next(a for a in result["animations"] if a["name"] == "idle")
        ends = [float(_read_accessor(result, blob, s["input"]).max()) for s in idle["samplers"]]
        self.assertGreater(max(ends), 0.0)

    def test_the_animations_are_the_ones_the_viewer_asks_for_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, _, _ = _build(tmp)
        self.assertEqual({a["name"] for a in result["animations"]}, {"draw", "idle", "shoot"})

    def test_a_missing_clip_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clip(tmp, KINDS["idle"])
            with self.assertRaises(SystemExit):
                _build(tmp)

    def test_a_weapon_bone_named_like_an_arm_bone_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            e, primitives = _weapon_export()
            next(n for n in e.g["nodes"] if n["name"] == "clip")["name"] = "hand_L"
            with self.assertRaises(SystemExit):
                pv.build_viewmodel_weapon(e.g, bytes(e.blob), primitives, (None, {0}), tmp, KINDS)

    def test_the_parts_land_on_the_same_atlas_as_the_third_person_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            e, primitives = _weapon_export()
            atlas = ({0: (0.5, 0.0, 0.5)}, {0})
            result, blob, _ = pv.build_viewmodel_weapon(e.g, bytes(e.blob), primitives, atlas, tmp, KINDS)
        mesh = result["meshes"][_nodes(result)["clip"]["mesh"]]
        uv = _read_accessor(result, blob, mesh["primitives"][0]["attributes"]["TEXCOORD_0"])
        np.testing.assert_allclose(uv, [[0.75, 0.25]] * 3, atol=1e-6)

    def test_the_written_file_is_a_valid_glb(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clips(tmp)
            result, blob, _ = _build(tmp)
            out = Path(tmp) / "w.glb"
            pv.write_output(out, result, blob)
            data = out.read_bytes()
        self.assertEqual(data[:4], b"glTF")
        self.assertEqual(struct.unpack("<I", data[8:12])[0], len(data))


class SkeletonTest(unittest.TestCase):
    def test_the_bones_no_clip_holds_are_added_for_the_forearm(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clip(tmp, KINDS["idle"])
            clip, _ = pv._load_with_blob(pv.clip_file(tmp, KINDS["idle"]))
        bones = {b[0]: b[1] for b in pv.arm_bones(clip)}
        self.assertEqual(bones["arm_lower_L_TWIST"], "arm_lower_L")
        self.assertEqual(bones["root_motion"], None)
        self.assertNotIn("arm_lower_R_TWIST", bones)

    def test_a_parent_comes_before_its_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_clip(tmp, KINDS["idle"])
            clip, _ = pv._load_with_blob(pv.clip_file(tmp, KINDS["idle"]))
        seen = set()
        for name, parent, _, _ in pv.arm_bones(clip):
            self.assertTrue(parent is None or parent in seen, name)
            seen.add(name)


if __name__ == "__main__":
    unittest.main()
