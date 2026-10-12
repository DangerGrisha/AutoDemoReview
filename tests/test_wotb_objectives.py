"""Tests for the capture zones (assets/maps.json, gamedata.objectives) and the
capture updates of the packet stream (arena.py, battle.py `objectives`).

What would mislead a player: a base drawn where it is not, a point captured
by the wrong team, the score of one team shown for the other, a mode guessed
wrong for a replay without results. Every check below ties the updates to an
independent source -- the tracks (a capture needs a tank of the capturing
team inside the zone), the kill feed (Supremacy takes points from a team
exactly when it loses a tank) or battle_results.dat (the mode).
"""

import json
import math
from pathlib import Path

from blitzreview import arena, battle, battle_results, container, gamedata, packets, replay

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
SUPREMACY = ("v11_7_himmelsdorf_results.wotbreplay", "v11_20_savanna_no_results.wotbreplay")
RIFT = "v11_20_rift_base_capture.wotbreplay"


def _maps():
    return json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]


def _load(name):
    return battle.load(FIXTURES / name)


def _nearest(tracks, eids, t, x, z):
    """Distance from (x, z) to the closest of `eids` seen within a second of t."""
    best = math.inf
    for eid in eids:
        for st, sx, sz, _yaw in tracks.get(eid, ()):
            if abs(st - t) <= 1.0:
                best = min(best, math.hypot(sx - x, sz - z))
    return best


# --------------------------------------------------------------------------- #
# zones from the client's scenes
# --------------------------------------------------------------------------- #

def test_every_map_has_its_zones_inside_the_playable_border():
    for key, entry in _maps().items():
        x0, z0, x1, z1 = entry["border"]
        if 0 in entry["modes"]:
            x, z, r = entry["base"]
            assert x0 < x < x1 and z0 < z < z1 and 10 <= r <= 40, (key, entry["base"])
        if 1 in entry["modes"]:
            ids = [p[0] for p in entry["points"]]
            assert len(ids) >= 2 and ids == list(range(len(ids))), (key, ids)
            for _id, x, z, r in entry["points"]:
                assert x0 < x < x1 and z0 < z < z1 and 10 <= r <= 40, (key, _id)
        else:
            assert "points" not in entry, key  # Encounter-only variants


def test_objectives_follow_the_mode_and_letter_the_points():
    base = gamedata.objectives("canyon", 0)
    assert len(base) == 1 and base[0]["label"] == "" and base[0]["r"] == 25.0
    assert [z["label"] for z in gamedata.objectives("himmelsdorf", 1)] == ["A", "B", "C", "D"]
    assert gamedata.objectives("milbase_02", 1) == []         # the variant has no Supremacy
    assert gamedata.objectives("milbase_02", 0)[0]["r"] == 15.0  # its scene states a radius
    assert gamedata.objectives("canyon", None) == []
    assert gamedata.objectives("no_such_map", 1) == []


# --------------------------------------------------------------------------- #
# capture updates against the tracks, the kill feed and the results
# --------------------------------------------------------------------------- #

def test_the_stream_tells_the_mode_that_battle_results_report():
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        files = container.open_replay(path)
        mode = arena.parse(packets.read_stream(files.data))["mode"]
        if files.battle_results is not None:
            assert mode == battle_results.parse(files.battle_results)["mode"], path.name
        else:
            assert mode == arena.MODE_SUPREMACY, path.name  # savanna sends point states
            assert _load(path.name)["mode"]["name"] == "Превосходство"


def test_a_point_capture_starts_with_a_tank_of_that_team_on_the_point():
    # Enemies are drawn only while spotted, so only the recorder's team's
    # captures can be checked -- those must all have a tank on the point.
    checked = 0
    for name in SUPREMACY:
        b = _load(name)
        zones = {z["id"]: z for z in gamedata.objectives(b["map"]["key"], 1)}
        tracks = {t["id"]: replay.decode(t["p"]) for t in b["replay"]["tracks"]}
        mine = [v["entity_id"] for v in b["vehicles"] if v["ally"]]
        progress = {}
        for t, point, _owner, capturer, value in b["objectives"]["points"]:
            assert point in zones, (name, point)
            started = value > 0 and progress.get(point, 0) == 0
            progress[point] = value
            if started and capturer == b["author"]["team"]:
                zone = zones[point]
                gap = _nearest(tracks, mine, t, zone["x"], zone["z"])
                assert gap <= zone["r"] + 6, (name, t, zone["label"], gap)
                checked += 1
    assert checked >= 4, checked


def test_the_encounter_base_capture_goes_to_the_team_standing_on_it():
    # On Rift a lone ally rolled onto the base twice. The server puts that
    # progress in the OTHER team's record; arena.py credits it to the ally's
    # team -- swap that and this test fails.
    b = _load(RIFT)
    base = gamedata.objectives("rift", 0)[0]
    tracks = {t["id"]: replay.decode(t["p"]) for t in b["replay"]["tracks"]}
    mine = [v["entity_id"] for v in b["vehicles"] if v["ally"]]
    rising = [(t, team) for t, team, value in b["objectives"]["base"] if value > 0]
    assert len(rising) >= 8
    for t, team in rising:
        assert team == b["author"]["team"], (t, team)
        assert _nearest(tracks, mine, t, base["x"], base["z"]) <= base["r"] + 3, t


def test_supremacy_takes_points_from_a_team_exactly_when_it_loses_a_tank():
    # The client's gameplay_settings: 40 points for a top-tier tank, 30 for a lower one.
    for name in SUPREMACY:
        b = _load(name)
        team_of = {v["entity_id"]: v["team"] for v in b["vehicles"]}
        losses = {}
        for kill in b["kills"]:
            losses.setdefault(team_of[kill["victim"]], []).append(kill["t"])
        assert b["objectives"]["goal"] == 1000
        last, drops = {}, 0
        for t, team, points in b["objectives"]["score"]:
            assert team in (1, 2) and 0 <= points <= 1000, (name, t, team, points)
            if team in last and points < last[team]:
                assert last[team] - points in (30, 40), (name, t, last[team], points)
                assert any(0 <= t - lost <= 1.0 for lost in losses.get(team, ())), (name, t, team)
                drops += 1
            last[team] = points
        end = b["replay"]["end"]
        assert drops == sum(1 for times in losses.values() for t in times if t <= end - 1), name


def test_encounter_battles_carry_no_score_and_supremacy_no_base():
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        b = battle.load(path)
        obj = b["objectives"]
        if b["mode"]["id"] == 0:
            assert obj["base"] and not obj["points"] and not obj["score"], path.name
        else:
            assert obj["points"] and obj["score"] and not obj["base"], path.name
