"""Derived numbers for the report: rates, team totals, lobby ranks.

Ranking follows demoreview/tactics.rank_against -- rank 1 is best, ties share
the better rank, the median is over the OTHER players -- but is re-implemented
here so the two games' packages stay independent until the UI merge.
"""

import statistics

# Fewer comparable players than this and a rank means nothing.
MIN_LOBBY_SAMPLE = 4

# metric -> True when higher is better. Shown-but-unranked numbers (damage
# received: a heavy SHOULD soak) are deliberately absent.
BETTER = {
    "damage": True,
    "kills": True,
    "accuracy": True,
    "pen_rate": True,
    "damage_per_shot": True,
    "assist": True,
    "damage_blocked": True,
    "xp": True,
}


def rates(stats):
    """Accuracy, penetration rate and damage per shot; None where undefined."""
    shots, hits = stats["shots"], stats["hits"]
    return {
        "accuracy": stats["hits"] / shots if shots else None,
        "pen_rate": stats["penetrations"] / hits if hits else None,
        "damage_per_shot": stats["damage"] / shots if shots else None,
    }


def metric(vehicle, key):
    stats = vehicle.get("stats")
    if stats is None:
        return None
    if key in stats:
        return stats[key]
    return rates(stats).get(key)


def rank_against(mine, others, higher_better=True):
    """{you, rank, of, median} or None when there is too little to compare."""
    values = [v for v in others if v is not None]
    if mine is None or len(values) < MIN_LOBBY_SAMPLE:
        return None
    better = sum(1 for v in values if (v > mine if higher_better else v < mine))
    return {"you": mine, "rank": better + 1, "of": len(values) + 1,
            "median": statistics.median(values)}


def author(battle):
    return next((v for v in battle["vehicles"] if v["is_author"]), None)


def lobby_ranks(battle):
    """Where the recorder ranks among all players with results, per metric."""
    me = author(battle)
    if me is None or me.get("stats") is None:
        return {}
    others = [v for v in battle["vehicles"] if v is not me and v.get("stats") is not None]
    out = {}
    for key, higher in BETTER.items():
        rank = rank_against(metric(me, key), [metric(o, key) for o in others], higher)
        if rank is not None:
            out[key] = rank
    return out


def leaders(battle):
    """{metric: best value in the battle} for highlighting table cells."""
    out = {}
    for key, higher in BETTER.items():
        values = [metric(v, key) for v in battle["vehicles"]]
        values = [v for v in values if v is not None]
        if values:
            out[key] = max(values) if higher else min(values)
    return out


def team_summary(battle, ally):
    side = [v for v in battle["vehicles"] if v["ally"] == ally]
    with_stats = [v for v in side if v.get("stats")]
    tiers = [v["tank"]["tier"] for v in side if v["tank"].get("tier")]
    ratings = [v["stats"]["mm_rating"] for v in with_stats
               if v["stats"].get("mm_rating") is not None]
    return {
        "players": len(side),
        "alive": sum(1 for v in side if v["alive"]),
        "damage": sum(v["stats"]["damage"] for v in with_stats) if with_stats else None,
        "kills": sum(v["stats"]["kills"] for v in with_stats) if with_stats else None,
        "avg_tier": sum(tiers) / len(tiers) if tiers else None,
        # Display rating = 3000 + 10 * mm_rating (see battle_results.py).
        "avg_rating": 3000 + 10 * statistics.mean(ratings) if ratings else None,
        "ratings_known": len(ratings),
    }
