"""Turn a Source2Viewer character export into the .glb the 3D replay viewer loads.

Run tools/convert_characters.ps1 rather than this directly: it does the export,
this step and the precompression. By hand:

    Source2Viewer-CLI.exe -i pak01_dir.vpk -f agents/models/ctm_sas/ctm_sas.vmdl_c -d ^
        --gltf_export_format gltf --gltf_export_materials --gltf_textures_adapt ^
        --gltf_export_animations --gltf_animation_list <clips> -o raw/characters/ctm_sas
    python tools/prepare_character.py raw/characters/ctm_sas <out>.glb --clips raw/character_clips

What an export holds and what the viewer needs differ a lot, so the output is
rebuilt from scratch rather than edited in place:

  * only the third-person meshes survive (no first-person arms, defuse kit or the
    weapon skeletons the animation graphs drag in);
  * primitives are trimmed to the vertices they use: the export shares vertex pools
    and bevy_gltf uploads a primitive's WHOLE pool (see merge_and_chunk in
    prepare_map.py for the same problem on maps);
  * jiggle and eye bones go, their vertex weights folding onto the nearest kept
    ancestor, and the identical per-mesh skins collapse into one;
  * all of it merges into one primitive with one material, its textures packed into
    an atlas. Bevy makes one entity per primitive, each with its own draws and its
    own copy of every joint matrix, and players are drawn twice (x-ray), so five
    materials cost a third of the frame rate on ten players;
  * clips are limited to the viewer's clip list. A track holding one value in every
    clip, tools/prepare_actions.py's included, moves nothing, so it is baked into the
    rest pose and dropped;
  * the weapon bones move from root_motion to the chest, with their tracks rewritten to
    match. CS2 keeps the hands on the gun with IK, which we do not have, so a gun hanging
    off root_motion left the hands whenever the upper body played anything the legs did
    not. Under the chest it stays in the hands whatever blends with what;
  * textures become JPEGs (the viewer's Bevy build decodes png and jpeg only).

The model stays in the export's frame: metres, Y up, facing +Z, feet at Y=0.
rust/viewer/src/scene.rs holds the matching scale and yaw.
"""

import argparse
import io
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_map import _read_accessor, load_gltf, write_glb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIP_LIST = REPO_ROOT / "rust" / "viewer" / "src" / "player_clips.txt"
ACTION_LIST = REPO_ROOT / "rust" / "viewer" / "src" / "player_actions.txt"

# What the skeleton changes get: the page caches these for a week by URL, so a rebuild that
# changes the bone tree needs a new name, or a browser mixes old models with new clips.
# rust/viewer/src/scene.rs and web/replay3d.py hold the same names.
RIG_T_NAME = "t_rig2.glb"
CT_NAME = "ct_rig2.glb"
ACTIONS_NAME = "actions_rig2.glb"

_CHEST_CHAIN = ("pelvis", "spine_0", "spine_1", "spine_2", "spine_3")
_WEAPON_ROOT = "wpnPivot"

# Secondary motion nobody can see at replay distance, and nothing binds to it.
_STRIP_BONE = re.compile(r"^(jiggle_|eyeball_|eye_target$|scap_(AIMUP|[LR]_AIMAT)$)")
_WORLD_CLIP = "animation/anims/world/"
_DEFAULT_SET = "/_default_"
_KEEP_ATTRS = ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "JOINTS_0", "WEIGHTS_0")

_FLOAT, _SHORT, _USHORT = 5126, 5122, 5123
_ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER = 34962, 34963
_TYPE_BY_WIDTH = {1: "SCALAR", 2: "VEC2", 3: "VEC3", 4: "VEC4", 16: "MAT4"}

# A rotation or translation moving less than this is treated as not moving.
_TRACK_EPS = 5e-4
# An all-constant clip would have zero duration, and Bevy loops by `seek % duration`.
_STILL_CLIP_SECONDS = 1.0
# Export units are metres; this only reports the height in the units everyone knows.
_METRES_PER_SOURCE_UNIT = 0.0254
# The output's one material. rust/viewer's hide_tool_brushes despawns meshes by material
# name, so tests check this one is not on its list.
_MATERIAL_NAME = "character"
# Atlas fill for a material without that texture.
_NO_BASE_COLOR = (128, 128, 128)
_FLAT_NORMAL = (128, 128, 255)
# Rounding in the export reaches a little past 0..1; more than this is real tiling.
_UV_SLACK = 0.01


def read_clip_list(path=CLIP_LIST):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def read_action_list(path=ACTION_LIST):
    """[(name, layer, clip under animation/anims/world/)]"""
    return [tuple(ln.split()) for ln in read_clip_list(path)]


def read_glb(path):
    """(gltf json, binary chunk) of a .glb."""
    data = Path(path).read_bytes()
    json_len = struct.unpack_from("<I", data, 12)[0]
    g = json.loads(data[20:20 + json_len])
    return g, data[20 + json_len + 8:]


def read_clip_tracks(clips_dir, source, index, full_pose=False):
    """One clip's own export as {(key, path): (times, values)}, where `index` maps a bone's
    name to its key and bones it does not name are skipped. `full_pose` reads the
    +non_additive twin of an additive clip."""
    suffix = ".vnmclip+non_additive" if full_pose else ""
    path = Path(clips_dir) / "animation" / "anims" / "world" / f"{source}{suffix}.gltf"
    g = load_gltf(path)
    blob = (path.parent / g["buffers"][0]["uri"]).read_bytes()
    tracks = {}
    anim = g["animations"][0]
    for ch in anim["channels"]:
        key = index.get(g["nodes"][ch["target"]["node"]].get("name"))
        if key is None:
            continue
        sampler = anim["samplers"][ch["sampler"]]
        times = _read_accessor(g, blob, sampler["input"]).astype(np.float32)
        values = _read_accessor(g, blob, sampler["output"]).astype(np.float32)
        tracks[(key, ch["target"]["path"])] = (times, values)
    return tracks


def find_character_gltf(raw_dir):
    """The model's own export; the physics hull lands beside it as *_physics.gltf."""
    found = [p for p in Path(raw_dir).rglob("*.gltf") if not p.stem.endswith("_physics")]
    if len(found) != 1:
        sys.exit(f"expected one character .gltf under {raw_dir}, found {len(found)}")
    return found[0]


class _Output:
    """The output glTF's binary chunk with its bufferViews and accessors."""

    def __init__(self):
        self.blob = bytearray()
        self.views = []
        self.accessors = []
        self._anim = bytearray()
        self._anim_seen = {}
        self._anim_accessors = []

    def view(self, data, target=None):
        self.blob.extend(b"\0" * (-len(self.blob) % 4))
        view = {"buffer": 0, "byteOffset": len(self.blob), "byteLength": len(data)}
        if target:
            view["target"] = target
        self.blob.extend(data)
        self.views.append(view)
        return len(self.views) - 1

    def accessor(self, arr, component, target=None, minmax=False):
        """One accessor with its own bufferView, as vertex attributes require."""
        width = arr.shape[1] if arr.ndim > 1 else 1
        acc = {"bufferView": self.view(arr.tobytes(), target), "componentType": component,
               "count": len(arr), "type": _TYPE_BY_WIDTH[width]}
        if minmax:
            acc["min"] = arr.min(axis=0).tolist()
            acc["max"] = arr.max(axis=0).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def anim_accessor(self, arr, minmax=False):
        """Animation data: thousands of accessors, so they share one bufferView
        and identical runs (every clip's key times, held poses) share an accessor."""
        width = arr.shape[1] if arr.ndim > 1 else 1
        data = arr.tobytes()
        key = (width, data)
        if key in self._anim_seen:
            return self._anim_seen[key]
        acc = {"byteOffset": len(self._anim), "componentType": _FLOAT,
               "count": len(arr), "type": _TYPE_BY_WIDTH[width]}
        if minmax:
            acc["min"] = np.atleast_1d(arr.min(axis=0)).tolist()
            acc["max"] = np.atleast_1d(arr.max(axis=0)).tolist()
        self._anim.extend(data)
        self.accessors.append(acc)
        self._anim_accessors.append(acc)
        self._anim_seen[key] = len(self.accessors) - 1
        return self._anim_seen[key]

    def anim_rotation(self, quats):
        """Rotation keys as normalized 16-bit integers, half the size and all a rotation needs."""
        data = np.round(np.clip(quats, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        key = (4, data)
        if key in self._anim_seen:
            return self._anim_seen[key]
        acc = {"byteOffset": len(self._anim), "componentType": _SHORT, "normalized": True,
               "count": len(quats), "type": "VEC4"}
        self._anim.extend(data)
        self._anim.extend(b"\0" * (-len(self._anim) % 4))
        self.accessors.append(acc)
        self._anim_accessors.append(acc)
        self._anim_seen[key] = len(self.accessors) - 1
        return self._anim_seen[key]

    def finish_animation_view(self):
        if self._anim:
            view = self.view(bytes(self._anim))
            for acc in self._anim_accessors:
                acc["bufferView"] = view


def _parents(nodes):
    return {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}


def _subtree(nodes, root):
    """`root` and everything under it, parents before children."""
    order, stack = [], [root]
    while stack:
        i = stack.pop(0)
        order.append(i)
        stack[:0] = nodes[i].get("children", [])
    return order


def _find_skeleton_root(g):
    """The scene root that owns `root_motion`; the others are weapon skeletons and meshes."""
    for root in g["scenes"][g.get("scene", 0)]["nodes"]:
        if any(g["nodes"][i].get("name") == "root_motion" for i in _subtree(g["nodes"], root)):
            return root
    sys.exit("no scene root contains a root_motion bone: not a character export?")


def _third_person_nodes(g):
    """Mesh nodes like `...ctm_sas.vmdl_c.thirdperson_body`."""
    found = [i for i, n in enumerate(g["nodes"])
             if "mesh" in n and "thirdperson" in n.get("name", "").rsplit(".", 1)[-1]]
    if not found:
        sys.exit("no third-person meshes in this export")
    return found


def fold_weights(joints, weights, joint_map):
    """Remap (N,4) joint indices through `joint_map` and merge the influences that
    land on the same joint, keeping each vertex's weights summing to 1."""
    mapped = joint_map[joints]
    out_j = np.zeros_like(mapped, dtype=np.uint16)
    out_w = np.zeros_like(weights, dtype=np.float32)
    for v in range(len(mapped)):
        merged = {}
        for j, w in zip(mapped[v].tolist(), weights[v].tolist()):
            if w > 0:
                merged[j] = merged.get(j, 0.0) + w
        items = sorted(merged.items(), key=lambda kv: -kv[1])
        total = sum(w for _, w in items)
        if not items:
            out_w[v, 0] = 1.0       # unweighted vertex: pin it to the skeleton root
        for k, (j, w) in enumerate(items):
            out_j[v, k] = j
            out_w[v, k] = w / total
    return out_j, out_w


def simplify_track(times, values, step):
    """One key for a track that does not move, otherwise every `step`-th key plus
    the last (the loop seam)."""
    if len(times) == 1 or np.ptp(values, axis=0).max() < _TRACK_EPS:
        return times[:1], values[:1]
    keep = list(range(0, len(times), step))
    if keep[-1] != len(times) - 1:
        keep.append(len(times) - 1)
    return times[keep], values[keep]


def _quat_conj(q):
    return q * np.array([-1.0, -1.0, -1.0, 1.0], q.dtype)


def _quat_mul(a, b):
    """Hamilton product of (..., 4) xyzw arrays, a then applied after b."""
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=-1)


def _quat_rotate(q, v):
    u = q[..., :3]
    return v + 2.0 * np.cross(u, np.cross(u, v) + q[..., 3:] * v)


def _sample_track(times, values, at):
    """`values` at the times `at`, clamped at the ends. Quaternions blend the short way."""
    if len(times) == 1:
        return np.repeat(values[:1], len(at), axis=0)
    upper = np.clip(np.searchsorted(times, at), 1, len(times) - 1)
    lower = upper - 1
    span = times[upper] - times[lower]
    frac = np.clip((at - times[lower]) / np.where(span > 0, span, 1.0), 0.0, 1.0)[:, None]
    a, b = values[lower], values[upper]
    if values.shape[1] == 4:
        b = np.where((a * b).sum(axis=1, keepdims=True) < 0.0, -b, b)
        out = a + (b - a) * frac
        return out / np.linalg.norm(out, axis=1, keepdims=True)
    return a + (b - a) * frac


def weapon_under_chest(tracks, index, nodes):
    """Rewrite the weapon root's tracks from root_motion-relative to chest-relative, so that
    it can hang off the chest (see the module docstring). `index` gives a bone's key in
    `tracks` and in `nodes`. Rest values stand in for a chain bone the clip does not move."""
    chain = [index[name] for name in _CHEST_CHAIN]
    weapon = index[_WEAPON_ROOT]
    if (weapon, "rotation") not in tracks and (weapon, "translation") not in tracks:
        return

    def rest(node, path):
        fallback = [0.0, 0.0, 0.0, 1.0] if path == "rotation" else [0.0, 0.0, 0.0]
        return np.array(nodes[node].get(path, fallback), np.float32)

    def at(node, path, when):
        if (node, path) in tracks:
            return _sample_track(*tracks[(node, path)], when)
        return np.repeat(rest(node, path)[None, :], len(when), axis=0)

    relevant = [k for k in tracks if k[0] in chain or k[0] == weapon]
    when = np.unique(np.concatenate([tracks[k][0] for k in relevant if k[1] != "scale"]))
    chest_pos = np.zeros((len(when), 3), np.float32)
    chest_rot = np.tile(np.array([0.0, 0.0, 0.0, 1.0], np.float32), (len(when), 1))
    for node in chain:
        chest_pos = chest_pos + _quat_rotate(chest_rot, at(node, "translation", when))
        chest_rot = _quat_mul(chest_rot, at(node, "rotation", when))
    inverse = _quat_conj(chest_rot)
    tracks[(weapon, "translation")] = (
        when, _quat_rotate(inverse, at(weapon, "translation", when) - chest_pos).astype(np.float32))
    tracks[(weapon, "rotation")] = (
        when, _quat_mul(inverse, at(weapon, "rotation", when)).astype(np.float32))


def collect_clips(g, bin_bytes, clip_names, kept_nodes, step):
    """[(clip name, {(node, path): (times, values)})] in `clip_names` order."""
    by_name = {}
    for anim in g.get("animations", []):
        full = anim.get("name", "")
        if full.startswith(_WORLD_CLIP) and _DEFAULT_SET in full:
            by_name[full.rsplit("/", 1)[-1]] = anim
    missing = [c for c in clip_names if c not in by_name]
    if missing:
        sys.exit(f"export lacks {len(missing)} clips, e.g. {missing[:5]}")

    clips = []
    for name in clip_names:
        anim = by_name[name]
        tracks = {}
        for ch in anim["channels"]:
            node, path = ch["target"]["node"], ch["target"]["path"]
            if node not in kept_nodes:
                continue
            sampler = anim["samplers"][ch["sampler"]]
            times = _read_accessor(g, bin_bytes, sampler["input"]).astype(np.float32)
            values = _read_accessor(g, bin_bytes, sampler["output"]).astype(np.float32)
            tracks[(node, path)] = simplify_track(times, values, step)
        clips.append((name, tracks))
    return clips


def bake_constant_tracks(clips, rest, others=()):
    """A track holding one value in EVERY clip moves nothing: store the value in the
    rest pose (`rest[node][path]`) and drop it. Bind pose is unaffected, it lives in
    the inverse bind matrices. A track that differs between clips has to stay even
    when it is constant inside each, or a crossfade would keep the old clip's value.
    `others` are the track sets of clips kept elsewhere, which count for this too: Bevy
    leaves a bone alone when no playing clip has a track for it, so a value only those
    clips move would stay there after they end."""
    for key in {k for _, tracks in clips for k in tracks}:
        node, path = key
        held = [tracks.get(key) for _, tracks in clips]
        if any(h is None or len(h[0]) > 1 for h in held):
            continue
        first = held[0][1][0]
        if any(key in other and np.abs(other[key][1] - first).max() >= _TRACK_EPS
               for other in others):
            continue
        if all(np.abs(h[1][0] - first).max() < _TRACK_EPS for h in held):
            rest.setdefault(node, {})[path] = first.tolist()
            for _, tracks in clips:
                del tracks[key]


def give_still_clips_a_length(clips, prefer):
    """Idle clips export as one key per track; see _STILL_CLIP_SECONDS."""
    for _, tracks in clips:
        if any(len(t) > 1 for t, _ in tracks.values()):
            continue
        key = prefer if prefer in tracks else next(iter(tracks))
        times, values = tracks[key]
        tracks[key] = (np.array([0.0, _STILL_CLIP_SECONDS], np.float32),
                       np.repeat(values[:1], 2, axis=0))


def atlas_layout(count):
    """`count` square slots as (x, y, side) fractions of the atlas, largest first.
    Up to four materials get a quadrant each. Past that the fourth quadrant splits
    into four smaller slots, which the smallest materials are the ones to lose."""
    quadrants = [(0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5)]
    if count == 1:
        return [(0.0, 0.0, 1.0)]
    if count <= 4:
        return [(x, y, 0.5) for x, y in quadrants[:count]]
    if count > 7:
        sys.exit(f"{count} materials do not fit the atlas layout")
    small = [(0.5 + dx, 0.5 + dy, 0.25) for dy in (0.0, 0.25) for dx in (0.0, 0.25)]
    return [(x, y, 0.5) for x, y in quadrants[:3]] + small[:count - 3]


def _encode_atlas(sources, layout, side, quality, fill):
    """One JPEG with each source image in its slot; None leaves the slot at `fill`."""
    atlas = Image.new("RGB", (side, side), fill)
    for path, (x, y, slot) in zip(sources, layout):
        if path is not None:
            size = round(slot * side)
            img = Image.open(path).convert("RGB").resize((size, size), Image.LANCZOS)
            atlas.paste(img, (round(x * side), round(y * side)))
    buf = io.BytesIO()
    atlas.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _action_tracks(clips_dir, actions, kept_bones, nodes):
    """The tracks of tools/prepare_actions.py's replacing clips, on this model's nodes.
    Upper clips do not touch the legs and added clips are not poses, so neither counts there."""
    index = {nodes[i]["name"]: i for i in kept_bones}
    # the weapon is not under the spine yet on the export's nodes
    upper = set(_subtree(nodes, index["spine_0"])) | set(_subtree(nodes, index[_WEAPON_ROOT]))
    found = []
    for _, layer, source in actions:
        if layer == "add":
            continue
        tracks = read_clip_tracks(clips_dir, source, index)
        weapon_under_chest(tracks, index, nodes)
        if layer == "upper":
            tracks = {k: v for k, v in tracks.items() if k[0] in upper}
        found.append(tracks)
    return found


def build_character(g, bin_bytes, image_dir, clip_names, base_atlas=2048, normal_atlas=1024,
                    jpeg_quality=82, step=2, clips_dir=None, actions=()):
    """Returns (gltf json, binary blob, stats) for the viewer-ready model."""
    nodes = g["nodes"]
    parents = _parents(nodes)
    root = _find_skeleton_root(g)
    mesh_nodes = _third_person_nodes(g)

    # --- skeleton: all the kept meshes must share one joint list and bind pose
    skin = g["skins"][nodes[mesh_nodes[0]]["skin"]]
    old_joints = skin["joints"]
    ibm = _read_accessor(g, bin_bytes, skin["inverseBindMatrices"])
    for n in mesh_nodes[1:]:
        other = g["skins"][nodes[n]["skin"]]
        if other["joints"] != old_joints or not np.allclose(
                _read_accessor(g, bin_bytes, other["inverseBindMatrices"]), ibm):
            sys.exit(f"mesh {nodes[n].get('name')} uses a different skin")

    bones = [i for i in _subtree(nodes, root) if i != root]
    stripped = {i for i in bones if _STRIP_BONE.match(nodes[i].get("name", ""))}
    for i in stripped:
        kept_children = set(nodes[i].get("children", [])) - stripped
        if kept_children:
            sys.exit(f"stripped bone {nodes[i]['name']} has kept children {kept_children}")
    kept_bones = [i for i in bones if i not in stripped]
    new_node = {old: k + 1 for k, old in enumerate(kept_bones)}   # node 0 is the root

    kept_joints = [j for j in old_joints if j not in stripped]
    joint_index = {j: k for k, j in enumerate(kept_joints)}
    joint_map = np.zeros(len(old_joints), np.uint16)
    for k, j in enumerate(old_joints):
        while j not in joint_index:
            j = parents[j]
        joint_map[k] = joint_index[j]

    out = _Output()

    # --- every kept primitive, trimmed to the vertices it uses
    parts = []
    for n in mesh_nodes:
        for prim in g["meshes"][nodes[n]["mesh"]]["primitives"]:
            indices = _read_accessor(g, bin_bytes, prim["indices"]).astype(np.uint32)
            used, remapped = np.unique(indices, return_inverse=True)
            attrs = {k: _read_accessor(g, bin_bytes, prim["attributes"][k])[used]
                     for k in _KEEP_ATTRS if k in prim["attributes"]}
            if set(attrs) != set(_KEEP_ATTRS):
                sys.exit(f"mesh {nodes[n].get('name')} lacks {set(_KEEP_ATTRS) - set(attrs)}")
            attrs["JOINTS_0"], attrs["WEIGHTS_0"] = fold_weights(
                attrs["JOINTS_0"].astype(np.intp), attrs["WEIGHTS_0"], joint_map)
            parts.append((prim["material"], attrs, remapped))

    # --- one atlas slot per material, the most triangles getting the largest
    tris_of = {}
    for material, _, part_indices in parts:
        tris_of[material] = tris_of.get(material, 0) + len(part_indices) // 3
    ordered = sorted(tris_of, key=lambda m: -tris_of[m])
    layout = atlas_layout(len(ordered))
    slot_of = dict(zip(ordered, layout))

    def texture_path(tex_info):
        if tex_info is None:
            return None
        if tex_info.get("texCoord", 0) != 0:
            sys.exit("material samples TEXCOORD_1, which is not kept")
        return Path(image_dir) / g["images"][g["textures"][tex_info["index"]]["source"]]["uri"]

    images, textures = [], []

    def add_atlas(sources, side, fill):
        data = _encode_atlas(sources, layout, side, jpeg_quality, fill)
        images.append({"bufferView": out.view(data), "mimeType": "image/jpeg"})
        textures.append({"source": len(images) - 1})
        return {"index": len(textures) - 1}

    pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.8}
    pbr["baseColorTexture"] = add_atlas(
        [texture_path(g["materials"][m].get("pbrMetallicRoughness", {}).get("baseColorTexture"))
         for m in ordered], base_atlas, _NO_BASE_COLOR)
    material = {"name": _MATERIAL_NAME, "pbrMetallicRoughness": pbr}
    if normal_atlas:
        material["normalTexture"] = add_atlas(
            [texture_path(g["materials"][m].get("normalTexture")) for m in ordered],
            normal_atlas, _FLAT_NORMAL)

    # --- one primitive for the whole character
    inset = 0.5 / (min(base_atlas, normal_atlas) if normal_atlas else base_atlas)
    merged = {k: [] for k in _KEEP_ATTRS}
    merged_indices, vertex_count = [], 0
    for old_material, attrs, part_indices in parts:
        uv = attrs["TEXCOORD_0"]
        if uv.min() < -_UV_SLACK or uv.max() > 1 + _UV_SLACK:
            sys.exit(f"material {g['materials'][old_material].get('name')!r} tiles its texture "
                     f"(UVs {uv.min():.2f}..{uv.max():.2f}), which an atlas slot cannot do")
        x, y, slot = slot_of[old_material]
        attrs["TEXCOORD_0"] = (np.clip(uv, 0.0, 1.0) * (slot - 2 * inset)
                               + np.array([x + inset, y + inset])).astype(np.float32)
        for key in _KEEP_ATTRS:
            merged[key].append(attrs[key])
        merged_indices.append(part_indices + vertex_count)
        vertex_count += len(attrs["POSITION"])
    if vertex_count > 0xFFFF:
        sys.exit("character too large for 16-bit indices")
    merged = {k: np.concatenate(v) for k, v in merged.items()}
    merged_indices = np.concatenate(merged_indices).astype(np.uint16)

    out_attrs = {}
    for key, arr in merged.items():
        component = _USHORT if key == "JOINTS_0" else _FLOAT
        out_attrs[key] = out.accessor(arr, component, _ARRAY_BUFFER, minmax=key == "POSITION")
    meshes = [{"name": "character_mesh", "primitives": [{
        "attributes": out_attrs,
        "indices": out.accessor(merged_indices, _USHORT, _ELEMENT_ARRAY_BUFFER),
        "material": 0,
    }]}]
    stats = {"verts": vertex_count, "tris": len(merged_indices) // 3}
    y_min = float(merged["POSITION"][:, 1].min())
    y_max = float(merged["POSITION"][:, 1].max())

    # --- clips (the rest pose is settled here, so nodes are written after this)
    clips = collect_clips(g, bin_bytes, clip_names, set(kept_bones), step)
    index = {nodes[i]["name"]: i for i in kept_bones}
    for _, tracks in clips:
        weapon_under_chest(tracks, index, nodes)
    rest = {}
    others = _action_tracks(clips_dir, actions, kept_bones, nodes) if clips_dir else ()
    bake_constant_tracks(clips, rest, others)
    pelvis = next(i for i in kept_bones if nodes[i].get("name") == "pelvis")
    give_still_clips_a_length(clips, (pelvis, "rotation"))

    animations, channel_total = [], 0
    for name, tracks in clips:
        channels, samplers = [], []
        for (node, path), (times, values) in sorted(tracks.items()):
            samplers.append({"input": out.anim_accessor(times, minmax=True),
                             "output": out.anim_rotation(values) if path == "rotation"
                             else out.anim_accessor(values)})
            channels.append({"sampler": len(samplers) - 1,
                             "target": {"node": new_node[node], "path": path}})
        channel_total += len(channels)
        animations.append({"name": name, "channels": channels, "samplers": samplers})
    out.finish_animation_view()

    # --- nodes: 0 is the character root, then the bones, then the mesh instances.
    # Bevy targets animated bones by the path of names from this root, hence the
    # fixed name: both teams' models then hash to the same targets.
    out_nodes = [{"name": "character",
                  "children": [new_node[c] for c in nodes[root].get("children", []) if c in new_node]}]
    # The weapon hangs off the chest (see the module docstring), and its rest pose only
    # matters until the first clip applies, so the first clip's first key will do.
    chest, weapon = index[_CHEST_CHAIN[-1]], index[_WEAPON_ROOT]
    weapon_rest = {path: clips[0][1][(weapon, path)][1][0].tolist()
                   for path in ("translation", "rotation")}
    for old in kept_bones:
        src = nodes[old]
        node = {"name": src["name"]}
        trs = {k: src[k] for k in ("translation", "rotation", "scale") if k in src}
        trs.update(rest.get(old, {}))
        if old == weapon:
            trs.update(weapon_rest)
        if trs.get("scale") and np.allclose(trs["scale"], 1.0, atol=1e-3):
            del trs["scale"]
        node.update(trs)
        kids = [c for c in src.get("children", []) if c != weapon]
        if old == chest:
            kids.append(weapon)
        kids = [new_node[c] for c in kids if c in new_node]
        if kids:
            node["children"] = kids
        out_nodes.append(node)
    first_mesh_node = len(out_nodes)
    for k, mesh in enumerate(meshes):
        out_nodes.append({"name": mesh["name"], "mesh": k, "skin": 0})

    kept_ibm = np.array([ibm[old_joints.index(j)] for j in kept_joints], np.float32)
    skin_out = {"joints": [new_node[j] for j in kept_joints],
                "inverseBindMatrices": out.accessor(kept_ibm, _FLOAT)}

    result = {
        "asset": {"version": "2.0", "generator": "tools/prepare_character.py"},
        "scene": 0,
        "scenes": [{"nodes": [0] + list(range(first_mesh_node, len(out_nodes)))}],
        "nodes": out_nodes,
        "meshes": meshes,
        "skins": [skin_out],
        "materials": [material],
        "textures": textures,
        "images": images,
        "animations": animations,
        "accessors": out.accessors,
        "bufferViews": out.views,
        "buffers": [{"byteLength": len(out.blob)}],
    }
    stats.update(joints=len(kept_joints), clips=len(animations), channels=channel_total,
                 materials=len(ordered), y_min=y_min, y_max=y_max)
    return result, out.blob, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("raw_dir", help="folder Source2Viewer exported the model into")
    ap.add_argument("out", help="output .glb")
    ap.add_argument("--base-atlas", type=int, default=2048, help="base colour atlas side, px")
    ap.add_argument("--normal-atlas", type=int, default=1024, help="normal atlas side, px; 0 drops it")
    ap.add_argument("--jpeg-quality", type=int, default=82)
    ap.add_argument("--key-step", type=int, default=2,
                    help="keep every Nth animation key (the export is 30 per second)")
    ap.add_argument("--clips", help="folder Source2Viewer exported the action clips into")
    args = ap.parse_args()

    gltf_path = find_character_gltf(args.raw_dir)
    g = load_gltf(gltf_path)
    if len(g.get("buffers", [])) != 1:
        sys.exit("expected a single-buffer export")
    bin_bytes = (gltf_path.parent / g["buffers"][0]["uri"]).read_bytes()

    result, blob, stats = build_character(
        g, bin_bytes, gltf_path.parent, read_clip_list(),
        base_atlas=args.base_atlas, normal_atlas=args.normal_atlas,
        jpeg_quality=args.jpeg_quality, step=args.key_step,
        clips_dir=args.clips, actions=read_action_list() if args.clips else ())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result["buffers"][0]["byteLength"] = len(blob) + (-len(blob) % 4)
    write_glb(out, result, blob)

    json_size = len(json.dumps(result, separators=(",", ":")))
    height = stats["y_max"] / _METRES_PER_SOURCE_UNIT
    print(f"{out.name}: {stats['verts']} verts, {stats['tris']} tris, {stats['joints']} joints, "
          f"{stats['materials']} materials in one atlas")
    print(f"  {stats['clips']} clips, {stats['channels']} channels")
    print(f"  feet at y={stats['y_min']:.3f} m, height {height:.1f} Source units")
    print(f"  {out.stat().st_size / 1e6:.2f} MB (json {json_size / 1e6:.2f} MB)")
    if abs(stats["y_min"]) > 0.05:
        print("WARNING: feet are not at the origin; the viewer assumes they are")
    if not 60 <= height <= 90:
        print("WARNING: height looks wrong; did Source2Viewer change its export units?")


if __name__ == "__main__":
    main()
