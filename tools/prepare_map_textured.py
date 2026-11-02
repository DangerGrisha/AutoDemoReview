"""Build the textured, lightmapped CS2 map for the 3D replay viewer.

Input: an export made by tools/convert_maps.ps1 -Textured (Source2Viewer 20.x):

    raw/<map>_v2/maps/<map>/world.gltf + world.bin + *.png    geometry, materials
    raw/<map>_v2/irradiance.exr, direct_light_shadows.png     baked light
    raw/<map>_v2/sky.exr                                      sky, equirectangular
    raw/<map>_v2/world.txt, ents.txt                          text dumps

Output: web/static/replay3d/assets/maps_v2/<map>/ (gitignored, never committed)

    world.c2map          header JSON + geometry + light grid, see write_world()
    tex/<hash>.ktx2      colour textures, block compressed
    lightmap.ktx2        irradiance in RGB, sun visibility in A
    sky.ktx2             the map's sky

Why these formats, none of which the code alone explains:

* BC in KTX2: the wasm build has one thread. 340 JPEGs would decode on it and
  take about 600 MB of video memory, block data needs no decoding at all.
* One lightmap holding both: one texture bind per draw instead of two. BC3's
  alpha block has the same quality as a separate BC4 would.
* Irradiance is stored as sqrt(x / range): the file holds linear light up to
  about 16, with the median at 0.38 and the 99th percentile at 1.05. Eight bits
  of linear would band badly in the dark rooms.
* Compact vertices (32 bytes against 56 from glTF) and our own file instead of
  glb: bevy_gltf expands colours to four floats, and a glb would make the loader
  build materials and scenes that the viewer then throws away.

Run through tools/convert_maps.ps1 -Textured, not by hand. Requires numpy,
Pillow and etcpak (pip install etcpak).
"""

import argparse
import hashlib
import json
import re
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

import prepare_map as pm

Image.MAX_IMAGE_PIXELS = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_ROOT = REPO_ROOT / "web" / "static" / "replay3d" / "assets" / "maps_v2"

# Bump when the file layout in write_world() changes. The viewer refuses other
# versions and falls back to the plain map.
FORMAT_VERSION = 1
MAGIC = b"C2MAP2\0\0"

# Irradiance above this clips. 99.9 % of de_mirage's atlas is under 2.9.
LIGHT_RANGE = 4.0
# How far the bounds may be from the plain map's before the frame is called wrong.
FRAME_TOLERANCE = 1500.0
FRAME_MIN_SHARE = 0.8
FRAME_MIN_SPREAD = 0.05
# Stored lightmap size. The source is 8192, which would be 64 MB of video memory.
LIGHTMAP_SIZE = 4096

# A chunk stays under this many vertices (16-bit indices, and the attribute cost
# is per vertex) and under this size in world units, so the frustum can cull it.
MAX_CHUNK_VERTS = 65000
DEFAULT_CHUNK_SIZE = 2000.0
MIN_SPLIT_TRIANGLES = 4000

# Shaders that never draw anything visible in a match.
SKIP_SHADERS = {"csgo_effects.vfx", "csgo_black_unlit.vfx", "generic.vfx"}
# These write their colour directly, without any light.
UNLIT_SHADERS = {"csgo_unlitgeneric.vfx"}

# Light grid for players and for props without a lightmap.
GRID_CELL = np.array([64.0, 96.0, 64.0])
# Which surfaces have nothing above them, per column of this many units...
COLUMN_CELL = 64.0
# ...counting anything within this distance of the top as out in the open.
OPEN_SKY_MARGIN = 48.0
# Surfaces that face the sun must have this many times the atlas's average sun.
LIGHTMAP_FIT = 1.5
# A sun dimmer than this lights nothing.
NO_SUN = 0.01
# What de_mirage uses, for an export that did not save them.
DEFAULT_EXPOSURE = 0.9
DEFAULT_WHITE_POINT = 1.5
# Valve's filmic curve, in the order the viewer takes it. These numbers make it a straight
# line, which is what a map without a post-processing file gets.
TONE_CURVE_KEYS = ("m_flShoulderStrength", "m_flLinearStrength", "m_flLinearAngle",
                   "m_flToeStrength", "m_flToeNum", "m_flToeDenom")
DEFAULT_TONE_CURVE = (0.0, 0.001, 0.001, 1.0, 1.0, 1.0)
# A lightmap UV a little off the atlas is rounding. Further off, it is not a lightmap UV.
LIGHTMAP_UV_SLACK = 0.01

# Source2Viewer 20 writes vertices as 0.0254 * (Sy, Sz, Sx) under identity nodes,
# and the viewer's contract is (Sx, Sz, -Sy) in Hammer units. Version 19 kept the
# same frame in the root node's matrix, which is what prepare_map.py reads, and
# reading it here gives a map 39 times too small and on its side.
METRES_PER_UNIT = 0.0254
EXPORT_TO_VIEWER = np.array([[0, 0, 1 / METRES_PER_UNIT, 0],
                             [0, 1 / METRES_PER_UNIT, 0, 0],
                             [-1 / METRES_PER_UNIT, 0, 0, 0],
                             [0, 0, 0, 1]], float)


# ---------------------------------------------------------------------------
# EXR: only what Source2Viewer writes (ZIP, 32-bit float scanlines)
# ---------------------------------------------------------------------------


def _exr_header(data):
    """(channel names, width, height, offset of the block table)."""
    if data[:4] != b"\x76\x2f\x31\x01":
        raise ValueError("not an EXR file")
    pos = 8
    chans, comp, win = [], None, None

    def cstr(p):
        e = data.index(b"\0", p)
        return data[p:e].decode(), e + 1

    while data[pos] != 0:
        name, pos = cstr(pos)
        typ, pos = cstr(pos)
        n = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        val = data[pos:pos + n]
        pos += n
        if typ == "chlist":
            i = 0
            while val[i] != 0:
                e = val.index(b"\0", i)
                chans.append((val[i:e].decode(), struct.unpack_from("<i", val, e + 1)[0]))
                i = e + 1 + 16
        elif name == "compression":
            comp = val[0]
        elif name == "dataWindow":
            win = struct.unpack("<4i", val)
    if comp != 3:
        raise ValueError(f"EXR compression {comp}: only ZIP (3) is handled")
    if any(pixel_type != 2 for _, pixel_type in chans):
        raise ValueError(f"EXR channels {chans}: only 32-bit float is handled")
    return [c for c, _ in chans], win[2] - win[0] + 1, win[3] - win[1] + 1, pos + 1


def _exr_block(raw, lines, chans, w):
    """Undo EXR's ZIP filters: a byte delta, then two interleaved halves."""
    t = np.frombuffer(zlib.decompress(raw), np.uint8).astype(np.int16)
    d = (np.cumsum(np.concatenate((t[:1], t[1:] - 128))) & 255).astype(np.uint8)
    mid = (len(d) + 1) // 2
    un = np.empty_like(d)
    un[0::2] = d[:mid]
    un[1::2] = d[mid:]
    return np.frombuffer(un.tobytes(), "<f4").reshape(lines, chans, w)


def read_exr_rgb(path, shrink=1):
    """An EXR as float32 RGB, box-averaged down by `shrink` (1 or 2). Lines are
    decoded block by block, so a 8192 atlas never needs its full 1 GB in memory."""
    data = Path(path).read_bytes()
    names, w, h, table = _exr_header(data)
    blocks = (h + 15) // 16
    offsets = struct.unpack_from(f"<{blocks}Q", data, table)
    res = np.zeros((h // shrink, w // shrink, 3), np.float32)
    want = [names.index(c) for c in "RGB"]

    def one(k):
        off = offsets[k]
        y, size = struct.unpack_from("<iI", data, off)
        lines = min(y + 16, h) - y
        arr = _exr_block(data[off + 8:off + 8 + size], lines, len(names), w)
        if shrink == 2:
            even = lines // 2 * 2
            a = arr[:even]
            # the black between charts stays out of the average, or every chart gets a dark rim
            solid = (a.max(1, keepdims=True) > 0).astype(np.float32)
            a = a * solid
            a = a[0::2] + a[1::2]
            solid = solid[0::2] + solid[1::2]
            a = a[:, :, 0:w // 2 * 2:2] + a[:, :, 1:w // 2 * 2:2]
            solid = solid[:, :, 0:w // 2 * 2:2] + solid[:, :, 1:w // 2 * 2:2]
            a = a / np.maximum(solid, 1.0)
        else:
            a = arr
        for out_c, in_c in enumerate(want):
            res[y // shrink:y // shrink + a.shape[0], :, out_c] = a[:, in_c]

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, range(blocks)))
    return res


# ---------------------------------------------------------------------------
# KTX2 + BC
# ---------------------------------------------------------------------------

# vkFormat, block bytes
BC1_SRGB = (132, 8)
BC3_SRGB = (138, 16)
BC3_UNORM = (137, 16)
BC1_UNORM = (131, 8)

_KTX2_MAGIC = bytes([0xAB, 0x4B, 0x54, 0x58, 0x20, 0x32, 0x30, 0xBB, 0x0D, 0x0A, 0x1A, 0x0A])


def write_ktx2(path, fmt, width, height, levels):
    """A minimal KTX2 file: `levels` are the block data, largest first. No
    supercompression, because the server's Brotli does that better and the wasm
    would otherwise have to decompress on its only thread."""
    vk_format, block_bytes = fmt
    srgb = vk_format in (BC1_SRGB[0], BC3_SRGB[0])
    has_alpha = block_bytes == 16
    # Data Format Descriptor, Khronos basic block for one BC plane
    color_model = 130 if has_alpha else 128  # BC3 / BC1A
    transfer = 2 if srgb else 1
    # (channel, bit offset, bit count): BC3 is an alpha block, then a colour block
    samples = [(15, 0, 64), (0, 64, 64)] if has_alpha else [(0, 0, 64)]
    block_size = 24 + 16 * len(samples)
    dfd = struct.pack("<I", 4 + block_size)
    dfd += struct.pack("<IHH", 0, 2, block_size)  # vendor + type, version, size
    dfd += bytes([color_model, 1, transfer, 0])  # model, primaries BT709, transfer, flags
    dfd += bytes([3, 3, 0, 0])  # 4x4 block
    dfd += bytes([block_bytes, 0, 0, 0, 0, 0, 0, 0])
    for channel, bit_offset, bits in samples:
        dfd += struct.pack("<HBB", bit_offset, bits - 1, channel)
        dfd += bytes(4)
        dfd += struct.pack("<II", 0, 0xFFFFFFFF)

    n = len(levels)
    # identifier (12) + header (36) + index (32) = 80, then the level index
    dfd_at = 80 + 24 * n
    data_at = dfd_at + len(dfd)
    # levels are stored smallest first and aligned to the block size
    entries = []
    pos = data_at
    for lv in reversed(levels):
        pos += (-pos) % block_bytes
        entries.append((pos, len(lv)))
        pos += len(lv)
    entries.reverse()

    with open(path, "wb") as f:
        f.write(_KTX2_MAGIC)
        f.write(struct.pack("<9I", vk_format, 1, width, height, 0, 0, 1, n, 0))
        f.write(struct.pack("<IIII", dfd_at, len(dfd), 0, 0))
        f.write(struct.pack("<QQ", 0, 0))
        for off, size in entries:
            f.write(struct.pack("<QQQ", off, size, size))
        f.write(dfd)
        for lv, (off, _) in zip(reversed(levels), reversed(entries)):
            f.write(bytes(off - f.tell()))
            f.write(lv)


def _srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _half(a):
    """Box-filter a float image to half size on each axis that can still halve."""
    h, w = a.shape[:2]
    if h >= 2:
        a = (a[0:h // 2 * 2:2] + a[1:h // 2 * 2:2]) * 0.5
    if w >= 2:
        a = (a[:, 0:w // 2 * 2:2] + a[:, 1:w // 2 * 2:2]) * 0.5
    return a


def _pot(n):
    return max(4, 1 << round(np.log2(max(n, 1))))


def _keep_coverage(alpha, cutoff, target):
    """Scale `alpha` so the share of pixels over `cutoff` stays `target`. Without
    this, cut-out leaves and fences thin out with distance until they vanish."""
    if target <= 0.0 or alpha.size == 0:
        return alpha
    lo, hi = 0.0, 8.0
    for _ in range(10):
        s = (lo + hi) / 2
        if float((alpha * s >= cutoff).mean()) < target:
            lo = s
        else:
            hi = s
    return np.clip(alpha * hi, 0.0, 1.0)


def _encode_rgba8(rgba, fmt):
    """`rgba` is a uint8 (h, w, 4) array whose sides are multiples of 4."""
    import etcpak

    h, w = rgba.shape[:2]
    data = rgba.tobytes()
    if fmt[1] == 8:
        return etcpak.compress_bc1(data, w, h)
    return etcpak.compress_bc3(data, w, h)


def encode_texture(path, out_dir, max_size, use_alpha, cutoff=0.5, linear=False):
    """PNG -> KTX2 with mips, named by the content's hash so textures that CS2
    stores twice are stored once. Returns (relative name, mean linear colour)."""
    with Image.open(path) as im:
        im = im.convert("RGBA")
        w, h = im.size
        tw = min(_pot(w), max_size)
        th = min(_pot(h), max_size)
        if (tw, th) != (w, h):
            im = im.resize((tw, th), Image.LANCZOS)
        rgba = np.asarray(im, np.uint8)

    has_alpha = use_alpha and int(rgba[..., 3].min()) < 255
    if linear:
        fmt = BC3_UNORM if has_alpha else BC1_UNORM
    else:
        fmt = BC3_SRGB if has_alpha else BC1_SRGB

    rgb = rgba[..., :3].astype(np.float32) / 255.0
    if not linear:
        rgb = _srgb_to_linear(rgb)
    alpha = rgba[..., 3].astype(np.float32) / 255.0
    mean = rgb.reshape(-1, 3).mean(0)
    coverage = float((alpha >= cutoff).mean()) if has_alpha else 0.0

    levels = []
    level0 = rgba if has_alpha else np.dstack([rgba[..., :3], np.full((th, tw), 255, np.uint8)])
    levels.append(_encode_rgba8(level0, fmt))
    while min(rgb.shape[:2]) > 4:
        rgb = _half(rgb)
        alpha_h = _half(alpha[..., None])[..., 0]
        alpha = alpha_h
        out_rgb = rgb if linear else _linear_to_srgb(rgb)
        a = _keep_coverage(alpha, cutoff, coverage) if has_alpha else np.ones_like(alpha)
        img = np.dstack([(np.clip(out_rgb, 0, 1) * 255 + 0.5).astype(np.uint8), (a * 255 + 0.5).astype(np.uint8)])
        levels.append(_encode_rgba8(img, fmt))

    digest = hashlib.sha1(b"".join(levels[:1]) + bytes([fmt[0]])).hexdigest()[:16]
    name = f"tex/{digest}.ktx2"
    target = out_dir / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        write_ktx2(target, fmt, tw, th, levels)
    return name, mean.tolist()


# ---------------------------------------------------------------------------
# Baked light
# ---------------------------------------------------------------------------


def dilate_charts(rgb, vis, passes=2):
    """Pull chart edges out over the empty texels between charts, so that the filtering
    of a texture sampled near an edge finds more of the same light and not black."""
    for _ in range(passes):
        solid = rgb.max(2) > 0
        total, seen, count = np.zeros_like(rgb), np.zeros_like(vis), np.zeros_like(vis)
        for axis in (0, 1):
            for step in (1, -1):
                total += np.roll(rgb * solid[..., None], step, axis)
                seen += np.roll(vis * solid, step, axis)
                count += np.roll(solid, step, axis)
        fill = ~solid & (count > 0)
        rgb[fill] = total[fill] / count[fill][:, None]
        vis[fill] = seen[fill] / count[fill]
    return rgb, vis


def sun_shadow_channel(raw):
    """Which channel of direct_light_shadows holds the sun. A map bakes up to four lights
    there, and light_environment names its own. de_mirage has the same thing in the first
    three, so mixing them works there and nowhere else."""
    ents = raw / "ents.txt"
    text = ents.read_text(encoding="utf-8", errors="replace") if ents.exists() else ""
    at = text.find('classname = "light_environment"')
    if at < 0:
        return 0
    block = text[text.rfind("keyValues3Data", 0, at):at]
    m = re.search(r'bakedshadowindex\s*=\s*"?(\d+)', block)
    return int(m.group(1)) if m else 0


def read_visibility(path, channel, size):
    """How much sun each texel gets. The file stores the shadow: white is in shadow."""
    with Image.open(path) as im:
        bands = im.getbands()
        sh = im.getchannel(bands[channel] if channel < len(bands) else bands[0])
        if sh.size != size:
            sh = sh.resize(size, Image.BOX)
        return 1.0 - np.asarray(sh, np.float32) / 255.0


def build_lightmap(raw, out_dir):
    """Irradiance + sun visibility into one BC3 atlas. Returns the arrays the
    rest of the bake samples: (irradiance float32, sun visibility float32)."""
    ir_path = raw / "irradiance.exr"
    with open(ir_path, "rb") as f:
        _, w, _, _ = _exr_header(f.read(4096))
    shrink = 2 if w > LIGHTMAP_SIZE else 1
    irradiance = read_exr_rgb(ir_path, shrink)
    h, w = irradiance.shape[:2]

    channel = sun_shadow_channel(raw)
    print(f"  the sun's shadow is in channel {channel}")
    visibility = read_visibility(raw / "direct_light_shadows.png", channel, (w, h))
    irradiance, visibility = dilate_charts(irradiance, visibility)

    def pack(rgb, vis):
        code = np.sqrt(np.clip(rgb / LIGHT_RANGE, 0.0, 1.0))
        return np.dstack([(code * 255 + 0.5).astype(np.uint8), (vis * 255 + 0.5).astype(np.uint8)])

    levels = [_encode_rgba8(pack(irradiance, visibility), BC3_UNORM)]
    # a short chain: charts are packed tight, and past this they bleed into each other
    rgb_m, vis_m = irradiance, visibility
    for _ in range(3):
        rgb_m, vis_m = _half(rgb_m), _half(vis_m[..., None])[..., 0]
        levels.append(_encode_rgba8(pack(rgb_m, vis_m), BC3_UNORM))
    write_ktx2(out_dir / "lightmap.ktx2", BC3_UNORM, w, h, levels)
    return irradiance, visibility


def build_sky(raw, out_dir):
    """The sky is already equirectangular in the export. Display-referred 8 bits
    are enough: it is only ever looked at, never lit by."""
    path = raw / "sky.exr"
    if not path.exists():
        return None
    rgb = read_exr_rgb(path, 1)
    h, w = rgb.shape[:2]
    tw, th = min(_pot(w), 2048), min(_pot(h), 1024)
    img = Image.fromarray((_linear_to_srgb(rgb) * 255 + 0.5).astype(np.uint8)).resize((tw, th), Image.LANCZOS)
    a = np.dstack([np.asarray(img), np.full((th, tw), 255, np.uint8)])
    levels = [_encode_rgba8(a, BC1_SRGB)]
    lin = _srgb_to_linear(a[..., :3].astype(np.float32) / 255.0)
    while min(lin.shape[:2]) > 4 and len(levels) < 6:
        lin = _half(lin)
        hh, ww = lin.shape[:2]
        levels.append(_encode_rgba8(np.dstack([(_linear_to_srgb(lin) * 255 + 0.5).astype(np.uint8), np.full((hh, ww), 255, np.uint8)]), BC1_SRGB))
    write_ktx2(out_dir / "sky.ktx2", BC1_SRGB, tw, th, levels)
    return "sky.ktx2"


def sample_lightmap(irradiance, visibility, uv):
    """Nearest texel per vertex: enough for a coarse grid, no filtering across charts."""
    h, w = irradiance.shape[:2]
    x = np.clip((uv[:, 0] * w).astype(np.int64), 0, w - 1)
    y = np.clip((uv[:, 1] * h).astype(np.int64), 0, h - 1)
    return irradiance[y, x], visibility[y, x]


def uv_scale_from_dump(text):
    """m_vLightmapUvScale of the lighting block that actually has lightmaps. The
    dump also holds an unused block with 1.0 and m_bHasLightmaps = false."""
    block = re.search(r"m_worldLightingInfo\s*=\s*\{(.*?)m_lightMaps", text, re.S)
    scope = block.group(1) if block else text
    m = re.search(r"m_vLightmapUvScale\s*=\s*\[\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\]", scope)
    if not m:
        raise ValueError("no m_vLightmapUvScale in the world dump")
    return float(m.group(1)), float(m.group(2))


# ---------------------------------------------------------------------------
# Scene
# ---------------------------------------------------------------------------


def read_tone(raw):
    """The map's exposure and tone curve, from what the export saved of its entities and
    its post-processing file. CS2 adapts between the two exposures; a replay jumps
    between rooms and streets too quickly for that to mean much, so the middle it is."""
    ents = raw / "ents.txt"
    post = raw / "post.txt"
    text = ents.read_text(encoding="utf-8", errors="replace") if ents.exists() else ""
    lo = re.search(r"minexposure\s*=\s*([-\d.]+)", text)
    hi = re.search(r"maxexposure\s*=\s*([-\d.]+)", text)
    exposure = (float(lo.group(1)) + float(hi.group(1))) / 2 if lo and hi else DEFAULT_EXPOSURE
    text = post.read_text(encoding="utf-8", errors="replace") if post.exists() else ""

    def post_value(key, fallback):
        m = re.search(key + r"\s*=\s*([-+\d.eE]+)", text)
        return float(m.group(1)) if m else fallback

    if not (lo and hi) or "m_flWhitePoint" not in text:
        print("  no exposure or tone curve in the export, using the defaults")
    return {
        "exposure": exposure * 2.0 ** post_value("m_flExposureBias", 0.0),
        "white": post_value("m_flWhitePoint", DEFAULT_WHITE_POINT),
        "curve": [post_value(k, d) for k, d in zip(TONE_CURVE_KEYS, DEFAULT_TONE_CURVE)],
    }


def find_sun(g):
    """Direction towards the sun in viewer space, and its linear colour times its
    brightness. Valve's brightness is exported as candela, 683 per unit."""
    ext = g.get("extensions", {}).get("KHR_lights_punctual", {}).get("lights", [])
    for mesh_or_none, world, node in _all_nodes(g):
        light = node.get("extensions", {}).get("KHR_lights_punctual", {}).get("light")
        if light is None:
            continue
        rot = world[:3, :3]
        # a glTF light shines along its local -Z, so +Z points back at the sun
        to_sun = rot @ np.array([0.0, 0.0, 1.0])
        to_sun = to_sun / np.linalg.norm(to_sun)
        src = ext[light]
        return to_sun.tolist(), (np.array(src["color"]) * src["intensity"] / 683.0).tolist()
    return [0.45, 0.78, 0.42], [3.0, 2.2, 1.35]


def _all_nodes(g):
    nodes = g.get("nodes", [])
    scenes = g.get("scenes", [])
    if not scenes:
        return
    # reversed: the stack pops last-in first, and the bake should not depend on that
    stack = [(i, EXPORT_TO_VIEWER) for i in reversed(scenes[g.get("scene", 0)].get("nodes", []))]
    while stack:
        ni, parent = stack.pop()
        n = nodes[ni]
        world = parent @ pm._node_matrix(n)
        yield n.get("mesh"), world, n
        for c in reversed(n.get("children", [])):
            stack.append((c, world))


# Only static world geometry has a lightmap. Props placed as entities use the same UV
# slots for something else, so only the name tells them apart.
_WORLD_NODE = re.compile(r"^n\d+_lr\d+")


def texture_and_lightmap_sets(attrs, force_uv2, read):
    """Which UV sets a primitive's texture and lightmap read. The lightmap's is the last
    set: TEXCOORD_1 on most, TEXCOORD_2 where the material has a second UV of its own, as
    on over half of de_ancient. F_FORCE_UV2 moves the texture to the second set. A lightmap
    UV stays on the atlas and a material's own UV seldom does, which tells a primitive with
    no lightmap from one with."""
    texture = "TEXCOORD_1" if force_uv2 and "TEXCOORD_1" in attrs else "TEXCOORD_0"
    for key in ("TEXCOORD_2", "TEXCOORD_1"):
        if key in attrs and key != texture:
            values = read(attrs[key])
            if values.min() >= -LIGHTMAP_UV_SLACK and values.max() <= 1.0 + LIGHTMAP_UV_SLACK:
                return texture, key
    return texture, None


def material_info(mat):
    v = (mat.get("extras") or {}).get("vmat") or {}
    return v, v.get("TextureParams", {}), v.get("IntParams", {}), v.get("FloatParams", {}), v.get("VectorParams", {})


def is_skipped(mat):
    v, _, _, _, _ = material_info(mat)
    if v.get("ShaderName") in SKIP_SHADERS:
        return True
    return pm.is_hidden_material(mat.get("name"), mat.get("extras"))


class Soup:
    """Everything drawn with one material, baked to world space."""

    __slots__ = ("pos", "nrm", "uv", "lm", "col", "tri", "count")

    def __init__(self):
        self.pos, self.nrm, self.uv, self.lm, self.col, self.tri = [], [], [], [], [], []
        self.count = 0


def gather(g, blob, uv_scale):
    """Walk all instances: bake transforms, keep only the vertices each primitive
    uses. Primitives index into huge shared pools (25.7 M vertices for 1.3 M
    triangles on de_mirage), so compacting them is what makes the rest feasible."""
    meshes = g["meshes"]
    mats = g["materials"]
    soups = {}
    cache = {}

    def acc(i):
        a = cache.get(i)
        if a is None:
            a = pm._read_accessor(g, blob, i)
            cache[i] = a
        return a

    stats = {"instances": 0, "dropped": 0, "lm_tris": 0, "tris": 0}
    for _, world, node in _all_nodes(g):
        mi = node.get("mesh")
        if mi is None:
            continue
        name = node.get("name") or ""
        if any(k in name.lower() for k in pm._MODE_MARKERS):
            stats["dropped"] += 1
            continue
        stats["instances"] += 1
        rot = world[:3, :3]
        trans = world[:3, 3]
        det = np.linalg.det(rot)
        normal_m = np.linalg.inv(rot).T
        in_world = bool(_WORLD_NODE.match(name))
        for prim in meshes[mi]["primitives"]:
            mat_i = prim.get("material")
            if mat_i is None or is_skipped(mats[mat_i]) or "indices" not in prim:
                continue
            attrs = prim["attributes"]
            if "POSITION" not in attrs or "TEXCOORD_0" not in attrs:
                continue
            idx = acc(prim["indices"]).astype(np.int64)
            if idx.size == 0:
                continue
            used, inv = np.unique(idx, return_inverse=True)
            tri = inv.reshape(-1, 3)
            if det < 0:
                tri = tri[:, [0, 2, 1]]

            pos = acc(attrs["POSITION"])[used].astype(np.float64) @ rot.T + trans
            if "NORMAL" in attrs:
                nrm = acc(attrs["NORMAL"])[used].astype(np.float64) @ normal_m.T
                nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
            else:
                nrm = np.tile([0.0, 1.0, 0.0], (len(used), 1))

            _, _, ints, _, _ = material_info(mats[mat_i])
            uv_key, lm_key = texture_and_lightmap_sets(attrs, ints.get("F_FORCE_UV2"), lambda i: acc(i)[used])
            uv = acc(attrs[uv_key])[used].astype(np.float32)

            col = np.zeros((len(used), 4), np.float32)
            if in_world and lm_key:
                lm = acc(attrs[lm_key])[used].astype(np.float64) * uv_scale
                col[:, 3] = 1.0  # A = 1: this vertex reads the lightmap
                stats["lm_tris"] += len(tri)
            else:
                lm = np.zeros((len(used), 2))
            # vertex-paint blend between a material's two layers
            if "_TEXCOORD_4" in attrs:
                col[:, 0] = np.clip(acc(attrs["_TEXCOORD_4"])[used][:, 0], 0.0, 1.0)

            s = soups.setdefault(mat_i, Soup())
            s.pos.append(pos.astype(np.float32))
            s.nrm.append(nrm.astype(np.float32))
            s.uv.append(uv)
            s.lm.append(lm.astype(np.float32))
            s.col.append(col)
            s.tri.append(tri + s.count)
            s.count += len(used)
            stats["tris"] += len(tri)

    for s in soups.values():
        s.pos = np.concatenate(s.pos)
        s.nrm = np.concatenate(s.nrm)
        s.uv = np.concatenate(s.uv)
        s.lm = np.concatenate(s.lm)
        s.col = np.concatenate(s.col)
        s.tri = np.concatenate(s.tri)
    print(f"  {stats['instances']} instances, {stats['dropped']} mode barriers dropped, "
          f"{stats['tris']} triangles, {stats['lm_tris']} with a lightmap, {len(soups)} materials")
    return soups


def split_triangles(pos, tri, max_size, max_verts):
    """Median-split triangles by the longest axis until each part is small enough.
    Returns a list of triangle index arrays. Works on centroids, so a triangle is
    in exactly one part."""
    centroids = pos[tri].mean(1)
    out = []
    stack = [np.arange(len(tri))]
    while stack:
        ids = stack.pop()
        verts = len(np.unique(tri[ids]))
        c = centroids[ids]
        lo, hi = c.min(0), c.max(0)
        extent = hi - lo
        # a small material scattered around the map stays in one piece: culling it could
        # save a few triangles, while every extra piece is a draw in every view of the map
        small_enough = extent.max() <= max_size or len(ids) < MIN_SPLIT_TRIANGLES
        if (verts <= max_verts and small_enough) or len(ids) < 2:
            out.append(ids)
            continue
        axis = int(np.argmax(extent))
        order = np.argsort(c[:, axis], kind="stable")
        mid = len(order) // 2
        stack.append(ids[order[:mid]])
        stack.append(ids[order[mid:]])
    return out


def _surface_points(pos, tri, cell):
    """Triangle corners plus interior points in proportion to area, so a roof made of
    two huge triangles still covers every column it spans."""
    v0, v1, v2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    k = np.clip(np.ceil(area / (cell * cell / 8)), 0, 20000).astype(np.int64)
    rep = np.repeat(np.arange(len(tri)), k)
    pts = [v0, v1, v2]
    if len(rep):
        rng = np.random.default_rng(0xC0FFEE)
        u, v = rng.random((2, len(rep), 1), dtype=np.float32)
        flip = (u + v) > 1
        u, v = np.where(flip, 1 - u, u), np.where(flip, 1 - v, v)
        t = tri[rep]
        pts.append(pos[t[:, 0]] * (1 - u - v) + pos[t[:, 1]] * u + pos[t[:, 2]] * v)
    return np.concatenate(pts)


def column_tops(soups, opaque):
    """The highest solid surface over each column of the map. Leaves do not count: a
    palm over a roof does not make that roof indoors."""
    points = [_surface_points(s.pos, s.tri, COLUMN_CELL) for mi, s in soups.items() if mi in opaque]
    points = np.concatenate(points) if points else np.zeros((1, 3), np.float32)
    lo = points[:, [0, 2]].min(0)
    dims = np.ceil((points[:, [0, 2]].max(0) - lo) / COLUMN_CELL).astype(np.int64) + 1
    top = np.full(tuple(dims), -np.inf, np.float64)
    ij = ((points[:, [0, 2]] - lo) / COLUMN_CELL).astype(np.int64)
    np.maximum.at(top, (ij[:, 0], ij[:, 1]), points[:, 1])
    return lo, dims, top


def faces_open_sky(pos, nrm, tops):
    """Is there nothing above the air in front of this surface? Stepping out along the
    normal first is what lets the outer wall of a building count, not only its roof."""
    lo, dims, top = tops
    p = pos + nrm * COLUMN_CELL * 1.5
    ij = np.clip(((p[:, [0, 2]] - lo) / COLUMN_CELL).astype(np.int64), 0, dims - 1)
    return p[:, 1] >= top[ij[:, 0], ij[:, 1]] - OPEN_SKY_MARGIN


def choose_lightmap_scale(soups, tops, irradiance, visibility, sun_dir, dump_scale):
    """Which scale puts the UVs on their charts? The map's file names one, and Source2Viewer
    20 has already applied it on export, so using it again sends every lookup to some other
    chart. That looks perfectly plausible and is random, so the data decides: of the
    surfaces that face the sun with open sky, a right mapping has far more in the sun than
    the atlas has on average. Raises when neither scale does."""
    to_sun = np.asarray(sun_dir, np.float32)
    points = []
    for s in soups.values():
        m = np.nonzero((s.col[:, 3] > 0.5) & (s.nrm @ to_sun > 0.5))[0]
        if len(m):
            points.append(s.lm[m[faces_open_sky(s.pos[m], s.nrm[m], tops)]])
    points = np.concatenate(points) if points else np.zeros((0, 2), np.float32)
    candidates = [np.array([1.0, 1.0]), np.asarray(dump_scale, np.float64)]
    fits = [float(sample_lightmap(irradiance, visibility, points * c)[1].mean()) if len(points) else 0.0
            for c in candidates]
    average = float(visibility.mean())
    best = int(np.argmax(fits))
    print(f"  sun on open surfaces: {fits[0]:.2f} with the UVs as exported, {fits[1]:.2f} scaled by "
          f"{np.round(candidates[1], 4).tolist()}, atlas average {average:.2f}")
    if fits[best] < average * LIGHTMAP_FIT:
        raise ValueError("the lightmap UVs fit neither scale: the shadows would land on the wrong surfaces")
    return candidates[best]


def build_light_grid(soups, irradiance, visibility, sun_dir, sun_rgb):
    """Coarse 3D table of the light falling on nearby floors. Players read it under
    their feet, props without a lightmap read it per vertex. Sampled from the
    lightmap itself so it can never disagree with what the floor shows."""
    pts, values = [], []
    up_sun = max(0.0, sun_dir[1])
    # no 1/pi: a Source 2 light's colour already carries it, see the note in map_material.wgsl
    sun_on_floor = np.array(sun_rgb) * up_sun
    for s in soups.values():
        lit = s.col[:, 3] > 0.5
        if not lit.any():
            continue
        tri = s.tri
        keep = lit[tri].all(1)
        tri = tri[keep]
        if not len(tri):
            continue
        v0, v1, v2 = s.pos[tri[:, 0]], s.pos[tri[:, 1]], s.pos[tri[:, 2]]
        n = np.cross(v1 - v0, v2 - v0)
        area = 0.5 * np.linalg.norm(n, axis=1)
        ny = n[:, 1] / np.maximum(2 * area, 1e-6)
        floor = ny > 0.5
        if not floor.any():
            continue
        tri = tri[floor]
        area = area[floor]
        # samples per triangle in proportion to its area, a few dozen at most
        k = np.clip(np.ceil(area / (GRID_CELL[0] ** 2 / 2)), 1, 64).astype(np.int64)
        rep = np.repeat(np.arange(len(tri)), k)
        rng = np.random.default_rng(0xC0FFEE)
        u, v = rng.random((2, len(rep), 1), dtype=np.float32)
        flip = (u + v) > 1
        u, v = np.where(flip, 1 - u, u), np.where(flip, 1 - v, v)
        t = tri[rep]
        w0 = 1 - u - v
        p = s.pos[t[:, 0]] * w0 + s.pos[t[:, 1]] * u + s.pos[t[:, 2]] * v
        uv = s.lm[t[:, 0]] * w0 + s.lm[t[:, 1]] * u + s.lm[t[:, 2]] * v
        irr, vis = sample_lightmap(irradiance, visibility, uv)
        pts.append(p)
        values.append(irr + vis[:, None] * sun_on_floor)
    if not pts:
        raise ValueError("no lit floors: the lightmap UVs did not match any world geometry")
    pts = np.concatenate(pts)
    values = np.concatenate(values)

    lo = pts.min(0) - GRID_CELL
    hi = pts.max(0) + GRID_CELL
    dims = np.ceil((hi - lo) / GRID_CELL).astype(np.int64)
    sums = np.zeros((*dims, 3), np.float64)
    hits = np.zeros(tuple(dims), np.float64)
    ijk = ((pts - lo) / GRID_CELL).astype(np.int64)
    np.add.at(sums, (ijk[:, 0], ijk[:, 1], ijk[:, 2]), values)
    np.add.at(hits, (ijk[:, 0], ijk[:, 1], ijk[:, 2]), 1.0)

    # grow values into empty cells: above a floor, then sideways, then anywhere,
    # so a player mid-jump or a tree top still finds the light below it
    filled = hits > 0
    grid = np.zeros_like(sums)
    grid[filled] = sums[filled] / hits[filled][:, None]
    for _ in range(dims[1]):
        above = np.zeros_like(filled)
        above[:, 1:, :] = filled[:, :-1, :] & ~filled[:, 1:, :]
        if not above.any():
            break
        grid[:, 1:, :][above[:, 1:, :]] = grid[:, :-1, :][above[:, 1:, :]]
        filled = filled | above
    for _ in range(int(dims.max())):
        if filled.all():
            break
        acc = np.zeros_like(grid)
        cnt = np.zeros(tuple(dims))
        for axis in range(3):
            for step in (1, -1):
                acc += np.roll(grid * filled[..., None], step, axis)
                cnt += np.roll(filled.astype(np.float64), step, axis)
        new = (~filled) & (cnt > 0)
        grid[new] = acc[new] / cnt[new][:, None]
        filled = filled | new
    return lo, dims, grid


def bake_props(soups, lo, dims, grid, sun_dir, sun_rgb):
    """Vertices without a lightmap get their full light into the vertex colour:
    the grid value, turned towards the sun by the vertex normal."""
    for s in soups.values():
        need = s.col[:, 3] < 0.5
        if not need.any():
            continue
        ijk = np.clip(((s.pos[need] - lo) / GRID_CELL).astype(np.int64), 0, dims - 1)
        base = grid[ijk[:, 0], ijk[:, 1], ijk[:, 2]]
        ndl = s.nrm[need] @ np.array(sun_dir, np.float32)
        # the grid holds floor light, so a vertex facing away from the sun keeps
        # roughly half of it: half-Lambert instead of a hard terminator
        wrap = np.clip(ndl * 0.5 + 0.5, 0.15, 1.0)[:, None]
        light = base * wrap
        s.col[need, :3] = np.sqrt(np.clip(light / LIGHT_RANGE, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_world(path, header, chunks, grid_bytes):
    """Layout, all little-endian:

        8 bytes   MAGIC
        u32       JSON length
        u32       binary offset (JSON padded so the binary is 4-aligned)
        JSON      header (materials, chunk table, sun, ...)
        binary    per chunk: position f32x3, normal i8x4, uv f32x2, lm u16x2,
                  colour u8x4, then indices u16; then the light grid u8x3
    """
    offset = 0
    blobs = []
    table = []
    for c in chunks:
        parts = [c["pos"], c["nrm"], c["uv"], c["lm"], c["col"], c["idx"]]
        size = 0
        padded = []
        for p in parts:
            b = p.tobytes()
            b += bytes((-len(b)) % 4)
            padded.append(b)
            size += len(b)
        blobs.extend(padded)
        table.append({
            "m": c["material"],
            "v": int(len(c["pos"])),
            "i": int(len(c["idx"])),
            "o": offset,
            "lo": [float(x) for x in c["pos"].min(0)],
            "hi": [float(x) for x in c["pos"].max(0)],
        })
        offset += size
    header = dict(header)
    header["chunks"] = table
    header["grid"]["offset"] = offset
    head = json.dumps(header, separators=(",", ":")).encode()
    head += b" " * ((-(len(head) + 16)) % 4)
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<II", len(head), 16 + len(head)))
        f.write(head)
        for b in blobs:
            f.write(b)
        f.write(grid_bytes)


def check_against_plain_map(soups, map_name):
    """The plain map is registered with player positions, so its bounds are the
    reference. A new Source2Viewer that changes the frame again fails here instead
    of shipping a map the players walk beside. It looks at where the bulk of the map falls,
    because the outer edge says little: de_vertigo's tower goes 10000 units below what the
    plain map kept. Returns the share of vertices inside the plain map's bounds."""
    plain = REPO_ROOT / "web" / "static" / "replay3d" / "assets" / "maps" / map_name / "world.glb"
    if not plain.exists():
        print(f"  no plain map at {plain} to compare the frame with")
        return None
    with open(plain, "rb") as f:
        f.seek(12)
        size = struct.unpack("<I", f.read(4))[0]
        f.seek(20)
        og = json.loads(f.read(size))
    old_lo, old_hi = np.full(3, np.inf), np.full(3, -np.inf)
    for mesh_i, world in pm._mesh_instances(og):
        for p in og["meshes"][mesh_i]["primitives"]:
            a = og["accessors"][p["attributes"]["POSITION"]]
            corners = np.array([[x, y, z, 1.0] for x in (a["min"][0], a["max"][0])
                                for y in (a["min"][1], a["max"][1])
                                for z in (a["min"][2], a["max"][2])])
            w = (world @ corners.T).T[:, :3]
            old_lo, old_hi = np.minimum(old_lo, w.min(0)), np.maximum(old_hi, w.max(0))
    new_lo = np.min([s.pos.min(0) for s in soups.values()], axis=0)
    new_hi = np.max([s.pos.max(0) for s in soups.values()], axis=0)
    inside = sum(int(np.all((s.pos >= old_lo - FRAME_TOLERANCE) & (s.pos <= old_hi + FRAME_TOLERANCE), axis=1).sum())
                 for s in soups.values())
    share = inside / sum(len(s.pos) for s in soups.values())
    # a map 39 times too small sits inside the bounds too, so it also has to spread across them
    lo, hi = np.percentile(np.concatenate([s.pos for s in soups.values()]), [1, 99], axis=0)
    spread = float(((hi - lo) / np.maximum(old_hi - old_lo, 1.0))[[0, 2]].min())
    print(f"  bounds {new_lo.round(0).tolist()} .. {new_hi.round(0).tolist()}, "
          f"{share:.0%} of it inside the plain map's, spreading over {spread:.0%} of them")
    if share < FRAME_MIN_SHARE or spread < FRAME_MIN_SPREAD:
        raise ValueError(f"{share:.0%} of the map is inside the plain map's bounds and spreads over "
                         f"{spread:.0%} of them: the export's frame changed?")
    return share


def pack_chunks(soups, max_size):
    chunks = []
    for mat_i, s in soups.items():
        for ids in split_triangles(s.pos, s.tri, max_size, MAX_CHUNK_VERTS):
            t = s.tri[ids]
            used, inv = np.unique(t, return_inverse=True)
            if len(used) > 65535:
                raise ValueError(f"chunk with {len(used)} vertices after the split")
            chunks.append({
                "material": mat_i,
                "pos": s.pos[used].astype("<f4"),
                "nrm": np.concatenate([np.clip(s.nrm[used] * 127.0, -127, 127), np.zeros((len(used), 1))], 1).astype("i1"),
                "uv": s.uv[used].astype("<f4"),
                "lm": (np.clip(s.lm[used], 0.0, 1.0) * 65535 + 0.5).astype("<u2"),
                "col": (np.clip(s.col[used], 0.0, 1.0) * 255 + 0.5).astype("u1"),
                "idx": inv.reshape(-1).astype("<u2"),
            })
    return chunks


def build_materials(g, raw_gltf_dir, used, out_dir, max_size):
    """Encode the textures each drawn material needs. Returns the material table
    (in `used` order) and the map from glTF material index to table index."""
    mats = g["materials"]
    uri = {im.get("name"): im.get("uri") for im in g.get("images", [])}

    def tex_path(name):
        u = uri.get(name)
        if not u:
            return None
        p = raw_gltf_dir / u
        return p if p.exists() else None

    jobs = {}
    entries = []
    for mi in used:
        m = mats[mi]
        v, tex, ints, floats, vectors = material_info(m)
        shader = v.get("ShaderName", "")
        mode = m.get("alphaMode", "OPAQUE")
        color = tex.get("g_tColor") or tex.get("g_tColor1") or tex.get("g_tGlassTintColor")
        layer2 = tex.get("g_tLayer2Color") if ints.get("F_LAYERS") else None
        blend = tex.get("g_tBlendModulation") if ints.get("F_LAYERS") else None
        tint = vectors.get("g_vColorTint") or vectors.get("g_vLayer1Tint") or [1, 1, 1, 0]
        entries.append({
            "name": m.get("name", ""),
            "alpha": {"OPAQUE": 0, "MASK": 1, "BLEND": 2}.get(mode, 0),
            "cutoff": float(m.get("alphaCutoff", floats.get("g_flAlphaTestReference", 0.5))),
            "two_sided": bool(m.get("doubleSided") or ints.get("F_RENDER_BACKFACES")),
            "unlit": shader in UNLIT_SHADERS,
            "decal": bool(ints.get("F_OVERLAY") or shader == "csgo_static_overlay.vfx"),
            "tint": [float(x) for x in _srgb_to_linear(np.array(tint[:3], np.float64))],
            "opacity": float(floats.get("g_flOpacityScale", 1.0)),
            "_paths": (color, layer2, blend),
        })
        for k, name in (("color", color), ("color2", layer2), ("blend", blend)):
            p = tex_path(name) if name else None
            if not p:
                continue
            key = (str(p), k == "blend")
            job = jobs.setdefault(key, {"path": p, "alpha": False, "linear": k == "blend", "cutoff": 0.5})
            if k == "color" and mode != "OPAQUE":
                job["alpha"] = True
                job["cutoff"] = entries[-1]["cutoff"]

    keys = list(jobs)
    t0 = time.time()

    def run(key):
        j = jobs[key]
        return encode_texture(j["path"], out_dir, max_size, j["alpha"], j["cutoff"], j["linear"])

    with ThreadPoolExecutor(8) as ex:
        results = dict(zip(keys, ex.map(run, keys)))
    print(f"  {len(keys)} textures encoded in {time.time() - t0:.0f} s")

    for e in entries:
        color, layer2, blend = e.pop("_paths")
        for k, name in (("color", color), ("color2", layer2), ("blend", blend)):
            p = tex_path(name) if name else None
            res = results.get((str(p), k == "blend")) if p else None
            e[k] = res[0] if res else None
            if k == "color":
                e["flat"] = res[1] if res else [0.5, 0.5, 0.5]
    return entries


def build(raw, map_name, out_root, max_size, chunk_size):
    """The whole bake. Returns a dict of the numbers worth printing."""
    gltf_path = raw / "maps" / map_name / "world.gltf"
    if not gltf_path.exists():
        gltf_path = pm.find_world_gltf(raw)
    out_dir = out_root / map_name
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in (out_dir / "tex").glob("*.ktx2") if (out_dir / "tex").exists() else []:
        old.unlink()

    g = pm.load_gltf(gltf_path)
    blob = (gltf_path.parent / g["buffers"][0]["uri"]).read_bytes()
    uv_scale = uv_scale_from_dump((raw / "world.txt").read_text(encoding="utf-8", errors="replace"))

    sun_dir, sun_rgb = find_sun(g)
    print(f"  sun direction {np.round(sun_dir, 3).tolist()} colour {np.round(sun_rgb, 3).tolist()}")
    tone = read_tone(raw)
    print(f"  exposure {tone['exposure']}, white point {tone['white']}, curve {tone['curve']}")

    t0 = time.time()
    irradiance, visibility = build_lightmap(raw, out_dir)
    print(f"  lightmap {irradiance.shape[1]} px in {time.time() - t0:.0f} s")
    sky = build_sky(raw, out_dir)

    soups = gather(g, blob, np.array([1.0, 1.0]))
    check_against_plain_map(soups, map_name)
    opaque = {mi for mi in soups if g["materials"][mi].get("alphaMode", "OPAQUE") == "OPAQUE"}
    tops = column_tops(soups, opaque)
    if max(sun_rgb) < NO_SUN:
        # de_train: nothing to check the UVs against, and Source2Viewer 20 scales them itself
        print("  this map has no sun: taking the lightmap UVs as exported")
        scale = np.array([1.0, 1.0])
    else:
        scale = choose_lightmap_scale(soups, tops, irradiance, visibility, sun_dir, uv_scale)
    for s in soups.values():
        s.lm = (s.lm * scale).astype(np.float32)
    lo, dims, grid = build_light_grid(soups, irradiance, visibility, sun_dir, sun_rgb)
    bake_props(soups, lo, dims, grid, sun_dir, sun_rgb)
    del irradiance, visibility

    used = sorted(soups)
    index_of = {mi: i for i, mi in enumerate(used)}
    materials = build_materials(g, gltf_path.parent, used, out_dir, max_size)
    chunks = pack_chunks(soups, chunk_size)
    for c in chunks:
        c["material"] = index_of[c["material"]]

    grid_code = (np.sqrt(np.clip(grid / LIGHT_RANGE, 0.0, 1.0)) * 255 + 0.5).astype(np.uint8)
    header = {
        "version": FORMAT_VERSION,
        "materials": materials,
        "lightmap": "lightmap.ktx2",
        "light_range": LIGHT_RANGE,
        "sky": sky,
        "sun": {"dir": sun_dir, "rgb": sun_rgb},
        "tone": tone,
        "grid": {"origin": lo.tolist(), "cell": GRID_CELL.tolist(), "dims": dims.tolist()},
    }
    write_world(out_dir / "world.c2map", header, chunks, grid_code.tobytes())

    tex_bytes = sum(p.stat().st_size for p in (out_dir / "tex").glob("*.ktx2"))
    verts = sum(len(c["pos"]) for c in chunks)
    tris = sum(len(c["idx"]) for c in chunks) // 3
    out = {
        "materials": len(materials),
        "chunks": len(chunks),
        "vertices": verts,
        "triangles": tris,
        "world_mb": (out_dir / "world.c2map").stat().st_size / 1e6,
        "textures_mb": tex_bytes / 1e6,
        "textures": sum(1 for _ in (out_dir / "tex").glob("*.ktx2")),
        "lightmap_mb": (out_dir / "lightmap.ktx2").stat().st_size / 1e6,
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("raw", type=Path, help="export directory, raw/<map>_v2")
    ap.add_argument("map_name")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    ap.add_argument("--max-size", type=int, default=2048, help="largest texture side")
    ap.add_argument("--chunk-size", type=float, default=DEFAULT_CHUNK_SIZE,
                    help="largest chunk side in world units")
    a = ap.parse_args()
    t0 = time.time()
    res = build(a.raw, a.map_name, a.out_root, a.max_size, a.chunk_size)
    print("  " + ", ".join(f"{k} {round(v, 1) if isinstance(v, float) else v}" for k, v in res.items()))
    print(f"  done in {time.time() - t0:.0f} s -> {a.out_root / a.map_name}")


if __name__ == "__main__":
    sys.exit(main())
