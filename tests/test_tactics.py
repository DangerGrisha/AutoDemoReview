"""Tests for the tactical facts engine (demoreview/tactics.py).

The dangerous bug class here is a metric ranked the wrong way round. It is
silent: the report still renders, still cites real numbers, and still sounds
confident -- it just praises the player for the thing that is losing them the
match. Two such bugs have already shipped (`deaths` ranked as higher-is-better,
and `avg_death_time_s` inverted so dying first in the server came out as rank 1
of 10). Hence DIRECTIONS below: it restates, in plain human terms, which of two
players is the better one, and fails if `BETTER` ever disagrees.
"""

from demoreview import tactics

# (metric, a clearly WORSE value, a clearly BETTER value). Read each line as a
# sentence: "a player with <worse> is doing worse than one with <better>".
# Deliberately written from scratch rather than derived from tactics.BETTER --
# a table generated from the code under test would agree with any bug in it.
DIRECTIONS = [
    ("kills", 5, 25),                              # more kills is better
    ("deaths", 22, 8),                             # more deaths is worse
    ("hs_pct", 20, 60),
    ("opening_win_pct", 20, 70),
    ("avg_death_time_s", 18.0, 50.0),              # dying at 0:18 is the failure
    ("untraded_death_pct", 80, 20),                # deaths nobody trades: bad
    ("trade_conversion_pct", 10, 60),
    ("median_isolation_u", 1500, 400),             # dying far from help: bad
    ("shots_per_kill", 40.0, 12.0),
    ("deaths_to_blinded_enemy", 6, 0),             # losing to a flashed enemy
    ("deaths_through_smoke", 5, 0),
    ("deaths_to_wallbang", 4, 0),
    ("teammates_blinded", 20, 1),                  # blinding your own team
    ("team_blind_s", 70.0, 5.0),
    ("bad_flash_pct", 80, 10),
    ("flash_conversion_pct", 0, 50),               # flashes that led to a kill
    ("rounds_died_with_no_util_thrown", 9, 0),
]

# Counts with no inherent direction: throwing the most flashes in the server is
# neither good nor bad, so they must stay UNRANKED -- "rank 1 of 10 in flashes
# thrown" would read as praise for something that isn't an achievement.
VOLUME_STATS = {
    "rounds", "opening_duels", "trade_chances", "trades_made",
    "median_enemy_dist_at_death_u", "nades_per_round", "flashes_thrown",
    "enemies_blinded", "enemy_blind_s",
}


def _numeric_metrics():
    """Every numeric key a player summary can carry."""
    summary = tactics._P("76561198000000001", "somebody").summary()
    return {k for k, v in summary.items()
            if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))}


# --------------------------------------------------------------------------- #
# ranking direction -- the load-bearing tests
# --------------------------------------------------------------------------- #

def test_every_metric_is_ranked_the_way_a_human_would_rank_it():
    for key, worse, better in DIRECTIONS:
        direction = tactics.BETTER[key]
        lobby_of_betters = tactics.rank_against(worse, [better] * 9, direction)
        lobby_of_worses = tactics.rank_against(better, [worse] * 9, direction)
        assert lobby_of_betters["rank"] == 10, (
            f"{key}: {worse} should be LAST in a lobby of {better}, "
            f"got rank {lobby_of_betters['rank']}")
        assert lobby_of_worses["rank"] == 1, (
            f"{key}: {better} should be FIRST in a lobby of {worse}, "
            f"got rank {lobby_of_worses['rank']}")


def test_the_direction_table_covers_exactly_the_ranked_metrics():
    """A new metric must not slip in unranked, or ranked without a direction."""
    assert {key for key, _, _ in DIRECTIONS} == set(tactics.BETTER), (
        "DIRECTIONS and tactics.BETTER disagree -- a metric was added or "
        "removed without deciding which way is good")


def test_ranked_metrics_all_exist_in_a_player_summary():
    """A typo'd key in BETTER silently means 'this metric is never ranked'."""
    unknown = set(tactics.BETTER) - _numeric_metrics()
    assert not unknown, f"BETTER names metrics no summary produces: {unknown}"


def test_unranked_metrics_are_only_volume_counts():
    unranked = _numeric_metrics() - set(tactics.BETTER)
    assert unranked == VOLUME_STATS, (
        f"unexpected unranked metric(s): {unranked ^ VOLUME_STATS}")


# --------------------------------------------------------------------------- #
# rank_against
# --------------------------------------------------------------------------- #

def test_rank_places_you_inside_the_whole_lobby():
    got = tactics.rank_against(50, [10, 20, 30, 40, 60, 70, 80, 90, 100], "high")

    assert got["you"] == 50
    assert got["of"] == 10                 # nine others plus you
    assert got["rank"] == 6                # five players are above you
    assert got["lobby_median"] == 60.0     # median of the nine OTHERS, not of the pool


def test_ties_do_not_push_you_down_the_ranking():
    """Matching the field is not worse than the field."""
    assert tactics.rank_against(10, [10] * 9, "low")["rank"] == 1
    assert tactics.rank_against(10, [10] * 9, "high")["rank"] == 1


def test_rank_needs_a_real_lobby():
    assert tactics.rank_against(5, [1, 2, 3], "high") is None
    assert tactics.rank_against(5, [], "high") is None
    assert tactics.rank_against(5, [1, 2, 3, 4], "high") is not None


def test_rank_ignores_players_the_metric_could_not_be_computed_for():
    """`None` (never died, never shot) must not be compared as a number."""
    got = tactics.rank_against(30.0, [None, 40.0, 50.0, 60.0, 70.0], "high")

    assert got["of"] == 5                  # the None is dropped, not counted as a player
    assert got["rank"] == 5                # last among the values that do exist
    assert got["lobby_median"] == 55.0     # and it does not drag the median either


def test_rank_does_not_treat_booleans_as_numbers():
    assert tactics.rank_against(1, [True, False, True, False], "high") is None


# --------------------------------------------------------------------------- #
# death hotspot
# --------------------------------------------------------------------------- #

def _spot(rnd, x, y):
    return (rnd, 100, x, y)               # (round, sample, x, y)


def test_hotspot_finds_repeated_deaths_in_one_place():
    spots = [_spot(3, 100, 100), _spot(7, 180, 60), _spot(9, 60, 140),
             _spot(12, 120, 120), _spot(5, 5000, 5000)]

    got = tactics._hotspot(spots)

    assert got["deaths"] == 4
    assert got["rounds"] == [3, 7, 9, 12]         # the outlier is not included
    assert abs(got["x"] - 115) <= 10 and abs(got["y"] - 105) <= 10


def test_hotspot_needs_at_least_three_deaths():
    assert tactics._hotspot([_spot(1, 0, 0), _spot(2, 50, 50)]) is None
    assert tactics._hotspot([]) is None


def test_scattered_deaths_are_not_a_hotspot():
    """Dying all over the map is not the 'same spot' problem."""
    spots = [_spot(n, n * 2000, n * 2000) for n in range(1, 6)]

    assert tactics._hotspot(spots) is None


def test_hotspot_radius_is_the_documented_one():
    """Two deaths just inside 450 units cluster; just outside, they don't."""
    inside = [_spot(1, 0, 0), _spot(2, 400, 0), _spot(3, 200, 0)]
    outside = [_spot(1, 0, 0), _spot(2, 460, 0), _spot(3, 920, 0)]

    assert tactics.HOTSPOT_RADIUS_U == 450.0
    assert tactics._hotspot(inside)["deaths"] == 3
    assert tactics._hotspot(outside) is None


def test_hotspot_reports_each_round_once():
    """Two deaths cannot happen in one round; a duplicate would inflate the count."""
    spots = [_spot(4, 0, 0), _spot(4, 10, 10), _spot(8, 20, 20)]

    assert tactics._hotspot(spots)["rounds"] == [4, 8]


# --------------------------------------------------------------------------- #
# small numeric helpers
# --------------------------------------------------------------------------- #

def test_median_handles_both_parities_and_gaps():
    assert tactics._median([3, 1, 2]) == 2
    assert tactics._median([1, 2, 3, 4]) == 2.5
    assert tactics._median([None, 1, None, 3]) == 2
    assert tactics._median([]) is None
    assert tactics._median([None]) is None


def test_pct_returns_none_rather_than_dividing_by_zero():
    assert tactics._pct(3, 10) == 30
    assert tactics._pct(1, 3) == 33
    assert tactics._pct(0, 5) == 0
    assert tactics._pct(4, 0) is None      # nothing happened -> no percentage


def test_distance_is_planar():
    """Spacing is measured on the floor plan; height must not dilute it."""
    assert tactics._dist(0, 0, 3, 4) == 5.0
    assert tactics._dist(-100, -100, -100, -100) == 0.0


# --------------------------------------------------------------------------- #
# player summary
# --------------------------------------------------------------------------- #

def test_summary_of_a_player_who_did_nothing_is_all_none_not_zero():
    """A player with no kills has no HS%; reporting 0% would be a lie."""
    summary = tactics._P("1", "idle").summary()

    assert summary["hs_pct"] is None
    assert summary["shots_per_kill"] is None
    assert summary["avg_death_time_s"] is None
    assert summary["kills"] == 0           # counts are still real zeros


def test_summary_folds_self_blindness_into_team_blindness():
    """Flashing yourself is a mistake of the same kind as flashing a mate."""
    p = tactics._P("1", "flasher")
    p.flashes = 4
    p.enemy_blind_s = 10.0
    p.team_blind_s = 6.0
    p.self_blind_s = 4.0

    summary = p.summary()

    assert summary["team_blind_s"] == 10.0            # 6 own + 4 self
    assert summary["bad_flash_pct"] == 50             # 10 of 20 seconds wasted


def test_summary_counts_effective_blinds_not_flickers():
    """EFFECTIVE_BLIND_S is what separates a real flash from a flicker."""
    assert tactics.EFFECTIVE_BLIND_S == 0.7
    assert tactics.TRADE_WINDOW_S == 4.0
    assert tactics.TRADE_RANGE_U == 800.0
