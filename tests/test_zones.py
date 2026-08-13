"""Tests for map callout zones (demoreview/zones.py + assets/zones.json).

The risk with naming a location is not that the lookup crashes -- it is that it
confidently returns the WRONG callout. A report that says "you keep dying at
Palace" when the deaths were at Connector is worse than one that says "the same
spot", because the player will go and check.

So the table itself is under test, not just the lookup: it is generated from
the maps' own `env_cs_place` entities by tools/extract_zones.py, and these
checks pin down that the generated file is sane (real maps, real callouts,
plausible volumes) and that lookups land where they should on positions taken
from the actual game.
"""

import json
import unittest
from pathlib import Path

from demoreview import zones

ROOT = Path(__file__).resolve().parent.parent

# Measured on the full 218k-sample ground truth; the fixture below is a 400-row
# random draw from it, so leave headroom. Set above the 87.5% that ignoring the
# Z axis scores, so dropping height would fail rather than merely look worse.
MIN_ACCURACY = 0.88


def _table():
    raw = json.loads((ROOT / "src" / "demoreview" / "assets"
                      / "zones.json").read_text(encoding="utf-8"))
    return raw["maps"]


def _ground_truth():
    """Real positions paired with the callout CS2 itself reported there."""
    path = ROOT / "tests" / "data" / "mirage_places.json"
    if not path.is_file():
        raise unittest.SkipTest("ground-truth fixture missing")
    return json.loads(path.read_text(encoding="utf-8"))["samples"]


def _widest(map_name, name):
    """The largest volume carrying `name` (a callout can be several boxes)."""
    return max((z for z in _table()[map_name] if z["name"] == name),
               key=lambda z: (z["max"][0] - z["min"][0]) * (z["max"][1] - z["min"][1]))


# --------------------------------------------------------------------------- #
# the generated table
# --------------------------------------------------------------------------- #

def test_the_shipped_table_covers_the_maps_we_can_replay():
    """Every map with converted 3D geometry should be nameable."""
    maps_dir = ROOT / "web" / "static" / "replay3d" / "assets" / "maps"
    converted = {p.name for p in maps_dir.iterdir() if p.is_dir()} \
        if maps_dir.is_dir() else set()
    if not converted:
        raise unittest.SkipTest("no converted maps on this machine")

    missing = {m for m in converted if not zones.has_zones(m)}
    assert not missing, f"no callout zones for {sorted(missing)}"


def test_every_zone_is_a_real_box_of_plausible_size():
    for map_name, entries in _table().items():
        assert entries, map_name
        for z in entries:
            lo, hi = z["min"], z["max"]
            assert z["name"].strip(), map_name
            for axis in range(3):
                assert hi[axis] > lo[axis], (map_name, z["name"], axis)
            # A callout is a room or a corner, not a pixel and not the map.
            span = max(hi[i] - lo[i] for i in range(2))
            assert 32 < span < 6000, (map_name, z["name"], span)
            # Hammer units: competitive maps live well inside +-20000.
            assert all(abs(v) < 20_000 for v in lo + hi), (map_name, z["name"])


def test_one_callout_may_be_made_of_several_volumes():
    """Mappers split a callout into pieces (cs_shelter has five "Yard" boxes).

    That is legitimate and the lookup handles it -- each box is tested on its
    own -- but two IDENTICAL boxes would mean the extractor emitted a duplicate.
    """
    for map_name, entries in _table().items():
        boxes = [(z["name"], tuple(z["min"]), tuple(z["max"])) for z in entries]
        assert len(boxes) == len(set(boxes)), f"{map_name} has duplicate volumes"


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

def test_lookup_agrees_with_the_game_on_real_positions():
    """The only check that matters: does it name the place CS2 names?"""
    if not zones.has_zones("de_mirage"):
        raise unittest.SkipTest("de_mirage zones missing")
    samples = _ground_truth()
    hits = sum(1 for x, y, z, name in samples
               if zones.zone_of("de_mirage", x, y, z) == name)
    accuracy = hits / len(samples)
    assert accuracy >= MIN_ACCURACY, (
        f"only {accuracy*100:.1f}% of {len(samples)} real positions got the "
        f"callout the game reports (floor is {MIN_ACCURACY*100:.0f}%)")


def test_every_real_position_gets_some_name():
    """Silence would be a worse failure than an imperfect callout."""
    if not zones.has_zones("de_mirage"):
        raise unittest.SkipTest("de_mirage zones missing")
    unnamed = [s for s in _ground_truth()
               if zones.zone_of("de_mirage", s[0], s[1], s[2]) is None]
    assert not unnamed, f"{len(unnamed)} in-play positions got no zone at all"


def test_height_separates_the_levels_of_a_stacked_map():
    """Nuke stacks its bombsites, so the same (x, y) is two different places."""
    if not zones.has_zones("de_nuke"):
        raise unittest.SkipTest("de_nuke zones missing")

    # Find the biggest genuinely stacked pair rather than assuming which
    # callouts they are -- map updates rename places, they don't unstack them.
    entries = _table()["de_nuke"]
    stacked = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            ox = min(a["max"][0], b["max"][0]) - max(a["min"][0], b["min"][0])
            oy = min(a["max"][1], b["max"][1]) - max(a["min"][1], b["min"][1])
            if ox > 0 and oy > 0 and (a["min"][2] > b["max"][2]
                                      or b["min"][2] > a["max"][2]):
                stacked.append((ox * oy, a, b))
    if not stacked:
        raise unittest.SkipTest("no stacked volumes in this build")
    _area, upper, lower = max(stacked, key=lambda t: t[0])
    if upper["min"][2] < lower["min"][2]:
        upper, lower = lower, upper

    # One column of the map, two heights -> two different answers.
    x = (max(upper["min"][0], lower["min"][0])
         + min(upper["max"][0], lower["max"][0])) / 2
    y = (max(upper["min"][1], lower["min"][1])
         + min(upper["max"][1], lower["max"][1])) / 2
    high = zones.zone_of("de_nuke", x, y, (upper["min"][2] + upper["max"][2]) / 2)
    low = zones.zone_of("de_nuke", x, y, (lower["min"][2] + lower["max"][2]) / 2)

    assert high and low and high != low, (high, low)
    # ...and dropping the height collapses them into one answer, which is the
    # whole reason death spots carry z.
    flat = zones.zone_of("de_nuke", x, y)
    assert flat in (high, low)


def test_a_point_far_off_the_map_is_not_given_a_name():
    """Better no callout than a confident wrong one."""
    assert zones.zone_of("de_mirage", 50_000.0, 50_000.0, 0.0) is None


def test_a_point_just_outside_a_volume_snaps_to_it():
    """Boosts and ledges put a death a few units above the box."""
    if not zones.has_zones("de_mirage"):
        raise unittest.SkipTest("de_mirage zones missing")
    box = _widest("de_mirage", "Middle")
    cx = (box["min"][0] + box["max"][0]) / 2
    cy = (box["min"][1] + box["max"][1]) / 2

    just_above = box["max"][2] + zones.MAX_SNAP_U / 2
    far_above = box["max"][2] + zones.MAX_SNAP_U * 10

    assert zones.zone_of("de_mirage", cx, cy, just_above) is not None
    assert zones.zone_of("de_mirage", cx, cy, far_above) is None


def test_an_unknown_map_returns_nothing_instead_of_guessing():
    assert zones.zone_of("de_notamap", 0.0, 0.0, 0.0) is None
    assert zones.zone_label("de_notamap", 0.0, 0.0) is None
    assert not zones.has_zones("de_notamap")


def test_overlapping_volumes_resolve_to_the_more_specific_one():
    """Measured rule: the smallest containing volume matches the game best.

    On de_mirage this scored 92.5% against the game's own answer, versus 87.9%
    for "deepest inside" and 86.9% for "smallest footprint".
    """
    if not zones.has_zones("de_mirage"):
        raise unittest.SkipTest("de_mirage zones missing")
    big, small = _widest("de_mirage", "PalaceInterior"), _widest("de_mirage", "Scaffolding")
    assert (big["max"][0] - big["min"][0]) * (big["max"][1] - big["min"][1]) > \
           (small["max"][0] - small["min"][0]) * (small["max"][1] - small["min"][1])

    # A point inside the small one is inside the big one too on this map.
    cx = (small["min"][0] + small["max"][0]) / 2
    cy = (small["min"][1] + small["max"][1]) / 2
    cz = (small["min"][2] + small["max"][2]) / 2
    if not (big["min"][0] <= cx <= big["max"][0]
            and big["min"][1] <= cy <= big["max"][1]
            and big["min"][2] <= cz <= big["max"][2]):
        raise unittest.SkipTest("these two volumes no longer overlap")
    assert zones.zone_of("de_mirage", cx, cy, cz) == "Scaffolding"


# --------------------------------------------------------------------------- #
# labels
# --------------------------------------------------------------------------- #

def test_run_together_callouts_are_split_for_reading():
    assert zones.label("PalaceInterior") == "Palace Interior"
    assert zones.label("BackAlley") == "Back Alley"
    assert zones.label("SnipersNest") == "Snipers Nest"
    assert zones.label("CTSpawn") == "CT Spawn"      # not "CTS pawn"
    assert zones.label("TSpawn") == "T Spawn"
    assert zones.label("TRamp") == "T Ramp"
    assert zones.label("TopofMid") == "Top of Mid"   # generic rules can't fix it
    assert zones.label("BombsiteA") == "Bombsite A"
    assert zones.label("Middle") == "Middle"
    assert zones.label(None) is None


def test_every_shipped_name_produces_a_readable_label():
    for map_name, entries in _table().items():
        for z in entries:
            text = zones.label(z["name"])
            assert text and not text.startswith(" ") and "  " not in text, \
                (map_name, z["name"], text)
