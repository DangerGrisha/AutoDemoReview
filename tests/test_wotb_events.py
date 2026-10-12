"""Tests for the in-battle events (blitzreview/events.py): health, shots, hits,
damage and the turret's yaw.

None of these numbers has a schema behind it -- every index was found by
correlating the stream with the battle results -- so the checks here are the
plan's self-check: what the events add up to must equal what the results say.
A tank of the recorder's team is in view all battle, so when the recording
runs to the end of the battle its shots, hits received and damage received
must match the results EXACTLY; a recording that stops early may only fall
short. Damage dealt matches too, once the health an enemy lost out of view
(`unseen`) is counted. A patch that renumbers a method breaks these sums at
once, instead of quietly drawing nonsense.

Two more checks tie the geometry down without any results: a shell that hit
someone ends where that tank was, and the hull's yaw plus the turret's yaw
points along every shot. A synthetic stream pins the decoder's bookkeeping:
which hit gets the damage, a ricochet, health lost out of view.
"""

import json
import math
import os
import struct
import unittest
from bisect import bisect_right
from collections import Counter
from pathlib import Path

from blitzreview import arena, battle, battle_results, events
from blitzreview.container import open_replay
from blitzreview.packets import Stream, positions, read_stream

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
LOCAL_REPLAYS = Path(os.environ.get(
    "WOTB_REPLAY_DIR", Path.home() / "AppData" / "Local" / "wotblitz" / "DAVAProject" / "replays"))


class _Replay:
    """One replay's stream, arena updates, events and battle results."""

    def __init__(self, path):
        files = open_replay(path)
        self.name = Path(path).name
        self.stream = read_stream(files.data)
        self.arena = arena.parse(self.stream)
        self.events = events.parse(self.stream, self.arena["roster"])
        self.results = battle_results.parse(files.battle_results) if files.battle_results else None
        self.full = any(p["period"] == arena.PERIOD_AFTERBATTLE for p in self.arena["periods"])

    def rows(self):
        return {r["entity_id"]: r for r in self.results["results"]}

    def allies(self):
        team = self.results["author"]["team"]
        return [eid for eid, row in self.rows().items() if row["team"] == team]


def _fixtures():
    return [_Replay(path) for path in sorted(FIXTURES.glob("*.wotbreplay"))]


def _with_results():
    return [r for r in _fixtures() if r.results]


def _wrap(radians):
    return (radians + math.pi) % (2 * math.pi) - math.pi


def _check_sums(rep):
    """The self-check of one replay with results; returns what was checked."""
    rows = rep.rows()
    shots, hits_on, damage_on, dealt = Counter(), Counter(), Counter(), Counter()
    for burst in rep.events["bursts"]:
        shots[burst["shooter"]] += burst["shells"]
    for hit in rep.events["hits"]:
        hits_on[hit["victim"]] += 1
    for drop in rep.events["damage"]:
        damage_on[drop["victim"]] += drop["amount"]
        if drop["attacker"] != drop["victim"]:  # a fall or a crash is nobody's damage
            dealt[drop["attacker"]] += drop["amount"]
    for eid in rep.allies():
        row = rows[eid]
        for label, got, want in (("shots", shots[eid], row["shots"]),
                                 ("hits received", hits_on[eid], row["hits_received"]),
                                 ("damage received", damage_on[eid], row["damage_received"])):
            if rep.full:
                assert got == want, (rep.name, eid, label, got, want)
            else:
                assert got <= want, (rep.name, eid, label, got, want)
    for eid, row in rows.items():
        assert dealt[eid] <= row["damage"], (rep.name, eid, dealt[eid], row["damage"])
    if rep.full:
        unseen = sum(u["amount"] for u in rep.events["unseen"])
        missing = sum(row["damage"] for row in rows.values()) - sum(dealt.values())
        assert missing == unseen, (rep.name, missing, unseen)
    return len(rep.allies())


# --------------------------------------------------------------------------- #
# against the battle results
# --------------------------------------------------------------------------- #

def test_the_recorders_team_adds_up_to_the_results():
    reps = _with_results()
    assert sum(r.full for r in reps) >= 2  # rift and himmelsdorf run to the end
    assert sum(_check_sums(r) for r in reps if r.full) >= 14


def test_damage_out_of_view_is_what_the_dealt_sum_misses():
    # Rift: an enemy came back into view 249 hp lower (most likely it burned:
    # every hit the results count for its attacker is in view).
    rep = next(r for r in _with_results() if "rift" in r.name)
    assert [u["amount"] for u in rep.events["unseen"]] == [249]
    _check_sums(rep)


def test_health_starts_full_and_is_zero_at_every_kill_in_view():
    for rep in _fixtures():
        health = rep.events["health"]
        if rep.results:
            for eid in rep.allies():
                max_hp = rep.rows()[eid]["max_hp"]
                if max_hp:  # 11.20 writes it
                    assert health[eid][0][1] == max_hp, (rep.name, eid)
        for kill in rep.arena["kills"]:
            points = health.get(kill["victim"], [])
            before = [value for clock, value in points if clock <= kill["clock"] + 0.05]
            if before:
                assert before[-1] == 0, (rep.name, kill)


def test_every_shell_of_the_recorders_team_has_a_tracer():
    # Burst guns fire three shells per shooting call (canyon: GumisEZD), and
    # each shell gets its own tracer -- a ricochet's flight on is not a shell.
    bursts = 0
    for rep in _with_results():
        shells, flights = Counter(), Counter()
        for burst in rep.events["bursts"]:
            shells[burst["shooter"]] += burst["shells"]
            bursts += burst["shells"] > 1
        for shot in rep.events["tracers"]:
            flights[shot["shooter"]] += not shot["bounce"]
        for eid in rep.allies():
            assert shells[eid] == flights[eid], (rep.name, eid, shells[eid], flights[eid])
    assert bursts >= 4


def test_hit_kinds_agree_with_the_damage_they_did():
    kinds = Counter()
    for rep in _fixtures():
        for hit in rep.events["hits"]:
            kinds[hit["kind"]] += 1
            if hit["kind"] == events.HIT_PIERCED:
                assert hit["damage"] > 0, (rep.name, hit)
            if hit["kind"] in (events.HIT_RICOCHET, events.HIT_NOT_PIERCED):
                assert hit["damage"] == 0, (rep.name, hit)
    assert kinds[events.HIT_PIERCED] > 100 and kinds[events.HIT_NOT_PIERCED] and kinds[events.HIT_RICOCHET]


def test_a_shell_that_hit_ends_where_its_victim_was():
    gaps = []
    for rep in _fixtures():
        tracks = positions(rep.stream)
        for shot in rep.events["tracers"]:
            if not shot["victim"] or not shot["end"]:
                continue
            track = tracks.get(shot["victim"], [])
            times = [p[0] for p in track]
            i = bisect_right(times, shot["clock"])
            near = [track[j] for j in (i - 1, i)
                    if 0 <= j < len(track) and abs(track[j][0] - shot["clock"]) < 0.5]
            if near:
                gaps.append(min(math.hypot(p[1] - shot["end"][0], p[3] - shot["end"][1])
                                for p in near))
    gaps.sort()
    assert len(gaps) > 150
    assert gaps[len(gaps) * 9 // 10] < 4.0 and gaps[-1] < 15.0, gaps[-5:]


def test_hull_and_turret_yaw_point_along_every_shot():
    errors = []
    for rep in _fixtures():
        tracks = positions(rep.stream)
        for shot in rep.events["tracers"]:
            if shot["bounce"] or not shot["end"]:
                continue
            dx, dz = shot["end"][0] - shot["start"][0], shot["end"][1] - shot["start"][1]
            track, turret = tracks.get(shot["shooter"]), rep.events["turret"].get(shot["shooter"])
            if math.hypot(dx, dz) < 20 or not track or not turret:
                continue
            i = bisect_right([p[0] for p in track], shot["clock"]) - 1
            j = bisect_right([c for c, _ in turret], shot["clock"]) - 1
            if i < 0 or j < 0 or shot["clock"] - track[i][0] > 0.3:
                continue  # the shooter out of view
            errors.append(abs(_wrap(math.atan2(dx, dz) - track[i][4] - turret[j][1])))
    errors.sort()
    assert len(errors) > 200
    assert errors[len(errors) // 2] < math.radians(1) and errors[len(errors) * 9 // 10] < math.radians(3)


# --------------------------------------------------------------------------- #
# the battle dict
# --------------------------------------------------------------------------- #

def test_the_battle_dict_carries_the_events_compactly():
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        b = battle.load(path)
        ev = b["events"]
        assert ev and len(json.dumps(ev, separators=(",", ":"))) < 40_000, path.name
        ids = {v["entity_id"] for v in b["vehicles"]}
        for entry in ev["hp"]:
            assert entry["id"] in ids and entry["max"] >= max(entry["p"][1::2]) > 0, path.name
            assert entry["p"][0::2] == sorted(entry["p"][0::2]), path.name
        assert all(len(s) == 10 and s[1] in ids for s in ev["shots"]), path.name
        assert all((s[6] == 0) == (s[7] == -1) for s in ev["shots"]), path.name
        assert all(len(h) == 5 and h[2] in ids for h in ev["hits"]), path.name
        assert all(len(d) == 5 and d[3] > 0 for d in ev["damage"]), path.name
        me = next(v for v in b["vehicles"] if v["is_author"])
        mine = [s for s in ev["shots"] if s[1] == me["entity_id"] and not s[9]]
        if me["stats"]:
            assert len(mine) <= me["stats"]["shots"], path.name


def test_a_battle_without_a_stream_has_no_events():
    b = battle.build(battle.normalize_meta({"mapName": "canyon"}), None, arena.parse_empty(),
                     None, fight=events.parse_empty())
    assert b["events"] is None and b["replay"] is None


# --------------------------------------------------------------------------- #
# the decoder on a hand-made stream
# --------------------------------------------------------------------------- #

AVATAR, ALLY, ENEMY = 99, 1, 2


def _create(eid, health, gun=0x8000):
    head = struct.pack("<IHII3f3f", eid, events.VEHICLE_TYPE, 1, 0, 0, 0, 0, 0, 0, 0)
    props = (bytes([18, 0, 0, 1, 1, 2]) + struct.pack("<H", gun) + bytes([3])
             + struct.pack("<h", health) + bytes([4, 0, 0]))
    return head + struct.pack("<I", len(props)) + props


def _call(eid, index, args):
    return struct.pack("<III", eid, index, len(args)) + args


def _hit(attacker, victim, kind):
    return _call(victim, events.VM_HIT, struct.pack("<IIB", attacker, victim, 1)
                 + bytes([kind]) + bytes(7) + bytes([4, 100, 1, 1]))


def _health(eid, value, attacker, reason=events.REASON_SHELL):
    return _call(eid, events.VM_HEALTH_CHANGED, struct.pack("<hIB", value, attacker, reason))


def _tracer(shooter, shot, x, z):
    return _call(AVATAR, events.AM_TRACER,
                 struct.pack("<IIB3f3ff", shooter, shot, 4, x, 25.0, z, 0.0, 0.0, 700.0, 6.3))


def _stop(shot, x, z):
    return _call(AVATAR, events.AM_TRACER_STOP, struct.pack("<I3f", shot, x, 25.0, z))


def _stream():
    turret_right = round(270 / 360 * 1023) << 6  # 90 degrees to the right of the hull
    packets = [
        (0, 0.0, struct.pack("<I", AVATAR)),
        (5, 1.0, _create(ALLY, 1000, gun=turret_right)),
        (5, 1.0, _create(ENEMY, 800)),
        # a pierce: 300 damage
        (8, 2.0, _call(ALLY, events.VM_SHOOTING, b"\x01")),
        (8, 2.0, _tracer(ALLY, 5, 0.0, 0.0)),
        (8, 2.0, _stop(5, 100.0, 0.0)),
        (8, 2.0, _hit(ALLY, ENEMY, events.HIT_PIERCED)),
        (7, 2.0, _call(ENEMY, 3, struct.pack("<h", 500))),
        (8, 2.0, _health(ENEMY, 500, ALLY)),
        # a ricochet off the enemy, then the shell flies on into nothing
        (8, 3.0, _call(ALLY, events.VM_SHOOTING, b"\x03")),  # a burst of three
        (8, 3.0, _tracer(ALLY, 6, 0.0, 0.0)),
        (8, 3.0, _hit(ALLY, ENEMY, events.HIT_OTHER)),
        (8, 3.0, _tracer(ALLY, 6, 90.0, 5.0)),
        (8, 3.1, _stop(6, 90.0, 300.0)),
        # the enemy drives off, burns out of view and comes back 100 hp lower
        (5, 4.0, _create(ENEMY, 400)),
        # the ally falls off a cliff
        (8, 5.0, _health(ALLY, 900, ALLY, events.REASON_FALL)),
    ]
    return Stream(version="11.20.0", client_hash="", packets=packets)


def test_the_decoder_keeps_the_books_of_a_small_fight():
    ev = events.parse(_stream(), {ALLY, ENEMY})
    assert ev["health"] == {ALLY: [(1.0, 1000), (5.0, 900)],
                            ENEMY: [(1.0, 800), (2.0, 500), (4.0, 400)]}
    assert [(d["victim"], d["attacker"], d["amount"], d["reason"]) for d in ev["damage"]] == [
        (ENEMY, ALLY, 300, events.REASON_SHELL), (ALLY, ALLY, 100, events.REASON_FALL)]
    assert [(h["kind"], h["damage"]) for h in ev["hits"]] == [(events.HIT_PIERCED, 300),
                                                             (events.HIT_OTHER, 0)]
    assert ev["unseen"] == [{"clock": 4.0, "victim": ENEMY, "amount": 100}]
    assert sum(b["shells"] for b in ev["bursts"]) == 4
    flights = [(t["start"], t["end"], t["victim"], t["kind"], t["damage"], t["bounce"])
               for t in ev["tracers"]]
    assert flights == [((0.0, 0.0), (100.0, 0.0), ENEMY, events.HIT_PIERCED, 300, False),
                       ((0.0, 0.0), (90.0, 5.0), ENEMY, events.HIT_OTHER, 0, False),  # to the ricochet
                       ((90.0, 5.0), (90.0, 300.0), None, None, 0, True)]  # and on, missing
    (_clock, yaw), = ev["turret"][ALLY]
    assert abs(yaw - math.pi / 2) < 0.01


def test_unknown_entities_and_layouts_are_skipped():
    packets = _stream().packets
    odd = bytearray(_create(3, 1000))
    odd[struct.calcsize("<IHII3f3fI") + 5] = 7  # property 2 is not where we expect it
    packets.insert(1, (5, 0.5, bytes(odd)))
    packets.insert(1, (8, 0.5, _call(3, events.VM_HEALTH_CHANGED, b"\x00")))  # wrong size
    ev = events.parse(Stream(version="", client_hash="", packets=packets), {ALLY, ENEMY, 3})
    assert 3 not in ev["health"] and len(ev["damage"]) == 2
    assert events.vehicle_create(b"\x00" * 10) is None


# --------------------------------------------------------------------------- #
# every replay on this machine
# --------------------------------------------------------------------------- #

def test_every_local_replay_adds_up():
    paths = sorted(LOCAL_REPLAYS.glob("*.wotbreplay")) if LOCAL_REPLAYS.is_dir() else []
    if not paths:
        raise unittest.SkipTest(f"no local replays in {LOCAL_REPLAYS}")
    for path in paths:
        rep = _Replay(path)
        if rep.results:
            _check_sums(rep)
