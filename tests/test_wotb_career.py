"""Tests for the Blitz career (blitzreview/career.py): many battles of one
player, per tank and their habits.

What would mislead a player: a single bad battle called a habit, a battle
without results dragging the average damage down as a zero, a win rate over
battles whose outcome is unknown, or a habit whose links lead to battles
where it did not happen. The habit thresholds and the averages are pinned on
hand-made battle lines; the lines themselves on real replays.
"""

from pathlib import Path

from blitzreview import battle, career

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"


def _line(i, tank="ИСУ-130", outcome="win", damage=2000, problems=(), strengths=(),
          results=True, alive=True, started=None):
    return {"id": i, "started_at": started if started is not None else 1_000_000 + i,
            "map": f"map{i}", "mode": "Встречный бой", "tank": tank, "tank_type": "TD",
            "tank_tier": 8, "outcome": outcome if results else "unknown",
            "has_results": results, "damage": damage if results else None,
            "kills": 1 if results else None, "shots": 10 if results else None,
            "hits": 8 if results else None, "pens": 6 if results else None,
            "damage_received": 1000 if results else None, "alive": alive,
            "problems": [{"code": code, "title": code, "severity": "medium", "moment": 60.0 + i}
                         for code in problems],
            "strengths": list(strengths)}


def test_a_habit_needs_two_battles_and_a_fifth_of_them():
    lines = [_line(i) for i in range(10)]
    lines[0]["problems"] = _line(0, problems=["isolated_death"])["problems"]
    assert career.summarize(lines)["habits"] == []             # once is an accident
    for i in (1, 2):
        lines[i]["problems"] = _line(i, problems=["isolated_death"])["problems"]
    habits = career.summarize(lines)["habits"]
    assert [(h["code"], h["count"]) for h in habits] == [("isolated_death", 3)]
    assert habits[0]["title"] == "Гибель в отрыве от команды"   # named for the pattern
    assert [b["id"] for b in habits[0]["battles"]] == [2, 1, 0]  # newest first, with links
    assert habits[0]["battles"][0]["moment"] == 62.0
    many = [_line(i) for i in range(20)]
    for i in (0, 1, 2):
        many[i]["problems"] = _line(i, problems=["misses"])["problems"]
    assert career.summarize(many)["habits"] == []                # 3 of 20 is under a fifth


def test_habits_are_ordered_by_how_often_they_happen():
    lines = [_line(0, problems=["misses", "cheap_death"]), _line(1, problems=["misses"]),
             _line(2, problems=["misses", "cheap_death"]), _line(3)]
    assert [h["code"] for h in career.summarize(lines)["habits"]] == ["misses", "cheap_death"]


def test_averages_skip_battles_without_results():
    lines = [_line(0, damage=3000), _line(1, damage=1000, outcome="loss"),
             _line(2, results=False, alive=False)]
    o = career.summarize(lines)["overall"]
    assert o["battles"] == 3 and o["with_results"] == 2
    assert o["avg_damage"] == 2000 and o["win_rate"] == 0.5     # not dragged to 1333 / 33 %
    assert abs(o["survival"] - 2 / 3) < 1e-9                    # fate is known without results
    assert o["accuracy"] == 0.8 and o["pen_rate"] == 0.75


def test_tanks_are_grouped_and_the_most_played_comes_first():
    lines = [_line(0, tank="Т-54"), _line(1, tank="ИСУ-130"), _line(2, tank="ИСУ-130")]
    tanks = career.summarize(lines)["tanks"]
    assert [(t["tank"], t["battles"]) for t in tanks] == [("ИСУ-130", 2), ("Т-54", 1)]


def test_recent_is_newest_first_and_an_empty_career_is_fine():
    lines = [_line(i, started=t) for i, t in enumerate((300, 100, 200))]
    assert [b["id"] for b in career.summarize(lines)["recent"]] == [0, 2, 1]
    empty = career.summarize([])
    assert empty["recent"] == [] and empty["overall"]["battles"] == 0
    assert empty["overall"]["avg_damage"] is None and empty["overall"]["win_rate"] is None


def test_battle_lines_of_real_replays():
    canyon = career.battle_line(1, battle.load(FIXTURES / "v11_20_canyon_results.wotbreplay"))
    assert canyon["tank"] == "Объект 704" and canyon["damage"] == 1399 and canyon["alive"] is False
    assert [(p["code"], p["moment"]) for p in canyon["problems"]] == [("isolated_death", 98.9)]
    rift = career.battle_line(2, battle.load(FIXTURES / "v11_20_rift_base_capture.wotbreplay"))
    assert rift["problems"] == [] and rift["strengths"] == ["damage", "kills"]
    savanna = career.battle_line(3, battle.load(FIXTURES / "v11_20_savanna_no_results.wotbreplay"))
    assert savanna["has_results"] is False and savanna["damage"] is None
    good = career.summarize([canyon, rift, savanna])
    assert good["overall"]["with_results"] == 2 and good["overall"]["avg_damage"] == (1399 + 2495) / 2
