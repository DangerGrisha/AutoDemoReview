"""Tests for the Blitz battle report (blitzreview/render.py, analysis.py).

The report prints strings that come straight from an uploaded file -- every
nickname and clan tag is chosen by whoever made the replay -- so the first
thing pinned here is that hostile strings come out escaped. The rest guards
meaning: the ranks shown next to "your" numbers must point the right way, a
replay without results must explain itself instead of showing zeros, and the
lobby ranking must treat ties and small samples sanely.
"""

import copy
from pathlib import Path

from blitzreview import analysis, battle
from blitzreview.render import render_battle_html

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"


def _battle(name="v11_20_canyon_results.wotbreplay"):
    return battle.load(FIXTURES / name)


def test_every_fixture_renders_with_every_player_named():
    for path in sorted(FIXTURES.glob("*.wotbreplay")):
        b = battle.load(path)
        page = render_battle_html(b)
        assert page.startswith("<!doctype html>"), path.name
        assert b["map"]["name"] in page, path.name
        for v in b["vehicles"]:
            assert v["nickname"] in page, (path.name, v["nickname"])


def test_hostile_strings_from_the_replay_are_escaped():
    b = copy.deepcopy(_battle())
    b["vehicles"][0]["nickname"] = '<img src=x onerror=alert(1)>'
    b["vehicles"][1]["clan_tag"] = '"><script>alert(2)</script>'
    b["vehicles"][2]["tank"]["short"] = "<b>tank</b>"
    page = render_battle_html(b)
    assert "<img src=x" not in page and "<script>alert" not in page and "<b>tank" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "&quot;&gt;&lt;script&gt;" in page


def test_site_nav_is_inserted_verbatim():
    page = render_battle_html(_battle(), site_nav='<nav class="site-nav">NAV</nav>')
    assert '<nav class="site-nav">NAV</nav>' in page


def test_a_replay_without_results_explains_itself():
    page = render_battle_html(_battle("v11_20_savanna_no_results.wotbreplay"))
    assert "Итогов боя в реплее нет" in page
    assert "Итог неизвестен" in page
    assert 'class="tiles"' not in page  # no zero-filled stat tiles


def test_the_cut_off_recording_is_called_out():
    page = render_battle_html(_battle())
    assert "Запись обрывается на 2:04 из 2:16" in page


def test_lobby_ranks_point_the_right_way():
    b = _battle()
    ranks = analysis.lobby_ranks(b)
    me = analysis.author(b)
    # 2 hits from 2 = 100 % penetration: nobody can beat it, so rank 1.
    assert me["stats"]["penetrations"] == me["stats"]["hits"] == 2
    assert ranks["pen_rate"]["rank"] == 1
    # 1399 damage: exactly 7 players did more -> 8th of 14.
    more = sum(1 for v in b["vehicles"] if v["stats"]["damage"] > 1399)
    assert ranks["damage"] == {"you": 1399, "rank": more + 1, "of": 14,
                               "median": ranks["damage"]["median"]}
    assert "damage_received" not in ranks  # a heavy should soak: not ranked


def test_rank_against_ties_median_and_small_samples():
    assert analysis.rank_against(5, [5, 5, 1, 0])["rank"] == 1        # ties share the better rank
    assert analysis.rank_against(1, [5, 4, 3, 2])["rank"] == 5
    assert analysis.rank_against(1, [5, 4, 3, 2], higher_better=False)["rank"] == 1
    assert analysis.rank_against(3, [1, 2, 4, 5])["median"] == 3      # median of the others
    assert analysis.rank_against(3, [1, 2, 4]) is None                # too few to compare
    assert analysis.rank_against(None, [1, 2, 3, 4]) is None


def test_team_summary_counts_the_living_and_known_ratings():
    b = _battle()
    allies, enemies = analysis.team_summary(b, True), analysis.team_summary(b, False)
    assert allies["alive"] == 0 and enemies["alive"] == 6  # canyon: allies wiped out
    assert allies["players"] == enemies["players"] == 7
    assert allies["damage"] == sum(v["stats"]["damage"] for v in b["vehicles"] if v["ally"])
