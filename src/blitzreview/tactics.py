"""Facts for the coach: one battle seen from the recorder's tank.

The coach (coachrules.py) never reads the battle dict itself, only this fact
sheet. It holds two kinds of facts:

  * ranked -- the recorder's results ranked inside their own lobby (every
    player has battle results), so "the worst in this battle" holds at every
    skill level and needs no tuning. `BETTER` says which way is good;
  * moments -- what happened on the map, from the tracks and the decoded
    events: where the recorder died and how far the nearest ally was, who
    was shooting at them, whether they stood still under fire, where they
    went at the start, which shots missed. Each comes with battle times,
    which the report turns into links into the 2D replay.

The recorder's team is in view all battle and enemies only while spotted, so
the map facts are about the recorder and their allies. A battle stored before
the events were decoded (format < 4) or without battle results simply has
fewer facts; nothing here guesses the missing ones.

The thresholds below were measured on the 13 battles we have (43 deaths of the
recorder's teams, ~90 tanks alive at 60 s); each says how rare it is.
"""

import bisect
import math

from . import analysis, replay

# Which way is good for each ranked metric (True: higher). The direction test
# in tests/test_wotb_tactics.py restates every entry in plain words.
BETTER = dict(analysis.BETTER)
BETTER.update({
    "damage_net": True,   # damage dealt minus damage received: the HP trade
    "survival_s": True,   # dying early is the failure mode
})

# The nearest living ally farther than this when the recorder died: 7 of 43
# deaths (median 66 m).
ISOLATED_M = 150.0
# At ALONE_AT seconds, no ally within ALONE_M: 5 of 89 tanks (median 43 m).
ALONE_AT = 60.0
ALONE_M = 120.0
# Enemies that damaged the recorder this long before the death count as the
# ones focusing them (1-2 is typical, 3 happened in about a quarter of deaths).
FOCUS_WINDOW = 20.0
# Damaging hits this close together are one exchange; standing means moving
# slower than STAND_SPEED over it, with no shot of one's own in it.
STAND_GAP = 12.0
STAND_SPEED = 1.0  # m/s
STAND_MIN_HITS = 2
# Shots and hits below these say nothing about aim or penetration.
MIN_SHOTS = 5
MIN_HITS = 4


def _rank(mine, others, key):
    return analysis.rank_against(mine, others, higher_better=BETTER[key])


def _value(battle, vehicle, key):
    """One ranked metric of one vehicle, None where it is not defined."""
    stats = vehicle.get("stats")
    if key == "survival_s":
        if vehicle.get("death_time") is not None:
            return vehicle["death_time"]
        if vehicle.get("alive") and stats is not None:  # survived the whole battle
            return battle.get("duration")
        return None
    if stats is None:
        return None
    if key == "damage_net":
        return stats["damage"] - stats["damage_received"]
    return analysis.metric(vehicle, key)


def lobby(battle, me):
    """{metric: rank dict} for every metric of BETTER the lobby can rank."""
    out = {}
    others = [v for v in battle["vehicles"] if v is not me]
    for key in BETTER:
        mine = _value(battle, me, key)
        if mine is None:
            continue
        ranked = _rank(mine, [_value(battle, v, key) for v in others], key)
        if ranked:
            out[key] = ranked
    return out


def _tracks(battle):
    rp = battle.get("replay") or {}
    return {t["id"]: replay.decode(t["p"], rp.get("format", 1))
            for t in rp.get("tracks") or ()}


def _at(track, t):
    """The last sample at or before battle time t, or None."""
    i = bisect.bisect_right([s[0] for s in track], t) - 1
    return track[i] if i >= 0 else None


def _alive_at(vehicle, t):
    return vehicle.get("death_time") is None or vehicle["death_time"] > t


def _nearest_ally(battle, tracks, me, t):
    """(distance in metres, allies within 100 m) from `me` to its living allies
    at time t; distance None when nobody else of the team was alive."""
    here = _at(tracks.get(me["entity_id"], []), t)
    if here is None:
        return None, 0
    gaps = []
    for v in battle["vehicles"]:
        if v is me or not v["ally"] or not _alive_at(v, t):
            continue
        there = _at(tracks.get(v["entity_id"], []), t)
        if there is not None and t - there[0] <= 2:
            gaps.append(math.hypot(there[1] - here[1], there[2] - here[2]))
    return (round(min(gaps)) if gaps else None), sum(1 for g in gaps if g <= 100)


def _moved(track, t0, t1):
    """Metres per second between the samples at t0 and t1, None without both."""
    a, b = _at(track, t0), _at(track, t1)
    if a is None or b is None or b[0] - a[0] < 1:
        return None
    return math.hypot(b[1] - a[1], b[2] - a[2]) / (b[0] - a[0])


def _death(battle, tracks, me, events):
    t = me["death_time"]
    deaths = sorted(v["death_time"] for v in battle["vehicles"] if v["death_time"] is not None)
    nearest, near = _nearest_ally(battle, tracks, me, t)
    attackers, lost = [], 0
    if events:
        for when, attacker, victim, amount, _reason in events["damage"]:
            if victim == me["entity_id"] and t - FOCUS_WINDOW <= when <= t + 0.1:
                lost += amount
                if attacker != victim and attacker not in attackers:
                    attackers.append(attacker)
    dealt = (me["stats"] or {}).get("damage")
    if dealt is None and events:
        dealt = sum(d[3] for d in events["damage"] if d[1] == me["entity_id"] != d[2] and d[0] <= t)
    return {"t": t, "order": deaths.index(t) + 1, "of": len(battle["vehicles"]),
            "killer": me.get("killer_id"), "nearest_ally_m": nearest, "allies_near": near,
            "attackers": attackers if events else None, "lost_in_window": lost if events else None,
            "dealt": dealt}


def _standing(track, me_id, events):
    """Exchanges where the recorder took STAND_MIN_HITS or more damaging hits
    while standing and not shooting: [{t0, t1, hits, damage}]."""
    hits = [(t, damage) for t, _a, victim, _kind, damage in events["hits"]
            if victim == me_id and damage > 0]
    shots = [s[0] for s in events["shots"] if s[1] == me_id and not s[9]]
    groups, current = [], []
    for hit in hits:
        if current and hit[0] - current[-1][0] > STAND_GAP:
            groups.append(current)
            current = []
        current.append(hit)
    if current:
        groups.append(current)
    out = []
    for group in groups:
        if len(group) < STAND_MIN_HITS:
            continue
        t0, t1 = group[0][0], group[-1][0]
        speed = _moved(track, t0 - 2, t1)
        if speed is None or speed >= STAND_SPEED:
            continue
        if any(t0 - 4 <= s <= t1 + 1 for s in shots):
            continue
        out.append({"t0": t0, "t1": t1, "hits": len(group), "damage": sum(d for _, d in group)})
    return out


def _shots(me_id, events):
    mine = [s for s in events["shots"] if s[1] == me_id]
    fired = [s for s in mine if not s[9]]
    return {
        "fired": len(fired),
        "missed": [s[0] for s in fired if not s[6]],
        # hits that did nothing: ricochets and armour not pierced
        "no_damage": [(h[0], h[3]) for h in events["hits"]
                      if h[1] == me_id and h[4] == 0 and h[3] in (0, 1, 2)],
    }


def analyse(battle):
    """The fact sheet of the battle's recorder, or None when the replay does
    not say who recorded it."""
    me = analysis.author(battle)
    if me is None:
        return None
    stats = me.get("stats") or {}
    events = battle.get("events")
    tracks = _tracks(battle)
    hp = next((h for h in (events or {}).get("hp", ()) if h["id"] == me["entity_id"]), None)
    max_hp = stats.get("max_hp") or (hp or {}).get("max")
    if not max_hp and stats and me["alive"] is False:
        max_hp = stats["damage_received"]  # a destroyed tank received exactly all of it
    recording_end = (battle.get("replay") or {}).get("end")
    facts = {
        "me": {"id": me["entity_id"], "name": me["nickname"], "tank": me["tank"]["name"],
               "class": me["tank"].get("type"), "tier": me["tank"].get("tier"),
               "alive": me["alive"], "death_t": me["death_time"], "max_hp": max_hp,
               "damage": stats.get("damage"), "kills": stats.get("kills"),
               "shots": stats.get("shots"), "hits": stats.get("hits"),
               "pens": stats.get("penetrations"), "assist": stats.get("assist"),
               "damage_received": stats.get("damage_received")},
        "outcome": battle.get("outcome"),
        "duration": battle.get("duration") or recording_end,
        "recording_end": recording_end,
        "has_results": bool(battle.get("has_results")),
        "has_events": bool(events),
        "lobby": lobby(battle, me) if stats else {},
        "death": None,
        "standing": [],
        "alone_at_start": None,
        "shots": None,
        "names": {v["entity_id"]: {"name": v["nickname"], "tank": v["tank"]["short"],
                                   "ally": v["ally"]} for v in battle["vehicles"]},
    }
    track = tracks.get(me["entity_id"], [])
    if me["death_time"] is not None and track:
        facts["death"] = _death(battle, tracks, me, events)
    if events and track:
        facts["standing"] = _standing(track, me["entity_id"], events)
    if events:
        facts["shots"] = _shots(me["entity_id"], events)
    if (track and recording_end and recording_end > ALONE_AT and _alive_at(me, ALONE_AT)):
        nearest, near = _nearest_ally(battle, tracks, me, ALONE_AT)
        if nearest is not None:
            facts["alone_at_start"] = {"t": ALONE_AT, "nearest_ally_m": nearest,
                                       "allies_near": near}
    return facts
