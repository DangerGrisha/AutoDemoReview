"""Tests for the joined battle dict and static game data (battle.py, gamedata.py).

The battle dict is what the site stores and the report renders, so the risks
are about MEANING, not crashes: the wrong player marked as "you", allies and
enemies swapped, a win shown as a loss, a tank shown by its id because the
tank table and the replay disagree on how ids are built. The checks below pin
those down on real replays -- including one whose recorder left before the
results arrived, which must still produce a usable (if thinner) report.

The sweep at the end re-runs the invariants over every replay in the local
game folder when it exists (WOTB_REPLAY_DIR overrides the default path).
"""

import json
import os
import unittest
from pathlib import Path

from blitzreview import battle, gamedata

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
LOCAL_REPLAYS = Path(os.environ.get(
    "WOTB_REPLAY_DIR", Path.home() / "AppData" / "Local" / "wotblitz" / "DAVAProject" / "replays"))


def _load(name):
    return battle.load(FIXTURES / name)


def _invariants(b, label):
    """Checks every battle dict must pass, with or without battle results."""
    json.dumps(b)  # storable as-is
    vehicles = b["vehicles"]
    assert len(vehicles) == 14, (label, len(vehicles))
    authors = [v for v in vehicles if v["is_author"]]
    assert len(authors) == 1, label
    me = authors[0]
    assert me["ally"] and me["nickname"] == b["author"]["nickname"], label
    assert me["tank"]["tank_id"] == b["author"]["tank"]["tank_id"], label
    assert sum(v["ally"] for v in vehicles) == 7, label
    assert all(v["tank"]["known"] for v in vehicles), (label, [v["tank"] for v in vehicles])
    assert b["map"]["known"] and b["map"]["has_minimap"], (label, b["map"])
    # allies first; within a side the biggest damage first
    sides = [v["ally"] for v in vehicles]
    assert sides == sorted(sides, reverse=True), label
    if b["has_results"]:
        assert b["outcome"] in ("win", "loss", "draw"), label
        for team_ally in (True, False):
            side = [v for v in vehicles if v["ally"] == team_ally]
            if all(v["alive"] is False for v in side):
                assert b["outcome"] == ("loss" if team_ally else "win"), (label, b["outcome"])
        names = {v["entity_id"] for v in vehicles}
        for v in vehicles:
            assert (v["killer_id"] in names) == (v["alive"] is False), (label, v["nickname"])
    else:
        assert b["outcome"] == "unknown", label
    for kill in b["kills"]:
        assert kill["t"] is not None and 0 <= kill["t"] <= (b["duration"] or 1e9) + 1, (label, kill)


def test_canyon_with_results():
    b = _load("v11_20_canyon_results.wotbreplay")
    _invariants(b, "canyon")
    assert b["map"]["name"] == "Каньон" and b["mode"]["name"] == "Встречный бой"
    assert b["outcome"] == "loss"  # every ally was destroyed
    me = next(v for v in b["vehicles"] if v["is_author"])
    assert me["tank"]["name"] == "Объект 704" and me["tank"]["tier"] == 9
    assert me["stats"]["damage"] == 1399 and me["alive"] is False
    killer = next(v for v in b["vehicles"] if v["entity_id"] == me["killer_id"])
    assert killer["nickname"] == "Kossmooss" and not killer["ally"]
    assert me["death_time"] == 108.9  # 128.54 s into the recording, battle began at 19.62


def test_old_client_version_with_results():
    b = _load("v11_7_himmelsdorf_results.wotbreplay")
    _invariants(b, "himmelsdorf")
    assert b["client_version"] == "11.7.1" and b["map"]["key"] == "himmelsdorf"


def test_a_replay_without_results_still_names_everyone():
    b = _load("v11_20_savanna_no_results.wotbreplay")
    _invariants(b, "savanna")
    assert not b["has_results"] and b["mode"] is None
    assert any("no battle results" in w for w in b["warnings"])
    assert all(v["stats"] is None for v in b["vehicles"])
    me = next(v for v in b["vehicles"] if v["is_author"])
    assert me["alive"] is False and me["death_time"] is not None
    assert len(b["kills"]) == 3


def test_tank_ids_follow_the_nation_nibble_of_the_shipped_table():
    """tank_id = index << 8 | nation << 4 | 1 -- a shuffled NATIONS breaks names."""
    table = json.loads((gamedata.ASSETS / "vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    assert len(table) > 500
    for tank_id, entry in table.items():
        tid = int(tank_id)
        assert tid & 0xF == 1, tank_id
        assert gamedata.NATIONS[(tid >> 4) & 0xF] == entry["nation"], (tank_id, entry)
        assert 1 <= entry["tier"] <= 10 and entry["type"] in gamedata.CLASS_NAMES, entry


def test_unknown_tanks_and_maps_degrade_to_ids():
    tank = gamedata.vehicle(0xFF71)
    assert tank["known"] is False and tank["name"] == f"#{0xFF71}" and tank["nation"] == "other"
    assert gamedata.map_info("no_such_map")["known"] is False
    assert gamedata.map_info(map_id=27)["key"] == "canyon"


def test_minimaps_are_webp_images():
    files = sorted((gamedata.ASSETS / "minimaps").glob("*.webp"))
    assert len(files) >= 40
    for path in files:
        head = path.read_bytes()[:12]
        assert head[:4] == b"RIFF" and head[8:12] == b"WEBP", path.name


def test_every_local_replay_passes_the_invariants():
    replays = sorted(LOCAL_REPLAYS.glob("*.wotbreplay")) if LOCAL_REPLAYS.is_dir() else []
    if not replays:
        raise unittest.SkipTest(f"no local replays in {LOCAL_REPLAYS}")
    for path in replays:
        _invariants(battle.load(path), path.name)
