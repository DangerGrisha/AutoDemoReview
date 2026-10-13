"""Tests for the 2D replay tracks (blitzreview/replay.py) and the minimap
geometry they are drawn on (gamedata.minimap, assets/maps.json).

What would mislead a player: tanks drawn in the wrong place (axes swapped or
mirrored, a wrong scale), a dead tank that keeps driving, an enemy shown
where nobody could see it, or a "last seen" spot that is off by the
decimation. Two checks pin the axes against the client's own map data, with
no hand-made expectations: every position must lie inside the map's playable
border (a mirrored x leaves it on Himmelsdorf, whose border is lopsided), and
the recorder's team must start on the map's spawn points (a mirrored z or
swapped axes moves the start hundreds of metres away on Canyon, Savanna or
Himmelsdorf). Each of the three fixtures catches a different mistake.
"""

import json
import math
from pathlib import Path

from blitzreview import battle, gamedata, replay

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"


def _battles():
    return [(path.name, battle.load(path)) for path in sorted(FIXTURES.glob("*.wotbreplay"))]


def _same_yaw(a, b, tolerance=0.51):
    return abs((a - b + 180) % 360 - 180) <= tolerance


# --------------------------------------------------------------------------- #
# encoding and decimation (synthetic)
# --------------------------------------------------------------------------- #

def test_the_delta_coding_round_trips_across_the_yaw_seam():
    samples = [(-3.0, -250.04, 260.0, 179.4, -179.7, 6.98), (-2.7, -249.0, 259.5, -179.6, 179.2, 7.0),
               (-2.4, -248.2, 259.0, -170.0, 90.0, -19.97), (12.3, 100.06, -5.0, 0.0, 0.0, 0.0),
               (12.6, 100.5, -4.0, 359.0, -45.4, 2.24)]
    decoded = replay.decode(replay.encode(samples))
    assert len(decoded) == len(samples)
    for (t, x, z, yaw, turret, pitch), (dt, dx, dz, dyaw, dturret, dpitch) in zip(samples, decoded):
        assert abs(t - dt) < 0.051 and abs(x - dx) < 0.051 and abs(z - dz) < 0.051
        assert _same_yaw(yaw, dyaw) and -180 <= dyaw < 180
        assert _same_yaw(turret, dturret) and -180 <= dturret < 180
        assert abs(pitch - dpitch) < 0.051  # tenths: a zoomed view shows a whole degree


def test_tracks_stored_in_older_formats_still_decode():
    # Format 1 (battles stored before format 4): four numbers a sample, no turret.
    old = [-30, 1000, -2000, 90, 3, 5, 4, -10]
    assert replay.decode(old, track_format=1) == [(-3.0, 100.0, -200.0, 90, 0, 0.0),
                                                  (-2.7, 100.5, -199.6, 80, 0, 0.0)]
    # Format 2 (format 4 battles): five, the gun level.
    old = [-30, 1000, -2000, 90, -45, 3, 5, 4, -10, 5]
    assert replay.decode(old, track_format=2) == [(-3.0, 100.0, -200.0, 90, -45, 0.0),
                                                  (-2.7, 100.5, -199.6, 80, -40, 0.0)]


def test_decimation_takes_the_gun_angles_last_sent_before_each_sample():
    raw = [(i * 0.1, float(i), 0.0, 0.0, 0.0) for i in range(60)]        # 0 .. 5.9 s
    turret = [(0.0, 0.0), (1.05, math.pi / 2), (3.0, -math.pi / 4)]
    pitch = [(0.5, -3.5), (2.0, 6.0)]
    kept = replay.decimate(raw, 0.0, end=10.0, turret=turret, pitch=pitch)
    for t, _x, _z, _yaw, yaw_turret, gun_pitch in kept:
        want = 0 if t < 1.05 else 90 if t < 3.0 else -45
        assert _same_yaw(yaw_turret, want), (t, yaw_turret)
        assert gun_pitch == (0.0 if t < 0.5 else -3.5 if t < 2.0 else 6.0), (t, gun_pitch)


def test_decimation_spaces_samples_but_keeps_the_last_one_before_a_gap():
    start = 100.0
    raw = [(start + i * 0.1, float(i), 0.0, 0.0, 0.0) for i in range(-50, 41)]       # t -5 .. 4
    raw += [(start + 9.0 + i * 0.1, 50.0 + i, 0.0, 0.0, math.pi / 2) for i in range(10)]  # after 5 s unseen
    kept = replay.decimate(raw, start, end=60.0)
    times = [t for t, *_ in kept]
    assert times[0] >= -replay.PREROLL - 1e-6          # nothing long before the battle
    assert abs(times[0] + replay.PREROLL) < 0.11
    last_before_gap = max(t for t in times if t < 5)
    assert abs(last_before_gap - 4.0) < 1e-6           # the exact "last seen" sample
    assert any(abs(t - 9.0) < 1e-6 for t in times)     # the reappearance
    for a, b in zip(times, times[1:]):  # closer only when b is the last before a gap
        assert b - a >= replay.STEP - 1e-6 or abs(b - 4.0) < 1e-6 or b == times[-1], (a, b)
    assert all(_same_yaw(yaw, 90) for t, _x, _z, yaw, *_gun in kept if t > 5)


def test_decimation_stops_at_the_end_given():
    raw = [(i * 0.1, 0.0, 0.0, 0.0, 0.0) for i in range(200)]
    kept = replay.decimate(raw, 0.0, end=7.0)
    assert kept[-1][0] <= 7.0 and kept[-1][0] > 6.8


# --------------------------------------------------------------------------- #
# real replays
# --------------------------------------------------------------------------- #

def test_only_roster_vehicles_get_tracks_and_allies_from_the_start():
    for name, b in _battles():
        ids = {v["entity_id"] for v in b["vehicles"]}
        tracks = {t["id"]: replay.decode(t["p"]) for t in b["replay"]["tracks"]}
        assert set(tracks) <= ids, name                       # never the Avatar's track
        for v in b["vehicles"]:
            if v["ally"]:
                assert v["entity_id"] in tracks, (name, v["nickname"])
                assert tracks[v["entity_id"]][0][0] <= 0.5, (name, v["nickname"])


def test_tracks_end_where_the_vehicle_died():
    for name, b in _battles():
        tracks = {t["id"]: replay.decode(t["p"]) for t in b["replay"]["tracks"]}
        for v in b["vehicles"]:
            if v["death_time"] is None or v["entity_id"] not in tracks:
                continue
            last_t = tracks[v["entity_id"]][-1][0]
            assert last_t <= v["death_time"] + replay.DEATH_TAIL + 0.06, (name, v["nickname"])
            # every death in the stream comes with a position right before it
            assert v["death_time"] - last_t < 0.4, (name, v["nickname"], last_t)


def test_nothing_is_kept_past_the_end_of_the_recording():
    for name, b in _battles():
        end = b["replay"]["end"]
        assert abs(end - (b["recording_end_clock"] - b["battle_start_clock"])) < 0.06, name
        for t in b["replay"]["tracks"]:
            assert replay.decode(t["p"])[-1][0] <= end + 0.06, name


def test_every_position_lies_inside_the_maps_playable_border():
    for name, b in _battles():
        geo = gamedata.minimap(b["map"]["key"])
        assert geo is not None and geo["border"], name
        x0, z0, x1, z1 = geo["border"]
        for t in b["replay"]["tracks"]:
            for _t, x, z, *_angles in replay.decode(t["p"]):
                assert x0 - 3 <= x <= x1 + 3 and z0 - 3 <= z <= z1 + 3, (name, t["id"], x, z)


def test_the_recorders_team_starts_on_the_maps_spawn_points():
    maps = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]
    for name, b in _battles():
        respawn = maps[b["map"]["key"]]["respawn"]
        points = respawn["allies"] + respawn["enemies"]  # which list is whose varies by patch
        allies = {v["entity_id"] for v in b["vehicles"] if v["ally"]}
        misses = []
        for t in b["replay"]["tracks"]:
            before = [s for s in replay.decode(t["p"]) if s[0] <= 0]
            if t["id"] in allies and before:
                _t, x, z, *_angles = before[-1]
                misses.append(min(math.hypot(x - px, z - pz) for px, pz in points))
        misses.sort()
        assert len(misses) >= 5 and misses[len(misses) // 2] < 25, (name, misses)


def test_enemies_have_samples_only_while_spotted():
    # Allies are tracked without breaks; enemies drop out when they are not
    # spotted. Some enemy in these battles must have such a break, or the
    # "last seen" marker would never be exercised.
    gaps = 0
    for name, b in _battles():
        allies = {v["entity_id"] for v in b["vehicles"] if v["ally"]}
        for t in b["replay"]["tracks"]:
            times = [s[0] for s in replay.decode(t["p"])]
            breaks = sum(1 for a, c in zip(times, times[1:]) if c - a > replay.GAP)
            if t["id"] in allies:
                assert breaks == 0, (name, t["id"])
            else:
                gaps += breaks
    assert gaps > 0


def test_tracks_stay_small_enough_to_store_and_embed():
    for name, b in _battles():
        size = len(json.dumps(b["replay"], separators=(",", ":")))
        assert size < 150_000, (name, size)


# --------------------------------------------------------------------------- #
# minimap geometry
# --------------------------------------------------------------------------- #

def test_every_shipped_map_has_consistent_geometry():
    maps = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]
    for key, entry in maps.items():
        wx0, wz0, wx1, wz1 = entry["world"]
        bx0, bz0, bx1, bz1 = entry["border"]
        assert wx0 < bx0 < bx1 < wx1 and wz0 < bz0 < bz1 < wz1, key
        left, top, right, bottom = entry["minimap_crop"]
        assert 0 <= left < right <= 512 and 0 <= top < bottom <= 512, key
        for team in entry["respawn"].values():
            for x, z in team:
                assert bx0 - 1 <= x <= bx1 + 1 and bz0 - 1 <= z <= bz1 + 1, (key, x, z)


def test_map_variants_get_their_own_border():
    maps = json.loads((gamedata.ASSETS / "maps.json").read_text(encoding="utf-8"))["maps"]
    full, west = maps["milbase"]["border"], maps["milbase_02"]["border"]
    assert full != west and west[2] < 0 < full[2]  # milbase_02 is the western half


def test_the_replay_view_is_a_square_around_the_border_inside_the_world():
    for key in ("canyon", "himmelsdorf", "milbase_04", "karieri", "neptune"):
        geo = gamedata.minimap(key)
        vx0, vz0, vx1, vz1 = geo["view"]
        bx0, bz0, bx1, bz1 = geo["border"]
        wx0, wz0, wx1, wz1 = geo["world"]
        assert abs((vx1 - vx0) - (vz1 - vz0)) < 0.2, key
        assert vx0 <= bx0 and vz0 <= bz0 and vx1 >= bx1 and vz1 >= bz1, key
        assert vx0 >= wx0 - 0.1 and vz0 >= wz0 - 0.1 and vx1 <= wx1 + 0.1 and vz1 <= wz1 + 0.1, key


def test_unknown_maps_have_no_geometry():
    assert gamedata.minimap("no_such_map") is None
    assert gamedata.minimap(None) is None
