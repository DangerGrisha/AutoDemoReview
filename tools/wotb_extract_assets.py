#!/usr/bin/env python3
"""Extract the static game data the Blitz analyzer needs from an installed client.

    python tools/wotb_extract_assets.py [--client "<...>/World of Tanks Blitz"]

Writes into src/blitzreview/assets/ (all committed, so the analyzer itself never
needs the game installed and never calls the Wargaming API):

    vehicles.json       tank_id -> internal key, nation, tier, class, ru/en names;
                        for the first-person view `eye` (the gun's height
                        above the ground, metres) and `pitch` [elevation,
                        depression] (the gun's limits in degrees, up
                        negative), both of the top turret's top gun
    maps.json           map key -> id, scene name, modes, ru/en names,
                        assault-mode respawn points (world x/z), and for the
                        2D replay: `world` (the square the minimap shows),
                        `border` (the playable area), `minimap_crop`, the
                        Encounter `base` [x, z, radius] and the Supremacy
                        `points` [[id, x, z, radius], ...]; for the 3D view
                        `terrain` {file, n}
    minimaps/<key>.webp 512x512 battle minimaps (MiniMapSmall@2x)
    terrain/<scene>.bin the terrain's heights over the `world` square: n x n
                        points, row by row from the south edge, each row
                        west to east, in decimetres as i16le -- each row's
                        first value as is, the rest as the step from the
                        previous one; zlib-compressed (about 17 KB a map).
                        Map variants of one scene share the file

The tank id is the number replays carry: (index << 8) | (nation << 4) | 1,
where `index` is the vehicle's <id> in its nation's list.xml and `nation` the
<id> in vehicles/common/nations.xml. Re-run after a game update that adds
tanks or maps; the analyzer shows internal names for anything missing.

Minimap geometry (checked against the tracks of real replays, which run along
the roads, and against the variant minimaps, whose greyed-out part ends
exactly at the variant's border): a minimap shows the scene's whole terrain
square (`world`, x0 z0 x1 z1 -- 600 x 600 m around the origin on every map)
with x to the right and z up; the picture fills only the top-left
`minimap_crop` box of the 512 x 512 texture (505 x 505 -- the rest is
transparent padding). Measuring the crop needs Pillow (`pip install pillow`);
without it the crop is left out and the report uses the whole texture.
"""

import argparse
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from blitzreview import dava, dvpl  # noqa: E402

DEFAULT_CLIENT = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz")
OUT_DIR = ROOT / "src" / "blitzreview" / "assets"

# Radius of an Encounter base whose scene sets none (most of them): measured
# on Rift, where a lone tank's capture began as it rolled within 25 m of the
# base centre and stopped when it backed out to 27 m. Supremacy points all
# state theirs (15 m); the fallback only covers a scene that would not.
BASE_RADIUS = 25.0
POINT_RADIUS = 15.0

# The client's heightmaps are 512 x 512 over the 600 m terrain square; the
# 3D view resamples them to this many points a side (2.4 m apart) -- fine
# enough for a camera at a gun's height, about 45 KB a map compressed, and
# the most one WebGL mesh with 16-bit indices holds (256 x 256 = 65536).
TERRAIN_N = 256

# First matching tag wins; Blitz has no artillery in random battles but the
# client still knows the class, so it stays in the table.
CLASS_TAGS = (("lightTank", "LT"), ("mediumTank", "MT"), ("heavyTank", "HT"),
              ("AT-SPG", "TD"), ("SPG", "SPG"))

_STRING_RE = re.compile(r'^"((?:[^"\\\n]|\\.)*)":\s*"((?:[^"\\\n]|\\.)*)"\s*$', re.M)


def load_strings(data_dir, lang):
    """The client's `"key": "value"` localisation table, only `#...` keys."""
    text = dvpl.read(data_dir / "Strings" / f"{lang}.yaml.dvpl").decode("utf-8")
    out = {}
    for key, value in _STRING_RE.findall(text):
        if not key.startswith("#"):
            continue
        try:
            out[key] = json.loads(f'"{value}"')
        except json.JSONDecodeError:
            out[key] = value
    return out


def load_nations(data_dir):
    raw = dvpl.read(data_dir / "XML" / "item_defs" / "vehicles" / "common" / "nations.xml.dvpl")
    root = ET.fromstring(raw)
    return {child.tag: int(child.findtext("id")) for child in root}


def vehicle_class(tags):
    tokens = set(tags.split())
    for tag, code in CLASS_TAGS:
        if tag in tokens:
            return code
    return None


def _numbers(text):
    try:
        return [float(v) for v in (text or "").split()]
    except ValueError:
        return []


def vehicle_geometry(data_dir, nation, key, shared_guns):
    """{"eye", "pitch"} of a tank from its XML, or {} when the file does not
    say. The top turret's top gun, as most players drive it: `eye` is the
    gun's height above the ground (the top chassis' hull position, the hull's
    turret position and the turret's gun position, added up) -- where the
    sniper camera sits, which the replays confirm to a few centimetres (T77
    2.23 m, FV215b (183) 2.35 m); `pitch` the gun's [elevation, depression]
    in degrees, up negative, from the gun or its shared definition in
    components/guns.xml. The replays send the gun's pitch as 6 bits over
    exactly this range (events.py)."""
    path = data_dir / "XML" / "item_defs" / "vehicles" / nation / f"{key}.xml.dvpl"
    try:
        root = ET.fromstring(dvpl.read(path))
    except (OSError, ET.ParseError, dvpl.DvplError):
        return {}
    turret_at = root.find("hull/turretPositions")
    turrets = root.find("turrets0")
    if turret_at is None or len(turret_at) == 0 or turrets is None or len(turrets) == 0:
        return {}
    top = turrets[-1]
    out = {}
    chassis = root.find("chassis")
    hull_at = _numbers(chassis[-1].findtext("hullPosition")) if chassis is not None and len(chassis) else []
    base, gun_at = _numbers(turret_at[0].text), _numbers(top.findtext("gunPosition"))
    if len(base) == 3:
        out["eye"] = round(sum(p[1] for p in (hull_at, base, gun_at) if len(p) == 3), 2)
    guns = top.find("guns")
    if guns is not None and len(guns):
        gun = guns[-1]
        limits = _numbers(gun.findtext("pitchLimits"))
        if len(limits) != 2 and gun.tag in shared_guns:
            limits = _numbers(shared_guns[gun.tag].findtext("pitchLimits"))
        if len(limits) == 2 and limits[0] < limits[1]:
            out["pitch"] = limits
    return out


def load_shared_guns(data_dir, nation):
    """The nation's shared gun definitions by tag (components/guns.xml)."""
    path = data_dir / "XML" / "item_defs" / "vehicles" / nation / "components" / "guns.xml.dvpl"
    try:
        shared = ET.fromstring(dvpl.read(path)).find("shared")
    except (OSError, ET.ParseError, dvpl.DvplError):
        return {}
    return {child.tag: child for child in shared} if shared is not None else {}


def extract_vehicles(data_dir, strings):
    vehicles = {}
    for nation, nation_id in sorted(load_nations(data_dir).items(), key=lambda kv: kv[1]):
        path = data_dir / "XML" / "item_defs" / "vehicles" / nation / "list.xml.dvpl"
        if not path.is_file():
            continue
        root = ET.fromstring(dvpl.read(path))
        shared_guns = load_shared_guns(data_dir, nation)
        for node in root:
            index = node.findtext("id")
            if index is None:
                continue
            tank_id = (int(index) << 8) | (nation_id << 4) | 1
            user = (node.findtext("userString") or "").strip()
            short = (node.findtext("shortUserString") or "").strip()
            tags = node.findtext("tags") or ""
            entry = {
                "key": node.tag,
                "nation": nation,
                "tier": int(node.findtext("level") or 0),
                "type": vehicle_class(tags),
                "premium": node.find("price/gold") is not None,
                "collectible": "collectible" in tags.split(),
            }
            for lang, table in strings.items():
                name = table.get(user, node.tag)
                entry[f"name_{lang}"] = name
                entry[f"short_{lang}"] = table.get(short, name) if short else name
            entry.update(vehicle_geometry(data_dir, nation, node.tag, shared_guns))
            vehicles[str(tank_id)] = entry
    return vehicles


def extract_maps(data_dir, strings):
    """Targeted reader for maps.yaml -- a regular, client-generated YAML file."""
    text = dvpl.read(data_dir / "maps.yaml.dvpl").decode("utf-8")
    maps, current, team = {}, None, None
    for line in text.splitlines():
        m = re.match(r"^    ([A-Za-z0-9_]+):\s*$", line)
        if m:
            current = {"key": m.group(1), "respawn": {"allies": [], "enemies": []}}
            maps[m.group(1)] = current
            team = None
            continue
        if current is None:
            continue
        m = re.match(r"^        (id|localName|availableModes|tags):\s*(.+?)\s*$", line)
        if m:
            name, value = m.groups()
            if name == "id":
                current["id"] = int(value)
            elif name == "localName":
                current["scene"] = value.strip('"')
            elif name == "tags":
                current["tag"] = value.strip('"')
            else:
                current["modes"] = [int(v) for v in re.findall(r"-?\d+", value)]
            continue
        m = re.match(r"^            (allies|enemies):\s*$", line)
        if m:
            team = m.group(1)
            continue
        m = re.match(r"^\s+- \[(-?[\d.]+),\s*(-?[\d.]+)\]\s*$", line)
        if m and team:
            current["respawn"][team].append([round(float(m.group(1)), 2),
                                             round(float(m.group(2)), 2)])
    for key, entry in maps.items():
        loc_key = f"#maps:{key}:{entry.get('scene', '')}"
        for lang, table in strings.items():
            entry[f"name_{lang}"] = table.get(loc_key, key)
    return maps


def _rounded(values):
    return [round(v, 1) for v in values]


def pick_border(borders, tag):
    """A map variant's border: the one labelled with its maps.yaml tag; an
    untagged map lives in a scene with a single border."""
    if tag:
        tagged = [b for b in borders if tag in b["labels"]]
        return tagged[0] if len(tagged) == 1 else None
    return borders[0] if len(borders) == 1 else None


def pick_zones(zones, tag):
    """A map variant's capture zones: those labelled with its maps.yaml tag,
    else the unlabelled ones (which is all an untagged map has)."""
    if tag:
        tagged = [z for z in zones if tag in z["labels"]]
        if tagged:
            return tagged
    return [z for z in zones if not z["labels"]]


def add_zones(key, entry, zones, problems):
    """Add the Encounter `base` and the Supremacy `points` for the modes the
    map is played in (maps.yaml `availableModes`: 0 Encounter, 1 Supremacy)."""
    zones = pick_zones(zones, entry.get("tag"))
    modes = entry.get("modes") or []
    bases = [z for z in zones if z["kind"] == "base"]
    points = sorted((z for z in zones if z["kind"] == "point"), key=lambda z: z["id"] or 0)
    if 0 in modes:
        if len(bases) == 1:
            entry["base"] = _rounded((*bases[0]["pos"], bases[0]["radius"] or BASE_RADIUS))
        else:
            problems.append(f"{key}: {len(bases)} Encounter bases for tag {entry.get('tag')!r}")
    if 1 in modes:
        ids = [p["id"] or 0 for p in points]
        if len(points) >= 2 and ids == list(range(len(points))):
            entry["points"] = [[i, *_rounded((*p["pos"], p["radius"] or POINT_RADIUS))]
                               for i, p in zip(ids, points)]
        else:
            problems.append(f"{key}: Supremacy points numbered {ids}")
    x0, z0, x1, z1 = entry["border"]
    places = ([entry["base"][:2]] if "base" in entry else []) + [p[1:3] for p in entry.get("points", [])]
    if any(not (x0 <= x <= x1 and z0 <= z <= z1) for x, z in places):
        problems.append(f"{key}: a capture zone lies outside the border")


def resample(size, values, n):
    """`values` (size x size, row-major) bilinearly resampled to n x n points
    spanning the same square, edges included."""
    out = []
    step = (size - 1) / (n - 1)
    for j in range(n):
        y = j * step
        j0 = min(int(y), size - 2)
        fy = y - j0
        for i in range(n):
            x = i * step
            i0 = min(int(x), size - 2)
            fx = x - i0
            a = values[j0 * size + i0] * (1 - fx) + values[j0 * size + i0 + 1] * fx
            b = values[(j0 + 1) * size + i0] * (1 - fx) + values[(j0 + 1) * size + i0 + 1] * fx
            out.append(a * (1 - fy) + b * fy)
    return out


def write_terrain(data_dir, scene_name, land, out_dir):
    """Write terrain/<scene folder>.bin for one scene (format in the module
    docstring); returns the maps.json `terrain` entry."""
    folder = scene_name.split("/")[0]
    raw = dvpl.read(data_dir / "3d" / "Maps" / folder / f"{land['hmap']}.dvpl")
    size, values = dava.read_heightmap(raw)
    _x0, _y0, z0, _x1, _y1, z1 = land["bbox"]
    decimetres = [round(10 * (z0 + v / 65535 * (z1 - z0)))
                  for v in resample(size, values, TERRAIN_N)]
    steps = []
    for j in range(TERRAIN_N):
        row = decimetres[j * TERRAIN_N:(j + 1) * TERRAIN_N]
        steps.append(row[0])
        steps.extend(b - a for a, b in zip(row, row[1:]))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{folder}.bin").write_bytes(
        zlib.compress(struct.pack(f"<{len(steps)}h", *steps), 9))
    return {"file": folder, "n": TERRAIN_N}


def add_geometry(data_dir, maps, terrain_dir=None):
    """Add `world`, `border`, the capture zones and (with `terrain_dir`) the
    terrain to every map from its scene. Returns problems."""
    problems, scenes = [], {}
    for key, entry in sorted(maps.items()):
        scene_name = entry.get("scene")
        if not scene_name:
            problems.append(f"{key}: no scene in maps.yaml")
            continue
        if scene_name not in scenes:
            terrain = None
            try:
                scene = dava.read_scene(dvpl.read(data_dir / "3d" / "Maps" / f"{scene_name}.dvpl"))
                land = dava.landscape(scene)
                if terrain_dir is not None and land and land["hmap"]:
                    terrain = write_terrain(data_dir, scene_name, land, terrain_dir)
                scenes[scene_name] = (dava.landscape_bbox(scene), dava.map_borders(scene),
                                      dava.capture_zones(scene), terrain)
            except (OSError, dava.DavaError, dvpl.DvplError) as exc:
                scenes[scene_name] = (None, [], [], None)
                problems.append(f"{key}: {scene_name}: {exc}")
        world, borders, zones, terrain = scenes[scene_name]
        if world:
            entry["world"] = _rounded(world)
        if terrain:
            entry["terrain"] = terrain
        border = pick_border(borders, entry.get("tag"))
        if border is None:
            problems.append(f"{key}: no border for tag {entry.get('tag')!r} "
                            f"among {[b['labels'] for b in borders]}")
            continue
        entry["border"] = _rounded(border["rect"])
        x0, z0, x1, z1 = border["rect"]
        outside = sum(1 for team in entry["respawn"].values() for x, z in team
                      if not (x0 - 1 <= x <= x1 + 1 and z0 - 1 <= z <= z1 + 1))
        if outside:
            problems.append(f"{key}: {outside} respawn points outside the border")
        add_zones(key, entry, zones, problems)
    return problems


def add_minimap_crops(maps, minimap_dir):
    """Add `minimap_crop` (the opaque box of the texture) to every map with a
    minimap. Returns how many were measured, or None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    measured = 0
    for key, entry in maps.items():
        path = minimap_dir / f"{key}.webp"
        if not path.is_file():
            continue
        with Image.open(path) as image:
            box = image.getchannel("A").getbbox() if "A" in image.getbands() else None
            entry["minimap_crop"] = list(box or (0, 0, image.width, image.height))
        measured += 1
    return measured


def extract_minimaps(data_dir, keys, out_dir):
    src_dir = data_dir / "Gfx" / "UI" / "BattleScreenHUD" / "minimap"
    out_dir.mkdir(parents=True, exist_ok=True)
    written, missing = 0, []
    for key in sorted(keys):
        for name in ("MiniMapSmall@2x.packed.webp.dvpl", "MiniMapSmall.packed.webp.dvpl"):
            path = src_dir / key / name
            if path.is_file():
                (out_dir / f"{key}.webp").write_bytes(dvpl.read(path))
                written += 1
                break
        else:
            missing.append(key)
    return written, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--client", type=Path, default=DEFAULT_CLIENT,
                    help="World of Tanks Blitz install folder (contains Data/)")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--no-minimaps", action="store_true")
    ap.add_argument("--no-terrain", action="store_true",
                    help="keep the terrain files and maps.json `terrain` as they are")
    args = ap.parse_args()

    data_dir = args.client / "Data"
    if not data_dir.is_dir():
        sys.exit(f"No Data/ folder under {args.client}")
    version = dvpl.read(data_dir / "version.txt.dvpl").decode("utf-8", "replace").strip()
    strings = {lang: load_strings(data_dir, lang) for lang in ("ru", "en")}
    source = {"client_version": version, "extracted": date.today().isoformat()}

    vehicles = extract_vehicles(data_dir, strings)
    maps = extract_maps(data_dir, strings)
    terrain_dir = None if args.no_terrain else args.out / "terrain"
    problems = add_geometry(data_dir, maps, terrain_dir)
    if args.no_terrain:  # carry the existing entries over
        try:
            old = json.loads((args.out / "maps.json").read_text(encoding="utf-8"))["maps"]
        except (OSError, ValueError, KeyError):
            old = {}
        for key, entry in maps.items():
            if "terrain" in old.get(key, {}):
                entry["terrain"] = old[key]["terrain"]

    args.out.mkdir(parents=True, exist_ok=True)
    if not args.no_minimaps:
        written, missing = extract_minimaps(data_dir, maps, args.out / "minimaps")
        print(f"minimaps: {written} written" + (f", none for {missing}" if missing else ""))
    cropped = add_minimap_crops(maps, args.out / "minimaps")
    if cropped is None:
        problems.append("Pillow is not installed: minimap_crop left out (pip install pillow)")

    for name, payload in (("vehicles.json", {"source": source, "vehicles": vehicles}),
                          ("maps.json", {"source": source, "maps": maps})):
        (args.out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
    print(f"client {version}: {len(vehicles)} vehicles, {len(maps)} maps -> {args.out}")
    bare = sorted(v["key"] for v in vehicles.values() if "pitch" not in v or "eye" not in v)
    if bare:
        problems.append(f"{len(bare)} vehicles without gun geometry: {bare[:8]}")
    for problem in problems:
        print(f"  ! {problem}")


if __name__ == "__main__":
    main()
