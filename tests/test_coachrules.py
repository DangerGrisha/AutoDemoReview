"""Tests for the free coach (demoreview/coachrules.py).

Two failure modes are worth guarding against here, both of which have already
happened:

  * FABRICATED EVIDENCE -- a rule citing round numbers it did not actually
    measure (the opening-duel rule once cited "rounds where you died before
    0:25" as if those were the lost opening duels). A report that names a round
    is inviting the player to go watch it, so every cited round must come from
    the fact sheet. `test_report_never_cites_a_round_it_did_not_measure` is the
    permanent guard.
  * BROKEN RUSSIAN -- the report is prose, and "21 киллов" or "раундов 4, 6, 8"
    reads as machine output no matter how good the analysis behind it is.

The fact sheets below are built from `tactics._P().summary()` so that adding a
metric to the engine cannot leave these fixtures silently missing a key.
"""

from demoreview import coachrules, tactics

SID = "76561198000000001"


# --------------------------------------------------------------------------- #
# fact-sheet fixtures
# --------------------------------------------------------------------------- #

def _you(**over):
    """A complete player summary: every key the engine can emit, filled in."""
    base = tactics._P(SID, "you").summary()
    base.update({
        "rounds": 22, "kills": 18, "deaths": 15, "hs_pct": 42,
        "opening_duels": 6, "opening_win_pct": 50, "avg_death_time_s": 41.0,
        "untraded_death_pct": 40, "trade_chances": 12, "trades_made": 5,
        "trade_conversion_pct": 42, "median_isolation_u": 600,
        "median_enemy_dist_at_death_u": 900, "shots_per_kill": 19.0,
        "deaths_to_blinded_enemy": 1, "deaths_through_smoke": 1,
        "deaths_to_wallbang": 0, "nades_per_round": 2.1,
        "nades_by_type": {"flash": 12, "smoke": 20}, "flashes_thrown": 12,
        "enemies_blinded": 9, "teammates_blinded": 3, "enemy_blind_s": 40.0,
        "team_blind_s": 8.0, "bad_flash_pct": 17, "flash_conversion_pct": 33,
        "rounds_died_with_no_util_thrown": 2,
    })
    base.update(over)
    return base


def _lobby(you, ranks, medians=None):
    """vs_lobby entries: `ranks` maps metric -> rank, everything else is mid-table.

    `lobby_median` defaults to the player's own value (a dead tie) unless
    `medians` overrides it, so a fixture has to say explicitly when the player
    actually beats the field.
    """
    medians = medians or {}
    out = {}
    for key in tactics.BETTER:
        value = you.get(key)
        if value is None:
            continue
        out[key] = {"you": value, "lobby_median": medians.get(key, value),
                    "of": 10, "rank": ranks.get(key, 5)}
    return out


def _rounds(n=22):
    rows = []
    for i in range(1, n + 1):
        row = {"n": i, "side": "T" if i <= 12 else "CT", "k": i % 3,
               "survived": i % 5 == 0, "won": i % 2 == 0}
        if not row["survived"]:
            row["death_s"] = 20.0 + (i % 7) * 5
        rows.append(row)
    return rows


def _facts(you=None, ranks=None, rounds=None, hotspot=None, medians=None):
    you = you or _you()
    return {
        "map": "de_ancient",
        "rounds_analysed": 22,
        "player": {"sid": SID, "name": "you"},
        "you": you,
        "vs_lobby": _lobby(you, ranks or {}, medians),
        "death_hotspot": hotspot,
        "rounds": rounds if rounds is not None else _rounds(),
    }


def _everything_wrong():
    """A player who is bottom of the lobby at everything -- fires every rule."""
    you = _you(
        kills=6, deaths=21, hs_pct=12, opening_duels=9, opening_win_pct=11,
        avg_death_time_s=19.0, untraded_death_pct=85, trade_chances=14,
        trades_made=1, trade_conversion_pct=7, median_isolation_u=1400,
        shots_per_kill=41.0, deaths_to_blinded_enemy=5, deaths_through_smoke=4,
        deaths_to_wallbang=3, flashes_thrown=28, teammates_blinded=23,
        enemy_blind_s=30.0, team_blind_s=75.0, bad_flash_pct=71,
        flash_conversion_pct=4, rounds_died_with_no_util_thrown=8,
    )
    rounds = _rounds()
    for row in rounds:
        row["won"] = False
        row.setdefault("death_s", 18.0)
    for row in rounds[:4]:
        row["opening"] = "lost"
        row["missed_trades"] = 2
    for row in rounds[4:8]:
        row["nearest_mate_u"] = 1600
    for row in rounds[8:12]:
        row["util"] = ["flash", "flash"]
    ranks = {key: 10 for key in tactics.BETTER}
    hotspot = {"deaths": 5, "x": -189, "y": -720, "rounds": [4, 6, 8, 16, 21]}
    return _facts(you=you, ranks=ranks, rounds=rounds, hotspot=hotspot)


ALL_FIXTURES = {
    "clean": _facts,
    "everything_wrong": _everything_wrong,
    "early_death_only": lambda: _facts(ranks={"avg_death_time_s": 10}),
    "hotspot_only": lambda: _facts(
        hotspot={"deaths": 4, "x": 10, "y": 20, "rounds": [2, 9, 14, 19]}),
    "no_rounds_at_all": lambda: _facts(rounds=[]),
}


# --------------------------------------------------------------------------- #
# evidence integrity -- the headline guarantee of a rule engine
# --------------------------------------------------------------------------- #

def test_report_never_cites_a_round_it_did_not_measure():
    """Every round number in the report must exist in the fact sheet.

    This is the whole promise of the free coach over an LLM: it cannot invent
    evidence. A rule that guesses rounds from a proxy metric fails here.
    """
    for label, build in ALL_FIXTURES.items():
        facts = build()
        real = {r["n"] for r in facts["rounds"]}
        report = coachrules.build_report(facts)
        for problem in report["problems"]:
            invented = set(problem.get("rounds") or []) - real
            assert not invented, (
                f"{label}: '{problem['title']}' cites rounds {sorted(invented)} "
                f"that are not in the fact sheet")


def test_match_wide_findings_cite_no_rounds_at_all():
    """Spray and smoke deaths are match totals; decorating them with unrelated
    round numbers would be evidence theatre."""
    facts = _everything_wrong()
    for rule in (coachrules._rule_spray, coachrules._rule_smoke_deaths):
        problem = rule(facts, None)
        assert problem is not None, rule.__name__
        assert problem["rounds"] == [], rule.__name__


def test_hotspot_evidence_matches_the_hotspot_it_was_given():
    hotspot = {"deaths": 5, "x": -189, "y": -720, "rounds": [4, 6, 8, 16, 21]}
    problem = coachrules._rule_hotspot(_facts(hotspot=hotspot), None)

    assert problem["rounds"] == [4, 6, 8, 16, 21]
    assert "5 смертей" in problem["evidence"]
    assert "-189" in problem["evidence"] and "-720" in problem["evidence"]


# --------------------------------------------------------------------------- #
# Russian prose
# --------------------------------------------------------------------------- #

def test_counting_nouns_agree_with_the_number():
    cases = [(1, "килл"), (2, "килла"), (5, "киллов"), (11, "киллов"),
             (21, "килл"), (22, "килла"), (25, "киллов"), (101, "килл"),
             (111, "киллов"), (114, "киллов"), (0, "киллов")]
    for n, expected in cases:
        assert coachrules._kills_word(n) == expected, n

    assert coachrules._rounds_word(1) == "раунд"
    assert coachrules._rounds_word(3) == "раунда"
    assert coachrules._rounds_word(12) == "раундов"
    assert coachrules._deaths_word(1) == "смерть"
    assert coachrules._deaths_word(2) == "смерти"
    assert coachrules._deaths_word(5) == "смертей"


def test_listing_rounds_uses_the_nominative_not_the_counting_form():
    """'раунды 4, 6, 8' -- a list, not a count of eight rounds."""
    assert coachrules._list_rounds([4]) == "раунд 4"
    assert coachrules._list_rounds([4, 6, 8]) == "раунды 4, 6, 8"


def test_headline_reads_as_a_sentence_not_a_title():
    report = coachrules.build_report(_everything_wrong())
    headline = report["headline"]

    assert headline.endswith(".")
    # The lead problem is folded in mid-sentence, so it must be lower-cased.
    assert " Ты " not in headline, headline


# --------------------------------------------------------------------------- #
# rule firing
# --------------------------------------------------------------------------- #

def test_a_mid_table_match_produces_no_problems():
    report = coachrules.build_report(_facts())

    assert report["problems"] == []
    assert "Явных провалов" in report["headline"]
    assert report["drill"]["name"] == "Поддерживать уровень"


def test_dying_first_in_the_lobby_is_reported():
    facts = _facts(you=_you(avg_death_time_s=19.0),
                   ranks={"avg_death_time_s": 10})
    problem = coachrules._rule_early_death(facts, None)

    assert problem["severity"] == "high"
    assert "19.0 с" in problem["evidence"]
    assert problem["rounds"], "the rule must point at the earliest deaths"


def test_a_good_rank_fires_nothing():
    for rule in coachrules.RULES:
        assert rule(_facts(ranks={key: 2 for key in tactics.BETTER}), None) is None, \
            rule.__name__


def test_severity_follows_the_position_in_the_lobby():
    assert coachrules._severity(10, 10) == "high"
    assert coachrules._severity(9, 10) == "high"
    assert coachrules._severity(8, 10) == "medium"
    assert coachrules._severity(7, 10) == "medium"
    assert coachrules._severity(6, 10) == "low"


def test_teamflash_fires_on_a_bad_flash_share_even_at_a_decent_rank():
    """Blinding your own team half the time is a problem at any rank."""
    you = _you(teammates_blinded=14, team_blind_s=60.0, bad_flash_pct=62,
               flashes_thrown=25, flash_conversion_pct=8)
    facts = _facts(you=you, ranks={key: 4 for key in tactics.BETTER})

    assert coachrules._rule_teamflash(facts, None) is not None


# --------------------------------------------------------------------------- #
# merging and ordering
# --------------------------------------------------------------------------- #

def test_dying_early_in_the_same_spot_is_one_problem_not_two():
    """Two views of one habit must not eat two of the three report slots."""
    rounds = _rounds()
    for row in rounds:
        row["death_s"] = 15.0                       # every death is an early one
    facts = _facts(ranks={"avg_death_time_s": 10}, rounds=rounds,
                   hotspot={"deaths": 5, "x": 0, "y": 0,
                            "rounds": [r["n"] for r in rounds[:5]]})

    titles = [p["title"] for p in coachrules.build_report(facts)["problems"]]

    assert "Ты умираешь рано и в одной и той же точке" in titles
    assert "Ты умираешь раньше всех в лобби" not in titles
    assert "Ты умираешь в одной и той же точке" not in titles


def test_unrelated_early_deaths_and_hotspot_stay_separate():
    rounds = _rounds()
    for row in rounds:
        row["death_s"] = 60.0
    for row in rounds[:5]:                          # the early deaths...
        row["death_s"] = 12.0
    facts = _facts(ranks={"avg_death_time_s": 10}, rounds=rounds,
                   hotspot={"deaths": 4, "x": 0, "y": 0,
                            "rounds": [18, 19, 20, 21]})  # ...are elsewhere

    titles = [p["title"] for p in coachrules.build_report(facts)["problems"]]

    assert "Ты умираешь раньше всех в лобби" in titles
    assert "Ты умираешь в одной и той же точке" in titles


def test_at_most_three_problems_and_the_worst_comes_first():
    report = coachrules.build_report(_everything_wrong())
    problems = report["problems"]

    assert 0 < len(problems) <= 3
    order = {"high": 2, "medium": 1, "low": 0}
    severities = [order[p["severity"]] for p in problems]
    assert severities == sorted(severities, reverse=True), severities


def test_the_drill_addresses_the_top_problem():
    you = _you(teammates_blinded=25, team_blind_s=80.0, bad_flash_pct=74,
               flashes_thrown=30, flash_conversion_pct=3)
    facts = _facts(you=you, ranks={"teammates_blinded": 10, "bad_flash_pct": 10})

    report = coachrules.build_report(facts)

    assert report["problems"][0]["title"].startswith("Твои флешки")
    assert report["drill"]["name"] == coachrules.DRILLS["teamflash"][0]


# --------------------------------------------------------------------------- #
# report contract -- what web/coaching.py and the renderer rely on
# --------------------------------------------------------------------------- #

def test_report_shape_is_stable_across_fixtures():
    for label, build in ALL_FIXTURES.items():
        report = coachrules.build_report(build())

        assert set(report) == {"headline", "strengths", "problems", "drill"}, label
        assert isinstance(report["headline"], str) and report["headline"], label
        assert len(report["strengths"]) <= 2, label
        assert set(report["drill"]) == {"name", "how", "target"}, label
        for problem in report["problems"]:
            assert set(problem) == {"title", "severity", "cause", "evidence",
                                    "rounds", "fix"}, (label, problem.keys())
            assert problem["severity"] in ("high", "medium", "low"), label
            for field in ("title", "cause", "evidence", "fix"):
                assert problem[field].strip(), (label, field)


def test_internal_rule_codes_never_reach_the_reader():
    for build in ALL_FIXTURES.values():
        for problem in coachrules.build_report(build())["problems"]:
            assert "code" not in problem


def test_strengths_are_only_claimed_for_the_top_two_in_the_lobby():
    beat = {"kills": 11}                    # the player has 18, the lobby 11
    top = coachrules.build_report(
        _facts(ranks={"kills": 1}, medians=beat))["strengths"]
    assert top and "лучший результат" in top[0]["evidence"]

    second = coachrules.build_report(
        _facts(ranks={"kills": 2}, medians=beat))["strengths"]
    assert second and "второй результат" in second[0]["evidence"]

    assert coachrules.build_report(
        _facts(ranks={"kills": 3}, medians=beat))["strengths"] == []


def test_matching_the_lobby_is_not_a_strength():
    """Everyone tied on a metric shares rank 1, which is not an achievement.

    Real case: an old 3D artifact carries no headshot flags, so every player
    scores 0% and the report opened with "0% в голову — лучший результат
    в лобби". Praise has to require actually beating the field.
    """
    you = _you(hs_pct=0, kills=18)
    facts = _facts(you=you, ranks={"hs_pct": 1, "kills": 1})   # medians == values

    assert coachrules.build_report(facts)["strengths"] == []


def test_a_missing_metric_skips_its_rule_instead_of_killing_the_report():
    """Not every match measures everything; one gap must not lose the report."""
    you = _you()
    del you["team_blind_s"]                          # as if flashes were unmeasured
    facts = _facts(you=you, ranks={key: 10 for key in tactics.BETTER})

    report = coachrules.build_report(facts)

    assert report["headline"]
    assert all("флешки" not in p["title"] for p in report["problems"])


def test_an_empty_fact_sheet_is_refused_rather_than_faked():
    for empty in (None, {}):
        try:
            coachrules.build_report(empty)
        except ValueError:
            continue
        raise AssertionError(f"build_report({empty!r}) should have raised")
