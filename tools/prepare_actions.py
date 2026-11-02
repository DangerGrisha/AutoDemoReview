"""Build actions_rig2.glb: everything a CS2 player does besides walking around.

Run tools/convert_characters.ps1 rather than this directly. By hand, after both
models are built:

    Source2Viewer-CLI.exe -i pak01_dir.vpk --vpk_filepath animation/anims/world/ ^
        --vpk_extensions vnmclip_c -d --gltf_export_format gltf --gltf_export_animations ^
        -o raw/character_clips
    python tools/prepare_actions.py raw/character_clips <models dir>

The two sides wear different models on the same skeleton, so these clips live in one
file instead of twice in each model. It holds a copy of the skeleton's names and
nothing else: Bevy finds a clip's bones by the path of names from the root, which only
has to match, so the clips work on both models.

Clips come from their own files because the deaths are not in the models' animation
graphs (the game uses ragdolls), so exporting a model never reaches them.

Three layers, as rust/viewer/src/player_actions.txt says for each clip:

  * full replaces the whole body;
  * upper keeps only the spine and what hangs from it, so the legs keep running;
  * add goes on top of the others.

CS2 applies an additive clip as base * delta, and Bevy as delta * base. Those differ
by the base's rotation, so a clip as exported would turn each bone about the wrong
axis. Each one has a full-pose +non_additive twin, so instead of the stored delta this
writes the one that gives the right answer in Bevy's order:

    pose(t) * reference^-1, where reference = pose(0) * stored(0)^-1

That is exact on the pose the clip was authored over, and close on the others, because
the arms and the spine hold nearly the same pose whatever the legs do.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_character import (  # noqa: E402
    ACTIONS_NAME, CT_NAME, RIG_T_NAME, _Output, _quat_conj, _quat_mul, _subtree, _TRACK_EPS,
    give_still_clips_a_length, read_action_list, read_clip_tracks, read_glb, simplify_track,
    weapon_under_chest,
)
from prepare_map import write_glb  # noqa: E402

# Added clips keep every key: a shot's kick is over in a few frames.
_ADD_KEY_STEP = 1
_FINGERS = "finger_"
_UPPER_ROOT = "spine_0"


def animated_tracks(g):
    """(node, path) of every track the model's own clips hold. Whatever is missing was baked
    into its rest pose, which prepare_character.py only does when these clips agree."""
    return {(ch["target"]["node"], ch["target"]["path"])
            for anim in g["animations"] for ch in anim["channels"]}


def check_same_skeleton(a, b):
    def shape(g):
        names = [n.get("name") for n in g["nodes"] if "mesh" not in n]
        kids = [n.get("children", []) for n in g["nodes"] if "mesh" not in n]
        return names, kids

    if shape(a) != shape(b):
        sys.exit("the two models have different skeletons: rebuild both")
    if animated_tracks(a) != animated_tracks(b):
        sys.exit("the two models animate different tracks: rebuild both")


def reference_pose(pose, stored):
    """What an additive clip was authored over, as a one-key full-pose clip."""
    out = {}
    for key, (times, values) in pose.items():
        if key not in stored:
            continue
        first = stored[key][1][0]
        if key[1] == "rotation":
            ref = _quat_mul(values[0], _quat_conj(first))
        elif key[1] == "translation":
            ref = values[0] - first
        else:
            continue
        out[key] = (times[:1], ref[None, :])
    return out


def additive_from_poses(pose, reference):
    """pose(t) * reference^-1 for rotations, pose(t) - reference for translations."""
    out = {}
    for key, (times, values) in pose.items():
        if key not in reference:
            continue
        ref = reference[key][1][0]
        if key[1] == "rotation":
            out[key] = (times, _quat_mul(values, _quat_conj(ref)))
        elif key[1] == "translation":
            out[key] = (times, values - ref)
    return out


def _is_identity(path, values):
    rest = (0.0, 0.0, 0.0, 1.0) if path == "rotation" else (0.0, 0.0, 0.0)
    # q and -q are the same rotation
    return (np.abs(values - rest).max() < _TRACK_EPS
            or (path == "rotation" and np.abs(values + rest).max() < _TRACK_EPS))


def keep_for_layer(tracks, layer, animated, upper_nodes, finger_nodes):
    """The tracks of one clip worth writing. Nothing may stay that the model's own clips do
    not hold: added to a bone with no base value, Bevy would write the delta as the pose."""
    out = {}
    for key, track in tracks.items():
        node, path = key
        if key not in animated:
            continue
        if layer == "upper" and node not in upper_nodes:
            continue
        if layer == "add":
            if node in finger_nodes or path == "scale" or _is_identity(path, track[1]):
                continue
        out[key] = track
    return out


def build_actions(model, clips_dir, actions, step=2):
    """Returns (gltf json, blob, stats) from the built model's json and the exported clips."""
    nodes = [n for n in model["nodes"] if "mesh" not in n]
    index = {n["name"]: i for i, n in enumerate(nodes)}
    animated = animated_tracks(model)
    upper_nodes = set(_subtree(nodes, index[_UPPER_ROOT]))
    finger_nodes = {i for i, n in enumerate(nodes) if n["name"].startswith(_FINGERS)}
    out = _Output()

    clips = []
    for name, layer, source in actions:
        pose = read_clip_tracks(Path(clips_dir), source, index, full_pose=layer == "add")
        if layer == "add":
            stored = read_clip_tracks(Path(clips_dir), source, index)
            reference = reference_pose(pose, stored)
            weapon_under_chest(pose, index, nodes)
            weapon_under_chest(reference, index, nodes)
            tracks = additive_from_poses(pose, reference)
        else:
            weapon_under_chest(pose, index, nodes)
            tracks = pose
        tracks = keep_for_layer(tracks, layer, animated, upper_nodes, finger_nodes)
        key_step = _ADD_KEY_STEP if layer == "add" else step
        tracks = {k: simplify_track(t, v, key_step) for k, (t, v) in tracks.items()}
        clips.append((name, tracks))

    # A clip with no movement (the held poses) still has to have a length.
    give_still_clips_a_length(clips, (index["spine_1"], "rotation"))

    animations, channels_total = [], 0
    for name, tracks in clips:
        channels, samplers = [], []
        for (node, path), (times, values) in sorted(tracks.items()):
            samplers.append({"input": out.anim_accessor(times, minmax=True),
                             "output": out.anim_rotation(values) if path == "rotation"
                             else out.anim_accessor(values)})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node, "path": path}})
        channels_total += len(channels)
        animations.append({"name": name, "channels": channels, "samplers": samplers})
    out.finish_animation_view()

    result = {
        "asset": {"version": "2.0", "generator": "tools/prepare_actions.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "animations": animations,
        "accessors": out.accessors,
        "bufferViews": out.views,
        "buffers": [{"byteLength": len(out.blob)}],
    }
    return result, out.blob, {"clips": len(animations), "channels": channels_total}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("clips_dir", help="folder Source2Viewer exported the clips into")
    ap.add_argument("models_dir", help="folder with the built models; the output lands here")
    ap.add_argument("--key-step", type=int, default=2,
                    help="keep every Nth key of full and upper clips (the export is 30 per second)")
    args = ap.parse_args()

    models = Path(args.models_dir)
    t, _ = read_glb(models / RIG_T_NAME)
    ct, _ = read_glb(models / CT_NAME)
    check_same_skeleton(t, ct)

    result, blob, stats = build_actions(t, args.clips_dir, read_action_list(), step=args.key_step)
    out = models / ACTIONS_NAME
    result["buffers"][0]["byteLength"] = len(blob) + (-len(blob) % 4)
    write_glb(out, result, blob)
    print(f"{out.name}: {stats['clips']} clips, {stats['channels']} channels, "
          f"{out.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
