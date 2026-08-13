"""Extract map callout zones ("Palace", "Connector", ...) from the CS2 map files.

The coach knows WHERE a player keeps dying but, without this, can only say "the
same spot at (-189, -720)". Callout names are what the player actually thinks
in, so naming the spot is the difference between a report that reads like a
teammate and one that reads like a spreadsheet.

The names are not invented here and not transcribed by hand: every competitive
map ships `env_cs_place` entities, each with the official `place_name` and a
brush volume, which is exactly what the game itself uses to fill the
`last_place_name` player field. So this reads the source of truth:

  1. decompile `maps/<map>/entities/default_ents.vents_c` out of the map VPK
  2. keep the `env_cs_place` entities -- name, origin, and the volume model
  3. export those volume models and take each one's bounding box
  4. world AABB = entity origin + model bbox

Accuracy was measured against the game's own answer, by parsing a demo with
`last_place_name` and comparing (see the note in demoreview/zones.py): 94% of
sampled player positions fall inside a box and 92.5% get the exact callout the
game reports. The rest are mostly edge cases where two volumes overlap.

Usage (needs CS2 installed and tools/s2v/Source2Viewer-CLI.exe):

    python tools/extract_zones.py                 # every installed de_/cs_ map
    python tools/extract_zones.py de_mirage de_nuke
    python tools/extract_zones.py --maps-dir "D:/.../csgo/maps"

Writes src/demoreview/assets/zones.json. Re-run it after a map update.
"""

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
S2V = REPO / "tools" / "s2v" / "Source2Viewer-CLI.exe"
OUT_JSON = REPO / "src" / "demoreview" / "assets" / "zones.json"

# Map variants that are not the competitive layout; they would collide with the
# real map's name and are never what a demo reports.
SKIP_SUFFIXES = ("_vanity", "_night", "_snow")

_BLOCK = re.compile(r"^====\d+====$", re.M)
_KV = re.compile(r"^(\w+)\s+(.*)$")
_RESOURCE = re.compile(r'resource_name:"([^"]+)"')


def find_maps_dir(explicit=None):
    """The CS2 `game/csgo/maps` directory."""
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            sys.exit(f"--maps-dir does not exist: {path}")
        return path

    candidates = []
    for root in (r"C:\Program Files (x86)\Steam", r"D:\games\Steam",
                 r"D:\Steam", r"C:\Steam", r"E:\Steam"):
        candidates.append(Path(root))
        # Extra library folders are listed in libraryfolders.vdf.
        vdf = Path(root) / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s*"([^"]+)"', vdf.read_text(encoding="utf-8")):
                candidates.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    for base in candidates:
        maps = (base / "steamapps" / "common" / "Counter-Strike Global Offensive"
                / "game" / "csgo" / "maps")
        if maps.is_dir():
            return maps
    sys.exit("CS2 install not found -- pass --maps-dir explicitly.")


def _run(args, timeout=900):
    proc = subprocess.run([str(S2V), *args], capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[-400:])
    return proc.stdout or ""


def parse_entities(path):
    """The `.vents` text dump -> a list of {key: value} entity dicts."""
    text = path.read_text(encoding="utf-8", errors="replace")
    entities = []
    for block in _BLOCK.split(text):
        ent = {}
        for line in block.splitlines():
            m = _KV.match(line.strip())
            if m:
                ent[m.group(1)] = m.group(2).strip()
        if ent:
            entities.append(ent)
    return entities


def _vec3(raw):
    return [float(v) for v in raw.strip("[] ").split(",")]


def glb_bbox(path):
    """(min, max) over every POSITION accessor in a .glb, or None.

    glTF requires POSITION accessors to carry min/max, so the bounding box is
    read straight out of the JSON chunk without touching the binary payload.
    """
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        return None
    (json_len,) = struct.unpack_from("<I", data, 12)
    doc = json.loads(data[20:20 + json_len])
    accessors = {p["attributes"]["POSITION"]
                 for mesh in doc.get("meshes", [])
                 for p in mesh.get("primitives", [])
                 if "POSITION" in p.get("attributes", {})}
    boxes = [(doc["accessors"][i].get("min"), doc["accessors"][i].get("max"))
             for i in accessors]
    boxes = [(lo, hi) for lo, hi in boxes if lo and hi and len(lo) == 3]
    if not boxes:
        return None
    lo = [min(b[0][i] for b in boxes) for i in range(3)]
    hi = [max(b[1][i] for b in boxes) for i in range(3)]
    return lo, hi


def extract_map(vpk, work):
    """[{name, min, max}] for one map VPK, or [] when it has no callouts."""
    map_name = vpk.stem
    ents_rel = f"maps/{map_name}/entities/default_ents.vents_c"
    out_dir = work / map_name
    try:
        _run(["-i", str(vpk), "-f", ents_rel, "-d", "-o", str(out_dir)])
    except RuntimeError as exc:
        print(f"  {map_name}: entity lump not readable ({exc})")
        return []

    dumps = list(out_dir.rglob("default_ents.vents"))
    if not dumps:
        print(f"  {map_name}: no entity dump produced")
        return []

    places = []
    for ent in parse_entities(dumps[0]):
        if ent.get("classname") != '"env_cs_place"':
            continue
        name = ent.get("place_name", "").strip('"').strip()
        model = _RESOURCE.search(ent.get("model", ""))
        if not name or not model:
            continue
        places.append({"name": name, "origin": _vec3(ent["origin"]),
                       "model": model.group(1)})
    if not places:
        return []

    # One invocation for every volume: Source2Viewer takes a comma-separated
    # path filter, and preloading the VPK per model would dominate the runtime.
    _run(["-i", str(vpk), "-f", ",".join(p["model"] + "_c" for p in places),
          "-d", "--gltf_export_format", "glb", "-o", str(out_dir)])

    zones, missing = [], []
    for place in places:
        stem = Path(place["model"]).stem
        # The volume is a brush, so its shape lives in the physics mesh; the
        # render mesh of a place entity is empty.
        found = list(out_dir.rglob(f"{stem}_physics.glb")) or \
            list(out_dir.rglob(f"{stem}.glb"))
        box = glb_bbox(found[0]) if found else None
        if box is None:
            missing.append(place["name"])
            continue
        ox, oy, oz = place["origin"]
        lo, hi = box
        zones.append({
            "name": place["name"],
            "min": [round(lo[0] + ox, 1), round(lo[1] + oy, 1), round(lo[2] + oz, 1)],
            "max": [round(hi[0] + ox, 1), round(hi[1] + oy, 1), round(hi[2] + oz, 1)],
        })
    if missing:
        print(f"  {map_name}: no volume for {', '.join(sorted(missing))}")
    zones.sort(key=lambda z: z["name"])
    return zones


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("maps", nargs="*", help="map names (default: every installed map)")
    ap.add_argument("--maps-dir", help="CS2 game/csgo/maps directory")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args()

    if not S2V.is_file():
        sys.exit(f"Source2Viewer-CLI not found at {S2V} (see docs/REPLAY3D.md)")

    maps_dir = find_maps_dir(args.maps_dir)
    print(f"CS2 maps: {maps_dir}")

    if args.maps:
        vpks = [maps_dir / f"{m}.vpk" for m in args.maps]
        for vpk in vpks:
            if not vpk.is_file():
                sys.exit(f"no such map: {vpk}")
    else:
        vpks = sorted(p for p in maps_dir.glob("*.vpk")
                      if p.stem.startswith(("de_", "cs_"))
                      and not p.stem.endswith(SKIP_SUFFIXES))

    out_path = Path(args.out)
    existing = {}
    if out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8")).get("maps", {})
        except (OSError, json.JSONDecodeError):
            pass

    work = Path(tempfile.mkdtemp(prefix="zones_"))
    try:
        for vpk in vpks:
            zones = extract_map(vpk, work)
            if zones:
                existing[vpk.stem] = zones
                print(f"  {vpk.stem}: {len(zones)} zones")
            else:
                print(f"  {vpk.stem}: skipped")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    payload = {
        "_comment": "Callout zones extracted from CS2 env_cs_place entities by "
                    "tools/extract_zones.py. World-space AABBs in Hammer units.",
        "maps": dict(sorted(existing.items())),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    total = sum(len(z) for z in existing.values())
    print(f"\nwrote {out_path} -- {len(existing)} maps, {total} zones")


if __name__ == "__main__":
    main()
