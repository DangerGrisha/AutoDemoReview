"""Ranking a player inside their own lobby.

An absolute threshold ("dying 1000 units from a teammate is bad") is wrong at
every skill level; "the worst in this match" is true at all of them and needs
no tuning. Rank 1 is the best result, ties share the better rank, and the
median is over the OTHER players.
"""

import statistics

# Fewer comparable players than this and a rank means nothing.
MIN_LOBBY_SAMPLE = 4


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def rank_against(mine, others, higher_better=True, min_sample=MIN_LOBBY_SAMPLE):
    """{you, rank, of, median} for `mine` among `others`, or None when `mine`
    is missing or fewer than `min_sample` others have a comparable number.

    The direction is applied HERE, so nothing downstream has to remember that
    fewer deaths and more kills are both good news."""
    values = [v for v in others if _number(v)]
    if not _number(mine) or len(values) < min_sample:
        return None
    better = sum(1 for v in values if (v > mine if higher_better else v < mine))
    return {"you": mine, "rank": better + 1, "of": len(values) + 1,
            "median": statistics.median(values)}
