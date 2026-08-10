"""Tests for the radar heatmap's side split (demoreview/render.py + parseutil).

Attacking on T and holding on CT are different games played on the same
picture, so a heatmap that averages both hides the very thing a player is
looking for. These pin down that every dot knows which side it belongs to, that
the control only appears when there is something to split, and that the two
sides stay consistent with each other -- a CT kill is a T death, always.
"""

import re

from demoreview import maps, parseutil, render

INFO = maps.map_info("de_mirage")

DOT = re.compile(r'<(?:circle|line)\b[^>]*data-type="(kill|death|nade)"[^>]*>')


def _kill(round_n=1, killer_side="CT", victim_side="T", me=False):
    return {
        "round": round_n,
        "kx": 100.0, "ky": 100.0, "vx": 200.0, "vy": 200.0,
        "killer": "alice", "victim": "bob", "weapon": "ak47",
        "killer_is_me": me, "victim_is_me": False,
        "killer_sid": "1", "victim_sid": "2",
        "killer_side": killer_side, "victim_side": victim_side,
    }


def _sides(html, kind):
    out = []
    for tag in DOT.finditer(html):
        if tag.group(1) != kind:
            continue
        m = re.search(r'data-side="([^"]*)"', tag.group(0))
        out.append(m.group(1) if m else None)
    return out


# --------------------------------------------------------------------------- #
# side_name
# --------------------------------------------------------------------------- #

def test_team_numbers_map_to_the_two_playing_sides():
    assert parseutil.side_name(2) == "T"
    assert parseutil.side_name(3) == "CT"
    assert parseutil.side_name(2.0) == "T"          # demoparser hands back floats


def test_anything_that_is_not_a_side_is_not_guessed():
    """Unassigned and spectator slots must not be drawn as a side."""
    for junk in (None, 0, 1, 4, float("nan"), "", "CT", [3]):
        assert parseutil.side_name(junk) is None, junk


# --------------------------------------------------------------------------- #
# the dots
# --------------------------------------------------------------------------- #

def test_every_kill_and_death_dot_carries_its_side():
    html = render.render_map([_kill(), _kill(killer_side="T", victim_side="CT")],
                             [], {"map_name": "de_mirage"}, INFO)

    assert _sides(html, "kill") == ["CT", "T"]
    assert _sides(html, "death") == ["T", "CT"]


def test_a_kill_on_one_side_is_a_death_on_the_other():
    """The invariant that makes the filter trustworthy."""
    kills = [_kill(killer_side="CT", victim_side="T") for _ in range(3)]
    kills += [_kill(killer_side="T", victim_side="CT") for _ in range(2)]
    html = render.render_map(kills, [], {"map_name": "de_mirage"}, INFO)

    kill_sides, death_sides = _sides(html, "kill"), _sides(html, "death")
    assert kill_sides.count("CT") == death_sides.count("T") == 3
    assert kill_sides.count("T") == death_sides.count("CT") == 2


def test_the_tooltip_says_which_side_it_happened_on():
    html = render.render_map([_kill()], [], {"map_name": "de_mirage"}, INFO)

    assert "· as CT" in html and "· as T" in html


def test_grenades_carry_the_throwers_side_too():
    """Otherwise picking CT would leave both sides' utility on screen."""
    nade = {"t": "smoke", "me": False, "round": 3, "side": "T",
            "ox": 10.0, "oy": 10.0, "dx": 20.0, "dy": 20.0}
    html = render.render_map([_kill()], [nade], {"map_name": "de_mirage"}, INFO)

    assert _sides(html, "nade") == ["T", "T"]        # the line and the dot


def test_a_dot_with_no_known_side_is_left_unmarked():
    """Old reports and world deaths have no side; they must not be mislabelled."""
    html = render.render_map([_kill(killer_side=None, victim_side=None)], [],
                             {"map_name": "de_mirage"}, INFO)

    assert _sides(html, "kill") == [None]
    assert "data-side" not in html


# --------------------------------------------------------------------------- #
# the control
# --------------------------------------------------------------------------- #

def test_the_side_filter_appears_when_the_match_has_both_halves():
    html = render.render_map([_kill(), _kill(killer_side="T", victim_side="CT")],
                             [], {"map_name": "de_mirage"}, INFO)

    assert 'data-group="side"' in html
    assert 'data-val="CT"' in html and 'data-val="T"' in html
    assert "Both sides" in html


def test_no_side_filter_when_there_is_nothing_to_split():
    """A knife round or a half-parsed match should not offer an empty toggle."""
    one_side = render.render_map([_kill(killer_side="CT", victim_side=None)], [],
                                 {"map_name": "de_mirage"}, INFO)
    nothing = render.render_map([], [], {"map_name": "de_mirage"}, INFO)

    assert 'data-group="side"' not in one_side
    assert 'data-group="side"' not in nothing


def test_the_default_shows_both_sides():
    html = render.render_map([_kill(), _kill(killer_side="T", victim_side="CT")],
                             [], {"map_name": "de_mirage"}, INFO)
    seg = html.split('data-group="side"')[1].split("</div>")[0]

    assert re.search(r'data-val="all"[^>]*class="active"', seg), seg


# --------------------------------------------------------------------------- #
# backfill for matches stored before sides were recorded
# --------------------------------------------------------------------------- #

def _old_kill(round_n, killer_sid, victim_sid):
    """A map_kills entry as it was stored before sides existed."""
    k = _kill(round_n)
    del k["killer_side"], k["victim_side"]
    k["killer_sid"], k["victim_sid"] = killer_sid, victim_sid
    return k


def _round(number, my_side, kills):
    return {"number": number, "my_side_label": my_side, "kills": kills}


def _stored_kill(attacker_sid, victim_sid, attacker_rel, victim_rel):
    return {"attacker_sid": attacker_sid, "victim_sid": victim_sid,
            "attacker_rel": attacker_rel, "victim_rel": victim_rel}


def test_sides_are_recovered_from_the_stored_rounds():
    """No re-parse: the round's own side plus ally/enemy is enough."""
    kills = [_old_kill(1, "me", "them"), _old_kill(2, "them", "me")]
    rounds = [
        _round(1, "CT", [_stored_kill("me", "them", "ally", "enemy")]),
        _round(2, "T", [_stored_kill("them", "me", "enemy", "ally")]),
    ]

    render.backfill_kill_sides(kills, rounds)

    assert (kills[0]["killer_side"], kills[0]["victim_side"]) == ("CT", "T")
    # Round 2 is after the swap: the same players, the other way round.
    assert (kills[1]["killer_side"], kills[1]["victim_side"]) == ("CT", "T")


def test_backfill_never_overwrites_a_side_the_parser_recorded():
    kills = [_kill(1, killer_side="T", victim_side="CT")]
    rounds = [_round(1, "CT", [_stored_kill("1", "2", "ally", "enemy")])]

    render.backfill_kill_sides(kills, rounds)

    assert (kills[0]["killer_side"], kills[0]["victim_side"]) == ("T", "CT")


def test_a_kill_the_rounds_cannot_explain_keeps_no_side():
    """Better an unfiltered dot than one attributed to the wrong half."""
    kills = [_old_kill(1, "ghost", "other"), _old_kill(9, "me", "them")]
    rounds = [_round(1, "CT", [_stored_kill("me", "them", "ally", "enemy")])]

    render.backfill_kill_sides(kills, rounds)

    assert kills[0].get("killer_side") is None     # no matching kill in round 1
    assert kills[1].get("killer_side") is None     # round 9 was never stored


def test_an_unresolved_relation_yields_no_side():
    kills = [_old_kill(1, "world", "victim")]
    rounds = [_round(1, "CT", [_stored_kill("world", "victim", "", "enemy")])]

    render.backfill_kill_sides(kills, rounds)

    assert kills[0]["killer_side"] is None
    assert kills[0]["victim_side"] == "T"


def test_a_team_kill_puts_both_dots_on_the_same_side():
    """Friendly fire is real; it must not be mangled into an enemy kill."""
    kills = [_old_kill(1, "a", "b")]
    rounds = [_round(1, "CT", [_stored_kill("a", "b", "enemy", "enemy")])]

    render.backfill_kill_sides(kills, rounds)

    assert (kills[0]["killer_side"], kills[0]["victim_side"]) == ("T", "T")


def test_backfill_copes_with_an_empty_or_broken_blob():
    assert render.backfill_kill_sides([], []) == []
    assert render.backfill_kill_sides(None, []) is None
    kills = [_old_kill(1, "a", "b")]
    render.backfill_kill_sides(kills, [{"number": 1}])          # no side, no kills
    assert kills[0].get("killer_side") is None


def test_the_filter_script_knows_about_the_side_group():
    """The buttons are inert unless the page script reads their group."""
    assert "mapSide" in render.PAGE_SCRIPT
    assert "'side'" in render.PAGE_SCRIPT
    # An unsided dot must survive the default view rather than vanish.
    assert "mapSide === 'all'" in render.PAGE_SCRIPT
