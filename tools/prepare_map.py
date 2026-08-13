"""Prepare a Source2Viewer glTF map export for the 3D replay viewer.

Input: the raw export directory produced by Source2Viewer-CLI, e.g.

    Source2Viewer-CLI.exe -i "<cs2>/game/csgo/maps/de_mirage.vpk" \
        -e "vwrld_c" -d --recursive --gltf_export_materials -o raw/de_mirage

Output: web/static/replay3d/assets/maps/<map_name>/world.gltf|.glb (+ textures),
ready for the Bevy viewer (which loads everything below its asset root).

What it does, in order:
  1. finds the world .gltf in the export (largest .gltf by default);
  2. drops primitives whose material is an invisible Source 2 tool/effect brush
     (clips, triggers, block-light, csgo_effects sheets) -- server-side twin of
     the viewer's is_hidden_material filter in rust/viewer/src/scene.rs;
  3. strips normal/metallic-roughness/occlusion/emissive texture refs
     (--keep-maps disables) -- the viewer has no baked lighting anyway, and
     albedo-only cuts most of the ~700 textures a full map ships;
  4. --geometry-only additionally strips base-color textures (color factors stay);
  5. prunes now-orphaned textures/images/samplers and deletes unused texture
     files from the copy;
  6. downscales remaining texture files larger than --max-size (default 1024)
     in place with Pillow (WebGL2 memory: full maps otherwise OOM the tab);
  7. --glb repacks everything into a single world.glb (one HTTP fetch instead
     of hundreds through uvicorn);
  8. merge+chunk pass (on by default, --no-merge disables): bakes every node's
     world transform into the vertices, welds duplicates bit-exactly, and
     rebuilds the whole map as a handful of spatial chunk meshes with 16-bit
     indices -- see merge_and_chunk() for why the raw export murders Bevy.

Usage:
    python tools/prepare_map.py <raw_export_dir> <map_name> [--max-size 1024]
        [--keep-maps] [--geometry-only] [--glb] [--dev] [--out-root DIR]
        [--no-merge] [--grid N] [--weld-position-only]

    # re-optimize an ALREADY-prepared world.glb in place (or to --out):
    python tools/prepare_map.py --refine <in.glb> [--out <out.glb>]
        [--grid N] [--weld-position-only]

Parses glTF as raw JSON + numpy (no pygltflib). Requires: pip install numpy Pillow
"""

import argparse
import json
import re
import shutil
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO_ROOT / "web" / "static" / "replay3d" / "assets" / "maps"
DEV_OUT_ROOT = REPO_ROOT / "rust" / "viewer" / "www" / "assets" / "maps"

# Mirror of rust/viewer/src/scene.rs::is_hidden_material -- keep in sync.
def is_hidden_material(name, extras_json):
    n = (name or "").lower()
    if (n.startswith("tools") or n == "black_simple" or "trigger" in n
            or "nodraw" in n or "occluder" in n or "playerclip" in n
            or "invisible" in n):
        return True
    raw = json.dumps(extras_json) if extras_json else ""
    return "materials/tools/" in raw or "csgo_effects.vfx" in raw


def find_world_gltf(raw_dir):
    """The world export is by far the largest .gltf in the directory tree."""
    candidates = sorted(raw_dir.rglob("*.gltf"), key=lambda p: p.stat().st_size)
    if not candidates:
        sys.exit(f"no .gltf found under {raw_dir}")
    return candidates[-1]


def load_gltf(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Viewer coordinate contract (rust/viewer/src/coords.rs): a Source-world point
# (Sx,Sy,Sz) must reach the glTF as (Sx, Sz, -Sy), in raw Hammer units, so the
# viewer's single Transform::from_scale(sceneScale) registers the map with player
# positions. As a 4x4 (row-major) that Source->glTF map is:
_CONTRACT = np.array([[1, 0, 0, 0],
                      [0, 0, 1, 0],
                      [0, -1, 0, 0],
                      [0, 0, 0, 1]], float)


def _node_matrix(n):
    """A glTF node's local transform as a row-major 4x4 numpy matrix
    (column-vector convention; spec composition order M = T * R * S)."""
    if "matrix" in n:
        return np.array(n["matrix"], float).reshape(4, 4).T  # glTF is column-major
    m = np.eye(4)
    if "translation" in n:
        t = np.eye(4); t[:3, 3] = n["translation"]; m = m @ t
    if "rotation" in n:
        x, y, z, w = n["rotation"]
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        t = np.eye(4); t[:3, :3] = r; m = m @ t
    if "scale" in n:
        m = m @ np.diag(list(n["scale"]) + [1.0])
    return m


def orient_scene_to_contract(g):
    """Reorient/rescale the export into the viewer's (x, z, -y) raw-units contract.

    Source2Viewer bakes a global axis+scale into every world root node: 19.x maps
    Source (Sx,Sy,Sz) -> glTF 0.0254*(Sy,Sz,Sx), i.e. inch->metre scale AND a 90°
    yaw vs what the viewer expects, so an untouched export is ~39x too small and
    rotated (players walk off the map). All roots share that one matrix M_root, so
    we wrap them in a single parent carrying W = CONTRACT * inv(M_root): then
    W * M_root == CONTRACT and the geometry lands exactly where players are. Derived
    from the export's own matrix, so it self-corrects if a future Source2Viewer
    changes the convention again. No-op when the export already satisfies the
    contract; skipped (with a warning) if the roots don't share one transform.
    """
    scenes = g.get("scenes")
    if not scenes:
        return
    scene = scenes[g.get("scene", 0)]
    roots = scene.get("nodes", [])
    if not roots:
        return
    nodes = g.get("nodes", [])

    from collections import Counter
    mats = [_node_matrix(nodes[i]) for i in roots]
    # The global axis+scale convention is the transform shared by the world roots;
    # individual props may carry their own placement on top, but they live in the
    # SAME exported frame (M_common . Source), so one global re-frame corrects the
    # whole scene.
    #
    # Counting raw matrices and demanding a >50% majority is NOT enough: on
    # de_nuke the placed props outnumber the world roots (the frame matrix won
    # only 3844/8667), the pass bailed out, and the map stayed in METRES -- ~39x
    # too small, which then made `--simplify 5` (5 units = 5 metres there)
    # collapse the whole map to 3155 vertices. So prefer the frame matrix by
    # SHAPE instead of by majority: the global frame is a pure axis-swap plus one
    # uniform scale and never displaces anything (a placed prop, by definition,
    # carries a translation). Among the roots that look like that, the most
    # common one is M_common; the old majority rule stays as the fallback.
    def _is_frame(m):
        lin = m[:3, :3]
        if np.linalg.norm(m[:3, 3]) > 1e-6:
            return False                       # a placement, not the global frame
        s2 = lin @ lin.T
        scale = np.sqrt(np.trace(s2) / 3.0)
        return scale > 1e-9 and np.allclose(s2, (scale ** 2) * np.eye(3), atol=1e-6 * max(scale ** 2, 1.0))

    keys = Counter(np.round(m, 6).tobytes() for m in mats if _is_frame(m))
    how = "frame-shaped roots"
    if not keys:                                # fall back to the old majority rule
        keys = Counter(np.round(m, 6).tobytes() for m in mats)
        how = "majority"
        if keys.most_common(1)[0][1] < 0.5 * len(mats):
            print(f"  orient: no dominant world transform "
                  f"({keys.most_common(1)[0][1]}/{len(mats)}) -- skipping")
            return
    common_key, count = keys.most_common(1)[0]
    m0 = next(m for m in mats if np.round(m, 6).tobytes() == common_key)
    print(f"  orient: frame from {count}/{len(mats)} roots ({how})")
    try:
        w = _CONTRACT @ np.linalg.inv(m0)
    except np.linalg.LinAlgError:
        print("  orient: root transform not invertible -- skipping reorientation")
        return
    if np.allclose(w, np.eye(4), atol=1e-4):
        return  # export already matches the contract

    nodes.append({"name": "__orient_to_contract__",
                  "matrix": w.T.reshape(-1).tolist(),  # back to column-major
                  "children": list(roots)})
    g["nodes"] = nodes
    scene["nodes"] = [len(nodes) - 1]
    lin = m0[:3, :3]
    print(f"oriented to viewer contract (x,z,-y): export node scale "
          f"~{np.linalg.norm(lin[:, 0]):.4g}, wrapped roots with CONTRACT*inv(M_root)")


def _drop_meshes_by_node_name(g, predicate):
    """Empty the meshes referenced by nodes whose NAME matches `predicate(name)`
    and detach them, so nothing renders (slim_and_compact then GCs the freed
    accessors). A mesh also used by a non-matching node is left intact -- we only
    drop geometry that is exclusively the matched kind. Returns (meshes, prims).
    """
    nodes = g.get("nodes", [])
    matched = {n["mesh"] for n in nodes
               if "mesh" in n and predicate(n.get("name") or "")}
    for n in nodes:  # never drop a mesh a non-matching node also uses
        if "mesh" in n and n["mesh"] in matched and not predicate(n.get("name") or ""):
            matched.discard(n["mesh"])
    if not matched:
        return 0, 0
    meshes = g.get("meshes", [])
    prims = 0
    for mi in matched:
        prims += len(meshes[mi].get("primitives", []))
        meshes[mi]["primitives"] = []
    for n in nodes:
        if n.get("mesh") in matched:
            n.pop("mesh", None)
    return len(matched), prims


def drop_overlays(g):
    """Drop decal/overlay meshes -- posters, signs, blood/dirt splats that CS2
    projects onto surfaces. Source2Viewer exports each as its own mesh whose node
    name contains 'overlay' (e.g. '..._mesh_overlay12.meshset_0'). CS2's VCS 71
    shaders block material/texture export, so we can only render them as opaque
    flat planes -- grey "poster-walls" hovering on the real walls -- pure clutter
    in the untextured viewer, so remove the geometry. (The reference CS2MapViewer
    keeps them because it alpha-tests their real textures; we can't.)
    """
    return _drop_meshes_by_node_name(g, lambda nm: "overlay" in nm.lower())


# Node-name markers for brushes that only exist in non-competitive game modes.
# CS2 spawns these barriers (small walls fencing players in) for Retakes/Wingman/
# etc.; in a normal competitive match they are absent -- but Source2Viewer still
# exports the brush geometry, and no tool-material filter catches it (the material
# is ordinary). Matched by node name, e.g. 'retake_asite_2_60417_4613.hammer_mesh_0'.
_MODE_MARKERS = ("retake", "wingman", "guardian", "coop", "armsrace",
                 "demolition", "survival", "dangerzone")


def drop_mode_barriers(g):
    """Drop game-mode-only barriers (retake/wingman/... walls). They don't exist
    in a competitive match ('they vanish by themselves in CS2'), so the replay of
    a competitive demo must not render them. Returns (meshes, primitives) dropped.
    """
    return _drop_meshes_by_node_name(
        g, lambda nm: any(k in nm.lower() for k in _MODE_MARKERS))


# Vegetation. In-game these are alpha-tested cards and thin trunks; with no
# texture export possible they render as SOLID shields that block the analyst's
# view, and on a map like de_ancient they are also a sixth of the whole triangle
# budget (banyan trunks + tropical foliage + ferns + leaves = ~1.4 M tris).
#
# Markers must match a WHOLE name component, not a substring: plain `in` checks
# made "moss" eat 2086 `blend_sculpture_stelle_rock-topmoss-wet` nodes (stone
# stelae, i.e. architecture) and would let "tree" eat every "street". "moss" is
# gone entirely — it only ever appears as a surface variant of stone.
_FOLIAGE_MARKERS = ("banyan", "foliage", "fern", "leaves", "leaf", "ivy", "vine",
                    "bush", "shrub", "grass", "hedge", "palm", "plant", "tree",
                    "flower", "weed")
_FOLIAGE_RE = re.compile(r"(?:^|[^a-z])(?:" + "|".join(_FOLIAGE_MARKERS) + r")(?:[^a-z]|$)")


def drop_foliage(g):
    """Drop vegetation meshes (trees, leaves, ferns, ...). Returns (meshes, prims)."""
    return _drop_meshes_by_node_name(g, lambda nm: bool(_FOLIAGE_RE.search(nm.lower())))


def _smooth_normals(verts, tris):
    """Area-weighted smooth vertex normals (bincount, not np.add.at: the latter
    is an order of magnitude slower on the millions of triangles a map has)."""
    v64 = verts.astype(np.float64)
    face_n = np.cross(v64[tris[:, 1]] - v64[tris[:, 0]],
                      v64[tris[:, 2]] - v64[tris[:, 0]])   # |.| == 2 * area
    idx = tris.ravel()
    w = np.repeat(face_n, 3, axis=0)
    acc = np.zeros_like(v64)
    for c in range(3):
        acc[:, c] = np.bincount(idx, weights=w[:, c], minlength=len(v64))
    ln = np.linalg.norm(acc, axis=1, keepdims=True)
    return (acc / np.where(ln == 0.0, 1.0, ln)).astype(np.float32)


def gc_unused(g):
    """Drop textures/images unreferenced by any material, remapping indices.

    Samplers are left alone (tiny). Returns the set of image URIs still in use.
    """
    used_tex = set()
    for mat in g.get("materials", []):
        pbr = mat.get("pbrMetallicRoughness", {})
        for slot in (pbr.get("baseColorTexture"), pbr.get("metallicRoughnessTexture"),
                     mat.get("normalTexture"), mat.get("occlusionTexture"),
                     mat.get("emissiveTexture")):
            if slot and "index" in slot:
                used_tex.add(slot["index"])

    textures = g.get("textures", [])
    tex_remap = {}
    new_textures = []
    for i, tex in enumerate(textures):
        if i in used_tex:
            tex_remap[i] = len(new_textures)
            new_textures.append(tex)
    used_img = {tex["source"] for tex in new_textures if "source" in tex}

    images = g.get("images", [])
    img_remap = {}
    new_images = []
    for i, img in enumerate(images):
        if i in used_img:
            img_remap[i] = len(new_images)
            new_images.append(img)
    for tex in new_textures:
        if "source" in tex:
            tex["source"] = img_remap[tex["source"]]

    for mat in g.get("materials", []):
        pbr = mat.get("pbrMetallicRoughness", {})
        for slot in (pbr.get("baseColorTexture"), pbr.get("metallicRoughnessTexture"),
                     mat.get("normalTexture"), mat.get("occlusionTexture"),
                     mat.get("emissiveTexture")):
            if slot and "index" in slot:
                slot["index"] = tex_remap[slot["index"]]

    if new_textures:
        g["textures"] = new_textures
    else:
        g.pop("textures", None)
    if new_images:
        g["images"] = new_images
    else:
        g.pop("images", None)
    return {img["uri"] for img in new_images if "uri" in img}


def slim_and_compact(g, src_bin, keep):
    """Drop unused vertex attributes, then rebuild the single buffer to only the
    bytes still referenced (GC of accessors + bufferViews).

    keep: attribute semantics to retain besides indices (e.g. {"POSITION","NORMAL"}).
    Returns (new_bin_bytes, dropped_attr_names). Falls back to a verbatim copy
    (returns src_bin, empty set) when the glTF uses features this simple compactor
    does not handle (multiple buffers, sparse accessors, morph targets).
    """
    uri_bufs = [b for b in g.get("buffers", []) if b.get("uri")]
    if len(uri_bufs) != 1:
        print("  slim skipped: not a single-buffer glTF")
        return src_bin, set()
    if any("sparse" in a for a in g.get("accessors", [])):
        print("  slim skipped: sparse accessors present")
        return src_bin, set()
    if any("targets" in p for m in g.get("meshes", []) for p in m.get("primitives", [])):
        print("  slim skipped: morph targets present")
        return src_bin, set()

    # 1. strip unwanted attributes from every primitive
    dropped = set()
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            for name in list(prim.get("attributes", {})):
                if name not in keep:
                    dropped.add(name)
                    del prim["attributes"][name]

    # 2. GC accessors down to those still used by a primitive (attributes/indices)
    used = []
    seen = set()
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            for ai in prim.get("attributes", {}).values():
                if ai not in seen:
                    seen.add(ai); used.append(ai)
            if "indices" in prim and prim["indices"] not in seen:
                seen.add(prim["indices"]); used.append(prim["indices"])
    old_acc = g["accessors"]
    acc_remap = {old: new for new, old in enumerate(used)}
    g["accessors"] = [old_acc[i] for i in used]
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            prim["attributes"] = {k: acc_remap[v] for k, v in prim.get("attributes", {}).items()}
            if "indices" in prim:
                prim["indices"] = acc_remap[prim["indices"]]

    # 3. rebuild the buffer from only the bufferViews the surviving accessors touch
    old_bvs = g["bufferViews"]
    bv_remap = {}
    new_bvs = []
    newbin = bytearray()
    for a in g["accessors"]:
        bvi = a.get("bufferView")
        if bvi is None or bvi in bv_remap:
            continue
        bv = old_bvs[bvi]
        off = bv.get("byteOffset", 0)
        ln = bv["byteLength"]
        newbin.extend(b"\0" * (-len(newbin) % 4))  # 4-byte align each view
        nb = {"buffer": 0, "byteOffset": len(newbin), "byteLength": ln}
        if "byteStride" in bv:
            nb["byteStride"] = bv["byteStride"]
        if "target" in bv:
            nb["target"] = bv["target"]
        newbin.extend(src_bin[off:off + ln])
        bv_remap[bvi] = len(new_bvs)
        new_bvs.append(nb)
    for a in g["accessors"]:
        if a.get("bufferView") is not None:
            a["bufferView"] = bv_remap[a["bufferView"]]
    g["bufferViews"] = new_bvs
    g["buffers"] = [{"byteLength": len(newbin), "uri": uri_bufs[0]["uri"]}]
    return bytes(newbin), dropped


def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def apply_flat_color(g, hexcolor):
    """Give every primitive one matte material of `hexcolor` (glTF baseColorFactor
    is linear, so convert from sRGB). Turns the untextured map from Bevy-default
    white into a readable grey without touching geometry or needing UVs."""
    h = hexcolor.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [round(_srgb_to_linear(c), 5) for c in rgb]
    idx = len(g.setdefault("materials", []))
    g["materials"].append({
        "name": "map_flat",
        "pbrMetallicRoughness": {"baseColorFactor": lin + [1.0],
                                 "metallicFactor": 0.0, "roughnessFactor": 0.85},
    })
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            prim["material"] = idx
    print(f"flat colour: {hexcolor} -> linear baseColorFactor {lin} on all primitives")


# ---------------------------------------------------------------------------
# merge + chunk pass
# ---------------------------------------------------------------------------

_COMPONENT_DTYPES = {5120: "i1", 5121: "u1", 5122: "i2",
                     5123: "u2", 5125: "u4", 5126: "f4"}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
                    "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _read_accessor(g, bin_bytes, ai):
    """Accessor `ai` as a numpy array, shape (count, ncomp) or (count,) for
    SCALAR. Tightly packed little-endian data only (no interleaving/sparse)."""
    a = g["accessors"][ai]
    dt = np.dtype("<" + _COMPONENT_DTYPES[a["componentType"]])
    nc = _TYPE_COMPONENTS[a["type"]]
    bv = g["bufferViews"][a["bufferView"]]
    stride = bv.get("byteStride")
    if stride not in (None, dt.itemsize * nc):
        raise ValueError(f"accessor {ai}: interleaved byteStride={stride} unsupported")
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    arr = np.frombuffer(bin_bytes, dtype=dt, count=a["count"] * nc, offset=off)
    return arr.reshape(a["count"], nc) if nc > 1 else arr


def _mesh_instances(g):
    """(mesh_index, world_matrix) for every mesh-carrying node reachable from
    the scene roots, local transforms composed down the hierarchy -- i.e. the
    render-time placement of every mesh instance."""
    nodes = g.get("nodes", [])
    scenes = g.get("scenes", [])
    if not scenes:
        return []
    roots = scenes[g.get("scene", 0)].get("nodes", [])
    out = []
    stack = [(i, np.eye(4)) for i in reversed(roots)]
    while stack:
        ni, parent = stack.pop()
        node = nodes[ni]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            out.append((node["mesh"], world))
        for c in reversed(node.get("children", [])):
            stack.append((c, world))
    return out


def merge_and_chunk(g, bin_bytes, grid=8, weld_positions_only=False, simplify=0.0):
    """Bake world transforms into vertices, weld duplicates, and rebuild the
    scene as a small set of single-primitive chunk meshes with u16 indices,
    binned by triangle centroid on X/Z (after baking, up is +Y).

    Why: Source2Viewer world exports carry thousands of primitives that index
    into a few hundred huge shared POSITION/NORMAL accessor pools. Bevy's glTF
    loader (bevy_gltf 0.19) uploads the FULL pool per primitive with no
    index-based trimming, so a de_mirage with 1.47 M unique vertices becomes
    ~25.7 M uploaded vertices (~617 MB of vertex buffers) and ~3.2 k draw
    calls per camera. Rebinning triangles into fat spatial chunks that own
    exactly their vertices fixes both, losslessly: the default weld key is the
    exact (position, normal) float bits, so shading cannot change.

    weld_positions_only=True instead welds on position bits alone and
    recomputes area-weighted smooth normals (changes shading -- off by
    default, CLI --weld-position-only).

    Mutates `g` (meshes/nodes/scenes/accessors/bufferViews/buffers rebuilt,
    materials trimmed to the single used one, punctual lights dropped) and
    returns the new binary buffer. Returns `bin_bytes` untouched (and leaves
    `g` alone) when the scene isn't the single-material untextured kind this
    pass is built for.
    """
    meshes = g.get("meshes", [])

    # -- guards: this pass merges everything into one material ---------------
    mats_used = {p.get("material") for m in meshes for p in m.get("primitives", [])}
    mats_used.discard(None)
    if len(mats_used) > 1:
        print(f"  merge skipped: {len(mats_used)} materials in use (needs exactly 1)")
        return bin_bytes
    mat_idx = mats_used.pop() if mats_used else None
    if mat_idx is not None:
        mat = g["materials"][mat_idx]
        pbr = mat.get("pbrMetallicRoughness", {})
        slots = (pbr.get("baseColorTexture"), pbr.get("metallicRoughnessTexture"),
                 mat.get("normalTexture"), mat.get("occlusionTexture"),
                 mat.get("emissiveTexture"))
        if any(s and "index" in s for s in slots):
            print("  merge skipped: material samples textures (would need UVs)")
            return bin_bytes
    if any("sparse" in a for a in g.get("accessors", [])):
        print("  merge skipped: sparse accessors present")
        return bin_bytes
    if g.get("skins") or g.get("animations"):
        print("  merge skipped: skins/animations present")
        return bin_bytes

    # -- 1./2. walk instances, gather used vertices, bake world transforms ---
    pos_parts, nrm_parts, tri_parts = [], [], []
    base = 0
    n_prims = 0
    for mesh_i, world in _mesh_instances(g):
        prims = meshes[mesh_i].get("primitives", [])
        if not prims:
            continue
        rot = world[:3, :3]
        trans = world[:3, 3]
        try:
            inv_rot = np.linalg.inv(rot)
        except np.linalg.LinAlgError:
            inv_rot = np.linalg.pinv(rot)
        for prim in prims:
            if prim.get("mode", 4) != 4:
                print(f"  merge skipped: non-TRIANGLES primitive (mode {prim.get('mode')})")
                return bin_bytes
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                continue
            pos = _read_accessor(g, bin_bytes, attrs["POSITION"]).astype(np.float64)
            if "indices" in prim:
                idx = np.asarray(_read_accessor(g, bin_bytes, prim["indices"]),
                                 dtype=np.int64).ravel()
            else:
                idx = np.arange(len(pos), dtype=np.int64)
            if len(idx) == 0:
                continue
            if len(idx) % 3:
                print(f"  merge skipped: index count {len(idx)} not divisible by 3")
                return bin_bytes
            used, local = np.unique(idx, return_inverse=True)
            p = pos[used] @ rot.T + trans          # world space, float64
            if "NORMAL" in attrs:
                nr = _read_accessor(g, bin_bytes, attrs["NORMAL"]).astype(np.float64)[used]
                nr = nr @ inv_rot                   # == (inv(rot).T @ n) per row
                ln = np.linalg.norm(nr, axis=1, keepdims=True)
                nr = nr / np.where(ln == 0.0, 1.0, ln)
            else:
                nr = np.zeros_like(p)
            pos_parts.append(p.astype(np.float32))
            nrm_parts.append(nr.astype(np.float32))
            tri_parts.append(np.asarray(local, np.int64).reshape(-1, 3) + base)
            base += len(used)
            n_prims += 1
    if not tri_parts:
        print("  merge skipped: no triangle geometry found")
        return bin_bytes

    pos_all = np.vstack(pos_parts)
    nrm_all = np.vstack(nrm_parts)
    tris = np.vstack(tri_parts)
    gathered = len(pos_all)

    # -- 3. weld: bit-exact on (position, normal) bytes -- lossless ----------
    if weld_positions_only:
        key = np.ascontiguousarray(pos_all).view(np.dtype((np.void, 12))).ravel()
    else:
        packed = np.ascontiguousarray(np.hstack([pos_all, nrm_all]).astype(np.float32))
        key = packed.view(np.dtype((np.void, 24))).ravel()
    _, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    verts = pos_all[first]
    norms = nrm_all[first]
    tris = np.asarray(inverse, np.int64)[tris]

    if weld_positions_only:  # recompute area-weighted smooth normals
        norms = _smooth_normals(verts, tris)

    # A CS2 map is thousands of units across; anything this small means the
    # inch->metre scale was never undone (see orient_scene_to_contract), and every
    # unit-based option below -- `--simplify`, `--voxel`, the AO grid -- would be
    # off by ~39x. Loud, because the result still *looks* like a valid glb.
    extent = float((verts.max(axis=0) - verts.min(axis=0)).max())
    if extent < 1000.0:
        print(f"  WARNING: scene is only {extent:.1f} units across -- this export looks "
              f"like METRES, not game units. Re-check the orientation pass before "
              f"trusting --simplify/--voxel.")

    # -- 3b. optional decimation: cluster vertices onto a `simplify`-unit grid.
    # Maps like de_ancient model brickwork as GEOMETRY (blend_wall_01 alone is
    # 1.8 M triangles), which is invisible detail once the map renders
    # untextured -- collapsing every vertex in a cell onto their centroid wipes
    # that micro-relief while walls, stairs and doorways keep their shape. Keep
    # the cell well under the thinnest wall (CS2 walls are >= 8 u).
    if simplify > 0:
        before_v, before_t = len(verts), len(tris)
        origin = verts.min(axis=0).astype(np.float64)
        cell = np.floor((verts.astype(np.float64) - origin) / simplify).astype(np.int64)
        span = cell.max(axis=0) + 1
        key = (cell[:, 0] * span[1] + cell[:, 1]) * span[2] + cell[:, 2]
        _, inv_c = np.unique(key, return_inverse=True)
        inv_c = np.asarray(inv_c, np.int64)
        n_new = int(inv_c.max()) + 1
        # representative = centroid of the cell's members (keeps surfaces centred
        # rather than snapping them to a lattice, which would dent flat walls)
        acc = np.empty((n_new, 3), np.float64)
        counts = np.bincount(inv_c, minlength=n_new)
        for c in range(3):
            acc[:, c] = np.bincount(inv_c, weights=verts[:, c].astype(np.float64),
                                    minlength=n_new)
        verts = (acc / counts[:, None]).astype(np.float32)
        tris = inv_c[tris]
        # collapsed triangles: two corners landed in the same cell
        keep = ((tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2])
                & (tris[:, 0] != tris[:, 2]))
        tris = tris[keep]
        # and faces that became duplicates of each other (double-sided detail)
        srt = np.ascontiguousarray(np.sort(tris, axis=1))
        _, uidx = np.unique(srt.view(np.dtype((np.void, 24))).ravel(), return_index=True)
        tris = tris[np.sort(uidx)]
        norms = _smooth_normals(verts, tris)
        print(f"simplify: {simplify:g}u grid | verts {before_v} -> {len(verts)} "
              f"({100 * len(verts) / max(before_v, 1):.0f}%) | tris {before_t} -> "
              f"{len(tris)} ({100 * len(tris) / max(before_t, 1):.0f}%)")
        if len(verts) < 0.05 * max(before_v, 1):
            print(f"  WARNING: decimation kept under 5% of the vertices -- the cell "
                  f"({simplify:g}u) is far too coarse for this scene; the map is "
                  f"almost certainly destroyed.")

    # -- 4. bin triangles by centroid into a grid x grid X/Z lattice ---------
    cent = (verts[tris[:, 0]].astype(np.float64)
            + verts[tris[:, 1]] + verts[tris[:, 2]]) / 3.0
    lo = verts.min(axis=0).astype(np.float64)
    span = np.maximum(verts.max(axis=0).astype(np.float64) - lo, 1e-9)
    ix = np.clip(((cent[:, 0] - lo[0]) / span[0] * grid).astype(np.int64), 0, grid - 1)
    iz = np.clip(((cent[:, 2] - lo[2]) / span[2] * grid).astype(np.int64), 0, grid - 1)
    cell = ix * grid + iz

    max_verts = 65535  # u16-indexable
    chunk_tri_ids = []

    def _emit(tids):
        """Emit tids as one chunk, or split 2x2 on centroid bounds until each
        piece's welded vertex count fits u16 indices."""
        if len(np.unique(tris[tids])) <= max_verts:
            chunk_tri_ids.append(tids)
            return
        c = cent[tids]
        mid = (c.min(axis=0) + c.max(axis=0)) / 2.0
        left, below = c[:, 0] < mid[0], c[:, 2] < mid[2]
        for mask in (left & below, left & ~below, ~left & below, ~left & ~below):
            sub = tids[mask]
            if len(sub) == len(tids):  # all centroids coincide: halve by count
                _emit(sub[: len(sub) // 2])
                _emit(sub[len(sub) // 2:])
                return
            if len(sub):
                _emit(sub)

    order = np.argsort(cell, kind="stable")
    sorted_cells = cell[order]
    starts = np.unique(sorted_cells, return_index=True)[1]
    for s, e in zip(starts, list(starts[1:]) + [len(order)]):
        _emit(order[s:e])

    # -- 4b./5. serialize chunks; replace the node/mesh forest ---------------
    blob = bytearray()
    accessors, views, out_meshes = [], [], []

    def _add_view(data, target):
        blob.extend(b"\0" * (-len(blob) % 4))  # 4-byte-align every view
        views.append({"buffer": 0, "byteOffset": len(blob),
                      "byteLength": len(data), "target": target})
        blob.extend(data)
        return len(views) - 1

    total_chunk_verts = 0
    total_chunk_tris = 0
    for ci, tids in enumerate(chunk_tri_ids):
        corner = tris[tids]
        used = np.unique(corner)
        local = np.searchsorted(used, corner).astype("<u2")
        cp = np.ascontiguousarray(verts[used], dtype="<f4")
        cn = np.ascontiguousarray(norms[used], dtype="<f4")
        iv = _add_view(local.tobytes(), 34963)   # ELEMENT_ARRAY_BUFFER
        pv = _add_view(cp.tobytes(), 34962)      # ARRAY_BUFFER
        nv = _add_view(cn.tobytes(), 34962)
        ia = len(accessors)
        accessors.append({"bufferView": iv, "componentType": 5123,
                          "count": int(local.size), "type": "SCALAR"})
        pa = len(accessors)
        accessors.append({"bufferView": pv, "componentType": 5126,
                          "count": int(len(used)), "type": "VEC3",
                          "min": [float(v) for v in cp.min(axis=0)],
                          "max": [float(v) for v in cp.max(axis=0)]})
        na = len(accessors)
        accessors.append({"bufferView": nv, "componentType": 5126,
                          "count": int(len(used)), "type": "VEC3"})
        prim = {"attributes": {"POSITION": pa, "NORMAL": na}, "indices": ia, "mode": 4}
        if mat_idx is not None:
            prim["material"] = 0
        out_meshes.append({"name": f"chunk_{ci:03d}", "primitives": [prim]})
        total_chunk_verts += len(used)
        total_chunk_tris += len(tids)

    g["meshes"] = out_meshes
    # identity nodes: orientation/placement now live in the vertices themselves
    g["nodes"] = [{"name": m["name"], "mesh": i} for i, m in enumerate(out_meshes)]
    g["scenes"] = [{"nodes": list(range(len(out_meshes)))}]
    g["scene"] = 0
    g["accessors"] = accessors
    g["bufferViews"] = views
    old_uri = next((b.get("uri") for b in g.get("buffers", []) if b.get("uri")), None)
    buf = {"byteLength": len(blob)}
    if old_uri:
        buf["uri"] = old_uri
    g["buffers"] = [buf]
    if mat_idx is not None:
        g["materials"] = [g["materials"][mat_idx]]
    # the punctual lights lived on nodes that no longer exist
    ext_used = [e for e in g.get("extensionsUsed", []) if e != "KHR_lights_punctual"]
    if ext_used:
        g["extensionsUsed"] = ext_used
    else:
        g.pop("extensionsUsed", None)
    if isinstance(g.get("extensions"), dict):
        g["extensions"].pop("KHR_lights_punctual", None)
        if not g["extensions"]:
            g.pop("extensions", None)

    vcounts = sorted(len(np.unique(tris[t])) for t in chunk_tri_ids)
    print(f"merge+chunk: {n_prims} prims -> {len(out_meshes)} chunks | "
          f"verts {gathered} gathered -> {len(verts)} welded -> "
          f"{total_chunk_verts} across chunks | tris {total_chunk_tris} | "
          f"chunk verts min/med/max {vcounts[0]}/{vcounts[len(vcounts)//2]}/{vcounts[-1]} | "
          f"buffer {len(blob)/1e6:.1f} MB")
    return bytes(blob)


# ---------------------------------------------------------------------------
# flat surface colours ("1x1 textures")
# ---------------------------------------------------------------------------

# CS2's real textures cannot be exported (VRF 19.2 rejects CS2's VCS 71 shaders),
# so the map ships untextured. This pass gives every surface ONE flat colour --
# the moral equivalent of a 1x1 texture per material -- picked from the geometry
# itself: which way a triangle faces, and how high it sits. It reads as a
# stylized/architectural render instead of one grey mass, and it costs no UVs.
#
# The colours ride on a COLOR_0 vertex attribute (u8 x4, normalized) rather than
# on extra materials, so the chunk count -- and therefore the draw-call count --
# is unchanged. Bevy's PBR shader multiplies COLOR_0 into base_color, so the
# single material is set to the palette's BRIGHTEST colour and every vertex
# colour is stored relative to it (always <= 1). If a viewer ever ignored
# COLOR_0, the map would degrade to that one flat colour rather than to white.
SURFACE_PALETTE = {
    "ceiling":    "#3b3833",   # undersides: arches, overhangs, ceilings
    "floor_low":  "#4f4b43",   # ground level
    "floor_mid":  "#6d6557",   # raised floors / mid platforms
    "floor_high": "#8d8271",   # rooftops and high ledges
    "ramp":       "#8f6a4e",   # slopes and pitched roofs (muted terracotta)
    "wall":       "#b3a48b",   # vertical surfaces (sandstone)
}
SURFACE_ORDER = ["ceiling", "floor_low", "floor_mid", "floor_high", "ramp", "wall"]
# |normal.y| thresholds separating flat-facing / sloped / vertical triangles.
# _RAMP_COS is deliberately high: a de_* map is full of near-vertical detail
# (bevels, crates, arches) and at 0.35 a quarter of the map came out terracotta.
_UP_COS, _RAMP_COS = 0.75, 0.45


# Baked shading. The viewer lights the map with a near-isotropic ambient
# (AMBIENT_MAP=1800 vs a 4200 lux directional in lib.rs), so two perpendicular
# walls come out equally bright and CORNERS DISAPPEAR -- a doorway reads as flat
# wall. Changing the lights needs a Rust rebuild; baking the shading into
# COLOR_0 does not, and it survives whatever the runtime lighting does.
#
# Two multipliers ride on top of the surface palette:
#   * ambient occlusion  - how enclosed a vertex is, from a voxel occupancy
#     grid sampled at two radii (contact + room scale). Corners, niches,
#     doorways and corridors go darker, which is what reveals depth.
#   * a fake sun         - Lambert against a fixed direction, so two walls that
#     meet at a corner differ in brightness even with no real light.
_SUN_DIR = np.array([0.45, 0.78, 0.42])
_SUN_DIR = _SUN_DIR / np.linalg.norm(_SUN_DIR)


def _tri_samples(pos, tri, voxel, max_extra=24):
    """Surface points for the occupancy grid: the 3 corners of every triangle
    plus area-proportional interior samples (a 4000-unit floor triangle would
    otherwise mark only its corners and read as empty space)."""
    v0, v1, v2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    pts = [v0, v1, v2]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    k = np.clip(np.ceil(area / (voxel * voxel)), 0, max_extra).astype(np.int32)
    rng = np.random.default_rng(0xC0FFEE)                 # deterministic bake
    for s in range(int(k.max()) if k.size else 0):
        m = k > s
        if not m.any():
            break
        u = rng.random((int(m.sum()), 1), dtype=np.float32)
        v = rng.random((int(m.sum()), 1), dtype=np.float32)
        flip = (u + v) > 1.0
        u = np.where(flip, 1.0 - u, u)
        v = np.where(flip, 1.0 - v, v)
        pts.append(v0[m] + u * (v1[m] - v0[m]) + v * (v2[m] - v0[m]))
    return np.concatenate(pts, axis=0)


def _occupancy_grid(g, bin_bytes, prims, voxel):
    """(prefix-summed occupancy, origin, dims): a 3D grid marking which voxels
    contain map surface, summed-area style so any box query is 8 lookups."""
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for prim in prims:
        a = g["accessors"][prim["attributes"]["POSITION"]]
        lo = np.minimum(lo, a["min"])
        hi = np.maximum(hi, a["max"])
    dims = np.maximum(np.ceil((hi - lo) / voxel).astype(np.int64) + 1, 1)
    occ = np.zeros(tuple(dims), dtype=np.float32)
    for prim in prims:
        pos = _read_accessor(g, bin_bytes, prim["attributes"]["POSITION"]).astype(np.float32)
        tri = _read_accessor(g, bin_bytes, prim["indices"]).astype(np.int64).reshape(-1, 3)
        s = _tri_samples(pos, tri, voxel)
        ijk = np.clip(((s - lo) / voxel).astype(np.int64), 0, dims - 1)
        flat = (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]
        occ.reshape(-1)[np.unique(flat)] = 1.0
    filled = float(occ.sum())
    # summed-area table with a zero pad, so box sums need no bounds branching
    sat = np.zeros(tuple(dims + 1), dtype=np.float32)
    sat[1:, 1:, 1:] = occ.cumsum(0).cumsum(1).cumsum(2)
    return sat, lo, dims, filled


def _box_occupancy(sat, dims, ijk, r):
    """Mean occupancy of the (2r+1)^3 box centred on each voxel index."""
    a = np.clip(ijk - r, 0, dims)
    b = np.clip(ijk + r + 1, 0, dims)
    x0, y0, z0 = a[:, 0], a[:, 1], a[:, 2]
    x1, y1, z1 = b[:, 0], b[:, 1], b[:, 2]
    total = (sat[x1, y1, z1] - sat[x0, y1, z1] - sat[x1, y0, z1] - sat[x1, y1, z0]
             + sat[x0, y0, z1] + sat[x0, y1, z0] + sat[x1, y0, z0] - sat[x0, y0, z0])
    vol = np.maximum((x1 - x0) * (y1 - y0) * (z1 - z0), 1)
    return total / vol


def _vertex_normals(g, bin_bytes, prim, pos, tri):
    """Unit vertex normals: the exported NORMAL when present, else area-weighted
    from the triangles. Used for the sun term and to push AO probes off-surface."""
    ni = prim["attributes"].get("NORMAL")
    if ni is not None:
        n = _read_accessor(g, bin_bytes, ni).astype(np.float32)
    else:
        v0, v1, v2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
        fn = np.cross(v1 - v0, v2 - v0)                  # length == 2 * area
        n = np.zeros_like(pos)
        for c in range(3):
            np.add.at(n, tri[:, c], fn)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return (n / np.where(ln == 0.0, 1.0, ln)).astype(np.float32)


def _tri_normal_y_and_height(pos, tri):
    """(normal.y, mean height) per triangle, from the GEOMETRIC normal — vertex
    normals are smoothed in the export and would blur floor/wall boundaries."""
    v0, v1, v2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(n, axis=1)
    ny = np.divide(n[:, 1], np.where(ln == 0.0, 1.0, ln))
    return ny, (v0[:, 1] + v1[:, 1] + v2[:, 1]) / 3.0


def _iter_prims(g):
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) == 4 and "POSITION" in prim.get("attributes", {}) \
                    and prim.get("indices") is not None:
                yield prim


def colorize_by_surface(g, bin_bytes, palette=None, ao=0.55, sun=0.38, voxel=32.0):
    """Add a flat per-surface COLOR_0 to every primitive; returns the new blob.

    Passes: (1) collect up-facing heights so the floor tiers split at real
    percentiles of THIS map, and build the voxel occupancy grid; (2) per vertex,
    resolve the surface class and measure occlusion; (3) normalize occlusion
    against this map's own distribution and write palette x AO x sun.

    `ao`/`sun` are the strengths of the two baked multipliers (0 disables),
    `voxel` is the occupancy grid resolution in world units (32 = player width).
    """
    pal = dict(SURFACE_PALETTE)
    pal.update(palette or {})
    lin = {k: np.array([_srgb_to_linear(int(pal[k].lstrip("#")[i:i + 2], 16) / 255.0)
                        for i in (0, 2, 4)]) for k in SURFACE_ORDER}
    base = max(lin.values(), key=lambda c: float(c.sum()))       # brightest entry
    rel = np.stack([np.clip(lin[k] / base, 0.0, 1.0) for k in SURFACE_ORDER])

    prims = list(_iter_prims(g))
    if not prims:
        print("  colorize skipped: no indexed triangle primitives")
        return bin_bytes
    # Re-running would append a second set of COLOR_0 views and orphan the first
    # (the buffer only grows), so refuse and point at the uncoloured input.
    if any("COLOR_0" in p["attributes"] for p in prims):
        print("  colorize skipped: this glb already has COLOR_0 "
              "(re-run on the pre-colour world.glb)")
        return bin_bytes

    # -- pass 1: floor tier boundaries from up-facing triangles ---------------
    heights = []
    for prim in prims:
        pos = _read_accessor(g, bin_bytes, prim["attributes"]["POSITION"]).astype(np.float32)
        tri = _read_accessor(g, bin_bytes, prim["indices"]).astype(np.int64).reshape(-1, 3)
        ny, ymid = _tri_normal_y_and_height(pos, tri)
        heights.append(ymid[ny > _UP_COS])
    heights = np.concatenate(heights) if heights else np.zeros(1)
    y_lo, y_hi = (np.percentile(heights, [55, 85]) if heights.size
                  else (0.0, 0.0))
    sat, gl, dims, filled = (_occupancy_grid(g, bin_bytes, prims, voxel)
                             if ao > 0 else (None, None, None, 0))

    # -- pass 2: per-vertex surface class + raw occlusion --------------------
    i_ceiling, i_low, i_mid, i_high, i_ramp, i_wall = range(6)
    tri_hist = np.zeros(len(SURFACE_ORDER), dtype=np.int64)
    per_prim = []
    for prim in prims:
        pos = _read_accessor(g, bin_bytes, prim["attributes"]["POSITION"]).astype(np.float32)
        tri = _read_accessor(g, bin_bytes, prim["indices"]).astype(np.int64).reshape(-1, 3)
        ny, ymid = _tri_normal_y_and_height(pos, tri)

        cls = np.full(len(tri), i_wall, dtype=np.int64)
        ramp = (np.abs(ny) > _RAMP_COS) & (np.abs(ny) <= _UP_COS)
        cls[ramp] = i_ramp
        up, down = ny > _UP_COS, ny < -_UP_COS
        cls[up] = np.where(ymid[up] > y_hi, i_high,
                           np.where(ymid[up] > y_lo, i_mid, i_low))
        cls[down] = i_ceiling
        tri_hist += np.bincount(cls, minlength=len(SURFACE_ORDER))

        # a vertex is shared by several triangles: let the majority class win
        n_v, n_c = len(pos), len(SURFACE_ORDER)
        key = tri.ravel() * n_c + np.repeat(cls, 3)
        votes = np.bincount(key, minlength=n_v * n_c).reshape(n_v, n_c)
        vcls = votes.argmax(axis=1).astype(np.int8)

        nrm = _vertex_normals(g, bin_bytes, prim, pos, tri)
        if ao > 0:
            # sample slightly OFF the surface, so a vertex never occludes itself
            probe = pos + nrm * (voxel * 1.5)
            ijk = np.clip(((probe - gl) / voxel).astype(np.int64), 0, dims - 1)
            occ_near = _box_occupancy(sat, dims, ijk, 1)     # contact / crevices
            occ_far = _box_occupancy(sat, dims, ijk, 4)      # room enclosure
        else:
            occ_near = occ_far = np.zeros(n_v, dtype=np.float32)
        per_prim.append((prim, vcls, nrm, occ_near, occ_far))

    # -- pass 3: normalize occlusion against this map, then write colours ----
    # absolute occupancy means nothing on its own (it scales with voxel size and
    # map density), so map each measure's own p20..p97 onto 0..1
    all_near = np.concatenate([p[3] for p in per_prim]) if per_prim else np.zeros(1)
    all_far = np.concatenate([p[4] for p in per_prim]) if per_prim else np.zeros(1)
    n_lo, n_hi = np.percentile(all_near, [20, 97])
    f_lo, f_hi = np.percentile(all_far, [20, 97])
    norm = lambda v, a, b: np.clip((v - a) / max(b - a, 1e-6), 0.0, 1.0)

    blob = bytearray(bin_bytes)
    added = 0
    ao_stat = []
    for prim, vcls, nrm, occ_near, occ_far in per_prim:
        shade = np.ones(len(vcls), dtype=np.float32)
        if ao > 0:
            enclosed = 0.6 * norm(occ_near, n_lo, n_hi) + 0.4 * norm(occ_far, f_lo, f_hi)
            shade *= (1.0 - ao * enclosed).astype(np.float32)
            ao_stat.append(shade.copy())
        if sun > 0:
            lambert = np.clip(nrm @ _SUN_DIR, 0.0, 1.0).astype(np.float32)
            shade *= (1.0 - sun) + sun * lambert

        col = np.empty((len(vcls), 4), dtype=np.uint8)
        col[:, :3] = np.rint(np.clip(rel[vcls] * shade[:, None], 0.0, 1.0) * 255.0)
        col[:, 3] = 255
        blob.extend(b"\0" * (-len(blob) % 4))
        g["bufferViews"].append({"buffer": 0, "byteOffset": len(blob),
                                 "byteLength": col.nbytes, "target": 34962})
        blob.extend(col.tobytes())
        g["accessors"].append({"bufferView": len(g["bufferViews"]) - 1,
                               "componentType": 5121, "normalized": True,
                               "count": len(vcls), "type": "VEC4"})
        prim["attributes"]["COLOR_0"] = len(g["accessors"]) - 1
        added += col.nbytes
    if ao_stat:
        a = np.concatenate(ao_stat)
        print(f"  AO: grid {tuple(int(d) for d in dims)} @ {voxel:g}u, "
              f"{filled:.0f} filled voxels | shade min/med/max "
              f"{a.min():.2f}/{np.median(a):.2f}/{a.max():.2f}")

    g["buffers"][0]["byteLength"] = len(blob)
    g["materials"] = [{
        "name": "map_surfaces",
        "pbrMetallicRoughness": {
            "baseColorFactor": [round(float(c), 5) for c in base] + [1.0],
            "metallicFactor": 0.0, "roughnessFactor": 0.9,
        },
    }]
    for prim in _iter_prims(g):
        prim["material"] = 0

    total = int(tri_hist.sum()) or 1
    share = "  ".join(f"{k} {100 * tri_hist[i] / total:.0f}%"
                      for i, k in enumerate(SURFACE_ORDER))
    print(f"surface colours: {len(prims)} prims, tiers at y={y_lo:.0f}/{y_hi:.0f}, "
          f"ao={ao:g} sun={sun:g} | {share} | +{added / 1e6:.1f} MB COLOR_0")
    return bytes(blob)


# ---------------------------------------------------------------------------
# GLB container I/O (used by --refine and pack_glb)
# ---------------------------------------------------------------------------

def load_glb(path):
    """(gltf_json, bin_bytes) from a binary glTF 2.0 container."""
    data = Path(path).read_bytes()
    if len(data) < 12:
        sys.exit(f"{path}: too small to be a GLB")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        sys.exit(f"{path}: not a GLB (bad magic)")
    g, bin_bytes, off = None, b"", 12
    while off + 8 <= min(length, len(data)):
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen + (-clen % 4)  # chunks are 4-byte aligned
        if ctype == 0x4E4F534A:    # JSON
            g = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:  # BIN
            bin_bytes = chunk
    if g is None:
        sys.exit(f"{path}: no JSON chunk found")
    return g, bin_bytes


def write_glb(path, g, blob):
    """Write glTF json + binary blob as a single .glb file."""
    blob = bytes(blob) + b"\0" * (-len(blob) % 4)
    json_bytes = json.dumps(g, separators=(",", ":")).encode()
    json_bytes += b" " * (-len(json_bytes) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))          # glTF magic, v2
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))    # JSON chunk
        f.write(json_bytes)
        f.write(struct.pack("<II", len(blob), 0x004E4942))          # BIN chunk
        f.write(blob)


def refine_main():
    """--refine mode: apply ONLY merge_and_chunk to an already-prepared .glb
    (orientation wrapper + flat material already baked by a previous run)."""
    ap = argparse.ArgumentParser(
        description="Re-optimize an already-prepared world.glb: bake node "
                    "transforms into vertices, weld, rebuild as u16-indexed "
                    "spatial chunks. Lossless by default.")
    ap.add_argument("--refine", type=Path, required=True, metavar="IN_GLB",
                    help="input .glb (a previous prepare_map.py output)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .glb (default: overwrite the input)")
    ap.add_argument("--grid", type=int, default=8,
                    help="chunk grid resolution over X/Z (default 8)")
    ap.add_argument("--weld-position-only", action="store_true",
                    help="weld on POSITION bits alone and recompute area-"
                         "weighted smooth normals (changes shading; default "
                         "is the lossless position+normal bit-exact weld)")
    args = ap.parse_args()

    g, bin_bytes = load_glb(args.refine)
    old_size = args.refine.stat().st_size
    new_bin = merge_and_chunk(g, bin_bytes, grid=args.grid,
                              weld_positions_only=args.weld_position_only)
    for b in g.get("buffers", []):
        b.pop("uri", None)  # GLB buffer 0 is uri-less
    out = args.out or args.refine
    write_glb(out, g, new_bin)
    print(f"refined: {args.refine} ({old_size/1e6:.1f} MB) -> "
          f"{out} ({out.stat().st_size/1e6:.1f} MB)")


def colorize_main():
    """--colorize mode: add flat per-surface colours to an already-prepared .glb
    (no CS2 install needed -- the colours come from the geometry, not from the
    map's real textures, which VRF cannot export while CS2 ships VCS 71)."""
    ap = argparse.ArgumentParser(
        description="Give an already-prepared world.glb flat per-surface "
                    "colours via a COLOR_0 attribute (floors by height tier, "
                    "walls, ceilings, ramps). Draw-call count is unchanged.")
    ap.add_argument("--colorize", type=Path, required=True, metavar="IN_GLB",
                    help="input .glb (a previous prepare_map.py output)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output .glb (default: overwrite the input)")
    ap.add_argument("--palette", type=Path, default=None,
                    help='JSON overriding SURFACE_PALETTE, e.g. {"wall":"#b3a48b"}. '
                         f"Classes: {', '.join(SURFACE_ORDER)}")
    ap.add_argument("--ao", type=float, default=0.55,
                    help="baked ambient-occlusion strength 0..1 (default 0.55; "
                         "0 disables). This is what makes corners, doorways and "
                         "corridors readable under the viewer's flat ambient.")
    ap.add_argument("--sun", type=float, default=0.38,
                    help="baked directional shading strength 0..1 (default 0.38; "
                         "0 disables) so perpendicular walls differ in brightness")
    ap.add_argument("--voxel", type=float, default=32.0,
                    help="AO occupancy grid resolution in world units "
                         "(default 32 = a player's width)")
    args = ap.parse_args()

    palette = json.loads(args.palette.read_text()) if args.palette else None
    if palette:
        unknown = set(palette) - set(SURFACE_ORDER)
        if unknown:
            sys.exit(f"--palette: unknown classes {sorted(unknown)}")
    g, bin_bytes = load_glb(args.colorize)
    old_size = args.colorize.stat().st_size
    new_bin = colorize_by_surface(g, bin_bytes, palette,
                                  ao=args.ao, sun=args.sun, voxel=args.voxel)
    for b in g.get("buffers", []):
        b.pop("uri", None)  # GLB buffer 0 is uri-less
    out = args.out or args.colorize
    write_glb(out, g, new_bin)
    print(f"colorized: {args.colorize} ({old_size/1e6:.1f} MB) -> "
          f"{out} ({out.stat().st_size/1e6:.1f} MB)")


def main():
    if any(a == "--refine" or a.startswith("--refine=") for a in sys.argv[1:]):
        return refine_main()
    if any(a == "--colorize" or a.startswith("--colorize=") for a in sys.argv[1:]):
        return colorize_main()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("raw_dir", type=Path, help="Source2Viewer export directory")
    ap.add_argument("map_name", help="e.g. de_mirage (output folder name)")
    ap.add_argument("--max-size", type=int, default=1024,
                    help="downscale textures larger than this (px, default 1024)")
    ap.add_argument("--keep-maps", action="store_true",
                    help="keep normal/MR/AO/emissive maps (default: strip)")
    ap.add_argument("--geometry-only", action="store_true",
                    help="strip ALL textures (low-fi fallback, tiny download)")
    ap.add_argument("--keep-overlays", action="store_true",
                    help="keep decal/overlay meshes (posters, signs, splats). "
                         "Default: drop them -- untextured they render as opaque "
                         "grey 'poster-walls' floating on the real walls.")
    ap.add_argument("--glb", action="store_true",
                    help="repack into a single world.glb")
    ap.add_argument("--slim-attrs", action="store_true",
                    help="drop vertex attributes the untextured viewer never uses "
                         "(TANGENT, extra TEXCOORDs, COLOR, JOINTS/WEIGHTS) and "
                         "rebuild the buffer -- large size cut, geometry unchanged")
    ap.add_argument("--color", default="#6f7378",
                    help="flat base colour (hex) for an untextured map -- one "
                         "material on every primitive so it reads as grey, not the "
                         "washed-out Bevy default white. Empty string disables.")
    ap.add_argument("--simplify", type=float, default=0.0, metavar="UNITS",
                    help="decimate by clustering vertices onto a UNITS grid "
                         "(0 = off). Wipes geometric micro-detail such as "
                         "de_ancient's modelled brickwork, which is invisible on "
                         "an untextured map; keep it well under the thinnest wall "
                         "(CS2 walls are >= 8 u), e.g. 5")
    ap.add_argument("--drop-foliage", action="store_true",
                    help="drop vegetation meshes (trees/leaves/ferns/...). Without "
                         "textures they render as solid view-blocking shields, and "
                         "on de_ancient they are ~15%% of all triangles")
    ap.add_argument("--surface-colors", action="store_true",
                    help="flat per-surface colours ('1x1 textures'): a COLOR_0 "
                         "vertex attribute picked from each triangle's facing "
                         "and height (floors/walls/ceilings/ramps), so an "
                         "untextured map reads as an architectural render "
                         "instead of one grey mass. Costs no extra draw calls.")
    ap.add_argument("--no-merge", action="store_true",
                    help="skip the merge+chunk pass (bake node transforms, "
                         "weld, rebuild as u16-indexed spatial chunks; the "
                         "pass is ON by default and lossless)")
    ap.add_argument("--grid", type=int, default=8,
                    help="merge pass: chunk grid resolution over X/Z (default 8)")
    ap.add_argument("--weld-position-only", action="store_true",
                    help="merge pass: weld on POSITION bits alone and recompute "
                         "area-weighted smooth normals (changes shading; default "
                         "is the lossless position+normal bit-exact weld)")
    ap.add_argument("--dev", action="store_true",
                    help="also mirror into rust/viewer/www/assets/maps/")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    args = ap.parse_args()

    src = find_world_gltf(args.raw_dir)
    print(f"world glTF: {src} ({src.stat().st_size/1e6:.1f} MB json)")
    g = load_gltf(src)

    # -- 1. sanitize: Bevy fails hard only on *required* unknown extensions
    dropped_ext = g.pop("extensionsRequired", None)
    if dropped_ext:
        print(f"dropped extensionsRequired: {dropped_ext}")

    # -- 1b. reorient/rescale the export into the viewer's (x,z,-y) raw-units contract
    orient_scene_to_contract(g)

    # -- 1c. drop decal/overlay meshes ("poster-walls") -- clutter without textures
    if not args.keep_overlays:
        n_ov_mesh, n_ov_prim = drop_overlays(g)
        print(f"overlays dropped: {n_ov_mesh} meshes, {n_ov_prim} primitives")

    # -- 1d. drop game-mode barriers (retake/wingman walls) -- absent in comp play
    n_mb_mesh, n_mb_prim = drop_mode_barriers(g)
    if n_mb_mesh:
        print(f"mode barriers dropped: {n_mb_mesh} meshes, {n_mb_prim} primitives")

    if args.drop_foliage:
        n_fo_mesh, n_fo_prim = drop_foliage(g)
        print(f"foliage dropped: {n_fo_mesh} meshes, {n_fo_prim} primitives")

    # -- 2. drop hidden-material primitives (tool brushes export as opaque sheets)
    hidden = {i for i, m in enumerate(g.get("materials", []))
              if is_hidden_material(m.get("name"), m.get("extras"))}
    dropped_prims = 0
    for mesh in g.get("meshes", []):
        kept = []
        for prim in mesh.get("primitives", []):
            if prim.get("material") in hidden:
                dropped_prims += 1
            else:
                kept.append(prim)
        # glTF requires >=1 primitive; keep a degenerate one only if all dropped
        mesh["primitives"] = kept or mesh["primitives"][:1]
    print(f"hidden materials: {len(hidden)}, primitives dropped: {dropped_prims}")

    # -- 3./4. strip texture slots
    for mat in g.get("materials", []):
        if not args.keep_maps or args.geometry_only:
            mat.pop("normalTexture", None)
            mat.pop("occlusionTexture", None)
            mat.pop("emissiveTexture", None)
            mat.get("pbrMetallicRoughness", {}).pop("metallicRoughnessTexture", None)
        if args.geometry_only:
            mat.get("pbrMetallicRoughness", {}).pop("baseColorTexture", None)

    # -- 4b. flat base colour for an untextured map (grey, not Bevy-default white)
    if args.color:
        apply_flat_color(g, args.color)

    # -- 5. garbage-collect + copy to the output layout
    used_uris = gc_unused(g)
    out_dir = args.out_root / args.map_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # buffers (.bin): slim+compact when asked, else keep verbatim
    if args.slim_attrs:
        keep = {"POSITION", "NORMAL"}
        # keep UV0 only if a surviving material still samples a base-color texture
        if any(m.get("pbrMetallicRoughness", {}).get("baseColorTexture")
               for m in g.get("materials", [])):
            keep.add("TEXCOORD_0")
        bin_uri = next(b["uri"] for b in g["buffers"] if b.get("uri"))
        newbin, dropped = slim_and_compact(g, (src.parent / bin_uri).read_bytes(), keep)
        (out_dir / bin_uri).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / bin_uri).write_bytes(newbin)
        print(f"slim-attrs: kept {sorted(keep)}, dropped {sorted(dropped)} -> "
              f"buffer {len(newbin) / 1e6:.1f} MB")
    else:
        for buf in g.get("buffers", []):
            uri = buf.get("uri")
            if uri and not uri.startswith("data:"):
                dst = out_dir / uri
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src.parent / uri, dst)

    # -- 5b. merge + chunk: bake transforms, weld, rebuild as u16 chunks,
    #        then (optionally) flat per-surface colours on top of the chunks
    if not args.no_merge or args.surface_colors:
        bin_uris = [b["uri"] for b in g.get("buffers", [])
                    if b.get("uri") and not b["uri"].startswith("data:")]
        if len(bin_uris) == 1:
            src_bin = (out_dir / bin_uris[0]).read_bytes()
            if not args.no_merge:
                src_bin = merge_and_chunk(g, src_bin, grid=args.grid,
                                          weld_positions_only=args.weld_position_only,
                                          simplify=args.simplify)
            if args.surface_colors:
                src_bin = colorize_by_surface(g, src_bin)
            (out_dir / bin_uris[0]).write_bytes(src_bin)
        else:
            print("  merge skipped: not a single external-buffer glTF")

    # referenced textures: copy + downscale
    n_scaled = 0
    for uri in sorted(used_uris):
        if uri.startswith("data:"):
            continue  # embedded in the json; nothing to copy
        s = src.parent / uri
        if not s.is_file():
            print(f"  WARNING: missing texture {uri}")
            continue
        dst = out_dir / uri
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, dst)
        try:
            with Image.open(dst) as im:
                if max(im.size) > args.max_size:
                    im.thumbnail((args.max_size, args.max_size))
                    im.save(dst)
                    n_scaled += 1
        except Exception as exc:  # noqa: BLE001 -- non-image or corrupt: keep as-is
            print(f"  WARNING: could not downscale {uri}: {exc}")
    print(f"textures kept: {len(used_uris)}, downscaled: {n_scaled}")

    out_gltf = out_dir / "world.gltf"
    with open(out_gltf, "w", encoding="utf-8") as f:
        json.dump(g, f, separators=(",", ":"))

    if args.glb:
        pack_glb(out_dir, g)
        out_gltf.unlink()
        # external files were embedded; remove them
        for p in sorted(out_dir.rglob("*")):
            if p.is_file() and p.name != "world.glb":
                p.unlink()
        for p in sorted(out_dir.rglob("*"), reverse=True):
            if p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass

    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print(f"output: {out_dir}  ({total/1e6:.1f} MB total)")

    if args.dev:
        dev_dir = DEV_OUT_ROOT / args.map_name
        if dev_dir.exists():
            shutil.rmtree(dev_dir)
        shutil.copytree(out_dir, dev_dir)
        print(f"dev mirror: {dev_dir}")


def _uri_bytes(out_dir, uri):
    """Bytes behind a glTF uri: base64 data: URI or a file relative to out_dir."""
    if uri.startswith("data:"):
        import base64
        return base64.b64decode(uri.split(",", 1)[1])
    return (out_dir / uri).read_bytes()


def pack_glb(out_dir, g):
    """Embed every buffer/image into one world.glb (binary glTF 2.0)."""
    blob = bytearray()

    def add_chunk(data):
        offset = len(blob)
        blob.extend(data)
        blob.extend(b"\0" * (-len(blob) % 4))
        return offset, len(data)

    buffer_views = g.setdefault("bufferViews", [])
    # existing buffer 0 first, so accessors' bufferView offsets stay valid
    uris = [b["uri"] for b in g.get("buffers", []) if "uri" in b]
    if len(uris) != 1:
        sys.exit(f"--glb expects exactly one buffer with a uri, got {len(uris)}")
    off, size = add_chunk(_uri_bytes(out_dir, uris[0]))
    assert off == 0
    g["buffers"] = [{"byteLength": size}]

    # images -> bufferViews into the same buffer
    for img in g.get("images", []):
        uri = img.pop("uri", None)
        if uri is None:
            continue
        off, size = add_chunk(_uri_bytes(out_dir, uri))
        img["bufferView"] = len(buffer_views)
        img["mimeType"] = "image/png" if uri.lower().endswith(".png") else "image/jpeg"
        buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": size})
    g["buffers"][0]["byteLength"] = len(blob)

    write_glb(out_dir / "world.glb", g, blob)


if __name__ == "__main__":
    main()
