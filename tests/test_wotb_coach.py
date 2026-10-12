"""Tests for the Blitz coach: the fact sheet (blitzreview/tactics.py) and the
rule engine over it (blitzreview/coachrules.py).

The dangerous bug class is the same as in the CS2 coach: a metric ranked the
wrong way round. It is silent -- the report still renders and still cites
real numbers -- it just praises the player for what loses them battles. So
DIRECTIONS restates in plain words which of two players is doing better, and
fails if `tactics.BETTER` ever disagrees.

The rest pins what the report may say: only facts from the fact sheet, only
moments inside the recording, never a rule that needs a missing fact (a
battle without results or stored before the events were decoded), and a
player's nickname quoted in the evidence comes out escaped.
"""

import copy
import os
import re
import unittest
from pathlib import Path

from blitzreview import analysis, battle, coachrules, tactics
from blitzreview.render import render_battle_html

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
LOCAL_REPLAYS = Path(os.environ.get(
    "WOTB_REPLAY_DIR", Path.home() / "AppData" / "Local" / "wotblitz" / "DAVAProject" / "replays"))

# (metric, a clearly WORSE value, a clearly BETTER value): "a player with
# <worse> is doing worse than one with <better>". Written from scratch, not
# derived from tactics.BETTER -- a table generated from the code under test
# would agree with any bug in it.
DIRECTIONS = [
    ("damage", 400, 2500),
    ("kills", 0, 3),
    ("accuracy", 0.4, 0.9),
    ("pen_rate", 0.5, 1.0),
    ("damage_per_shot", 120, 400),
    ("assist", 0, 900),                 # damage on your spotting / tracks
    ("damage_blocked", 0, 1500),
    ("xp", 300, 1500),
    ("damage_net", -1200, 900),         # dealt minus received: the HP trade
    ("survival_s", 70.0, 240.0),        # dying at 1:10 is the failure
]


def _load(name):
    return battle.load(FIXTURES / name)


def _report(b):
    facts, report = coachrules.analyse_and_build(b)
    return facts, report


# --------------------------------------------------------------------------- #
# ranking direction -- the load-bearing tests
# --------------------------------------------------------------------------- #

def test_every_metric_is_ranked_the_way_a_human_would_rank_it():
    for key, worse, better in DIRECTIONS:
        among_better = tactics._rank(worse, [better] * 13, key)
        among_worse = tactics._rank(better, [worse] * 13, key)
        assert among_better["rank"] == 14, (key, among_better)
        assert among_worse["rank"] == 1, (key, among_worse)


def test_the_direction_table_covers_exactly_the_ranked_metrics():
    assert {key for key, _, _ in DIRECTIONS} == set(tactics.BETTER)


def test_every_ranked_metric_has_a_value_for_a_player_with_results():
    b = _load("v11_20_rift_base_capture.wotbreplay")
    me = analysis.author(b)
    missing = [key for key in tactics.BETTER if tactics._value(b, me, key) is None]
    assert not missing, missing  # a typo'd key would silently never be ranked


# --------------------------------------------------------------------------- #
# the fact sheet on real battles
# --------------------------------------------------------------------------- #

def test_the_canyon_death_was_far_from_the_team_and_under_three_guns():
    facts, report = _report(_load("v11_20_canyon_results.wotbreplay"))
    death = facts["death"]
    assert death["t"] == 108.9 and death["order"] == 3 and death["of"] == 14
    assert death["nearest_ally_m"] == 255 and death["allies_near"] == 0
    names = {facts["names"][eid]["name"] for eid in death["attackers"]}
    assert names == {"Pla_hacker", "michal_1844", "Kossmooss"}
    assert all(not facts["names"][eid]["ally"] for eid in death["attackers"])
    assert [p["code"] for p in report["problems"]] == ["isolated_death"]
    assert report["problems"][0]["moments"] == [98.9]


def test_a_strong_battle_gets_strengths_and_no_invented_problems():
    facts, report = _report(_load("v11_20_rift_base_capture.wotbreplay"))
    assert facts["death"] is None and report["problems"] == []
    assert [s["point"] for s in report["strengths"]] == ["2 495 урона", "2 фрага"]
    assert facts["alone_at_start"]["nearest_ally_m"] < tactics.ALONE_M


def test_a_replay_without_results_is_judged_on_the_map_alone():
    facts, report = _report(_load("v11_20_savanna_no_results.wotbreplay"))
    assert facts["lobby"] == {} and report["strengths"] == []
    assert "Итогов боя в реплее нет" in report["headline"]
    assert facts["death"]["nearest_ally_m"] is not None  # the map facts are still there


def test_every_report_cites_only_moments_inside_the_recording():
    paths = sorted(FIXTURES.glob("*.wotbreplay"))
    if LOCAL_REPLAYS.is_dir():
        paths += sorted(LOCAL_REPLAYS.glob("*.wotbreplay"))
    for path in paths:
        b = battle.load(path)
        facts, report = _report(b)
        assert report["headline"] and len(report["problems"]) <= 3, path.name
        assert len(report["strengths"]) <= 2, path.name
        for p in report["problems"]:
            assert p["code"] in coachrules.WEIGHT and p["code"] in coachrules.DRILLS, path.name
            assert p["severity"] in coachrules.SEVERITY_ORDER, path.name
            assert all(p[k] for k in ("title", "cause", "evidence", "fix")), (path.name, p)
            for t in p["moments"]:
                assert 0 <= t <= facts["recording_end"], (path.name, p["code"], t)
        assert report["drill"]["name"] and report["drill"]["how"], path.name


# --------------------------------------------------------------------------- #
# rules on hand-made fact sheets
# --------------------------------------------------------------------------- #

def _facts(**over):
    me = {"id": 1, "name": "me", "tank": "Т-34", "class": "MT", "tier": 5, "alive": False,
          "death_t": 200.0, "max_hp": 1000, "damage": 900, "kills": 1, "shots": 8, "hits": 6,
          "pens": 5, "assist": 0, "damage_received": 1000}
    me.update(over.pop("me", {}))
    facts = {"me": me, "outcome": "loss", "duration": 300.0, "recording_end": 300.0,
             "has_results": True, "has_events": True, "lobby": {},
             "death": {"t": 200.0, "order": 8, "of": 14, "killer": 9, "nearest_ally_m": 40,
                       "allies_near": 2, "attackers": [9], "lost_in_window": 300, "dealt": 900},
             "standing": [], "alone_at_start": {"t": 60.0, "nearest_ally_m": 40, "allies_near": 2},
             "shots": {"fired": 8, "missed": [10.0, 20.0], "no_damage": []},
             "names": {9: {"name": "<b>enemy</b>", "tank": "КВ-1", "ally": False},
                       8: {"name": "two", "tank": "ИС", "ally": False},
                       7: {"name": "three", "tank": "Т-150", "ally": False}}}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(facts.get(key), dict):
            facts[key] = {**facts[key], **value}
        else:
            facts[key] = value
    return facts


def _codes(facts):
    return [p["code"] for p in coachrules.build_report(facts)["problems"]]


def test_an_ordinary_battle_fires_nothing():
    assert _codes(_facts()) == []


def test_early_death_is_about_order_and_the_first_half():
    assert _codes(_facts(death={"order": 1, "t": 80.0})) == ["early_death"]
    first = coachrules.build_report(_facts(death={"order": 1, "t": 80.0}))["problems"][0]
    assert first["severity"] == "high" and first["moments"] == [72.0]
    assert _codes(_facts(death={"order": 3, "t": 149.0})) == ["early_death"]
    assert _codes(_facts(death={"order": 4, "t": 80.0})) == []    # fourth is not "one of the first"
    assert _codes(_facts(death={"order": 1, "t": 151.0})) == []   # past half the battle


def test_a_cheap_death_is_less_than_half_the_hp_back():
    assert _codes(_facts(death={"dealt": 499})) == ["cheap_death"]
    assert _codes(_facts(death={"dealt": 500})) == []
    assert coachrules.build_report(_facts(death={"dealt": 200}))["problems"][0]["severity"] == "high"


def test_isolation_and_focus_are_not_said_twice():
    lone = _facts(death={"nearest_ally_m": 151, "attackers": [9, 8, 7]})
    assert _codes(lone) == ["isolated_death"]                 # names the three shooters itself
    crowded = _facts(death={"nearest_ally_m": 60, "attackers": [9, 8, 7]})
    assert _codes(crowded) == ["focus"]
    assert _codes(_facts(death={"nearest_ally_m": 150})) == []


def test_standing_and_alone_and_passive():
    spell = {"t0": 100.0, "t1": 106.0, "hits": 2, "damage": 450}
    assert _codes(_facts(standing=[spell])) == ["standing"]
    assert coachrules.build_report(_facts(standing=[spell]))["problems"][0]["severity"] == "high"
    alone = {"t": 60.0, "nearest_ally_m": 121, "allies_near": 0}
    assert _codes(_facts(alone_at_start=alone)) == ["alone_start"]
    assert _codes(_facts(alone_at_start=alone, me={"class": "LT"})) == []  # scouting is the job
    weak = {"damage": {"you": 300, "rank": 13, "of": 14, "median": 1400}}
    assert _codes(_facts(lobby=weak, me={"alive": True, "damage": 300}, death=None)) == ["passive"]
    assert _codes(_facts(lobby=weak, me={"alive": False, "damage": 300}, death=None)) == []


def test_aim_and_penetration_need_enough_shots():
    bad_aim = {"accuracy": {"you": 0.4, "rank": 13, "of": 14, "median": 0.8}}
    assert _codes(_facts(lobby=bad_aim)) == ["misses"]
    assert _codes(_facts(lobby=bad_aim, me={"shots": 4})) == []
    bad_pen = {"pen_rate": {"you": 0.4, "rank": 14, "of": 14, "median": 0.9}}
    assert _codes(_facts(lobby=bad_pen)) == ["no_pen"]
    assert _codes(_facts(lobby=bad_pen, me={"hits": 3})) == []


def test_the_worst_problem_leads_and_only_three_are_kept():
    facts = _facts(death={"order": 1, "t": 80.0, "dealt": 100, "nearest_ally_m": 300},
                   standing=[{"t0": 50.0, "t1": 55.0, "hits": 2, "damage": 500}],
                   alone_at_start={"t": 60.0, "nearest_ally_m": 200, "allies_near": 0})
    report = coachrules.build_report(facts)
    assert [p["code"] for p in report["problems"]] == ["early_death", "cheap_death", "isolated_death"]
    assert report["drill"]["name"] == coachrules.DRILLS["early_death"][0]
    assert "погибли первым в бою" in report["headline"]


def test_a_strength_must_beat_the_lobby_not_just_tie_it():
    tie = {"kills": {"you": 0, "rank": 1, "of": 14, "median": 0}}
    assert coachrules.build_report(_facts(lobby=tie, me={"kills": 0}))["strengths"] == []
    best = {"damage": {"you": 3000, "rank": 1, "of": 14, "median": 1200}}
    strengths = coachrules.build_report(_facts(lobby=best, me={"damage": 3000}))["strengths"]
    assert strengths[0]["point"] == "3 000 урона"


def test_standing_needs_two_hits_a_halt_and_no_shot_back():
    track = [(t / 2, 0.0, 0.0, 0, 0) for t in range(0, 400)]       # parked all battle
    def hits(*times):
        return [[t, 9, 1, 3, 200] for t in times]
    ev = {"hits": hits(100.0, 105.0), "shots": []}
    assert tactics._standing(track, 1, ev) == [{"t0": 100.0, "t1": 105.0, "hits": 2, "damage": 400}]
    assert tactics._standing(track, 1, {"hits": hits(100.0, 120.0), "shots": []}) == []  # apart
    fired = {"hits": hits(100.0, 105.0), "shots": [[103.0, 1, 0, 0, 0, 0, 9, 3, 200, 0]]}
    assert tactics._standing(track, 1, fired) == []                  # it shot back
    moving = [(t / 2, t * 2.0, 0.0, 0, 0) for t in range(0, 400)]    # 4 m/s
    assert tactics._standing(moving, 1, ev) == []


# --------------------------------------------------------------------------- #
# the rendered card
# --------------------------------------------------------------------------- #

def _coach_card(page):
    m = re.search(r'<section class="card coach" id="coach">(.*?)</section>', page, re.S)
    assert m, "no coach card"
    return m.group(1)


def test_the_coach_card_links_its_moments_and_escapes_names():
    b = copy.deepcopy(_load("v11_20_canyon_results.wotbreplay"))
    for v in b["vehicles"]:
        if v["nickname"] == "Kossmooss":
            v["nickname"] = '<img src=x onerror=alert(1)>'
    card = _coach_card(render_battle_html(b))
    assert "<img src=x" not in card and "&lt;img src=x onerror=alert(1)&gt;" in card
    assert 'href="#t=98"' in card and "Погибли в отрыве от команды" in card


def test_a_battle_stored_before_format_4_still_gets_a_coach():
    b = copy.deepcopy(_load("v11_7_himmelsdorf_results.wotbreplay"))
    b["format"] = 3
    del b["events"]
    b["replay"]["format"] = 1
    for track in b["replay"]["tracks"]:
        track["p"] = [n for i, n in enumerate(track["p"]) if i % 5 != 4]
    facts, report = _report(b)
    assert facts["standing"] == [] and facts["shots"] is None
    assert facts["death"]["attackers"] is None and facts["death"]["nearest_ally_m"] is not None
    card = _coach_card(render_battle_html(b))
    assert "Прочность отдана почти даром" in card  # results and tracks are enough for that


def test_every_local_replay_gets_a_report():
    paths = sorted(LOCAL_REPLAYS.glob("*.wotbreplay")) if LOCAL_REPLAYS.is_dir() else []
    if not paths:
        raise unittest.SkipTest(f"no local replays in {LOCAL_REPLAYS}")
    for path in paths:
        page = render_battle_html(battle.load(path))
        assert '<section class="card coach" id="coach">' in page, path.name
        assert "Разбор не удалось собрать" not in page, path.name
