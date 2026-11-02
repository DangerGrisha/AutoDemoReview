"""tools/prepare_character.py, on a tiny synthetic Source2Viewer-shaped export."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # asset tools only; the app itself needs neither
    raise unittest.SkipTest(f"needs requirements-tools.txt: {exc}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import prepare_character as pc  # noqa: E402
from prepare_map import is_hidden_material  # noqa: E402


class _Fixture:
    """Builds the json + bin of a small export: character root -> root_motion ->
    (pelvis -> (spine_0 to spine_3, jiggle_pouch), wpnPivot), two third-person meshes, a
    first-person mesh."""

    def __init__(self):
        self.bin = bytearray()
        self.accessors = []
        self.views = []

    def add(self, arr, component, kind):
        off = len(self.bin)
        self.bin.extend(arr.tobytes())
        self.views.append({"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes})
        self.accessors.append({"bufferView": len(self.views) - 1, "componentType": component,
                               "count": len(arr), "type": kind})
        return len(self.accessors) - 1

    def build(self, image_dir):
        f4, u2 = np.float32, np.uint16
        # a vertex pool of 6 where the body only uses the first 3
        pos = self.add(np.array([[0, 0, 0], [1, 0, 0], [0, 1.8, 0], [9, 9, 9], [9, 9, 8], [9, 8, 9]], f4), pc._FLOAT, "VEC3")
        nrm = self.add(np.tile(np.array([[0, 0, 1]], f4), (6, 1)), pc._FLOAT, "VEC3")
        tan = self.add(np.tile(np.array([[1, 0, 0, 1]], f4), (6, 1)), pc._FLOAT, "VEC4")
        uv0 = self.add(np.array([[0, 0], [1, 0], [0, 1], [0.5, 0.5], [0.5, 0.5], [0.5, 0.5]], f4), pc._FLOAT, "VEC2")
        uv1 = self.add(np.ones((6, 2), f4), pc._FLOAT, "VEC2")
        # joints index the skin: 0 root_motion, 1 pelvis, 2 spine, 3 jiggle_pouch
        joints = self.add(np.array([[1, 3, 0, 0], [2, 0, 0, 0], [3, 2, 0, 0], [0] * 4, [0] * 4, [0] * 4], u2), pc._USHORT, "VEC4")
        weights = self.add(np.array([[0.5, 0.5, 0, 0], [1, 0, 0, 0], [0.75, 0.25, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]], f4), pc._FLOAT, "VEC4")
        idx = self.add(np.array([0, 1, 2], u2), pc._USHORT, "SCALAR")
        idx_fp = self.add(np.array([3, 4, 5], u2), pc._USHORT, "SCALAR")
        idx_gloves = self.add(np.array([1, 2, 1], u2), pc._USHORT, "SCALAR")
        ibm = self.add(np.tile(np.eye(4, dtype=f4).reshape(1, 16), (4, 1)), pc._FLOAT, "MAT4")

        def key(values, kind, times=None):
            values = np.array(values, f4)
            t = np.array(times if times is not None else np.arange(len(values)) / 30.0, f4)
            return self.add(t, pc._FLOAT, "SCALAR"), self.add(values, pc._FLOAT, kind)

        def clip(name, spine_rot, jiggle=True):
            samplers, channels = [], []

            def track(node, path, sampler):
                samplers.append({"input": sampler[0], "output": sampler[1]})
                channels.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": path}})

            q = [0, 0, 0, 1]
            # root_motion: the same rotation in every clip, so it belongs in the rest pose
            track(1, "rotation", key([[-0.5, -0.5, -0.5, 0.5]], "VEC4", [0.0]))
            track(1, "scale", key([[1, 1, 1]], "VEC3", [0.0]))
            # pelvis holds a different value per clip
            track(2, "rotation", key([spine_rot[0]], "VEC4", [0.0]))
            track(3, "rotation", key(spine_rot, "VEC4"))
            track(12, "translation", key([[0, 1.5, 0.3]], "VEC3", [0.0]))
            track(12, "rotation", key([q], "VEC4", [0.0]))
            if jiggle:
                track(4, "rotation", key([q, q], "VEC4"))
            return {"name": name, "samplers": samplers, "channels": channels}

        move = [[0, 0, 0, 1], [0, 0.1, 0, 0.995], [0, 0.2, 0, 0.98], [0, 0.3, 0, 0.954], [0, 0.4, 0, 0.917]]
        still = [[0, 0, 0.1, 0.995]]
        Image.new("RGB", (64, 32), (200, 10, 10)).save(Path(image_dir) / "col.png")
        Image.new("RGB", (64, 32), (128, 128, 255)).save(Path(image_dir) / "nrm.png")
        Image.new("RGB", (8, 8)).save(Path(image_dir) / "orm.png")

        def body_attrs():
            return {"POSITION": pos, "NORMAL": nrm, "TANGENT": tan, "TEXCOORD_0": uv0,
                    "JOINTS_0": joints, "WEIGHTS_0": weights}

        g = {
            "scene": 0,
            "scenes": [{"nodes": [0, 5, 6, 7, 8]}],
            "nodes": [
                {"name": "agents\\models\\x\\x.vmdl_c", "children": [1]},
                {"name": "root_motion", "children": [2, 12]},
                {"name": "pelvis", "translation": [0, 1, 0], "children": [3, 4]},
                {"name": "spine_0", "translation": [0, 0.2, 0], "children": [9]},
                {"name": "jiggle_pouch", "translation": [0, 0.1, 0]},
                {"name": "x.vmdl_c.thirdperson_body", "mesh": 0, "skin": 0},
                {"name": "x.vmdl_c.firstperson_arms", "mesh": 1, "skin": 0},
                {"name": "weapon.vnmskel"},
                {"name": "x.vmdl_c.thirdperson_gloves", "mesh": 2, "skin": 0},
                {"name": "spine_1", "translation": [0, 0.1, 0], "children": [10]},
                {"name": "spine_2", "translation": [0, 0.1, 0], "children": [11]},
                {"name": "spine_3", "translation": [0, 0.1, 0]},
                {"name": "wpnPivot", "translation": [0, 1.5, 0.3]},
            ],
            "meshes": [
                {"primitives": [{"attributes": {**body_attrs(), "TEXCOORD_1": uv1}, "indices": idx, "material": 0}]},
                {"primitives": [{"attributes": {"POSITION": pos, "JOINTS_0": joints, "WEIGHTS_0": weights},
                                 "indices": idx_fp, "material": 1}]},
                {"primitives": [{"attributes": body_attrs(), "indices": idx_gloves, "material": 2}]},
            ],
            "skins": [{"joints": [1, 2, 3, 4], "inverseBindMatrices": ibm}],
            "materials": [
                {"name": "body", "normalTexture": {"index": 1},
                 "occlusionTexture": {"index": 2, "texCoord": 1},
                 "pbrMetallicRoughness": {"baseColorTexture": {"index": 0},
                                          "metallicRoughnessTexture": {"index": 2}},
                 "extras": {"vmat": "x"}},
                {"name": "arms"},
                {"name": "gloves"},
            ],
            "textures": [{"source": 0}, {"source": 1}, {"source": 2}],
            "images": [{"uri": "col.png"}, {"uri": "nrm.png"}, {"uri": "orm.png"}],
            "animations": [
                clip("animation/anims/world/rifle/_default_rifle/run_n_rifle", move),
                clip("animation/anims/world/rifle/_default_rifle/idle_rifle", still),
                # same base name, wrong skeleton: must never be picked up
                clip("animation/anims/viewmodel/rifle/_default_rifle/idle_rifle", move),
            ],
            "accessors": self.accessors,
            "bufferViews": self.views,
            "buffers": [{"byteLength": len(self.bin)}],
        }
        return g, bytes(self.bin)


def _build(clips=("idle_rifle", "run_n_rifle"), **kw):
    fixture = _Fixture()
    with tempfile.TemporaryDirectory() as d:
        g, blob = fixture.build(d)
        return pc.build_character(g, blob, d, list(clips), **kw)


def _read(result, blob, ai):
    # same layout as the input, so the tool's own reader can read its output
    return pc._read_accessor(result, bytes(blob), ai)


def test_clip_order_follows_the_list_and_ignores_other_skeletons():
    result, _, stats = _build()
    assert [a["name"] for a in result["animations"]] == ["idle_rifle", "run_n_rifle"]
    assert stats["clips"] == 2


def test_clip_order_can_be_any_list_order():
    result, _, _ = _build(clips=("run_n_rifle", "idle_rifle"))
    assert [a["name"] for a in result["animations"]] == ["run_n_rifle", "idle_rifle"]


def test_missing_clip_is_an_error():
    try:
        _build(clips=("idle_rifle", "walk_n_rifle"))
    except SystemExit as e:
        assert "walk_n_rifle" in str(e)
    else:
        raise AssertionError("a missing clip must stop the build")


def test_only_third_person_meshes_survive():
    result, _, stats = _build()
    assert stats["materials"] == 2, "body and gloves, not the first-person arms"
    assert "weapon.vnmskel" not in [n["name"] for n in result["nodes"]]


def test_the_whole_character_is_one_primitive_with_one_material():
    result, _, _ = _build()
    assert len(result["meshes"]) == 1
    assert len(result["meshes"][0]["primitives"]) == 1
    assert len(result["materials"]) == 1


def test_primitives_are_trimmed_to_used_vertices_and_merged():
    result, blob, stats = _build()
    prim = result["meshes"][0]["primitives"][0]
    assert stats["verts"] == 5, "3 of the body's pool of 6, 2 of the gloves'"
    assert stats["tris"] == 2
    assert _read(result, blob, prim["attributes"]["POSITION"]).shape == (5, 3)
    # the gloves' indices continue after the body's vertices
    assert _read(result, blob, prim["indices"]).tolist() == [0, 1, 2, 3, 4, 3]
    assert np.allclose(result["accessors"][prim["attributes"]["POSITION"]]["max"], [1.0, 1.8, 0.0])
    assert "TEXCOORD_1" not in prim["attributes"]


def test_each_material_keeps_to_its_own_atlas_slot():
    result, blob, _ = _build()
    prim = result["meshes"][0]["primitives"][0]
    uv = _read(result, blob, prim["attributes"]["TEXCOORD_0"])
    # two materials: the first quadrant and the second, across the top
    body, gloves = uv[:3], uv[3:]
    assert 0 <= body[:, 0].min() and body[:, 0].max() <= 0.5
    assert 0.5 <= gloves[:, 0].min() and gloves[:, 0].max() <= 1.0
    assert 0 <= uv[:, 1].min() and uv[:, 1].max() <= 0.5
    # the slot's corners survive, inset so bilinear filtering stays inside it
    assert 0 < body[0, 0] < body[1, 0] < 0.5


def test_a_material_that_tiles_its_texture_stops_the_build():
    fixture = _Fixture()
    with tempfile.TemporaryDirectory() as d:
        g, blob = fixture.build(d)
        uv = g["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
        view = g["bufferViews"][g["accessors"][uv]["bufferView"]]
        patched = bytearray(blob)
        patched[view["byteOffset"]:view["byteOffset"] + 8] = np.array([3.0, 0.0], np.float32).tobytes()
        try:
            pc.build_character(g, bytes(patched), d, ["idle_rifle"])
        except SystemExit as e:
            assert "tiles" in str(e)
        else:
            raise AssertionError("tiling would sample the neighbouring slot")


def test_atlas_layout_gives_the_largest_slots_first_and_never_overlaps():
    for count in range(1, 8):
        layout = pc.atlas_layout(count)
        assert len(layout) == count
        assert [s for _, _, s in layout] == sorted((s for _, _, s in layout), reverse=True)
        for i, (x, y, s) in enumerate(layout):
            assert 0 <= x and x + s <= 1 and 0 <= y and y + s <= 1
            for (x2, y2, s2) in layout[i + 1:]:
                assert x + s <= x2 or x2 + s2 <= x or y + s <= y2 or y2 + s2 <= y


def test_too_many_materials_for_the_layout_is_an_error():
    try:
        pc.atlas_layout(8)
    except SystemExit:
        pass
    else:
        raise AssertionError("eight materials do not fit")


def test_stripped_bone_weights_fold_onto_its_parent():
    result, blob, stats = _build()
    names = [result["nodes"][j]["name"] for j in result["skins"][0]["joints"]]
    assert names == ["root_motion", "pelvis", "spine_0"]
    assert stats["joints"] == 3
    prim = result["meshes"][0]["primitives"][0]
    joints = _read(result, blob, prim["attributes"]["JOINTS_0"]).tolist()
    weights = _read(result, blob, prim["attributes"]["WEIGHTS_0"]).tolist()
    # jiggle_pouch (old 3) -> pelvis (new 1): vertex 0 had pelvis .5 + jiggle .5
    assert joints[0][0] == 1 and weights[0][:2] == [1.0, 0.0]
    # vertex 2 had jiggle .75 + spine .25 -> pelvis first as the heavier one
    assert joints[2][:2] == [1, 2] and weights[2][:2] == [0.75, 0.25]
    assert all(abs(sum(w) - 1.0) < 1e-6 for w in weights)
    assert _read(result, blob, result["skins"][0]["inverseBindMatrices"]).shape == (3, 16)


def test_stripped_bone_loses_its_tracks():
    result, _, _ = _build()
    targets = {c["target"]["node"] for a in result["animations"] for c in a["channels"]}
    named = {result["nodes"][t]["name"] for t in targets}
    assert "jiggle_pouch" not in named


def test_track_constant_across_all_clips_moves_to_the_rest_pose():
    result, _, _ = _build()
    root_motion = next(n for n in result["nodes"] if n["name"] == "root_motion")
    assert root_motion["rotation"] == [-0.5, -0.5, -0.5, 0.5]
    assert "scale" not in root_motion, "a scale of 1 is the default"
    node = result["nodes"].index(root_motion)
    assert not [c for a in result["animations"] for c in a["channels"] if c["target"]["node"] == node]


def test_track_differing_between_clips_stays_even_when_held_within_each():
    result, _, _ = _build()
    spine = next(i for i, n in enumerate(result["nodes"]) if n["name"] == "spine_0")
    idle = result["animations"][0]
    assert any(c["target"] == {"node": spine, "path": "rotation"} for c in idle["channels"])


def test_the_gun_hangs_off_the_chest_with_its_pose_kept():
    result, blob, _ = _build()
    nodes = result["nodes"]
    index = {n["name"]: i for i, n in enumerate(nodes)}
    assert index["wpnPivot"] in nodes[index["spine_3"]]["children"]
    assert index["wpnPivot"] not in nodes[index["root_motion"]]["children"]

    # the fixture's gun is at (0, 1.5, 0.3) under root_motion in every clip, chest moving or not
    idle = next(a for a in result["animations"] if a["name"] == "idle_rifle")
    chain = ("pelvis", "spine_0", "spine_1", "spine_2", "spine_3", "wpnPivot")

    def value(name, path):
        node = nodes[index[name]]
        for ch in idle["channels"]:
            if ch["target"] == {"node": index[name], "path": path}:
                return _read(result, blob, idle["samplers"][ch["sampler"]]["output"])[0].astype(float)
        fallback = [0.0, 0.0, 0.0, 1.0] if path == "rotation" else [0.0, 0.0, 0.0]
        return np.array(node.get(path, fallback), float)

    pos, rot = np.zeros(3), np.array([0.0, 0.0, 0.0, 1.0])
    for name in chain:
        pos = pos + pc._quat_rotate(rot, value(name, "translation"))
        rot = pc._quat_mul(rot, value(name, "rotation") / np.linalg.norm(value(name, "rotation")))
    assert np.abs(pos - [0, 1.5, 0.3]).max() < 1e-3, pos


def test_still_clip_gets_a_length_so_looping_does_not_divide_by_zero():
    result, _, _ = _build()
    idle = result["animations"][0]
    ends = [result["accessors"][s["input"]]["max"][0] for s in idle["samplers"]]
    assert max(ends) == pc._STILL_CLIP_SECONDS


def test_keys_are_thinned_but_keep_the_loop_seam():
    result, blob, _ = _build(step=2)
    spine = next(i for i, n in enumerate(result["nodes"]) if n["name"] == "spine_0")
    run = result["animations"][1]
    ch = next(c for c in run["channels"] if c["target"] == {"node": spine, "path": "rotation"})
    times = _read(result, blob, run["samplers"][ch["sampler"]]["input"])
    assert len(times) == 3 and abs(times[-1] - 4 / 30) < 1e-6


def test_textures_are_atlas_jpegs_and_orm_is_gone():
    result, blob, _ = _build(base_atlas=32, normal_atlas=16)
    assert len(result["images"]) == 2
    mat = result["materials"][0]
    assert "extras" not in mat and "occlusionTexture" not in mat
    assert "metallicRoughnessTexture" not in mat["pbrMetallicRoughness"]
    atlases = []
    for image in result["images"]:
        assert image["mimeType"] == "image/jpeg"
        view = result["bufferViews"][image["bufferView"]]
        data = bytes(blob[view["byteOffset"]:view["byteOffset"] + view["byteLength"]])
        atlases.append(Image.open(io.BytesIO(data)).convert("RGB"))
    assert [a.size for a in atlases] == [(32, 32), (16, 16)]
    # the body's red colour map is in its slot, and the gloves have no texture
    red, neutral = atlases[0].getpixel((8, 8)), atlases[0].getpixel((24, 8))
    assert red[0] > 150 and red[1] < 80
    assert abs(neutral[0] - neutral[1]) < 12, "a material without a colour map stays neutral"
    # the gloves have no normal map either, so they must read as flat
    flat = atlases[1].getpixel((12, 4))
    assert flat[2] > 200 and abs(flat[0] - 128) < 20


def test_normal_maps_can_be_dropped():
    result, _, _ = _build(normal_atlas=0)
    assert "normalTexture" not in result["materials"][0]
    assert len(result["images"]) == 1


def test_material_name_is_not_one_the_viewer_despawns():
    result, _, _ = _build()
    assert not is_hidden_material(result["materials"][0]["name"], None)


def test_character_root_has_the_fixed_name_bevy_paths_hash():
    result, _, _ = _build()
    assert result["scenes"][0]["nodes"][0] == 0
    assert result["nodes"][0]["name"] == "character"
    assert "matrix" not in result["nodes"][0]


def test_clip_list_file_holds_the_viewer_poses():
    names = pc.read_clip_list()
    assert len(names) == 78 and len(set(names)) == 78
    assert {"idle_rifle", "run_n_rifle", "crouch_nw_knife", "idle_crouch_pistol"} <= set(names)


def test_output_is_valid_json_with_every_view_inside_the_blob():
    result, blob, _ = _build()
    json.loads(json.dumps(result))
    for view in result["bufferViews"]:
        assert view["byteOffset"] + view["byteLength"] <= len(blob)
        assert view["byteOffset"] % 4 == 0
    for acc in result["accessors"]:
        assert "bufferView" in acc


def test_images_travel_in_the_binary_chunk():
    result, _, _ = _build()
    assert not any("uri" in image for image in result["images"])
