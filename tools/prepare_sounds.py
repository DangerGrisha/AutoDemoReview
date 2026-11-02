"""Turn CS2's sound files into what the 3D replay plays.

Run tools/convert_sounds.ps1 rather than this directly: it does the extraction first.

CS2 describes each sound as an event: a name the game code plays, with the files to pick
from and a volume and pitch. Going through the events rather than through file names
gets the same files at the same mix, and tools/sound_events.txt only has to name the
events. A missing event or file is a warning and not a stop: the page synthesizes
whatever the index does not list.

The output folder holds one Ogg Opus file per source file, and index.json with what the
page needs to mix them the way the game does:

    {"sounds": {"shot:AK": {
        "f": ["weapons_ak47_ak47_01.ogg", ...],   the files to pick from
        "v": 1.1, "p": 1.0,                       volume and pitch
        "c": [[25.0, 0.8], [30.0, 1.0], ...],     volume by distance in world units
        "r": [-0.02, 0.02],                       random pitch offset (when there is one)
        "n": 1                                    voices one source may hold (when limited)
    }, ...}}
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_EVENT_RE = re.compile(r'^\s{0,4}"?([A-Za-z0-9_.\-]+)"?\s*=\s*\n?\s*\{', re.M)
_FILE_RE = re.compile(r'"(sounds/[^"]+\.vsnd)"')
_BASE_RE = re.compile(r'\bbase\s*=\s*"([^"]+)"')
_DATA_MARK = 'Data for block "DATA"'
# Opus is transparent on gunshots here, and a tenth of the source size.
_BITRATE = "96k"
# How the extraction may have written a source file, by what the original was.
_SOURCE_EXTENSIONS = (".wav", ".mp3")


def _number(body, key):
    match = re.search(rf"\b{key}\s*=\s*(-?[\d.]+)", body)
    return float(match.group(1)) if match else None


def _distance_curve(body):
    """[[distance, volume], ...] of an event. A point also holds its tangents; the page
    joins the points with straight lines, which is close enough to hear no difference."""
    match = re.search(r"\bdistance_volume_mapping_curve\s*=\s*\[", body)
    if not match:
        return []
    depth, end = 1, match.end()
    while depth and end < len(body):
        depth += {"[": 1, "]": -1}.get(body[end], 0)
        end += 1
    points = re.findall(r"\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", body[match.end():end - 1])
    return [[float(distance), float(volume)] for distance, volume in points]


def parse_sound_events(dump):
    """{event name: {"files", "volume", "pitch", "curve", ...}} from a `Source2Viewer -a` dump.

    Only the DATA block counts: the block before it names the same events in passing.
    A value is None (a list: empty) where the event leaves it to its base.
    """
    start = dump.find(_DATA_MARK)
    text = dump[start:] if start >= 0 else dump
    events = {}
    for match in _EVENT_RE.finditer(text):
        depth, end = 1, match.end()
        while depth and end < len(text):
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        body = text[match.end():end - 1]
        base = _BASE_RE.search(body)
        events[match.group(1)] = {
            "files": _FILE_RE.findall(body),
            "volume": _number(body, "volume"),
            "pitch": _number(body, "pitch"),
            "curve": _distance_curve(body),
            "pitch_min": _number(body, "pitch_random_min"),
            "pitch_max": _number(body, "pitch_random_max"),
            "limit": _number(body, "instance_limit"),
            "base": base.group(1) if base else None,
        }
    return events


def resolved(events, name):
    """An event with what it leaves unset filled in from its base chain, or None if unknown."""
    event = events.get(name)
    if event is None:
        return None
    out = dict(event)
    seen = {name}
    base = event["base"]
    while base and base in events and base not in seen:
        seen.add(base)
        parent = events[base]
        for key in ("files", "volume", "pitch", "curve", "pitch_min", "pitch_max", "limit"):
            if out[key] is None or out[key] == []:
                out[key] = parent[key]
        base = parent["base"]
    out["volume"] = 1.0 if out["volume"] is None else out["volume"]
    out["pitch"] = 1.0 if out["pitch"] is None else out["pitch"]
    return out


def index_entry(event, names):
    """What index.json says about one sound. Leaves out what would change nothing."""
    entry = {"f": names, "v": event["volume"], "p": event["pitch"]}
    if event["curve"]:
        entry["c"] = event["curve"]
    if event["pitch_min"] or event["pitch_max"]:
        entry["r"] = [event["pitch_min"] or 0.0, event["pitch_max"] or 0.0]
    if event["limit"]:
        entry["n"] = int(event["limit"])
    return entry


def parse_event_list(text):
    """[(key, event)] from tools/sound_events.txt."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, event = line.split()
            rows.append((key, event))
    return rows


def output_name(source):
    """`sounds/weapons/ak47/ak47_01.vsnd` -> `weapons_ak47_ak47_01.ogg`. Flat, because every
    path is one more thing to keep URL-safe, and unique because the whole path is in it."""
    relative = source.split("/", 1)[1]
    return re.sub(r"[^A-Za-z0-9_\-]", "_", relative.rsplit(".", 1)[0]) + ".ogg"


def find_source(files_dir, source):
    stem = Path(files_dir) / Path(source).with_suffix("")
    for extension in _SOURCE_EXTENSIONS:
        path = stem.with_suffix(extension)
        if path.is_file():
            return path
    return None


def encode(source, out):
    """Raises CalledProcessError when ffmpeg cannot read the source."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map_metadata", "-1", "-c:a", "libopus",
         "-b:a", _BITRATE, "-vbr", "on", "-compression_level", "10", str(out)],
        check=True, capture_output=True)


def build_index(events, wanted, files_dir, out_dir, encode_fn=encode):
    """Encode everything `wanted` names. Returns (index, warnings)."""
    index, warnings, done = {}, [], {}
    for key, event_name in wanted:
        event = resolved(events, event_name)
        if event is None:
            warnings.append(f"{key}: no event {event_name}")
            continue
        names = []
        for source in event["files"]:
            if source not in done:
                path = find_source(files_dir, source)
                if path is None:
                    warnings.append(f"{key}: {source} was not extracted")
                    done[source] = None
                    continue
                name = output_name(source)
                try:
                    encode_fn(path, Path(out_dir) / name)
                    done[source] = name
                except subprocess.CalledProcessError as exc:
                    warnings.append(f"{key}: ffmpeg failed on {source}: {exc.stderr!r}")
                    done[source] = None
            if done[source]:
                names.append(done[source])
        if names:
            index[key] = index_entry(event, names)
        else:
            warnings.append(f"{key}: {event_name} has no usable file")
    return index, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("raw_dir", help="holds events/*.txt (the dumps) and files/ (the extracted sounds)")
    ap.add_argument("out_dir", help="output folder")
    ap.add_argument("--events", default=str(Path(__file__).with_name("sound_events.txt")))
    args = ap.parse_args()

    raw, out = Path(args.raw_dir), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    events = {}
    for dump in sorted((raw / "events").glob("*.txt")):
        events.update(parse_sound_events(dump.read_text(encoding="utf-8-sig", errors="replace")))
    if not events:
        sys.exit(f"no sound events found under {raw / 'events'}")

    wanted = parse_event_list(Path(args.events).read_text(encoding="utf-8"))
    index, warnings = build_index(events, wanted, raw / "files", out)
    (out / "index.json").write_text(json.dumps({"sounds": index}, separators=(",", ":")), encoding="utf-8")

    total = sum(p.stat().st_size for p in out.glob("*.ogg"))
    count = sum(len(entry["f"]) for entry in index.values())
    print(f"{len(index)}/{len(wanted)} sounds, {count} uses of {len(list(out.glob('*.ogg')))} files, {total / 1e6:.1f} MB")
    for warning in warnings:
        print("WARNING:", warning)


if __name__ == "__main__":
    main()
