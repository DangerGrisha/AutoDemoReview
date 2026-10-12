"""Tests for battle_results.dat decoding (blitzreview/battle_results.py).

Two bug classes, both silent if untested:

1. Security. The file is a pickle. Unpickling an uploaded file runs whatever
   code its author put there, so the wrapper is parsed by hand and anything
   but the five expected opcodes must be rejected -- including real pickles
   that merely *need* a global, which is exactly what an exploit looks like.

2. Field meaning. There is no schema; every field number is reverse
   engineered. A wrong guess still decodes to a plausible number (the original
   bug here: top-level field 9 looked like "my team" -- it is the room type,
   and using it flipped the result of half the battles). So the decoded values
   are checked against each other on real replays: kills must equal the
   victims naming you as killer, hit points must add up, the author block must
   agree with the author's own row, and wiping out a team must decide the
   battle the way the decoded winner says.
"""

import pickle
import struct
from pathlib import Path

from blitzreview import battle_results, container
from blitzreview.battle_results import BattleResultsError

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
WITH_RESULTS = ("v11_20_canyon_results.wotbreplay", "v11_7_himmelsdorf_results.wotbreplay")


# --------------------------------------------------------------------------- #
# helpers: a spec-written protobuf encoder and the pickle wrapper
# --------------------------------------------------------------------------- #

def _varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _v(tag, value):
    return _varint(tag << 3) + _varint(value)


def _m(tag, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    return _varint((tag << 3) | 2) + _varint(len(payload)) + payload


def _wrap(arena_id, payload):
    size = (arena_id.bit_length() + 8) // 8
    return (b"\x80\x02\x8a" + bytes([size]) + arena_id.to_bytes(size, "little", signed=True)
            + b"T" + struct.pack("<I", len(payload)) + payload + b"\x86q\x01.")


def _raises(fn):
    try:
        fn()
    except BattleResultsError:
        return
    raise AssertionError("expected BattleResultsError")


def _real(name):
    return battle_results.parse(container.open_replay(FIXTURES / name).battle_results)


# --------------------------------------------------------------------------- #
# the wrapper
# --------------------------------------------------------------------------- #

def test_the_hand_parsed_wrapper_yields_arena_id_and_payload():
    assert battle_results.unwrap(_wrap(69685700140486317, b"\x08\x01")) == \
        (69685700140486317, b"\x08\x01")


def test_pickles_that_need_a_global_are_rejected_not_executed():
    exploit = b"\x80\x02cos\nsystem\nq\x00X\x04\x00\x00\x00echoq\x01\x85q\x02Rq\x03."
    _raises(lambda: battle_results.unwrap(exploit))
    # A genuine Python-3 pickle of the same tuple encodes bytes via a global.
    _raises(lambda: battle_results.unwrap(pickle.dumps((1, b"x"), protocol=2)))


def test_truncated_or_extended_wrappers_are_rejected():
    good = _wrap(5, b"abc")
    _raises(lambda: battle_results.unwrap(good[:-3]))
    _raises(lambda: battle_results.unwrap(good[:-1] + b"N."))
    _raises(lambda: battle_results.unwrap(b"\x80\x03" + good[2:]))


# --------------------------------------------------------------------------- #
# the protobuf, on a synthetic message
# --------------------------------------------------------------------------- #

def test_synthetic_results_decode_to_the_documented_fields():
    author = _v(101, 42) + _v(102, 1) + _v(103, 8193) + _v(8, 1399)
    me = _m(2, _m(1, "me") + _v(3, 1) + _m(5, "SKYLG"))
    result = _v(1, 100) + _m(2, _v(101, 42) + _v(102, 1) + _v(103, 8193)
                             + _v(1, -3) + _v(8, 1399) + _v(18, 2) + _v(25, 200)
                             + _v(9, 50) + _v(10, 25) + _v(117, 700))
    payload = (_v(1, (1 << 16) | 27) + _v(2, 1791774381) + _v(3, 2) + _m(8, author)
               + _v(9, 1) + _m(201, _v(1, 42) + me) + _m(301, result))
    out = battle_results.parse(_wrap(77, payload))
    assert (out["arena_id"], out["map_id"], out["mode"]) == (77, 27, 1)
    assert out["winner_team"] == 2 and out["room_type"] == 1
    assert out["author"]["team"] == 1 and out["author"]["damage"] == 1399
    assert out["players"] == [{"account_id": 42, "nickname": "me", "platoon_id": None,
                               "team": 1, "clan_tag": "SKYLG"}]
    row = out["results"][0]
    assert row["entity_id"] == 100 and row["hp_left"] == -3 and row["kills"] == 2
    assert row["killer_id"] == 200 and row["assist"] == 75 and row["damage_blocked"] == 700
    assert row["max_hp"] is None and row["mm_rating"] is None


def test_a_draw_has_no_winner_field():
    out = battle_results.parse(_wrap(1, _v(1, 27)))
    assert out["winner_team"] == 0 and out["author"] is None and out["results"] == []


def test_garbage_protobuf_raises_battle_results_error():
    _raises(lambda: battle_results.parse(_wrap(1, b"\x0a\x50short")))


# --------------------------------------------------------------------------- #
# real replays: the fields must agree with each other
# --------------------------------------------------------------------------- #

def test_kills_equal_the_victims_naming_each_player_as_killer():
    for name in WITH_RESULTS:
        rows = _real(name)["results"]
        for row in rows:
            victims = sum(1 for other in rows if other["killer_id"] == row["entity_id"])
            assert row["kills"] == victims, (name, row["entity_id"], row["kills"], victims)


def test_dead_vehicles_and_only_dead_vehicles_name_a_killer():
    for name in WITH_RESULTS:
        rows = _real(name)["results"]
        ids = {r["entity_id"] for r in rows}
        for row in rows:
            assert (row["hp_left"] <= 0) == (row["killer_id"] in ids), (name, row)


def test_hit_points_add_up_where_max_hp_is_known():
    rows = _real("v11_20_canyon_results.wotbreplay")["results"]
    assert all(r["max_hp"] for r in rows), "11.20 writes max_hp for everybody"
    for row in rows:
        assert row["max_hp"] - row["damage_received"] == max(row["hp_left"], 0), row
        assert row["hits_received"] == row["nonpen_received"] + row["pens_received"], row
    assert all(r["max_hp"] is None for r in _real("v11_7_himmelsdorf_results.wotbreplay")["results"])


def test_the_author_block_agrees_with_the_authors_own_row():
    for name in WITH_RESULTS:
        res = _real(name)
        author = res["author"]
        row = next(r for r in res["results"] if r["account_id"] == author["account_id"])
        for key in ("team", "tank_id", "shots", "hits", "penetrations", "damage", "hp_left"):
            assert author[key] == row[key], (name, key, author[key], row[key])
        listed = next(p for p in res["players"] if p["account_id"] == author["account_id"])
        assert listed["team"] == author["team"], name


def test_two_full_teams_of_seven():
    for name in WITH_RESULTS:
        res = _real(name)
        teams = sorted(r["team"] for r in res["results"])
        assert teams == [1] * 7 + [2] * 7, (name, teams)
        assert len(res["players"]) == 14


def test_a_wiped_out_team_lost_according_to_the_decoded_winner():
    """Guards the field-9 mix-up: annihilation must agree with winner_team."""
    checked = 0
    for name in WITH_RESULTS:
        res = _real(name)
        for team in (1, 2):
            if all(r["hp_left"] <= 0 for r in res["results"] if r["team"] == team):
                assert res["winner_team"] == 3 - team, (name, team, res["winner_team"])
                checked += 1
    assert checked, "at least one fixture must end in a wipe-out to make this check bite"
