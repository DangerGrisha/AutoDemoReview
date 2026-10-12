#!/usr/bin/env python3
"""Extract the static game data the Blitz analyzer needs from an installed client.

    python tools/wotb_extract_assets.py [--client "<...>/World of Tanks Blitz"]

Writes into src/blitzreview/assets/ (all committed, so the analyzer itself never
needs the game installed and never calls the Wargaming API):

    vehicles.json       tank_id -> internal key, nation, tier, class, ru/en names
    maps.json           map key -> id, scene name, modes, ru/en names,
                        assault-mode respawn points (world x/z)
    minimaps/<key>.webp 512x512 battle minimaps (MiniMapSmall@2x)

The tank id is the number replays carry: (index << 8) | (nation << 4) | 1,
where `index` is the vehicle's <id> in its nation's list.xml and `nation` the
<id> in vehicles/common/nations.xml. Re-run after a game update that adds
tanks or maps; the analyzer shows internal names for anything missing.
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

from blitzreview import dvpl  # noqa: E402

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
        m = re.match(r"^        (id|localName|availableModes):\s*(.+?)\s*$", line)
        if m:
            name, value = m.groups()
            if name == "id":
                current["id"] = int(value)
            elif name == "localName":
                current["scene"] = value.strip('"')
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

    args.out.mkdir(parents=True, exist_ok=True)
    for name, payload in (("vehicles.json", {"source": source, "vehicles": vehicles}),
                          ("maps.json", {"source": source, "maps": maps})):
        (args.out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
    print(f"client {version}: {len(vehicles)} vehicles, {len(maps)} maps -> {args.out}")

    if not args.no_minimaps:
        written, missing = extract_minimaps(data_dir, maps, args.out / "minimaps")
        print(f"minimaps: {written} written" + (f", none for {missing}" if missing else ""))


if __name__ == "__main__":
    main()
