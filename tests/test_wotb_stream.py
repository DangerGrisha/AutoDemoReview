"""Tests for the packet stream and arena updates (packets.py, arena.py).

The stream is where version drift will bite first: method and update ids come
from the client's entity definitions and may move in any patch, and a moved id
does not crash -- it just quietly yields an empty roster or a kill feed with
the wrong names. So the stream is checked against the INDEPENDENT source we
have, battle_results.dat: the roster must name the same tank, account and team
for every vehicle, and every recorded kill must agree with the killer the
results report. Fixtures cover two client versions (11.7.1 and 11.20.0).
"""

import struct
from pathlib import Path

from blitzreview import arena, battle_results, container, packets
from blitzreview.container import ReplayError

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
ALL = sorted(p.name for p in FIXTURES.glob("*.wotbreplay"))
WITH_RESULTS = ("v11_20_canyon_results.wotbreplay", "v11_7_himmelsdorf_results.wotbreplay")


def _open(name):
    files = container.open_replay(FIXTURES / name)
    return files, packets.read_stream(files.data)


def _stream_bytes(*packet_list, header_extra=b"\x0a"):
    header = bytes([4]) + b"abcd" + bytes([7]) + b"11.20.0" + header_extra
    body = b"".join(struct.pack("<IIf", len(p), t, c) + p for t, c, p in packet_list)
    rest = 4 + len(header) + len(body)
    return struct.pack("<III", packets.MAGIC, rest, len(header)) + header + body


def test_fixtures_are_present():
    assert len(ALL) >= 3, ALL


def test_every_fixture_stream_splits_exactly_into_packets():
    for name in ALL:
        files, stream = _open(name)
        assert not stream.truncated, name
        assert stream.version == files.meta["version"], (name, stream.version)
        assert len(stream.packets) > 10_000, name


def test_synthetic_stream_and_truncation():
    pos = struct.pack("<III3f3f3fB", 7, 1, 0, 1.0, 2.0, 3.0, 0, 0, 0, 0.5, 0, 0, 0)
    stream = packets.read_stream(_stream_bytes((packets.PT_POSITION, 1.5, pos), (99, 2.0, b"xy")))
    assert stream.version == "11.20.0" and stream.client_hash == "abcd"
    assert not stream.truncated and len(stream.packets) == 2
    assert packets.positions(stream) == {7: [(1.5, 1.0, 2.0, 3.0, 0.5)]}
    cut = packets.read_stream(_stream_bytes((99, 0.0, b"x" * 40))[:-10])
    assert cut.truncated and cut.packets == []


def test_bad_magic_is_a_replay_error():
    try:
        packets.read_stream(b"\x00" * 64)
    except ReplayError:
        return
    raise AssertionError("expected ReplayError")


def test_the_roster_matches_battle_results_for_every_vehicle():
    for name in WITH_RESULTS:
        files, stream = _open(name)
        roster = arena.parse(stream)["roster"]
        results = battle_results.parse(files.battle_results)["results"]
        assert len(roster) == 14 == len(results), name
        for row in results:
            entry = roster[row["entity_id"]]
            assert (entry["tank_id"], entry["account_id"], entry["team"]) == \
                (row["tank_id"], row["account_id"], row["team"]), (name, entry, row)


def test_the_recorders_tank_matches_meta():
    for name in ALL:
        files, stream = _open(name)
        roster = arena.parse(stream)["roster"].values()
        mine = next(v for v in roster if v["account_id"] == int(files.meta["dbid"]))
        assert mine["tank_id"] == files.meta["vehicleCompDescriptor"], name
        assert mine["nickname"] == files.meta["playerName"], name


def test_the_battle_period_starts_before_any_kill():
    for name in ALL:
        _, stream = _open(name)
        info = arena.parse(stream)
        periods = [p["period"] for p in info["periods"]]
        assert periods[:3] == [arena.PERIOD_WAITING, arena.PERIOD_PREBATTLE,
                               arena.PERIOD_BATTLE], (name, periods)
        assert 5.0 < info["battle_start"] < 60.0, (name, info["battle_start"])
        assert all(k["clock"] > info["battle_start"] for k in info["kills"]), name


def test_recorded_kills_agree_with_the_results_killer():
    checked = 0
    for name in WITH_RESULTS:
        files, stream = _open(name)
        killers = {r["entity_id"]: r["killer_id"]
                   for r in battle_results.parse(files.battle_results)["results"]}
        for kill in arena.parse(stream)["kills"]:
            assert killers[kill["victim"]] == kill["killer"], (name, kill)
            checked += 1
    assert checked >= 3


def test_positions_are_real_coordinates_for_the_roster():
    """Allies are always tracked; an enemy never spotted has no track at all."""
    for name in ALL:
        files, stream = _open(name)
        tracks = packets.positions(stream)
        roster = arena.parse(stream)["roster"]
        my_team = next(v["team"] for v in roster.values()
                       if v["account_id"] == int(files.meta["dbid"]))
        for eid, vehicle in roster.items():
            if eid not in tracks:
                assert vehicle["team"] != my_team, (name, vehicle["nickname"])
                continue
            for _, x, y, z, _yaw in tracks[eid]:
                assert -1000 < x < 1000 and -200 < y < 500 and -1000 < z < 1000, (name, eid, x, y, z)
