#!/usr/bin/env python3
"""Extract the static game data the Blitz analyzer needs from an installed client.

    python tools/wotb_extract_assets.py [--client "<...>/World of Tanks Blitz"]

Writes into src/blitzreview/assets/ (all committed, so the analyzer itself never
needs the game installed and never calls the Wargaming API):

    vehicles.json       tank_id -> internal key, nation, tier, class, ru/en names
    maps.json           map key -> id, scene name, modes, ru/en names,
                        assault-mode respawn points (world x/z), and for the
                        2D replay: `world` (the square the minimap shows),
                        `border` (the playable area), `minimap_crop`
    minimaps/<key>.webp 512x512 battle minimaps (MiniMapSmall@2x)

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
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from blitzreview import dava, dvpl  # noqa: E402

DEFAULT_CLIENT = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz")
OUT_DIR = ROOT / "src" / "blitzreview" / "assets"

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


def extract_vehicles(data_dir, strings):
    vehicles = {}
    for nation, nation_id in sorted(load_nations(data_dir).items(), key=lambda kv: kv[1]):
        path = data_dir / "XML" / "item_defs" / "vehicles" / nation / "list.xml.dvpl"
        if not path.is_file():
            continue
        root = ET.fromstring(dvpl.read(path))
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


def add_geometry(data_dir, maps):
    """Add `world` and `border` to every map from its scene. Returns problems."""
    problems, scenes = [], {}
    for key, entry in sorted(maps.items()):
        scene_name = entry.get("scene")
        if not scene_name:
            problems.append(f"{key}: no scene in maps.yaml")
            continue
        if scene_name not in scenes:
            try:
                scene = dava.read_scene(dvpl.read(data_dir / "3d" / "Maps" / f"{scene_name}.dvpl"))
                scenes[scene_name] = (dava.landscape_bbox(scene), dava.map_borders(scene))
            except (OSError, dava.DavaError, dvpl.DvplError) as exc:
                scenes[scene_name] = (None, [])
                problems.append(f"{key}: {scene_name}: {exc}")
        world, borders = scenes[scene_name]
        if world:
            entry["world"] = _rounded(world)
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
    args = ap.parse_args()

    data_dir = args.client / "Data"
    if not data_dir.is_dir():
        sys.exit(f"No Data/ folder under {args.client}")
    version = dvpl.read(data_dir / "version.txt.dvpl").decode("utf-8", "replace").strip()
    strings = {lang: load_strings(data_dir, lang) for lang in ("ru", "en")}
    source = {"client_version": version, "extracted": date.today().isoformat()}

    vehicles = extract_vehicles(data_dir, strings)
    maps = extract_maps(data_dir, strings)
    problems = add_geometry(data_dir, maps)

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
    for problem in problems:
        print(f"  ! {problem}")


if __name__ == "__main__":
    main()
