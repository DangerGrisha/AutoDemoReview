"""Tests for the DAVA scene reader (blitzreview/dava.py).

The reader only runs offline, on the installed client, but what it extracts
decides where every tank lands on the minimap: the terrain square and the
per-variant playable border. The scenes here are built by a small reference
encoder written from the format notes in dava.py, so the tests run without the
game; one optional test reads a real client scene when it is installed.
"""

import struct
from pathlib import Path

from blitzreview import dava

CLIENT_MAPS = Path(r"D:\games\Steam\steamapps\common\World of Tanks Blitz\Data\3d\Maps")


class _Encoder:
    """Writes the Blitz flavour of a scene: string table + id-keyed archives."""

    def __init__(self):
        self.strings = [""]

    def sid(self, text):
        if text not in self.strings:
            self.strings.append(text)
        return struct.pack("<I", 0x9E370000 + self.strings.index(text))

    def string(self, text):
        return b"\x04" + self.sid(text)

    def blob(self, data):
        return b"\x06" + struct.pack("<i", len(data)) + data

    def floats(self, *values):
        return self.blob(struct.pack(f"<{len(values)}f", *values))

    def archive(self, items):
        if not items:
            return b"KA\x02\xff"
        return (b"KA" + struct.pack("<HI", 0x0102, len(items))
                + b"".join(self.sid(key) + value for key, value in items.items()))

    def nested(self, items):
        body = self.archive(items)
        return b"\x08" + struct.pack("<i", len(body)) + body

    def vector(self, values):
        return b"\x1b" + struct.pack("<I", len(values)) + b"".join(values)

    def entity(self, name, comps, children=()):
        components = {"count": b"\x07" + struct.pack("<I", len(comps))}
        for i, comp in enumerate(comps):
            components[f"{i:04d}"] = self.nested(comp)
        items = {"##name": self.string("Entity"), "name": self.string(name),
                 "components": self.nested(components), "flags": b"\x07" + struct.pack("<I", 2)}
        if children:
            items["#hierarchy"] = self.vector([self.nested(c) for c in children])
        return items

    def scene(self, entities):
        body_items = {"#dataNodes": self.vector([]),
                      "#hierarchy": self.vector([self.nested(e) for e in entities])}
        body = struct.pack("<I", len(body_items)) + b"".join(
            self.sid(k) + v for k, v in body_items.items())
        table = b"KA\x02\x00" + struct.pack("<I", len(self.strings))
        for text in self.strings:
            raw = text.encode("utf-8")
            table += struct.pack("<H", len(raw)) + raw
        table += b"".join(struct.pack("<I", 0x9E370000 + i) for i in range(len(self.strings)))
        descriptor = struct.pack("<I", 20) + struct.pack("<I", 1) + bytes(16)
        tags = b"KA" + struct.pack("<HI", 1, 0)
        return b"SFV2" + struct.pack("<ii", 48, 3) + tags + descriptor + table + body


def _landscape(enc, half=300.0):
    render = {"comp.typename": enc.string("RenderComponent"),
              "rc.renderObj": enc.nested({"##name": enc.string("Landscape"),
                                          "bbox": enc.floats(-half, -half, 0, half, half, 90)})}
    return enc.entity("Landscape", [render])


def _border(enc, name, rect, label=None):
    comps = []
    if label:
        comps.append({"comp.typename": enc.string("LabelComponent"),
                      "lc.labels": enc.vector([enc.string(label)])})
    comps.append({"comp.typename": enc.string("MapBorderComponent"),
                  "mbc.rect": enc.floats(*rect), "mbc.visualHeight": b"\x03" + struct.pack("<f", .9)})
    return enc.entity(name, comps)


def _scene_bytes():
    enc = _Encoder()
    whole = _border(enc, "MapBorder1", (-264.0, -263.5, 264.0, 263.5), "mlb1")
    half = _border(enc, "MapBorder2", (-264.0, -263.5, -2.0, 263.5), "mlb2")
    group = enc.entity("borders", [], children=[whole, half])  # nested one level down
    return enc.scene([_landscape(enc), group])


def test_terrain_and_labelled_borders_come_out_of_a_scene():
    scene = dava.read_scene(_scene_bytes())
    assert scene["version"] == 48
    assert dava.landscape_bbox(scene) == (-300.0, -300.0, 300.0, 300.0)
    borders = dava.map_borders(scene)
    assert [(b["name"], b["labels"]) for b in borders] == [("MapBorder1", ["mlb1"]),
                                                            ("MapBorder2", ["mlb2"])]
    assert borders[1]["rect"] == (-264.0, -263.5, -2.0, 263.5)


def test_an_unlabelled_single_border_and_a_scene_without_landscape():
    enc = _Encoder()
    scene = dava.read_scene(enc.scene([_border(enc, "MapBorder", (-250, -250, 250, 250))]))
    assert dava.landscape_bbox(scene) is None
    assert dava.map_borders(scene) == [{"name": "MapBorder", "labels": [],
                                        "rect": (-250.0, -250.0, 250.0, 250.0)}]


def _zone(enc, name, props, pos, label=None):
    values = {key: (enc.string(value) if isinstance(value, str) else
                    b"\x03" + struct.pack("<f", value) if isinstance(value, float) else
                    b"\x02" + struct.pack("<i", value)) for key, value in props.items()}
    comps = [{"comp.typename": enc.string("TransformComponent"),
              "tc.worldTranslation": b"\x0c" + struct.pack("<3f", *pos)},
             {"comp.typename": enc.string("CustomPropertiesComponent"),
              "cpc.properties.archive": enc.nested(values)}]
    if label:
        comps.append({"comp.typename": enc.string("LabelComponent"),
                      "lc.labels": enc.vector([enc.string(label)])})
    return enc.entity(name, comps)


def test_capture_zones_come_with_their_kind_number_and_radius():
    enc = _Encoder()
    scene = dava.read_scene(enc.scene([
        _zone(enc, "ControlPoint", {"type": "controlpoint", "pointsPerSecond": 1.0}, (28.4, 24.9, 26.3)),
        _zone(enc, "2.sc2", {"type": "strategicpoint", "baseID": 1, "radius": 15.0}, (-54.7, 9.2, 30.0)),
        _zone(enc, "Control2", {"type": "controlpoint", "radius": 15.0}, (-110.0, -11.0, 5.0), "mlb2"),
        _zone(enc, "Spawn11", {"type": "spawnpoint", "team": 1}, (-236.0, 184.8, 31.6)),
    ]))
    zones = dava.capture_zones(scene)
    assert [(z["kind"], z["id"], z["labels"], z["radius"]) for z in zones] == [
        ("base", None, [], None), ("point", 1, [], 15.0), ("base", None, ["mlb2"], 15.0)]
    x, y = zones[1]["pos"]  # the ground plane: x and y, never the height
    assert abs(x + 54.7) < 1e-4 and abs(y - 9.2) < 1e-4


def _raises(data, words):
    try:
        dava.read_scene(data)
    except dava.DavaError as exc:
        assert words in str(exc), exc
        return
    raise AssertionError(f"no DavaError ({words})")


def test_broken_scenes_fail_loudly_instead_of_misreading():
    good = _scene_bytes()
    _raises(b"SFV3" + good[4:], "SFV2")
    _raises(good[:len(good) // 2], "truncated")
    _raises(good + b"\x00", "unread bytes")
    # a component value of a variant type the reader does not know
    enc = _Encoder()
    bad = enc.scene([enc.entity("x", [{"comp.typename": b"\x63"}])])
    _raises(bad, "variant type")


def test_the_reader_agrees_with_the_shipped_maps_json():
    path = CLIENT_MAPS / "25_canyon_ca" / "25_canyon_ca.sc2.dvpl"
    if not path.is_file():
        import unittest
        raise unittest.SkipTest("World of Tanks Blitz client not installed here")
    import json

    from blitzreview import dvpl, gamedata
    scene = dava.read_scene(dvpl.read(path))
    canyon = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]["canyon"]
    assert [round(v, 1) for v in dava.landscape_bbox(scene)] == canyon["world"]
    assert [round(v, 1) for v in dava.map_borders(scene)[0]["rect"]] == canyon["border"]
    zones = dava.capture_zones(scene)
    base = next(z for z in zones if z["kind"] == "base")
    assert [round(v, 1) for v in base["pos"]] == canyon["base"][:2]
    points = sorted((z for z in zones if z["kind"] == "point"), key=lambda z: z["id"])
    assert [[z["id"], *(round(v, 1) for v in z["pos"]), z["radius"]] for z in points] == canyon["points"]
