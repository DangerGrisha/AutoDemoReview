"""Build what first person shows: the player's arms and a rig for each weapon.

Run tools/convert_viewmodel.ps1 for the arms and tools/convert_weapons.ps1 for the weapons.

Two kinds of file, in models/cs2_v1/viewmodel/:

  * arms_t.glb and arms_ct.glb: the sleeves and gloves each side's model wears in first person,
    skinned to the first-person skeleton, and a clip that moves nothing, which is what makes
    Bevy hand out animation targets.
  * <weapon>.glb: the weapon's clips (draw, idle, shoot, reload) with a copy of the skeleton's
    names, since Bevy finds a clip's bones by the path of names from the root, and the moving
    parts of the weapon as plain meshes on their bones. It has no textures: it borrows the
    material of the third-person model, which prepare_weapon.py builds from the same export,
    so the two share an atlas and a weapon does not download its textures twice.

A CS2 first-person clip holds two skeletons. `viewmodel.vnmskel` has the arms and a `wpn` bone,
and `animation/skeletons/weapons/<weapon>.vnmskel` has the bolt and the magazine. The second
hangs from `wpn` (`m_attachToBoneID` in the skeleton), so that is how the bones nest here.

Weapons are rigid: each vertex follows one bone, so a part per bone needs no skinning, which
leaves Bevy's static mesh pipeline and no joint matrices to upload.

The export's frame is Source's with the axes swapped, done by a rotation on each root bone. The
first-person skeleton keeps that rotation on `root_motion`, because the arms' inverse bind
matrices have it too. Under `wpn` the frame is Source's: +X along the barrel, +Y to the left,
+Z up, in metres. rust/viewer/src/viewmodel.rs turns the root to face away from the camera.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_character import (  # noqa: E402
    _ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER, _FLOAT, _NO_BASE_COLOR, _FLAT_NORMAL, _USHORT, _Output,
    atlas_layout, find_character_gltf, fold_weights, give_still_clips_a_length, simplify_track)
from prepare_map import _read_accessor, load_gltf, write_glb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIP_LIST = REPO_ROOT / "tools" / "viewmodel_clips.txt"

_UINT = 5125
_CLIP_ROOT = "animation/anims/viewmodel"
_ARMS_SKELETON = "animation/skeletons/characters/viewmodel.vnmskel"
_WEAPON_SKELETONS = "animation/skeletons/weapons/"
# Bevy finds bones by the path of names from this root, in the arms and the weapon files alike.
_ROOT_NAME = "viewmodel"
_WEAPON_BONE = "wpn"
_ARM_MESHES = ("firstperson_default_gloves_arms", "firstperson_sleeves")
_ARM_ATTRS = ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "JOINTS_0", "WEIGHTS_0")
_PART_ATTRS = ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0")
# The forearm's twist bones are in the models and not in the clips. The game turns them by hand.
_TWIST_BONES = ("arm_lower_{}_TWIST", "arm_lower_{}_TWIST1", "arm_upper_{}_TWIST", "arm_upper_{}_TWIST1")
_TWIST_PARENTS = {"arm_lower_{}_TWIST": "arm_lower_{}", "arm_lower_{}_TWIST1": "arm_lower_{}",
                  "arm_upper_{}_TWIST": "arm_upper_{}", "arm_upper_{}_TWIST1": "arm_upper_{}"}
_AO_FLOOR = 0.35
_QUAT_IDENTITY = [0.0, 0.0, 0.0, 1.0]
# More of a weapon than this outside one bone per vertex would show as tearing.
_MAX_NON_RIGID = 0.02
_CLIP_KINDS = ("draw", "idle", "shoot", "shoot2", "reload")


def read_clip_list(path=CLIP_LIST):
    """{weapon file: {kind: clip path under animation/anims/viewmodel}}"""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        file, folder, *clips = line.split()
        kinds = {}
        for clip in clips:
            kind, name = clip.split("=", 1)
            if kind not in _CLIP_KINDS:
                sys.exit(f"{file}: unknown clip kind {kind!r}")
            kinds[kind] = f"{folder}/{name}"
        for needed in ("draw", "idle"):
            if needed not in kinds:
                sys.exit(f"{file}: the list has no {needed} clip")
        out[file] = kinds
    return out


def _load_with_blob(path):
    g = load_gltf(path)
    return g, (Path(path).parent / g["buffers"][0]["uri"]).read_bytes()


def clip_file(clips_dir, clip):
    return Path(clips_dir) / _CLIP_ROOT / f"{clip}.gltf"


def read_tracks(g, blob):
    """{(bone name, path): (times, values)} of the clip's translation and rotation tracks."""
    anim = g["animations"][0]
    out = {}
    for ch in anim["channels"]:
        path = ch["target"]["path"]
        if path == "scale":
            continue
        name = g["nodes"][ch["target"]["node"]].get("name")
        sampler = anim["samplers"][ch["sampler"]]
        out[(name, path)] = (_read_accessor(g, blob, sampler["input"]).astype(np.float32),
                             _read_accessor(g, blob, sampler["output"]).astype(np.float32))
    return out


def _scene_root(g, name):
    for root in g["scenes"][g.get("scene", 0)]["nodes"]:
        if g["nodes"][root].get("name") == name:
            return root
    return None


def arm_bones(clip):
    """[(name, parent or None, translation, rotation)] of the first-person skeleton, parents
    first, from any clip's export, with the twist bones the models add and no clip moves."""
    root = _scene_root(clip, _ARMS_SKELETON)
    if root is None:
        sys.exit(f"this clip has no {_ARMS_SKELETON}: did the export's naming change?")
    bones = []

    def walk(index, parent):
        node = clip["nodes"][index]
        bones.append((node["name"], parent, node.get("translation", [0.0, 0.0, 0.0]),
                      node.get("rotation", _QUAT_IDENTITY)))
        for child in node.get("children", []):
            walk(child, node["name"])

    for child in clip["nodes"][root].get("children", []):
        walk(child, None)
    names = {b[0] for b in bones}
    for side in "LR":
        for twist in _TWIST_BONES:
            parent = _TWIST_PARENTS[twist].format(side)
            if parent in names:
                bones.append((twist.format(side), parent, [0.0, 0.0, 0.0], _QUAT_IDENTITY))
    return bones


def _node_tree(bones):
    """glTF nodes for `bones`, with node 0 the fixed root. Returns (nodes, index by name)."""
    nodes = [{"name": _ROOT_NAME}]
    index = {}
    for name, parent, translation, rotation in bones:
        node = {"name": name}
        if np.abs(np.asarray(translation)).max() > 0:
            node["translation"] = [float(v) for v in translation]
        if np.abs(np.asarray(rotation) - _QUAT_IDENTITY).max() > 0:
            node["rotation"] = [float(v) for v in rotation]
        nodes.append(node)
        index[name] = len(nodes) - 1
        owner = nodes[0] if parent is None else nodes[index[parent]]
        owner.setdefault("children", []).append(index[name])
    return nodes, index


def _write_clips(out, clips, index):
    """glTF animations, each `(name, {(bone name, path): (times, values)})`."""
    animations = []
    for name, tracks in clips:
        channels, samplers = [], []
        for (bone, path), (times, values) in sorted(tracks.items()):
            if bone not in index:
                continue
            samplers.append({"input": out.anim_accessor(times, minmax=True),
                             "output": out.anim_rotation(values) if path == "rotation"
                             else out.anim_accessor(values)})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": index[bone], "path": path}})
        animations.append({"name": name, "channels": channels, "samplers": samplers})
    out.finish_animation_view()
    return animations


# --- the arms ---------------------------------------------------------------------------------


def _arm_primitives(g):
    found = []
    for node in g["nodes"]:
        if "mesh" in node and node.get("name", "").rsplit(".", 1)[-1] in _ARM_MESHES:
            found.extend((node, prim) for prim in g["meshes"][node["mesh"]]["primitives"])
    if not found:
        sys.exit(f"no {_ARM_MESHES} in this export: not an agent model?")
    return found


def build_arms(g, blob, image_dir, clip, base_size=2048, normal_size=1024, jpeg_quality=85):
    """Returns (gltf json, binary blob, stats) for one side's arms. `g` is the agent's export and
    `clip` the export of any first-person clip, which is where the skeleton comes from."""
    from prepare_weapon import _atlas, _base_colour, _normal_map  # noqa: PLC0415

    primitives = _arm_primitives(g)
    skin = g["skins"][primitives[0][0]["skin"]]
    joint_nodes = skin["joints"]
    joint_names = [g["nodes"][j].get("name") for j in joint_nodes]
    ibm = _read_accessor(g, blob, skin["inverseBindMatrices"])
    parents = {c: i for i, n in enumerate(g["nodes"]) for c in n.get("children", [])}

    bones = arm_bones(clip)
    # A twist bone's offset is the model's own, since no clip holds it.
    local = {n.get("name"): n for n in g["nodes"]}
    bones = [(name, parent, local[name].get("translation", t), local[name].get("rotation", r))
             if name.endswith(("_TWIST", "_TWIST1")) and name in local else (name, parent, t, r)
             for name, parent, t, r in bones]
    kept = {b[0] for b in bones}

    # Weights on anything else land on the nearest kept ancestor.
    used_joints, joint_of_old = [], np.zeros(len(joint_nodes), np.uint16)
    for k, node in enumerate(joint_nodes):
        while g["nodes"][node].get("name") not in kept:
            if node not in parents:
                sys.exit(f"bone {joint_names[k]} has no ancestor in the first-person skeleton")
            node = parents[node]
        name = g["nodes"][node].get("name")
        if name not in used_joints:
            used_joints.append(name)
        joint_of_old[k] = used_joints.index(name)

    parts = []
    for _node, prim in primitives:
        indices = _read_accessor(g, blob, prim["indices"]).astype(np.uint32)
        used, remapped = np.unique(indices, return_inverse=True)
        attrs = {k: _read_accessor(g, blob, prim["attributes"][k])[used] for k in _ARM_ATTRS}
        weights = attrs["WEIGHTS_0"].astype(np.float32)
        if weights.max() > 1.5:
            weights = weights / 255.0
        attrs["JOINTS_0"], attrs["WEIGHTS_0"] = fold_weights(
            attrs["JOINTS_0"].astype(np.intp), weights, joint_of_old)
        parts.append((prim["material"], attrs, remapped.astype(np.uint32)))

    tris_of = {}
    for material, _, indices in parts:
        tris_of[material] = tris_of.get(material, 0) + len(indices) // 3
    ordered = sorted(tris_of, key=lambda m: -tris_of[m])
    layout = atlas_layout(len(ordered))
    slot_of = dict(zip(ordered, layout))

    out = _Output()
    images, textures = [], []

    def add_texture(data):
        images.append({"bufferView": out.view(data), "mimeType": "image/jpeg"})
        textures.append({"source": len(images) - 1})
        return {"index": len(textures) - 1}

    base = _atlas([_base_colour(g, image_dir, g["materials"][m], max(1, round(base_size * slot_of[m][2])), _AO_FLOOR)
                   for m in ordered], layout, base_size, jpeg_quality, _NO_BASE_COLOR)
    normal = _atlas([_normal_map(g, image_dir, g["materials"][m], max(1, round(normal_size * slot_of[m][2])))
                     for m in ordered], layout, normal_size, jpeg_quality, _FLAT_NORMAL)
    material = {"name": "viewmodel_arms",
                "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.8,
                                         "baseColorTexture": add_texture(base)},
                "normalTexture": add_texture(normal)}

    inset = 0.5 / normal_size
    merged = {k: [] for k in _ARM_ATTRS}
    merged_indices, count = [], 0
    for old_material, attrs, indices in parts:
        x, y, slot = slot_of[old_material]
        attrs["TEXCOORD_0"] = (np.clip(attrs["TEXCOORD_0"], 0.0, 1.0) * (slot - 2 * inset)
                               + np.array([x + inset, y + inset])).astype(np.float32)
        for key in _ARM_ATTRS:
            merged[key].append(attrs[key])
        merged_indices.append(indices + count)
        count += len(attrs["POSITION"])
    if count > 0xFFFF:
        sys.exit("arms too large for 16-bit indices")
    merged = {k: np.concatenate(v) for k, v in merged.items()}
    out_attrs = {key: out.accessor(arr.astype(np.float32) if key != "JOINTS_0" else arr,
                                   _USHORT if key == "JOINTS_0" else _FLOAT, _ARRAY_BUFFER,
                                   minmax=key == "POSITION")
                 for key, arr in merged.items()}
    mesh = {"name": "arms_mesh", "primitives": [{
        "attributes": out_attrs,
        "indices": out.accessor(np.concatenate(merged_indices).astype(np.uint16), _USHORT, _ELEMENT_ARRAY_BUFFER),
        "material": 0,
    }]}

    nodes, index = _node_tree(bones)
    ibm_by_name = {}
    for k, node in enumerate(joint_nodes):
        ibm_by_name.setdefault(g["nodes"][node].get("name"), ibm[k])
    skin_out = {"joints": [index[name] for name in used_joints],
                "inverseBindMatrices": out.accessor(
                    np.array([ibm_by_name[name] for name in used_joints], np.float32), _FLOAT)}

    # A clip that holds the root still: without any clip Bevy gives the bones no targets.
    still = {("root_motion", "rotation"): (np.array([0.0, 1.0], np.float32),
                                           np.tile(np.array(nodes[index["root_motion"]].get("rotation", _QUAT_IDENTITY),
                                                            np.float32), (2, 1)))}
    animations = _write_clips(out, [("rest", still)], index)

    nodes.append({"name": "arms_mesh", "mesh": 0, "skin": 0})
    result = {
        "asset": {"version": "2.0", "generator": "tools/prepare_viewmodel.py"},
        "scene": 0,
        "scenes": [{"nodes": [0, len(nodes) - 1]}],
        "nodes": nodes,
        "meshes": [mesh],
        "skins": [skin_out],
        "materials": [material],
        "textures": textures,
        "images": images,
        "animations": animations,
        "accessors": out.accessors,
        "bufferViews": out.views,
        "buffers": [{"byteLength": len(out.blob)}],
    }
    stats = {"verts": count, "tris": sum(len(i) for i in merged_indices) // 3, "joints": len(used_joints),
             "materials": [g["materials"][m].get("name") for m in ordered]}
    return result, out.blob, stats


# --- a weapon ---------------------------------------------------------------------------------


def _bone_parts(g, blob, primitives, skin, atlas):
    """{bone name: (attrs, indices)}: each primitive's triangles by the bone they follow, in that
    bone's frame. `atlas` is `(slot of a material or None for no atlas, kept materials)`."""
    slot_of, kept_materials = atlas
    joint_names = [g["nodes"][j].get("name") for j in skin["joints"]]
    ibm = _read_accessor(g, blob, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
    collected, non_rigid, total = {}, 0.0, 0
    for _node, prim in primitives:
        if prim["material"] not in kept_materials:
            continue
        attrs = prim["attributes"]
        triangles = _read_accessor(g, blob, prim["indices"]).astype(np.uint32).reshape(-1, 3)
        joints = _read_accessor(g, blob, attrs["JOINTS_0"]).astype(np.intp)
        weights = _read_accessor(g, blob, attrs["WEIGHTS_0"]).astype(np.float32)
        if weights.max() > 1.5:
            weights = weights / 255.0
        strongest = weights.argmax(axis=1)
        bone_of = joints[np.arange(len(joints)), strongest]
        used = np.unique(triangles)
        non_rigid += float((weights[used].max(axis=1) < 0.99).sum())
        total += len(used)
        positions = _read_accessor(g, blob, attrs["POSITION"]).astype(np.float64)
        normals = _read_accessor(g, blob, attrs["NORMAL"]).astype(np.float64)
        tangents = _read_accessor(g, blob, attrs["TANGENT"]).astype(np.float64)
        uvs = _read_accessor(g, blob, attrs["TEXCOORD_0"]).astype(np.float32)
        if slot_of is not None:
            x, y, slot = slot_of[prim["material"]]
            uvs = (np.clip(uvs, 0.0, 1.0) * slot + np.array([x, y])).astype(np.float32)
        # A triangle goes with its first vertex: parts are rigid, so all three agree.
        owner = bone_of[triangles[:, 0]]
        for joint in np.unique(owner):
            tris = triangles[owner == joint]
            verts, remapped = np.unique(tris, return_inverse=True)
            matrix = ibm[joint]
            rotation = matrix[:3, :3]
            part = {
                "POSITION": (positions[verts] @ rotation.T + matrix[:3, 3]).astype(np.float32),
                "NORMAL": (normals[verts] @ rotation.T).astype(np.float32),
                "TANGENT": np.column_stack([tangents[verts, :3] @ rotation.T, tangents[verts, 3]]).astype(np.float32),
                "TEXCOORD_0": uvs[verts],
            }
            # NumPy 2 gives the inverse in the input's shape.
            collected.setdefault(joint_names[joint], []).append((part, remapped.reshape(-1).astype(np.uint32)))
    merged = {}
    for bone, pieces in collected.items():
        attrs = {k: np.concatenate([p[k] for p, _ in pieces]) for k in _PART_ATTRS}
        offsets = np.cumsum([0] + [len(p["POSITION"]) for p, _ in pieces[:-1]])
        merged[bone] = (attrs, np.concatenate([i + o for (_, i), o in zip(pieces, offsets)]))
    return merged, (non_rigid / total if total else 0.0)


def weapon_bones(g, root_name=None):
    """[(name, parent or None, translation, rotation)] of the weapon's own skeleton, parents
    first, under its root bone, which the export's frame swap sits on and which is dropped."""
    nodes = g["nodes"]
    model_root = next((i for i in g["scenes"][g.get("scene", 0)]["nodes"]
                       if "mesh" not in nodes[i] and nodes[i].get("children")), None)
    if model_root is None:
        sys.exit("no skeleton in this export: was it made with --gltf_export_animations?")
    bones = []

    def walk(index, parent):
        node = nodes[index]
        bones.append((node["name"], parent, node.get("translation", [0.0, 0.0, 0.0]),
                      node.get("rotation", _QUAT_IDENTITY)))
        for child in node.get("children", []):
            walk(child, node["name"])

    for top in nodes[model_root].get("children", []):
        # The clip's root stays at the origin of `wpn`, whatever the model's says.
        bones.append((nodes[top]["name"], None, [0.0, 0.0, 0.0], _QUAT_IDENTITY))
        for child in nodes[top].get("children", []):
            walk(child, nodes[top]["name"])
    return bones


def _clip_set(clips_dir, kinds, keep_bones, step=1):
    """[(kind, tracks)] with only the bones of this rig, as the viewer's kinds name them."""
    clips, durations = [], {}
    for kind in _CLIP_KINDS:
        if kind not in kinds:
            continue
        path = clip_file(clips_dir, kinds[kind])
        if not path.exists():
            sys.exit(f"missing clip export {path}")
        g, blob = _load_with_blob(path)
        tracks = {}
        for (bone, channel), (times, values) in read_tracks(g, blob).items():
            if bone in keep_bones:
                tracks[(bone, channel)] = simplify_track(times, values, step)
        durations[kind] = float(max(t[-1] for t, _ in tracks.values()))
        clips.append((kind, tracks))
    give_still_clips_a_length(clips, ("wpn", "rotation"))
    return clips, durations


def build_viewmodel_weapon(g, blob, primitives, atlas, clips_dir, kinds):
    """Returns (gltf json, binary blob, stats) for one weapon's rig. `g` must come from an
    export with the skeleton, and `primitives` and `atlas` from prepare_weapon.py's choice."""
    skinned = next((n for n, _ in primitives if "skin" in n), None)
    if skinned is None:
        sys.exit("the weapon's meshes have no skin: was it made with --gltf_export_animations?")
    skin = g["skins"][skinned["skin"]]
    parts, non_rigid = _bone_parts(g, blob, primitives, skin, atlas)

    first = clip_file(clips_dir, kinds["idle"])
    if not first.exists():
        sys.exit(f"missing clip export {first}")
    clip_g, _ = _load_with_blob(first)
    arms = arm_bones(clip_g)
    own = weapon_bones(g)
    if not any(b[0] == _WEAPON_BONE for b in arms):
        sys.exit(f"the skeleton has no {_WEAPON_BONE} bone")
    bones = list(arms) + [(name, parent or _WEAPON_BONE, t, r) for name, parent, t, r in own]
    names = [b[0] for b in bones]
    if len(set(names)) != len(names):
        sys.exit(f"a weapon bone has a name the arms use: {sorted(n for n in names if names.count(n) > 1)}")
    missing = sorted(set(parts) - set(names))
    if missing:
        sys.exit(f"parts on bones outside the skeleton: {missing}")

    # A weapon's root moves nothing here, and the clip's value for it holds the export's frame swap.
    roots = {name for name, parent, _, _ in own if parent is None}
    keep = (set(names) - roots) - {b for b in names if b.endswith(("_TWIST", "_TWIST1"))}
    clips, durations = _clip_set(clips_dir, kinds, keep)

    out = _Output()
    nodes, index = _node_tree(bones)
    meshes = []
    for bone in sorted(parts):
        attrs, indices = parts[bone]
        count = len(attrs["POSITION"])
        wide = count > 0xFFFF
        meshes.append({"name": f"{bone}_mesh", "primitives": [{
            "attributes": {k: out.accessor(attrs[k], _FLOAT, _ARRAY_BUFFER, minmax=k == "POSITION")
                           for k in _PART_ATTRS},
            "indices": out.accessor(indices.astype(np.uint32 if wide else np.uint16),
                                    _UINT if wide else _USHORT, _ELEMENT_ARRAY_BUFFER),
        }]})
        nodes[index[bone]]["mesh"] = len(meshes) - 1
    animations = _write_clips(out, clips, index)

    result = {
        "asset": {"version": "2.0", "generator": "tools/prepare_viewmodel.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "animations": animations,
        "accessors": out.accessors,
        "bufferViews": out.views,
        "buffers": [{"byteLength": len(out.blob)}],
    }
    stats = {"parts": {bone: len(parts[bone][0]["POSITION"]) for bone in sorted(parts)},
             "durations": durations, "non_rigid": non_rigid}
    return result, out.blob, stats


def write_output(out_path, result, blob):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result["buffers"][0]["byteLength"] = len(blob) + (-len(blob) % 4)
    write_glb(out_path, result, blob)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("raw_dir", help="folder Source2Viewer exported the agent into")
    ap.add_argument("clips_dir", help="folder Source2Viewer exported the first-person clips into")
    ap.add_argument("out", help="output .glb")
    ap.add_argument("--base-size", type=int, default=2048)
    ap.add_argument("--normal-size", type=int, default=1024)
    ap.add_argument("--jpeg-quality", type=int, default=85)
    args = ap.parse_args()

    gltf_path = find_character_gltf(args.raw_dir)
    g, blob = _load_with_blob(gltf_path)
    first_clip = next(iter(read_clip_list().values()))["idle"]
    clip, _ = _load_with_blob(clip_file(args.clips_dir, first_clip))
    result, out_blob, stats = build_arms(g, blob, gltf_path.parent, clip, args.base_size,
                                         args.normal_size, args.jpeg_quality)
    write_output(args.out, result, out_blob)
    print(f"{Path(args.out).name}: {stats['verts']} verts, {stats['tris']} tris, {stats['joints']} joints, "
          f"{stats['materials']}, {Path(args.out).stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
