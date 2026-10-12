"""Career: one player's battles together -- per tank, and their habits.

The coach (coachrules.py) says what went wrong in ONE battle. Over many
battles the useful question is what goes wrong again and again: a rule that
fires in half of a player's battles is a habit, not bad luck, and the
cheapest thing to fix. So the career counts every rule over the battles,
with the battles (and the moment) it fired in.

Only battles recorded by the same account belong together -- a user may
upload a friend's replays too (web/blitz.py groups by recorder). Numbers
that need battle results (damage, accuracy, wins) count only the battles
that have them; the map-based habits count every battle.
"""

from collections import defaultdict

from . import analysis, coachrules

# A rule that fired once is an accident; twice or more, and in at least this
# share of the battles, a habit worth a line.
HABIT_MIN_BATTLES = 2
HABIT_MIN_SHARE = 0.2
RECENT = 10


def battle_line(battle_id, battle):
    """One battle reduced to what the career needs, coach findings included.
    None when the replay does not say who recorded it."""
    facts = coachrules.tactics.analyse(battle)
    if facts is None:
        return None
    found = coachrules.findings(facts)
    report = coachrules.build_report(facts, found)
    me = analysis.author(battle)
    stats = me.get("stats") or {}
    return {
        "id": battle_id,
        "started_at": battle.get("started_at"),
        "map": battle["map"].get("name"),
        "mode": (battle.get("mode") or {}).get("name"),
        "tank": me["tank"]["name"],
        "tank_type": me["tank"].get("type"),
        "tank_tier": me["tank"].get("tier"),
        "outcome": battle.get("outcome"),
        "has_results": bool(stats),
        "damage": stats.get("damage"),
        "kills": stats.get("kills"),
        "shots": stats.get("shots"),
        "hits": stats.get("hits"),
        "pens": stats.get("penetrations"),
        "damage_received": stats.get("damage_received"),
        "alive": me.get("alive"),
        "problems": [{"code": p["code"], "title": p["title"], "severity": p["severity"],
                      "moment": p["moments"][0] if p["moments"] else None} for p in found],
        "strengths": [s["key"] for s in report["strengths"]],
    }


def _rate(part, whole):
    return part / whole if whole else None


def _totals(lines):
    """Averages over the battles that have results; None where nothing counts."""
    rated = [b for b in lines if b["has_results"]]
    decided = [b for b in rated if b["outcome"] in ("win", "loss", "draw")]
    shots = sum(b["shots"] or 0 for b in rated)
    hits = sum(b["hits"] or 0 for b in rated)
    known_fate = [b for b in lines if b["alive"] is not None]
    return {
        "battles": len(lines),
        "with_results": len(rated),
        "wins": sum(1 for b in decided if b["outcome"] == "win"),
        "losses": sum(1 for b in decided if b["outcome"] == "loss"),
        "win_rate": _rate(sum(1 for b in decided if b["outcome"] == "win"), len(decided)),
        "avg_damage": _rate(sum(b["damage"] for b in rated), len(rated)),
        "avg_kills": _rate(sum(b["kills"] for b in rated), len(rated)),
        "avg_damage_net": _rate(sum(b["damage"] - b["damage_received"] for b in rated), len(rated)),
        "accuracy": _rate(hits, shots),
        "pen_rate": _rate(sum(b["pens"] or 0 for b in rated), hits),
        "survival": _rate(sum(1 for b in known_fate if b["alive"]), len(known_fate)),
    }


def summarize(lines):
    """The career page of one player from their battle lines (battle_line),
    in any order:

        overall  _totals over every battle
        tanks    [{tank, type, tier, **_totals}] -- most played first
        habits   [{code, title, count, share, battles: [{id, started_at, map,
                 tank, moment}]}] -- rules that fired in HABIT_MIN_BATTLES and
                 HABIT_MIN_SHARE of the battles, the most frequent first
        good     [{key, count, share}] -- strengths, the same way
        recent   the RECENT newest battle lines
    """
    lines = sorted((b for b in lines if b), key=lambda b: (b["started_at"] or 0, b["id"]),
                   reverse=True)
    if not lines:
        return {"overall": _totals([]), "tanks": [], "habits": [], "good": [], "recent": []}
    by_tank = defaultdict(list)
    for b in lines:
        by_tank[(b["tank"], b["tank_type"], b["tank_tier"])].append(b)
    tanks = [dict(_totals(group), tank=tank, type=kind, tier=tier)
             for (tank, kind, tier), group in by_tank.items()]
    tanks.sort(key=lambda t: (-t["battles"], -(t["avg_damage"] or 0), t["tank"]))

    fired, titles = defaultdict(list), {}
    good = defaultdict(int)
    for b in lines:
        for p in b["problems"]:
            fired[p["code"]].append({"id": b["id"], "started_at": b["started_at"], "map": b["map"],
                                     "tank": b["tank"], "moment": p["moment"]})
            titles.setdefault(p["code"], p["title"])
        for key in b["strengths"]:
            good[key] += 1
    n = len(lines)
    habits = [{"code": code, "title": _HABIT_TITLES.get(code, titles[code]),
               "count": len(hits), "share": len(hits) / n, "battles": hits,
               "drill": coachrules.DRILLS[code]}
              for code, hits in fired.items()
              if len(hits) >= HABIT_MIN_BATTLES and len(hits) / n >= HABIT_MIN_SHARE]
    habits.sort(key=lambda h: (-h["count"], -coachrules.WEIGHT[h["code"]]))
    strengths = [{"key": key, "title": _GOOD_TITLES.get(key, key), "count": count,
                  "share": count / n}
                 for key, count in good.items() if count >= HABIT_MIN_BATTLES]
    strengths.sort(key=lambda s: -s["count"])
    return {"overall": _totals(lines), "tanks": tanks, "habits": habits, "good": strengths,
            "recent": lines[:RECENT]}


# A habit is named for the pattern, not for one battle ("you died first").
_HABIT_TITLES = {
    "early_death": "Гибель в первые минуты боя",
    "cheap_death": "Прочность отдаётся почти даром",
    "isolated_death": "Гибель в отрыве от команды",
    "standing": "Стоите на месте под огнём",
    "alone_start": "В начале боя уезжаете в одиночку",
    "focus": "Попадаете под огонь нескольких противников",
    "misses": "Много промахов",
    "no_pen": "Попадания без пробития",
    "passive": "Выживаете, почти не влияя на бой",
}
_GOOD_TITLES = {
    "damage": "Урон в двойке лучших боя",
    "kills": "Фраги в двойке лучших боя",
    "damage_net": "Размен прочности в двойке лучших",
    "accuracy": "Точность в двойке лучших",
    "pen_rate": "Пробиваемость в двойке лучших",
    "assist": "Помощь команде в двойке лучших",
    "damage_blocked": "Блок бронёй в двойке лучших",
}
