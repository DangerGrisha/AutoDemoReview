"""tools/prepare_actions.py and what it needs from prepare_character.py, on tiny synthetic clips."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:  # asset tools only; the app itself needs neither
    raise unittest.SkipTest(f"needs requirements-tools.txt: {exc}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import prepare_actions as pa  # noqa: E402
import prepare_character as pc  # noqa: E402

BONES = ["character", "root_motion", "pelvis", "spine_0", "spine_1", "spine_2", "spine_3",
         "wpnPivot", "finger_index", "leg_upper"]
INDEX = {name: i for i, name in enumerate(BONES)}
TIMES = np.array([0.0, 0.5, 1.0], np.float32)


def _quat(axis, degrees):
    half = np.radians(degrees) / 2.0
    return np.array([*(np.array(axis) * np.sin(half)), np.cos(half)], np.float32)


def _turning(axis, *degrees):
    return np.stack([_quat(axis, d) for d in degrees])


def _skeleton():
    nodes = [{"name": name} for name in BONES]
    for parent, kids in (("character", ["root_motion"]), ("root_motion", ["pelvis"]),
                         ("pelvis", ["spine_0", "leg_upper"]), ("spine_0", ["spine_1"]),
                         ("spine_1", ["spine_2"]), ("spine_2", ["spine_3"]),
                         ("spine_3", ["wpnPivot", "finger_index"])):
        nodes[INDEX[parent]]["children"] = [INDEX[k] for k in kids]
    return nodes


def _model():
    """What prepare_actions.py reads from a built character: every bone but the root animated."""
    channels = [{"sampler": 0, "target": {"node": INDEX[name], "path": path}}
                for name in BONES[2:] for path in ("rotation", "translation")]
    nodes = _skeleton() + [{"name": "character_mesh", "mesh": 0}]
    return {"nodes": nodes, "animations": [{"name": "idle", "channels": channels}]}


def _write_clip(clips_dir, source, tracks, suffix=""):
    """A clip file in Source2Viewer's layout: {(bone, path): (times, values)}."""
    path = Path(clips_dir) / "animation" / "anims" / "world" / f"{source}{suffix}.gltf"
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    views, accessors, samplers, channels = [], [], [], []
    kind = {1: "SCALAR", 3: "VEC3", 4: "VEC4"}

    def add(array):
        array = np.asarray(array, np.float32)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": array.nbytes})
        width = array.shape[1] if array.ndim > 1 else 1
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126,
                          "count": len(array), "type": kind[width]})
        blob.extend(array.tobytes())
        return len(accessors) - 1

    for (bone, track_path), (times, values) in tracks.items():
        samplers.append({"input": add(times), "output": add(values)})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": INDEX[bone], "path": track_path}})
    bin_name = f"{path.stem}.bin"
    (path.parent / bin_name).write_bytes(bytes(blob))
    path.write_text(json.dumps({
        "nodes": [{"name": name} for name in BONES],
        "buffers": [{"uri": bin_name, "byteLength": len(blob)}],
        "bufferViews": views,
        "accessors": accessors,
        "animations": [{"channels": channels, "samplers": samplers}],
    }), encoding="utf-8")


def _chest_pose(tracks, when):
    """Position and rotation of the chest relative to root_motion."""
    pos = np.zeros((len(when), 3), np.float32)
    rot = np.tile(np.array([0.0, 0.0, 0.0, 1.0], np.float32), (len(when), 1))
    for name in pc._CHEST_CHAIN:
        node = INDEX[name]
        t = tracks.get((node, "translation"))
        r = tracks.get((node, "rotation"))
        step = pc._sample_track(*t, when) if t else np.zeros((len(when), 3), np.float32)
        turn = pc._sample_track(*r, when) if r else rot * 0 + np.array([0, 0, 0, 1], np.float32)
        pos = pos + pc._quat_rotate(rot, step)
        rot = pc._quat_mul(rot, turn)
    return pos, rot


def test_the_gun_stays_in_the_same_place_when_it_moves_under_the_chest():
    pelvis, spine = INDEX["pelvis"], INDEX["spine_2"]
    weapon = INDEX["wpnPivot"]
    want_pos = np.array([[10.0, 40.0, 5.0], [12.0, 41.0, 6.0], [9.0, 39.0, 4.0]], np.float32)
    want_rot = _turning((0, 1, 0), 10, 25, -5)
    tracks = {
        (pelvis, "translation"): (TIMES, np.array([[0, 30, 0], [1, 31, 0], [0, 29, 2]], np.float32)),
        (pelvis, "rotation"): (TIMES, _turning((1, 0, 0), 0, 15, 30)),
        (spine, "translation"): (TIMES, np.array([[0, 6, 0], [0, 6, 0], [0, 6, 0]], np.float32)),
        (spine, "rotation"): (TIMES, _turning((0, 0, 1), -20, 0, 20)),
        (weapon, "translation"): (TIMES, want_pos.copy()),
        (weapon, "rotation"): (TIMES, want_rot.copy()),
    }
    pc.weapon_under_chest(tracks, INDEX, _skeleton())

    chest_pos, chest_rot = _chest_pose(tracks, TIMES)
    times, local_pos = tracks[(weapon, "translation")]
    _, local_rot = tracks[(weapon, "rotation")]
    assert np.array_equal(times, TIMES)
    assert np.abs(chest_pos + pc._quat_rotate(chest_rot, local_pos) - want_pos).max() < 1e-3
    assert np.abs(pc._quat_mul(chest_rot, local_rot) - want_rot).max() < 1e-3


def test_a_clip_without_the_gun_is_left_alone():
    tracks = {(INDEX["pelvis"], "rotation"): (TIMES, _turning((1, 0, 0), 0, 1, 2))}
    pc.weapon_under_chest(tracks, INDEX, _skeleton())
    assert list(tracks) == [(INDEX["pelvis"], "rotation")]


def test_an_added_clip_lands_on_the_pose_in_bevys_order():
    """CS2 builds the pose as base * delta. Bevy computes delta * base, so the stored delta
    would turn the bone about the wrong axis, and the rewritten one must not."""
    key = (3, "rotation")
    base = _quat((0, 1, 0), 70)
    stored = _turning((1, 0, 0), 0, 20, 5)
    pose = {key: (TIMES, pc._quat_mul(np.tile(base, (3, 1)), stored))}
    reference = pa.reference_pose(pose, {key: (TIMES, stored)})
    delta = pa.additive_from_poses(pose, reference)[key][1]

    landed = pc._quat_mul(delta, np.tile(base, (3, 1)))
    assert np.abs(landed - pose[key][1]).max() < 1e-5
    # and the naive order would not have got there
    assert np.abs(pc._quat_mul(stored, np.tile(base, (3, 1))) - pose[key][1]).max() > 0.05


def test_an_added_clip_starting_off_zero_keeps_its_start():
    key = (3, "rotation")
    base = _quat((0, 1, 0), 40)
    stored = _turning((1, 0, 0), 10, 20, 10)
    pose = {key: (TIMES, pc._quat_mul(np.tile(base, (3, 1)), stored))}
    reference = pa.reference_pose(pose, {key: (TIMES, stored)})
    assert np.abs(reference[key][1][0] - base).max() < 1e-5


def test_added_translations_are_the_difference_from_the_reference():
    key = (3, "translation")
    stored = np.array([[1, 0, 0], [2, 0, 0], [1, 0, 0]], np.float32)
    pose = {key: (TIMES, stored + np.array([5, 6, 7], np.float32))}
    reference = pa.reference_pose(pose, {key: (TIMES, stored)})
    delta = pa.additive_from_poses(pose, reference)[key][1]
    assert np.abs(delta - stored).max() < 1e-5


def test_identity_counts_the_opposite_quaternion_too():
    assert pa._is_identity("rotation", np.array([[0, 0, 0, 1]], np.float32))
    assert pa._is_identity("rotation", np.array([[0, 0, 0, -1]], np.float32))
    assert pa._is_identity("translation", np.zeros((2, 3), np.float32))
    assert not pa._is_identity("rotation", _turning((1, 0, 0), 5))


def _layer_tracks():
    moving = (TIMES, _turning((1, 0, 0), 0, 10, 20))
    return {
        (INDEX["leg_upper"], "rotation"): moving,
        (INDEX["spine_1"], "rotation"): moving,
        (INDEX["finger_index"], "rotation"): moving,
        (INDEX["spine_1"], "scale"): (TIMES, np.ones((3, 3), np.float32)),
        (INDEX["spine_2"], "rotation"): (TIMES, np.tile([0, 0, 0, 1.0], (3, 1)).astype(np.float32)),
        (INDEX["character"], "rotation"): moving,
    }


def _keep(layer):
    animated = pa.animated_tracks(_model()) | {(INDEX["spine_1"], "scale")}
    nodes = _skeleton()
    upper = set(pc._subtree(nodes, INDEX["spine_0"]))
    return set(pa.keep_for_layer(_layer_tracks(), layer, animated, upper, {INDEX["finger_index"]}))


def test_only_tracks_the_model_animates_are_kept():
    assert (INDEX["character"], "rotation") not in _keep("full")


def test_upper_clips_leave_the_legs_alone():
    kept = _keep("upper")
    assert (INDEX["leg_upper"], "rotation") not in kept
    assert (INDEX["spine_1"], "rotation") in kept
    assert (INDEX["leg_upper"], "rotation") in _keep("full")


def test_added_clips_lose_fingers_scale_and_tracks_that_add_nothing():
    kept = _keep("add")
    assert kept == {(INDEX["leg_upper"], "rotation"), (INDEX["spine_1"], "rotation")}


def _clip(key, value):
    return {key: (TIMES[:1], np.array([value], np.float32))}


def test_a_track_only_an_action_moves_is_not_baked():
    key = (3, "rotation")
    held = [0.0, 0.0, 0.0, 1.0]
    clips = [("idle", _clip(key, held)), ("run", _clip(key, held))]
    rest = {}
    pc.bake_constant_tracks(clips, rest, [{key: (TIMES, _turning((1, 0, 0), 0, 30, 60))}])
    assert rest == {}
    assert key in clips[0][1]


def test_a_track_every_action_agrees_with_is_baked():
    key = (3, "rotation")
    held = [0.0, 0.0, 0.0, 1.0]
    clips = [("idle", _clip(key, held)), ("run", _clip(key, held))]
    rest = {}
    pc.bake_constant_tracks(clips, rest, [{key: (TIMES[:1], np.array([held], np.float32))}])
    assert rest[3]["rotation"] == held
    assert key not in clips[0][1]


def test_models_on_different_skeletons_stop_the_build():
    other = _model()
    other["nodes"][INDEX["spine_1"]]["name"] = "spine_x"
    try:
        pa.check_same_skeleton(_model(), other)
    except SystemExit:
        return
    raise AssertionError("a different skeleton went through")


def test_models_animating_different_tracks_stop_the_build():
    other = _model()
    other["animations"][0]["channels"].pop()
    try:
        pa.check_same_skeleton(_model(), other)
    except SystemExit:
        return
    raise AssertionError("different tracks went through")


def _build_actions():
    moving = (TIMES, _turning((1, 0, 0), 0, 10, 20))
    with tempfile.TemporaryDirectory() as clips:
        _write_clip(clips, "shared/death", {("leg_upper", "rotation"): moving,
                                            ("spine_1", "rotation"): moving})
        _write_clip(clips, "gun/reload", {("leg_upper", "rotation"): moving,
                                          ("spine_1", "rotation"): moving})
        still = (TIMES[:1], _turning((1, 0, 0), 15))
        _write_clip(clips, "gun/hold", {("spine_1", "rotation"): still})
        delta = (TIMES, _turning((1, 0, 0), 0, 8, 0))
        pose = (TIMES, pc._quat_mul(np.tile(_quat((0, 1, 0), 30), (3, 1)), delta[1]))
        _write_clip(clips, "gun/shoot", {("spine_1", "rotation"): delta,
                                         ("finger_index", "rotation"): delta})
        _write_clip(clips, "gun/shoot", {("spine_1", "rotation"): pose,
                                         ("finger_index", "rotation"): pose},
                    suffix=".vnmclip+non_additive")
        actions = [("death", "full", "shared/death"), ("reload_AK", "upper", "gun/reload"),
                   ("hold_C4", "upper", "gun/hold"), ("shoot_AK", "add", "gun/shoot")]
        return pa.build_actions(_model(), clips, actions)


def _named(result, clip):
    anim = next(a for a in result["animations"] if a["name"] == clip)
    return anim, {(result["nodes"][c["target"]["node"]]["name"], c["target"]["path"])
                  for c in anim["channels"]}


def test_the_clip_file_has_the_skeletons_names_and_no_mesh():
    result, _, stats = _build_actions()
    assert stats["clips"] == 4
    assert [n["name"] for n in result["nodes"]] == BONES
    assert not any("mesh" in n for n in result["nodes"])
    assert result["scenes"][0]["nodes"] == [0]
    assert result["nodes"][INDEX["spine_3"]]["children"] == [INDEX["wpnPivot"], INDEX["finger_index"]]


def test_each_layer_keeps_what_it_should_in_the_file():
    result, _, _ = _build_actions()
    assert _named(result, "death")[1] == {("leg_upper", "rotation"), ("spine_1", "rotation")}
    assert _named(result, "reload_AK")[1] == {("spine_1", "rotation")}
    assert _named(result, "shoot_AK")[1] == {("spine_1", "rotation")}


def test_a_held_pose_gets_a_length_in_the_file():
    result, _, _ = _build_actions()
    anim, _ = _named(result, "hold_C4")
    ends = [result["accessors"][s["input"]]["max"][0] for s in anim["samplers"]]
    assert max(ends) == pc._STILL_CLIP_SECONDS


def test_the_list_says_one_layer_for_each_unique_name():
    actions = pc.read_action_list()
    names = [name for name, _, _ in actions]
    assert len(names) == len(set(names))
    assert {layer for _, layer, _ in actions} == {"full", "upper", "add"}
    assert all(source == source.strip("/") for _, _, source in actions)


def test_every_fall_in_the_list_is_on_the_whole_body():
    layers = {name: layer for name, layer, _ in pc.read_action_list()}
    deaths = [name for name in layers if name.startswith("death_")]
    assert deaths and all(layers[name] == "full" for name in deaths)
    assert layers["shoot_AK"] == "add"
    assert layers["reload_AK"] == "upper"
