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
     of hundreds through uvicorn).

Usage:
    python tools/prepare_map.py <raw_export_dir> <map_name> [--max-size 1024]
        [--keep-maps] [--geometry-only] [--glb] [--dev] [--out-root DIR]

Requires: pip install pygltflib Pillow
"""

import argparse
import json
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
    """A glTF node's local transform as a row-major 4x4 numpy matrix."""
    if "matrix" in n:
        return np.array(n["matrix"], float).reshape(4, 4).T  # glTF is column-major
    m = np.eye(4)
    if "scale" in n:
        m = m @ np.diag(list(n["scale"]) + [1.0])
    if "rotation" in n:
        x, y, z, w = n["rotation"]
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        t = np.eye(4); t[:3, :3] = r; m = m @ t
    if "translation" in n:
        t = np.eye(4); t[:3, 3] = n["translation"]; m = t @ m
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
    # The global axis+scale convention is the transform shared by the bulk of the
    # world roots; individual props may carry their own placement on top, but they
    # live in the SAME exported frame (M_common . Source), so one global re-frame
    # corrects the whole scene. Pick the dominant matrix as M_common.
    keys = Counter(np.round(m, 6).tobytes() for m in mats)
    common_key, count = keys.most_common(1)[0]
    if count < 0.5 * len(mats):
        print(f"  orient: no dominant world transform ({count}/{len(mats)}) -- skipping")
        return
    m0 = next(m for m in mats if np.round(m, 6).tobytes() == common_key)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("raw_dir", type=Path, help="Source2Viewer export directory")
    ap.add_argument("map_name", help="e.g. de_mirage (output folder name)")
    ap.add_argument("--max-size", type=int, default=1024,
                    help="downscale textures larger than this (px, default 1024)")
    ap.add_argument("--keep-maps", action="store_true",
                    help="keep normal/MR/AO/emissive maps (default: strip)")
    ap.add_argument("--geometry-only", action="store_true",
                    help="strip ALL textures (low-fi fallback, tiny download)")
    ap.add_argument("--glb", action="store_true",
                    help="repack into a single world.glb")
    ap.add_argument("--slim-attrs", action="store_true",
                    help="drop vertex attributes the untextured viewer never uses "
                         "(TANGENT, extra TEXCOORDs, COLOR, JOINTS/WEIGHTS) and "
                         "rebuild the buffer -- large size cut, geometry unchanged")
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

    json_bytes = json.dumps(g, separators=(",", ":")).encode()
    json_bytes += b" " * (-len(json_bytes) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(blob)
    with open(out_dir / "world.glb", "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))          # glTF magic, v2
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))    # JSON chunk
        f.write(json_bytes)
        f.write(struct.pack("<II", len(blob), 0x004E4942))          # BIN chunk
        f.write(blob)


if __name__ == "__main__":
    main()
