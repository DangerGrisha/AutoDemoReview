"""DAVA engine scenes (.sc2) as the Blitz client ships them -- enough to pull
map facts out offline (tools/wotb_extract_assets.py). Not used on uploads.

    "SFV2" | version i32 | node count i32
    version tags      keyed archive (format 1, empty in the client)
    descriptor        size u32 | `size` bytes (file type u32, hash)
    string table      "KA" 0x0002 | count u32 | count x (u16 length, utf-8)
                      | count x u32 id   -- every key and string below is an id
    body              an archive without its "KA" header: count u32, items

The body holds "#dataNodes" (materials) and "#hierarchy" (the entities; an
entity's children sit in its own "#hierarchy" list). An entity is
{"name", "id", "flags", "components": {"count", "0000": {...}, ...}} and every
component names its kind in "comp.typename".

Keyed archives: "KA" + u16 format, then
    0x0102  count u32, (u32 string id, variant value) pairs
    0xFF02  empty, nothing follows
    0x0001  count u32 -- only the empty version tags use it here; the older
            (variant key, variant value) layout stores strings inline and is
            refused rather than misread
A variant is a type byte and its payload; strings, fast names and file paths
are string ids in this flavour.

Map facts read from a scene (all in metres, x east / y north, which is the
replay's x / z):
  * the Landscape render object's bbox -- the terrain square, 600 x 600 m
    centred on the origin on every map; the battle minimap shows exactly it;
  * MapBorderComponent rects -- the playable area. Scenes with several map
    variants (milbase, milbase_02 ...) carry one border per variant, told
    apart by a LabelComponent label that maps.yaml repeats as the map's `tags`;
  * capture zones -- entities whose CustomPropertiesComponent says "type":
    "controlpoint" (the Encounter base) or "strategicpoint" (a Supremacy
    point, numbered by "baseID"), placed by their TransformComponent. Variant
    scenes label them like the borders.
"""

import struct

SIGNATURE = b"SFV2"


class DavaError(ValueError):
    pass


class _Reader:
    def __init__(self, raw):
        self.raw = raw
        self.names = {}

    def unpack(self, fmt, pos):
        try:
            return struct.unpack_from(fmt, self.raw, pos), pos + struct.calcsize(fmt)
        except struct.error:
            raise DavaError(f"truncated at {pos}") from None

    def take(self, size, pos):
        if size < 0 or pos + size > len(self.raw):
            raise DavaError(f"truncated at {pos}")
        return self.raw[pos:pos + size], pos + size

    def name(self, string_id):
        return self.names.get(string_id, f"#{string_id:08x}")

    _FIXED = {1: "<?", 2: "<i", 3: "<f", 7: "<I", 9: "<q", 10: "<Q", 11: "<2f", 12: "<3f",
              13: "<4f", 14: "<4f", 15: "<9f", 16: "<16f", 17: "<4f", 19: "<6f", 21: "<d",
              22: "<b", 23: "<B", 24: "<h", 25: "<H", 26: "<4f", 28: "<4f", 29: "<10f",
              30: "<4f"}

    def value(self, pos):
        (kind,), pos = self.unpack("<B", pos)
        if kind == 0:
            return None, pos
        if kind in (4, 18, 20):  # string, fast name, file path -> string id
            (string_id,), pos = self.unpack("<I", pos)
            return self.name(string_id), pos
        if kind == 5:  # wide string: u32 character count, utf-16
            (count,), pos = self.unpack("<i", pos)
            data, pos = self.take(2 * count, pos)
            return data.decode("utf-16-le", "replace"), pos
        if kind == 6:  # byte array
            (size,), pos = self.unpack("<i", pos)
            return self.take(size, pos)
        if kind == 8:  # nested keyed archive with a byte size in front
            (size,), pos = self.unpack("<i", pos)
            end = pos + size
            archive, stop = self.archive(pos)
            if stop != end:
                raise DavaError(f"archive at {pos} is {stop - pos} bytes, header says {size}")
            return archive, end
        if kind == 27:  # variant vector
            (count,), pos = self.unpack("<I", pos)
            items = []
            for _ in range(count):
                item, pos = self.value(pos)
                items.append(item)
            return items, pos
        fmt = self._FIXED.get(kind)
        if fmt is None:
            raise DavaError(f"unknown variant type {kind} at {pos - 1}")
        values, pos = self.unpack(fmt, pos)
        return (values[0] if len(values) == 1 else values), pos

    def items(self, pos, count):
        out = {}
        for _ in range(count):
            (key,), pos = self.unpack("<I", pos)
            out[self.name(key)], pos = self.value(pos)
        return out, pos

    def archive(self, pos):
        if self.raw[pos:pos + 2] != b"KA":
            raise DavaError(f"no keyed archive at {pos}")
        (version,), pos = self.unpack("<H", pos + 2)
        if version == 0xFF02:
            return {}, pos
        (count,), pos = self.unpack("<I", pos)
        if version == 0x0102:
            return self.items(pos, count)
        if version == 0x0001 and count == 0:  # the version tags; never filled in
            return {}, pos
        raise DavaError(f"keyed archive format {version:#06x} at {pos - 8}")


def read_scene(raw):
    """Parse a whole .sc2 into {"version", "nodes", "body": {...}}."""
    reader = _Reader(raw)
    if raw[:4] != SIGNATURE:
        raise DavaError("not a DAVA scene (no SFV2 signature)")
    (version, nodes), pos = reader.unpack("<ii", 4)
    _tags, pos = reader.archive(pos)
    (size,), pos = reader.unpack("<I", pos)
    _descriptor, pos = reader.take(size, pos)

    if raw[pos:pos + 4] != b"KA\x02\x00":
        raise DavaError(f"no string table at {pos}")
    (count,), pos = reader.unpack("<I", pos + 4)
    strings = []
    for _ in range(count):
        (length,), pos = reader.unpack("<H", pos)
        text, pos = reader.take(length, pos)
        strings.append(text.decode("utf-8", "replace"))
    ids, pos = reader.unpack(f"<{count}I", pos)
    reader.names = dict(zip(ids, strings))

    (count,), pos = reader.unpack("<I", pos)
    body, pos = reader.items(pos, count)
    if pos != len(raw):
        raise DavaError(f"{len(raw) - pos} unread bytes after the scene body")
    return {"version": version, "nodes": nodes, "body": body}


def entities(scene):
    """Every entity of the scene, depth first."""
    stack = list(reversed(scene["body"].get("#hierarchy") or []))
    while stack:
        entity = stack.pop()
        if not isinstance(entity, dict):
            continue
        yield entity
        stack.extend(reversed(entity.get("#hierarchy") or []))


def components(entity):
    """{typename: component} of one entity (the first of each kind)."""
    out = {}
    for key, comp in (entity.get("components") or {}).items():
        if key != "count" and isinstance(comp, dict):
            out.setdefault(comp.get("comp.typename"), comp)
    return out


def _floats(blob, count):
    if not isinstance(blob, bytes) or len(blob) != 4 * count:
        raise DavaError(f"expected {count} floats, got {blob!r:.40}")
    return struct.unpack(f"<{count}f", blob)


def landscape_bbox(scene):
    """(x0, y0, x1, y1) of the terrain, or None if the scene has no landscape."""
    for entity in entities(scene):
        render = components(entity).get("RenderComponent")
        obj = (render or {}).get("rc.renderObj")
        if isinstance(obj, dict) and obj.get("##name") == "Landscape" and "bbox" in obj:
            x0, y0, _z0, x1, y1, _z1 = _floats(obj["bbox"], 6)
            return (x0, y0, x1, y1)
    return None


def map_borders(scene):
    """[{"name", "labels", "rect": (x0, y0, x1, y1)}] for every map border."""
    out = []
    for entity in entities(scene):
        comps = components(entity)
        border = comps.get("MapBorderComponent")
        if border is None or "mbc.rect" not in border:
            continue
        out.append({"name": entity.get("name"), "labels": _labels(comps),
                    "rect": _floats(border["mbc.rect"], 4)})
    return out


def _labels(comps):
    return [str(label) for label in (comps.get("LabelComponent") or {}).get("lc.labels") or []]


_ZONE_KINDS = {"controlpoint": "base", "strategicpoint": "point"}


def capture_zones(scene):
    """[{"kind", "id", "labels", "pos": (x, y), "radius"}] for every capture
    zone: kind "base" (Encounter) or "point" (Supremacy, `id` = its baseID,
    the number the arena updates use). `radius` is None when the scene leaves
    it to the game -- most Encounter bases."""
    out = []
    for entity in entities(scene):
        comps = components(entity)
        props = (comps.get("CustomPropertiesComponent") or {}).get("cpc.properties.archive")
        kind = _ZONE_KINDS.get(props.get("type")) if isinstance(props, dict) else None
        if kind is None:
            continue
        where = (comps.get("TransformComponent") or {}).get("tc.worldTranslation")
        if not (isinstance(where, tuple) and len(where) == 3):
            raise DavaError(f"capture zone {entity.get('name')!r} has no position")
        radius = props.get("radius")
        out.append({"kind": kind, "id": props.get("baseID"), "labels": _labels(comps),
                    "pos": (where[0], where[1]),
                    "radius": float(radius) if isinstance(radius, (int, float)) else None})
    return out
