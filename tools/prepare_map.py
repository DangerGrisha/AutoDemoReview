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

    # buffers (.bin) are always kept
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
