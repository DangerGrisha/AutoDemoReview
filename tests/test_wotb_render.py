"""Tests for the Blitz battle report (blitzreview/render.py, analysis.py).

The report prints strings that come straight from an uploaded file -- every
nickname and clan tag is chosen by whoever made the replay -- so the first
thing pinned here is that hostile strings come out escaped. The rest guards
meaning: the ranks shown next to "your" numbers must point the right way, a
replay without results must explain itself instead of showing zeros, and the
lobby ranking must treat ties and small samples sanely. The 2D replay ships
the nicknames a second time, inside a JSON block that a browser would end at
the first '</script>' -- so that block is checked separately.
"""

import copy
import json
import re
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


def _replay_json(page):
    m = re.search(r'<script type="application/json" id="replay-data">(.*?)</script>', page, re.S)
    assert m, "no replay data block"
    return json.loads(m.group(1))


def test_the_2d_replay_ships_every_vehicle_and_its_track():
    b = _battle()
    page = render_battle_html(b)
    assert 'id="replay"' in page and 'id="rp-canvas"' in page and 'id="minimap-img"' in page
    data = _replay_json(page)
    assert {v["id"] for v in data["vehicles"]} == {v["entity_id"] for v in b["vehicles"]}
    assert data["tracks"] == b["replay"]["tracks"]
    assert data["view"] and data["world"] == [-300.0, -300.0, 300.0, 300.0]
    assert [k["t"] for k in data["kills"]] == [k["t"] for k in b["kills"]]


def test_a_hostile_nickname_cannot_break_out_of_the_replay_data():
    b = copy.deepcopy(_battle())
    evil = '</script><script>alert(1)</script><!--'
    b["vehicles"][0]["nickname"] = evil
    page = render_battle_html(b)
    assert "<script>alert(1)" not in page and "<!--" not in page
    names = {v["n"] for v in _replay_json(page)["vehicles"]}
    assert evil in names  # escaped on the way in, intact on the way out


def test_kill_times_jump_the_replay_and_the_feed_moves_into_its_card():
    page = render_battle_html(_battle())
    assert page.count('href="#t=') >= 3          # every kill in the feed + the author's death
    assert 'data-t="78.' in page or 'data-t="77.' in page
    assert page.count("<h2>Уничтожения</h2>") == 0  # no second, standalone feed card


def test_a_battle_stored_before_the_2d_replay_asks_for_a_fresh_upload():
    b = copy.deepcopy(_battle())
    b["format"] = 1
    del b["replay"]
    page = render_battle_html(b)
    assert "загрузите реплей заново" in page
    assert 'id="replay-data"' not in page and 'href="#t=' not in page
    assert "<h2>Уничтожения</h2>" in page         # the plain feed is back


def test_the_replay_knows_each_tanks_class_and_the_supremacy_points():
    b = _battle("v11_7_himmelsdorf_results.wotbreplay")
    data = _replay_json(render_battle_html(b))
    classes = {v["id"]: v["c"] for v in data["vehicles"]}
    assert classes == {v["entity_id"]: v["tank"]["type"] for v in b["vehicles"]}
    assert set(classes.values()) <= {"LT", "MT", "HT", "TD"}
    obj = data["obj"]
    assert obj["kind"] == "points" and [z["l"] for z in obj["zones"]] == ["A", "B", "C", "D"]
    assert obj["states"] and all(s[2] in ("a", "e", "") and s[3] in ("a", "e", "")
                                 for s in obj["states"])
    assert obj["score"][0][1:] == [300, 300] and obj["goal"] == 1000
    page = render_battle_html(b)
    assert 'id="rp-score"' in page and 'id="rp-cap"' not in page


def test_the_encounter_base_capture_is_shown_for_the_side_on_it():
    b = _battle("v11_20_rift_base_capture.wotbreplay")
    page = render_battle_html(b)
    obj = _replay_json(page)["obj"]
    assert obj["kind"] == "base" and len(obj["zones"]) == 1 and obj["zones"][0]["l"] == ""
    assert {side for _t, side, value in obj["base"] if value > 0} == {"a"}  # an ally was on it
    assert 'id="rp-cap"' in page and 'id="rp-score"' not in page


def test_the_advantage_strip_counts_the_tanks_still_in_the_battle():
    # Canyon: three allies fall at 1:18, 1:18 and 1:49 before the recording
    # stops (2:04); the rest of the wipe happened after the recorder left.
    page = render_battle_html(_battle())
    strip = re.search(r'<svg class="rp-adv".*?</svg>', page, re.S).group(0)
    counts = re.findall(r"(\d+:\d\d)–(\d+:\d\d): в строю (\d) : (\d)", strip)
    assert counts == [("0:00", "1:18", "7", "7"), ("1:18", "1:18", "6", "7"),
                      ("1:18", "1:49", "5", "7"), ("1:49", "2:04", "4", "7")]
    assert 'class="e"' in strip and 'class="a"' not in strip  # never ahead in that battle


def test_the_best_ally_button_picks_the_top_damage_teammate():
    b = _battle()
    best = max((v for v in b["vehicles"] if v["ally"]), key=lambda v: v["stats"]["damage"])
    assert f'id="rp-best" data-id="{best["entity_id"]}"' in render_battle_html(b)
    no_results = render_battle_html(_battle("v11_20_savanna_no_results.wotbreplay"))
    assert 'id="rp-best"' not in no_results and 'id="rp-card"' in no_results


def test_a_battle_stored_before_format_3_keeps_its_zones_without_history():
    b = copy.deepcopy(_battle("v11_7_himmelsdorf_results.wotbreplay"))
    b["format"] = 2
    del b["objectives"]
    page = render_battle_html(b)
    obj = _replay_json(page)["obj"]
    assert len(obj["zones"]) == 4 and obj["states"] == [] and obj["score"] == []
    assert 'id="rp-score"' not in page


def _fight(page):
    m = re.search(r'<section class="card fight">(.*?)</section>', page, re.S)
    assert m, "no fight card"
    return m.group(1)


def test_the_fight_card_lists_every_shot_and_what_hit_you():
    # Rift: six shots, all on target -- five pierced, the sixth bounced and
    # then hit -- plus 165 of fire after the third; three shells hit back.
    b = _battle("v11_20_rift_base_capture.wotbreplay")
    fight = _fight(render_battle_html(b))
    assert "Ваши выстрелы: 6, в цель 6" in fight and "Попадания по вам: 3" in fight
    assert "пожар −165" in fight
    mine, theirs = re.findall(r'<table class="flist"><tbody>(.*?)</tbody>', fight, re.S)
    damage = [int(d.replace(" ", "")) for d in re.findall(r'<td class="n">−([\d ]+)</td>', mine)]
    me = analysis.author(b)
    assert sum(damage) + 165 == me["stats"]["damage"] == 2495
    assert mine.count('href="#t=') == 6 and theirs.count('href="#t=') == 3
    hurt = [int(d.replace(" ", "")) for d in re.findall(r'<td class="n">−([\d ]+)</td>', theirs)]
    assert sum(hurt) == me["stats"]["damage_received"]


def test_the_fight_chart_marks_every_shot_and_links_it():
    b = _battle("v11_7_himmelsdorf_results.wotbreplay")
    fight = _fight(render_battle_html(b))
    chart = re.search(r'<svg.*?</svg>', fight, re.S).group(0)
    ticks = re.findall(r'<path class="(s[dnm])"', chart)
    assert len(ticks) == analysis.author(b)["stats"]["shots"] == 7
    assert ticks.count("sm") == 1 and ticks.count("sd") == 2  # one miss, two shots that did damage
    assert chart.count('class="kd"') == 1  # the recorder's death
    assert chart.count('<a href="#t=') == len(ticks) + 1


def test_the_replay_ships_the_fight_and_the_turret():
    b = _battle()
    data = _replay_json(render_battle_html(b))
    assert data["fmt"] == 2 and data["ev"] is not None
    assert data["ev"]["shots"] == b["events"]["shots"]
    assert {h["id"] for h in data["ev"]["hp"]} <= {v["entity_id"] for v in b["vehicles"]}


def test_a_battle_stored_before_format_4_keeps_its_replay_without_the_fight():
    b = copy.deepcopy(_battle("v11_7_himmelsdorf_results.wotbreplay"))
    b["format"] = 3
    del b["events"]
    b["replay"]["format"] = 1
    for track in b["replay"]["tracks"]:  # format 1: no turret column
        track["p"] = [n for i, n in enumerate(track["p"]) if i % 5 != 4]
    page = render_battle_html(b)
    assert "загрузите реплей заново, чтобы их увидеть" in page
    assert '<section class="card fight">' not in page
    data = _replay_json(page)
    assert data["fmt"] == 1 and data["ev"] is None and data["tracks"] == b["replay"]["tracks"]


def test_the_replay_ships_the_terrain_for_the_3d_view():
    import base64
    import struct
    import zlib

    from blitzreview import gamedata
    b = _battle()
    page = render_battle_html(b)
    data = _replay_json(page)
    terrain = data["terrain"]
    assert terrain["n"] == 160 and '<div class="seg" data-group="view">' in page
    assert 'id="rp-3dnote"' in page
    steps = struct.unpack(f"<{160 * 160}h", zlib.decompress(base64.b64decode(terrain["dz"])))
    first_row = [sum(steps[:i + 1]) / 10 for i in range(160)]  # row 0 decoded the browser's way
    n, heights = gamedata.terrain_heights(b["map"]["key"])
    assert first_row == heights[:160]
    assert len(page) < 700_000, len(page)  # minimap + terrain + tracks + events stay embeddable


def test_without_terrain_there_is_no_3d_switch():
    from blitzreview import render
    saved = render.gamedata.terrain
    render.gamedata.terrain = lambda key: None
    try:
        page = render_battle_html(_battle())
    finally:
        render.gamedata.terrain = saved
    assert _replay_json(page)["terrain"] is None
    assert '<div class="seg" data-group="view">' not in page
