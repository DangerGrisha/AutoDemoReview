"""Turn a Source2Viewer weapon export into the .glb the 3D replay viewer loads.

Run tools/convert_weapons.ps1 rather than this directly: it does the export, this
step and the precompression. It expects two things in the folder: the model's glTF,
and `model.txt`, Source2Viewer's text dump of the model (`-a`), because the bones and
attachments do not reach the glTF when no animation is exported.

The output is one static mesh with one material, in the frame of the weapon's root
bone: +X along the barrel, +Y to the player's left, +Z up, in metres. That is also
the frame of the player skeleton's `wpn` bone, so the viewer mounts it with no offset.

  * only the mesh group CS2 shows by default survives. `body_hd` is today's model and
    `body_legacy` the old one that older weapon skins are still painted for;
  * transparent parts go (scope glass, sticker gaps): Bevy would sort them as separate
    entities, and from a replay's distance nobody can see them;
  * the skeleton goes too: nothing animates a weapon's bolt or magazine, and a static
    mesh skips the per-frame joint work;
  * ambient occlusion is multiplied into the base colour, because the viewer has no
    environment light to apply it to. Metal and roughness maps go for the same reason.
"""

import argparse
import io
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_character import (  # noqa: E402
    _ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER, _FLOAT, _FLAT_NORMAL, _NO_BASE_COLOR, _UV_SLACK,
    _Output, atlas_layout, find_character_gltf)
from prepare_map import _node_matrix, _read_accessor, load_gltf, write_glb  # noqa: E402

_USHORT, _UINT = 5123, 5125
_METRES_PER_SOURCE_UNIT = 0.0254
# Source2Viewer writes Source (x, y, z) as metres (y, z, x), so this takes the export's
# (x, y, z) back to (z, x, y). A cyclic permutation is a true rotation: triangle winding
# and tangent handedness survive it.
_GLTF_TO_SOURCE = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
# The output's one material. rust/viewer's hide_tool_brushes despawns meshes by material
# name, so tests check this one is not on its list.
_MATERIAL_NAME = "weapon"
# Which mesh group to take, best first.
_MESH_GROUPS = ("body_hd", "body", "body_legacy")
# The bone a weapon hangs from. Dual Berettas have no weapon_offset: see _dual_pistol_keep.
_ROOT_BONES = ("weapon_r", "weapon_offset")
# The attachments the viewer reads, by node name.
_ATTACHMENTS = ("muzzle_flash", "shell_eject")
# How dark full occlusion gets. Not 0: the map's ambient light still reaches a crevice.
_AO_FLOOR = 0.35
_KEEP_ATTRS = ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0")
# No weapon is longer than this many Source units from its root bone; more means the
# frame is wrong and the gun would float away from the player's hands.
_MAX_REACH = 60.0

_BONE_RE = re.compile(
    r'm_boneName = "([^"]+)"\s+m_parentName = "([^"]*)"\s+m_invBindPose =\s+\[([^\]]+)\]')
_ATTACHMENT_RE = re.compile(
    r'key = "([^"]+)"\s+value =\s+\{\s+m_name = "[^"]+"\s+m_influenceNames =\s+\[\s+"([^"]*)"'
    r'.*?m_vInfluenceOffsets =\s+\[\s+\[([^\]]+)\]', re.S)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _numbers(text):
    return [float(x) for x in _NUMBER_RE.findall(text)]


def parse_model_dump(text):
    """(bones, attachments) from Source2Viewer's `-a` text dump, in Source units.

    `bones` maps a name to its 4x4 inverse bind pose, `attachments` a name to (bone,
    offset in that bone's space). A model repeats these per mesh with the same values,
    so the first of each wins.
    """
    bones, attachments = {}, {}
    for name, _parent, matrix in _BONE_RE.findall(text):
        values = _numbers(matrix)
        if name not in bones and len(values) == 12:
            bones[name] = np.vstack([np.array(values).reshape(3, 4), [0, 0, 0, 1]])
    for name, bone, offset in _ATTACHMENT_RE.findall(text):
        values = _numbers(offset)
        if name not in attachments and len(values) == 3:
            attachments[name] = (bone, np.array(values))
    return bones, attachments


def root_bone(bones):
    """The bone whose frame the output is in; for a skeleton without any known root,
    the first bone, which every model dump lists parents-first."""
    for name in _ROOT_BONES:
        if name in bones:
            return name
    if not bones:
        sys.exit("the model dump lists no bones: is model.txt from `Source2Viewer-CLI -a`?")
    return next(iter(bones))


def gltf_to_source(positions):
    """Metres in the export's frame to Source units; see _GLTF_TO_SOURCE."""
    return positions @ _GLTF_TO_SOURCE.T / _METRES_PER_SOURCE_UNIT


def _transform_points(matrix, points):
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def attachment_in_root_space(attachment, bones, root):
    """An attachment's position in the root bone's frame, in metres. Some sit on a
    child bone (the shell ejector on the slide), so they go through the model's frame."""
    bone, offset = attachment
    if bone not in bones:
        return None
    model = np.linalg.inv(bones[bone]) @ np.append(offset, 1.0)
    return (bones[root] @ model)[:3] * _METRES_PER_SOURCE_UNIT


def _dual_pistol_keep(bones, source_positions):
    """Dual Berettas are two pistols 20 units apart. The viewer has one weapon bone, so
    it takes the right one. None means keep everything: any other weapon."""
    if "weapon_r" not in bones or "weapon_l" not in bones:
        return None
    right = np.linalg.inv(bones["weapon_r"])[1, 3]
    left = np.linalg.inv(bones["weapon_l"])[1, 3]
    middle = (right + left) / 2
    return source_positions[:, 1] < middle if right < middle else source_positions[:, 1] >= middle


def select_primitives(g):
    """[(node, primitive)] of the default mesh group's opaque parts."""
    groups = {}
    for node in g["nodes"]:
        if "mesh" in node:
            groups.setdefault(node.get("name", "").rsplit(".", 1)[-1], []).append(node)
    group = next((groups[name] for name in _MESH_GROUPS if name in groups), None)
    if group is None:
        sys.exit(f"no mesh group among {_MESH_GROUPS} in this export, only {sorted(groups)}")
    picked = [(node, prim) for node in group for prim in g["meshes"][node["mesh"]]["primitives"]
              if g["materials"][prim["material"]].get("alphaMode") != "BLEND"]
    if not picked:
        sys.exit("the mesh group has no opaque parts")
    return picked


def _texture_image(g, image_dir, tex_info):
    if tex_info is None:
        return None
    if tex_info.get("texCoord", 0) != 0:
        sys.exit("material samples TEXCOORD_1, which is not kept")
    return Image.open(Path(image_dir) / g["images"][g["textures"][tex_info["index"]]["source"]]["uri"])


def _base_colour(g, image_dir, material, size, ao_floor):
    """Base colour × its factor × ambient occlusion, as an RGB image of `size` px."""
    pbr = material.get("pbrMetallicRoughness", {})
    image = _texture_image(g, image_dir, pbr.get("baseColorTexture"))
    if image is None:
        return Image.new("RGB", (size, size), _NO_BASE_COLOR)
    rgb = np.asarray(image.convert("RGB").resize((size, size), Image.LANCZOS), np.float32)
    rgb = rgb * np.array(pbr.get("baseColorFactor", [1, 1, 1])[:3], np.float32)
    occlusion = _texture_image(g, image_dir, pbr.get("metallicRoughnessTexture"))
    if occlusion is not None:
        # Source2Viewer packs occlusion, roughness, metal into R, G, B.
        ao = np.asarray(occlusion.convert("RGB").resize((size, size), Image.LANCZOS),
                        np.float32)[:, :, 0] / 255.0
        rgb = rgb * (ao_floor + (1.0 - ao_floor) * ao)[:, :, None]
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def _normal_map(g, image_dir, material, size):
    image = _texture_image(g, image_dir, material.get("normalTexture"))
    if image is None:
        return Image.new("RGB", (size, size), _FLAT_NORMAL)
    return image.convert("RGB").resize((size, size), Image.LANCZOS)


def _jpeg(image, quality):
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _atlas(images, layout, side, quality, fill):
    atlas = Image.new("RGB", (side, side), fill)
    for image, (x, y, _slot) in zip(images, layout):
        atlas.paste(image, (round(x * side), round(y * side)))
    return _jpeg(atlas, quality)


def _collect_parts(g, bin_bytes, primitives, keep_fn, bones, root):
    """Each primitive's kept triangles, trimmed to their vertices and in the root's frame."""
    parts = []
    to_root = bones[root]
    for node, prim in primitives:
        world = _node_matrix(node)
        indices = _read_accessor(g, bin_bytes, prim["indices"]).astype(np.uint32)
        positions = _transform_points(
            world, _read_accessor(g, bin_bytes, prim["attributes"]["POSITION"]).astype(np.float64))
        source = gltf_to_source(positions)
        keep = keep_fn(bones, source)
        if keep is not None:
            triangles = indices.reshape(-1, 3)
            indices = triangles[keep[triangles].all(axis=1)].reshape(-1)
        if not len(indices):
            continue
        used, remapped = np.unique(indices, return_inverse=True)
        rotation = to_root[:3, :3] @ _GLTF_TO_SOURCE @ world[:3, :3]
        normal = _read_accessor(g, bin_bytes, prim["attributes"]["NORMAL"])[used].astype(np.float64)
        tangent = _read_accessor(g, bin_bytes, prim["attributes"]["TANGENT"])[used].astype(np.float64)
        attrs = {
            "POSITION": (_transform_points(to_root, source[used]) * _METRES_PER_SOURCE_UNIT).astype(np.float32),
            "NORMAL": (normal @ rotation.T).astype(np.float32),
            "TANGENT": np.column_stack([tangent[:, :3] @ rotation.T, tangent[:, 3]]).astype(np.float32),
            "TEXCOORD_0": _read_accessor(g, bin_bytes, prim["attributes"]["TEXCOORD_0"])[used].astype(np.float32),
        }
        parts.append((prim["material"], attrs, remapped.astype(np.uint32)))
    if not parts:
        sys.exit("no triangles left in this model")
    return parts


def _tiles(uv):
    return uv.min() < -_UV_SLACK or uv.max() > 1 + _UV_SLACK


def _keep_atlas_safe(parts):
    """An atlas slot cannot repeat, so a model with several materials keeps only the
    biggest one if any part tiles. One material needs no atlas and may tile freely."""
    materials = {m for m, _, _ in parts}
    if len(materials) == 1 or not any(_tiles(a["TEXCOORD_0"]) for _, a, _ in parts):
        return parts
    tris = {}
    for material, _, indices in parts:
        tris[material] = tris.get(material, 0) + len(indices) // 3
    biggest = max(tris, key=tris.get)
    return [p for p in parts if p[0] == biggest]


def build_weapon(g, bin_bytes, image_dir, model_dump, base_size=1024, normal_size=512,
                 jpeg_quality=82, ao_floor=_AO_FLOOR):
    """Returns (gltf json, binary blob, stats) for the viewer-ready weapon."""
    bones, attachments = parse_model_dump(model_dump)
    root = root_bone(bones)
    parts = _collect_parts(g, bin_bytes, select_primitives(g), _dual_pistol_keep, bones, root)
    parts = _keep_atlas_safe(parts)

    # --- one atlas slot per material, the most triangles getting the largest
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

    def slot_size(side, slot):
        return max(1, round(slot * side))

    base = _atlas([_base_colour(g, image_dir, g["materials"][m], slot_size(base_size, slot_of[m][2]), ao_floor)
                   for m in ordered], layout, base_size, jpeg_quality, _NO_BASE_COLOR)
    pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.55, "baseColorTexture": add_texture(base)}
    material = {"name": _MATERIAL_NAME, "pbrMetallicRoughness": pbr}
    if normal_size:
        normal = _atlas([_normal_map(g, image_dir, g["materials"][m], slot_size(normal_size, slot_of[m][2]))
                         for m in ordered], layout, normal_size, jpeg_quality, _FLAT_NORMAL)
        material["normalTexture"] = add_texture(normal)

    # --- one primitive for the whole weapon
    use_atlas = len(ordered) > 1
    merged = {k: [] for k in _KEEP_ATTRS}
    merged_indices, vertex_count = [], 0
    for old_material, attrs, indices in parts:
        if use_atlas:
            x, y, slot = slot_of[old_material]
            attrs["TEXCOORD_0"] = (np.clip(attrs["TEXCOORD_0"], 0.0, 1.0) * slot
                                   + np.array([x, y])).astype(np.float32)
        for key in _KEEP_ATTRS:
            merged[key].append(attrs[key])
        merged_indices.append(indices + vertex_count)
        vertex_count += len(attrs["POSITION"])
    merged = {k: np.concatenate(v) for k, v in merged.items()}
    merged_indices = np.concatenate(merged_indices)
    index_type = (np.uint16, _USHORT) if vertex_count <= 0xFFFF else (np.uint32, _UINT)

    out_attrs = {key: out.accessor(arr, _FLOAT, _ARRAY_BUFFER, minmax=key == "POSITION")
                 for key, arr in merged.items()}
    meshes = [{"name": "weapon_mesh", "primitives": [{
        "attributes": out_attrs,
        "indices": out.accessor(merged_indices.astype(index_type[0]), index_type[1], _ELEMENT_ARRAY_BUFFER),
        "material": 0,
    }]}]

    # --- nodes: the mesh, then one empty per attachment the viewer reads
    nodes = [{"name": "weapon", "mesh": 0}]
    found = {}
    for name in _ATTACHMENTS:
        if name in attachments:
            position = attachment_in_root_space(attachments[name], bones, root)
            if position is not None:
                nodes.append({"name": name, "translation": position.tolist()})
                found[name] = position

    result = {
        "asset": {"version": "2.0", "generator": "tools/prepare_weapon.py"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [material],
        "textures": textures,
        "images": images,
        "accessors": out.accessors,
        "bufferViews": out.views,
        "buffers": [{"byteLength": len(out.blob)}],
    }
    position = merged["POSITION"]
    stats = {
        "root": root, "verts": vertex_count, "tris": len(merged_indices) // 3,
        "materials": len(ordered), "attachments": found,
        "min": position.min(axis=0) / _METRES_PER_SOURCE_UNIT,
        "max": position.max(axis=0) / _METRES_PER_SOURCE_UNIT,
    }
    return result, out.blob, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("raw_dir", help="folder holding the model's glTF and model.txt")
    ap.add_argument("out", help="output .glb")
    ap.add_argument("--base-size", type=int, default=1024, help="base colour side, px")
    ap.add_argument("--normal-size", type=int, default=512, help="normal map side, px; 0 drops it")
    ap.add_argument("--jpeg-quality", type=int, default=82)
    args = ap.parse_args()

    gltf_path = find_character_gltf(args.raw_dir)
    g = load_gltf(gltf_path)
    if len(g.get("buffers", [])) != 1:
        sys.exit("expected a single-buffer export")
    bin_bytes = (gltf_path.parent / g["buffers"][0]["uri"]).read_bytes()
    dump = (Path(args.raw_dir) / "model.txt").read_text(encoding="utf-8-sig", errors="replace")

    result, blob, stats = build_weapon(
        g, bin_bytes, gltf_path.parent, dump, base_size=args.base_size,
        normal_size=args.normal_size, jpeg_quality=args.jpeg_quality)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    result["buffers"][0]["byteLength"] = len(blob) + (-len(blob) % 4)
    write_glb(out, result, blob)

    low, high = stats["min"], stats["max"]
    print(f"{out.name}: {stats['verts']} verts, {stats['tris']} tris, {stats['materials']} material(s), "
          f"root {stats['root']}, {out.stat().st_size / 1e6:.2f} MB")
    print(f"  from its root: X {low[0]:.1f}..{high[0]:.1f}  Y {low[1]:.1f}..{high[1]:.1f}  "
          f"Z {low[2]:.1f}..{high[2]:.1f} Source units; attachments {sorted(stats['attachments'])}")
    if np.abs(np.concatenate([low, high])).max() > _MAX_REACH:
        print("WARNING: the weapon reaches far from its root bone; did the export's frame change?")


if __name__ == "__main__":
    main()
