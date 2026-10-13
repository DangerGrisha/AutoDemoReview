"""Tests for the view through a player's eyes: the recorder's own camera
(blitzreview/camera.py), the gun's pitch (events.gun_pitch) and the gun data
of every tank (assets/vehicles.json `eye` and `pitch`).

The camera packets carry no schema either, so the checks tie them to
geometry they must agree with. The camera's yaw must point from the
recorder's tank at the aim point the same packet names. In the sniper sight
the line of sight must cross the tank at the height of its gun -- the
tank's `eye` from the client's XML -- and in the arcade camera at the point
that camera orbits, which the 3D view's script hard-codes (ORBIT_UP). The
6 pitch bits of every gun, read over the limits from the XML, must match the
gun pitch the recorder's own camera packets state in radians. A wrong
height, a wrong axis or a wrong pitch range fails these by metres or
degrees. Synthetic streams pin the bookkeeping: sniper visits, the zoom,
the yaw turning through north, the thinning's promise.
"""

import bisect
import json
import math
import statistics
import struct
from pathlib import Path

from blitzreview import battle, camera, events, gamedata, render, replay
from blitzreview.container import open_replay
from blitzreview.packets import Stream, positions, read_stream

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"


def _fixtures():
    out = []
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        b = battle.load(path)
        stream = read_stream(open_replay(path).data)
        out.append((path.name, b, stream))
    return out


def _wrap(degrees):
    return (degrees + 180) % 360 - 180


def _camera_packets(stream):
    """(clock, yaw, pitch, aim x, aim y, aim z, turret rad, gun pitch rad) of every type 39."""
    return [(clock, *struct.unpack("<7f", payload)) for ptype, clock, payload in stream.packets
            if ptype == camera.PT_CAMERA and len(payload) == camera.CAMERA_SIZE]


def _in_sight(stream):
    """clock -> whether the recorder was in the sniper sight then."""
    changes = [(clock, struct.unpack("<I", p)[0] == 0) for ptype, clock, p in stream.packets
               if ptype == camera.PT_SNIPER]
    times = [c for c, _ in changes]

    def at(clock):
        i = bisect.bisect_right(times, clock) - 1
        return changes[i][1] if i >= 0 else False
    return at


# --------------------------------------------------------------------------- #
# the real replays
# --------------------------------------------------------------------------- #

def test_every_fixture_carries_the_recorders_camera():
    for name, b, _stream in _fixtures():
        cam, end = b["replay"]["camera"], b["replay"]["end"]
        samples = camera.decode_dir(cam["dir"])
        times = [t for t, _, _ in samples]
        assert len(samples) > 300 and times == sorted(times), name
        assert -replay.PREROLL - 0.01 <= times[0] and times[-1] <= end + 0.01, name
        visits = cam["sniper"]
        assert visits and all(t0 < t1 for t0, t1 in visits), name
        assert all(a[1] <= b_[0] for a, b_ in zip(visits, visits[1:])), name  # in order, apart
        for t, fov in cam["fov"]:  # the zoom only ever comes inside the sight
            assert any(t0 - 0.05 <= t <= t1 + 0.05 for t0, t1 in visits), (name, t)
            assert 1 < fov <= camera.ARCADE_FOV + 0.01, (name, fov)


def test_most_zooms_start_from_the_arcade_view():
    starts = []
    for _name, b, _stream in _fixtures():
        cam = b["replay"]["camera"]
        for t0, t1 in cam["sniper"]:
            inside = [fov for t, fov in cam["fov"] if t0 - 0.05 <= t <= t1 + 0.05]
            if inside:
                starts.append(inside[0])
    # a visit that begins while the last one's zoom-out still runs starts lower
    near = sum(1 for fov in starts if abs(fov - camera.ARCADE_FOV) < 0.3)
    assert near >= 0.8 * len(starts), starts


def test_the_camera_yaw_points_at_the_aim_point():
    for name, b, stream in _fixtures():
        track = positions(stream)[b["author"]["entity_id"]]
        times = [p[0] for p in track]
        errors = []
        for clock, yaw, _pitch, x, _y, z, _turret, _gun in _camera_packets(stream):
            i = bisect.bisect_right(times, clock) - 1
            if i < 0 or clock - times[i] > 0.3:
                continue
            _, px, _py, pz, _ = track[i]
            if 30 < math.hypot(x - px, z - pz) < 400:
                errors.append(abs(_wrap(yaw - math.degrees(math.atan2(x - px, z - pz)))))
        assert len(errors) > 500 and statistics.median(errors) < 1.0, (name, statistics.median(errors))


def test_the_sight_sits_on_the_gun_and_the_arcade_camera_orbits_above_the_tank():
    script = render._REPLAY_SCRIPT
    for name, b, stream in _fixtures():
        track = positions(stream)[b["author"]["entity_id"]]
        times, sight = [p[0] for p in track], _in_sight(stream)
        start = b["battle_start_clock"]
        heights = {True: [], False: []}
        for clock, yaw, pitch, x, y, z, _turret, _gun in _camera_packets(stream):
            i = bisect.bisect_right(times, clock) - 1
            if clock < start + 5 or i < 0 or i + 1 >= len(track) or times[i + 1] - times[i] > 0.5:
                continue
            (c0, x0, y0, z0, _), (c1, x1, y1, z1, _) = track[i], track[i + 1]
            f = (clock - c0) / (c1 - c0)
            px, py, pz = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, z0 + (z1 - z0) * f
            d = math.hypot(x - px, z - pz)
            if 8 < d < 400 and abs(_wrap(yaw - math.degrees(math.atan2(x - px, z - pz)))) < 1.5:
                # where the line of sight passes over the tank, above its feet
                heights[sight(clock)].append(d * math.tan(math.radians(pitch)) + (y - py))
        eye = gamedata.gun(b["author"]["tank"]["tank_id"])[0]
        assert abs(statistics.median(heights[True]) - eye) < 0.12, (name, statistics.median(heights[True]), eye)
        assert abs(statistics.median(heights[False]) - 4.8) < 0.15, (name, statistics.median(heights[False]))
    assert "ORBIT_UP = 4.8" in script


def test_the_pitch_bits_read_over_the_guns_limits_match_the_sight():
    for name, b, stream in _fixtures():
        me = b["author"]["entity_id"]
        codes = events.parse(stream, {me})["pitch"][me]
        limits = gamedata.gun(b["author"]["tank"]["tank_id"])[1]
        stated = [(clock, math.degrees(gun)) for clock, *_rest, gun in _camera_packets(stream)]
        times = [c for c, _ in stated]
        errors = []
        for k in range(1, len(codes) - 1):  # where the gun held still a moment
            clock, code = codes[k]
            if not codes[k - 1][1] == code == codes[k + 1][1]:
                continue
            i = bisect.bisect_right(times, clock) - 1
            if i >= 0 and limits[0] + 0.01 < stated[i][1] < limits[1] - 0.01:
                errors.append(abs(events.gun_pitch(code, limits) - stated[i][1]))
        assert len(errors) > 20 and statistics.median(errors) < 0.3, (name, statistics.median(errors))


def test_the_tracks_carry_each_guns_pitch_inside_its_limits():
    for name, b, _stream in _fixtures():
        for track in b["replay"]["tracks"]:
            tank = next(v["tank"] for v in b["vehicles"] if v["entity_id"] == track["id"])
            low, high = gamedata.gun(tank["tank_id"])[1]
            pitches = [s[5] for s in replay.decode(track["p"])]
            assert all(low - 0.06 <= p <= high + 0.06 for p in pitches), (name, track["id"])


def test_every_tank_knows_where_its_gun_sits_and_how_far_it_tilts():
    table = json.loads((gamedata.ASSETS / "vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    for tank_id, entry in table.items():
        assert 0.8 < entry["eye"] < 3.6, (entry["key"], entry["eye"])
        low, high = entry["pitch"]
        assert -90 < low < 0 < high < 30, (entry["key"], entry["pitch"])
    assert gamedata.gun(8193) == (1.66, (-18.0, 6.0))  # Object 704: a shared gun's limits
    assert gamedata.gun(999999) == (gamedata.DEFAULT_EYE, gamedata.DEFAULT_PITCH)


# --------------------------------------------------------------------------- #
# synthetic streams
# --------------------------------------------------------------------------- #

def _frame(clock, yaw, pitch=2.0):
    return (camera.PT_CAMERA, clock, struct.pack("<7f", yaw, pitch, 0, 0, 0, 0, 0))


def test_sniper_visits_and_the_zoom_they_had():
    packets = [_frame(i / 60, 10.0) for i in range(0, 600)]                       # 10 s of camera
    packets.append((camera.PT_SNIPER, 2.0, struct.pack("<I", 0)))
    packets += [(camera.PT_FOV, 2.0 + i / 60, struct.pack("<f", max(6.76, 54 - i)))  # 54 down to x8
                for i in range(1, 120)]
    packets.append((camera.PT_SNIPER, 4.0, struct.pack("<I", 1)))
    packets.append((camera.PT_SNIPER, 7.0, struct.pack("<I", 0)))                 # still in at the end
    packets += [(camera.PT_FOV, 7.0 + i / 60, struct.pack("<f", 27.0)) for i in range(1, 60)]
    packets.sort(key=lambda p: p[1])
    cam = camera.build(Stream("11.20.0", "", packets), start=0.0, end=9.0)
    assert cam["sniper"] == [[2.0, 4.0], [7.0, 9.0]]
    first = [fov for t, fov in cam["fov"] if t < 4]
    assert first[0] == 53.0 and first[-1] == 6.76 and len(first) < 10  # the ramp, thinned
    assert [fov for t, fov in cam["fov"] if t > 7] == [27.0, 27.0]       # a steady zoom: two ends


def test_the_camera_turns_smoothly_through_north():
    packets = [_frame(i / 60, _wrap(170 + i * 0.5)) for i in range(120)]  # 170 -> 229.5 degrees
    cam = camera.build(Stream("11.20.0", "", packets), start=0.0, end=2.0)
    yaws = [yaw for _t, yaw, _p in camera.decode_dir(cam["dir"])]
    assert yaws == sorted(yaws) and yaws[-1] > 200  # past 180, not back to -180
    assert abs(yaws[-1] - 229.5) < 0.06


def test_thinning_keeps_every_sample_within_the_tolerance():
    samples = [(i / 20, 30 * math.sin(i / 25) + (5 if i > 150 else 0), i * 0.01) for i in range(400)]
    kept = camera.thin(samples, 0.25, 1.0)
    assert kept[0] == samples[0] and kept[-1] == samples[-1] and len(kept) < len(samples) / 3
    times = [k[0] for k in kept]
    for t, a, b in samples:
        j = min(max(bisect.bisect_right(times, t) - 1, 0), len(kept) - 2)
        (t0, a0, b0), (t1, a1, b1) = kept[j], kept[j + 1]
        f = (t - t0) / (t1 - t0)
        assert abs(a0 + (a1 - a0) * f - a) <= 0.25 + 1e-9 and abs(b0 + (b1 - b0) * f - b) <= 0.25 + 1e-9
    assert all(b_ - a <= 1.0 + 1e-9 for a, b_ in zip(times, times[1:]))
    line = [(i / 20, i * 0.1, 0.0) for i in range(100)]
    assert len(camera.thin(line, 0.25, 1.0)) == 6  # a straight line: one sample a second


def test_a_stream_without_camera_packets_has_no_camera():
    assert camera.build(Stream("11.20.0", "", [(10, 1.0, b"\0" * 49)]), start=0.0, end=5.0) is None
    assert camera.build(None, 0.0, 5.0) is None
