"""Layer 7 -- tactical facts extracted from the 3D replay artifacts.

The Layer 4/5 analysis (analysis.py) works off the kill sequence: it knows WHAT
happened but not WHERE or WITH WHAT. That ceiling is why the coaching report
could only ever talk in aggregates ("your eco win rate is low"). This module
reads the per-round artifacts extract3d already wrote -- positions at 16 Hz, the
grenade timeline with flash victims, the shot stream -- and turns them into
objective, round-addressable facts a coach can point at:

  * trading      -- untraded deaths, trade chances you were in range for
  * positioning  -- how far the nearest teammate was when you died
  * utility      -- who your flashes actually blinded (enemies, mates, yourself)
  * engagement   -- shots per kill, deaths to blinded/smoked/wallbanged enemies

Every metric is computed for ALL TEN players and the reference player is ranked
inside their own lobby. That matters more than it sounds: an absolute threshold
("dying within 1000 units of a teammate is bad") is wrong at every rank, while
"the worst in this server" is true at every rank and needs no tuning.

Nothing here calls an LLM -- it produces the ground truth that coach.py then has
to phrase. Reading artifacts costs ~2 s for a full match versus ~20 s to
re-parse the .dem, so a report can be generated on demand.
"""

import json
import math
from collections import defaultdict
from pathlib import Path

from . import c2r3, zones

SIDE_OF = {2: "T", 3: "CT"}

# Sample rate is 16 Hz; these windows are expressed in seconds and converted
# per round so a future rate change doesn't silently rescale the analysis.
TRADE_WINDOW_S = 4.0      # a kill this soon after a death counts as the trade
TRADE_RANGE_U = 800.0     # ...and you had to be this close to have had a chance
FLASH_CONVERT_S = 3.0     # someone dies this soon after being blinded -> the flash did it
HOTSPOT_RADIUS_U = 450.0  # deaths inside this radius are "the same spot"

# A CS2 `blind_duration` counts the whole effect including the fade-out tail, so
# short values are a flicker nobody notices. This filter exists only to stop
# those being called "blinded" -- it is NOT the metric. Measured over 932 team
# blinds in this project's own matches, how long a teammate was blinded barely
# predicts whether they then died (7.2% under 0.7 s, 7.8% at 0.7-1.5 s, 12.8% at
# 1.5-2.5 s, 5.9% above that). Counting blinds therefore measures almost
# nothing; counting what they COST measures the thing a player cares about,
# which is why flash_assists / team_flash_cost below are the ranked numbers and
# the blind counts are left as unranked context.
REAL_BLIND_S = 1.5

# Which way is up for each metric, for the lobby ranking. Anything absent is a
# VOLUME stat with no inherent direction -- throwing the most flashes in the
# server is not by itself good or bad -- and is deliberately left unranked so
# the coach never reads "rank 1 of 10" as praise for it.
BETTER = {
    "kills": "high",
    "deaths": "low",
    "hs_pct": "high",
    "opening_win_pct": "high",
    "avg_death_time_s": "high",        # dying early is the failure mode
    "untraded_death_pct": "low",
    "trade_conversion_pct": "high",
    "median_isolation_u": "low",       # nearer help when you go down
    "shots_per_kill": "low",
    "deaths_to_blinded_enemy": "low",
    "deaths_through_smoke": "low",
    "deaths_to_wallbang": "low",
    # Utility is judged by what it did, not by how much of it happened: an
    # assist is an enemy who was blinded and then died, a cost is a teammate who
    # was. Raw blind counts and blind seconds are deliberately NOT ranked --
    # see REAL_BLIND_S for the measurement that ruled them out.
    "flash_assists": "high",
    "flash_assist_pct": "high",
    "team_flash_cost": "low",
    "rounds_died_with_no_util_thrown": "low",
}


# Below this many comparable opponents a "rank in the lobby" says nothing, so
# the metric is dropped rather than reported against a handful of players.
MIN_LOBBY_SAMPLE = 4


def _dist(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _pct(part, whole):
    return round(100.0 * part / whole) if whole else None


def rank_against(mine, others, direction):
    """Rank `mine` inside the pool `others + [mine]`; rank 1 is the best result.

    `direction` says which way is good ("low" or "high"), and it is applied HERE
    so nothing downstream has to remember that a low `deaths` and a high
    `avg_death_time_s` are both good news. Values that aren't comparable numbers
    are dropped; returns None when too few of the lobby had one.
    """
    others = [o for o in others
              if isinstance(o, (int, float)) and not isinstance(o, bool)]
    if len(others) < MIN_LOBBY_SAMPLE:
        return None
    pool = others + [mine]
    ahead = (sum(1 for o in pool if o < mine) if direction == "low"
             else sum(1 for o in pool if o > mine))
    return {
        "you": mine,
        "lobby_median": round(_median(others), 1),
        "rank": ahead + 1,
        "of": len(pool),
    }


class _P:
    """Accumulator for one player across the whole match."""

    def __init__(self, sid, name):
        self.sid = sid
        self.name = name
        self.rounds = 0
        self.kills = 0
        self.deaths = 0
        self.hs_kills = 0
        self.opening_taken = 0        # involved in the round's first duel
        self.opening_won = 0
        self.deaths_untraded = 0
        self.trade_chances = 0        # teammate died near you while you were alive
        self.trades_made = 0
        self.death_times = []
        self.isolation = []           # distance to nearest living mate at death
        self.nearest_enemy = []
        self.death_spots = []         # (round, sample, x, y)
        self.shots = 0
        self.deaths_to_blind = 0      # killer was flashed and still won the duel
        self.deaths_through_smoke = 0
        self.deaths_wallbang = 0
        self.nades = defaultdict(int)
        self.flashes = 0
        self.enemy_blind_s = 0.0
        self.team_blind_s = 0.0
        self.self_blind_s = 0.0
        self.enemies_blinded = 0      # blinded for REAL_BLIND_S or more
        self.mates_blinded = 0
        self.flash_assists = 0        # ...and then died: the flash did something
        self.team_flash_cost = 0      # ...and it was your own teammate
        self.rounds_no_util = 0       # died having thrown nothing that round

    def summary(self):
        return {
            "name": self.name,
            "rounds": self.rounds,
            "kills": self.kills,
            "deaths": self.deaths,
            "hs_pct": _pct(self.hs_kills, self.kills),
            "opening_duels": self.opening_taken,
            "opening_win_pct": _pct(self.opening_won, self.opening_taken),
            "avg_death_time_s": (round(sum(self.death_times) / len(self.death_times), 1)
                                 if self.death_times else None),
            "untraded_death_pct": _pct(self.deaths_untraded, self.deaths),
            "trade_chances": self.trade_chances,
            "trades_made": self.trades_made,
            "trade_conversion_pct": _pct(self.trades_made, self.trade_chances),
            "median_isolation_u": (round(_median(self.isolation)) if self.isolation else None),
            "median_enemy_dist_at_death_u": (round(_median(self.nearest_enemy))
                                             if self.nearest_enemy else None),
            "shots_per_kill": (round(self.shots / self.kills, 1)
                               if self.kills and self.shots else None),
            "deaths_to_blinded_enemy": self.deaths_to_blind,
            "deaths_through_smoke": self.deaths_through_smoke,
            "deaths_to_wallbang": self.deaths_wallbang,
            "nades_per_round": (round(sum(self.nades.values()) / self.rounds, 1)
                                if self.rounds else None),
            "nades_by_type": dict(self.nades),
            "flashes_thrown": self.flashes,
            # Context, not judgement: "blinded" here means REAL_BLIND_S or more.
            "enemies_blinded": self.enemies_blinded,
            "teammates_blinded": self.mates_blinded,
            "enemy_blind_s": round(self.enemy_blind_s, 1),
            "team_blind_s": round(self.team_blind_s + self.self_blind_s, 1),
            # What the utility actually did.
            "flash_assists": self.flash_assists,
            "flash_assist_pct": _pct(self.flash_assists, self.flashes),
            "team_flash_cost": self.team_flash_cost,
            "rounds_died_with_no_util_thrown": self.rounds_no_util,
        }


def _round_files(artifacts_dir):
    """(manifest, [(round_meta, path)]) for a match's artifact directory."""
    base = Path(artifacts_dir)
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    pairs = []
    for meta in manifest.get("rounds", []):
        path = base / meta.get("file", "")
        if path.is_file():
            pairs.append((meta, path))
    return manifest, pairs


def _hotspot(spots, map_name=None):
    """Largest cluster of death positions, as a dict, or None.

    Greedy single-pass clustering: good enough to answer "do you keep dying in
    the same place?", which is the only question being asked of it.

    `zone` is the callout the cluster sits in, by majority vote of its deaths
    rather than by the centroid's position -- a cluster straddling a boundary
    should be named after where the deaths are, not after the midpoint between
    them, which can land in a third place entirely.
    """
    best = None
    for anchor in spots:
        near = [s for s in spots
                if _dist(anchor[2], anchor[3], s[2], s[3]) <= HOTSPOT_RADIUS_U]
        if best is None or len(near) > len(best):
            best = near
    if not best or len(best) < 3:
        return None

    out = {
        "deaths": len(best),
        "x": round(sum(s[2] for s in best) / len(best)),
        "y": round(sum(s[3] for s in best) / len(best)),
        "rounds": sorted({s[0] for s in best}),
    }
    if map_name:
        named = [z for z in (zones.zone_of(map_name, s[2], s[3], s[4]) for s in best) if z]
        if named:
            winner = max(set(named), key=named.count)
            out["zone"] = zones.label(winner)
            out["zone_deaths"] = named.count(winner)
    return out


def analyse(artifacts_dir, ref_sid):
    """Build the tactical fact sheet for `ref_sid` from a match's artifacts.

    Returns None when the reference player never appears (wrong match, or a
    spectator). Raises FileNotFoundError if the artifacts are missing.
    """
    ref_sid = str(ref_sid)
    manifest, pairs = _round_files(artifacts_dir)
    if not pairs:
        return None
    map_name = manifest.get("map")

    # Persistent team identity: whichever side the reference player starts on.
    # Sides swap at half, so "teammate" has to be decided per round from that
    # round's own roster, not from a fixed side number.
    players = {}
    per_round = []

    for meta, path in pairs:
        rnd = c2r3.decode_round(path)
        roster = rnd.players
        by_sid = {p["sid"]: p for p in roster}
        if ref_sid not in by_sid:
            continue
        ref_team = by_sid[ref_sid]["team"]
        name_to_sid = {p["name"]: p["sid"] for p in roster}
        track_of = {p["sid"]: rnd.tracks[i] for i, p in enumerate(roster)}
        slot_to_sid = {p["slot"]: p["sid"] for p in roster}

        rate = rnd.sample_rate
        trade_win = TRADE_WINDOW_S * rate
        convert_win = FLASH_CONVERT_S * rate

        for p in roster:
            if p["sid"] not in players:
                players[p["sid"]] = _P(p["sid"], p["name"])
            players[p["sid"]].rounds += 1

        # ---- kill timeline ------------------------------------------------
        kills = sorted(meta.get("kills", []), key=lambda k: k["sample"])
        # Resolve names to sids once; a kill whose actor we cannot resolve
        # (disconnects, "world" deaths) still counts as a death for the victim.
        events = []
        for k in kills:
            events.append({
                "sample": k["sample"],
                "atk": name_to_sid.get(k.get("atk")),
                "vic": name_to_sid.get(k.get("vic")),
                "atk_team": k.get("atkTeam"),
                "hs": bool(k.get("hs")),
                "ab": bool(k.get("ab")),
                "ts": bool(k.get("ts")),
                "wb": bool(k.get("wb")),
            })

        # Snapshot the reference player's trade tallies so this round's own
        # missed chances can be attributed to it.
        ref_before = (players[ref_sid].trade_chances, players[ref_sid].trades_made)

        for i, e in enumerate(events):
            atk, vic = e["atk"], e["vic"]
            if atk and atk in players:
                players[atk].kills += 1
                if e["hs"]:
                    players[atk].hs_kills += 1
            if not vic or vic not in players:
                continue
            pv = players[vic]
            pv.deaths += 1
            pv.death_times.append(round(e["sample"] / rate, 1))
            if e["ab"]:
                pv.deaths_to_blind += 1
            if e["ts"]:
                pv.deaths_through_smoke += 1
            if e["wb"]:
                pv.deaths_wallbang += 1

            vic_team = by_sid[vic]["team"]
            # Untraded: nobody on the victim's side killed the attacker in time.
            if atk:
                traded = any(
                    f["vic"] == atk and f["atk"] in by_sid
                    and by_sid[f["atk"]]["team"] == vic_team
                    and 0 < f["sample"] - e["sample"] <= trade_win
                    for f in events[i + 1:]
                )
                if not traded:
                    pv.deaths_untraded += 1

            # Trade CHANCES for the victim's living teammates: they were alive,
            # close enough to intervene, and either did or didn't.
            sample = max(0, e["sample"] - 1)
            vt = track_of.get(vic)
            if vt and sample < len(vt["x"]) and atk:
                vx, vy = vt["x"][sample], vt["y"][sample]
                for mate_sid, mate in by_sid.items():
                    if mate_sid == vic or mate["team"] != vic_team:
                        continue
                    mt = track_of.get(mate_sid)
                    if not mt or sample >= len(mt["flags"]):
                        continue
                    if not mt["flags"][sample] & c2r3.F_ALIVE:
                        continue
                    if _dist(vx, vy, mt["x"][sample], mt["y"][sample]) > TRADE_RANGE_U:
                        continue
                    pm = players[mate_sid]
                    pm.trade_chances += 1
                    if any(f["atk"] == mate_sid and f["vic"] == atk
                           and 0 < f["sample"] - e["sample"] <= trade_win
                           for f in events[i + 1:]):
                        pm.trades_made += 1

            # Spacing at the moment of death: nearest living teammate / enemy.
            if vt and sample < len(vt["x"]):
                vx, vy = vt["x"][sample], vt["y"][sample]
                mates, foes = [], []
                for osid, other in by_sid.items():
                    if osid == vic:
                        continue
                    ot = track_of.get(osid)
                    if not ot or sample >= len(ot["flags"]):
                        continue
                    if not ot["flags"][sample] & c2r3.F_ALIVE:
                        continue
                    d = _dist(vx, vy, ot["x"][sample], ot["y"][sample])
                    (mates if other["team"] == vic_team else foes).append(d)
                if mates:
                    pv.isolation.append(min(mates))
                if foes:
                    pv.nearest_enemy.append(min(foes))
                # Height is carried so the callout lookup can tell a stacked
                # map's levels apart (Nuke's lower site is under the upper one).
                pv.death_spots.append((rnd.number, e["sample"], vx, vy,
                                       vt["z"][sample]))

        # Opening duel: the round's first kill, both sides of it.
        if events:
            first = events[0]
            for sid, won in ((first["atk"], True), (first["vic"], False)):
                if sid and sid in players:
                    players[sid].opening_taken += 1
                    if won:
                        players[sid].opening_won += 1

        # ---- shots --------------------------------------------------------
        for shot in rnd.header.get("shots", []):
            sid = slot_to_sid.get(shot.get("p"))
            if sid in players:
                players[sid].shots += 1

        # ---- utility ------------------------------------------------------
        threw_this_round = set()
        for item in rnd.header.get("utility", []):
            thrower = item.get("thrower")
            if thrower not in players:
                continue
            pt = players[thrower]
            gtype = item.get("type", "?")
            pt.nades[gtype] += 1
            threw_this_round.add(thrower)
            if gtype != "flash":
                continue
            pt.flashes += 1
            thrower_team = by_sid.get(thrower, {}).get("team")
            det = item.get("detSample", 0)
            for victim in item.get("affected", []):
                vsid = victim.get("sid")
                secs = float(victim.get("blindDuration") or 0.0)
                if vsid == thrower:
                    pt.self_blind_s += secs
                    continue
                # Did being blinded actually end this player's round? That is
                # the only part of a flash anyone can feel.
                died = any(f["vic"] == vsid and 0 <= f["sample"] - det <= convert_win
                           for f in events)
                if by_sid.get(vsid, {}).get("team") == thrower_team:
                    pt.team_blind_s += secs
                    if secs >= REAL_BLIND_S:
                        pt.mates_blinded += 1
                        if died:
                            pt.team_flash_cost += 1
                else:
                    pt.enemy_blind_s += secs
                    if secs >= REAL_BLIND_S:
                        pt.enemies_blinded += 1
                        if died:
                            pt.flash_assists += 1

        # Rounds a player died in without contributing utility. Not damning on
        # an eco -- the buy type lives in the 2D layer -- but the count is a
        # real signal when it is most of the match. Counted for everyone so the
        # lobby ranking compares like with like.
        died_this_round = {e["vic"] for e in events if e["vic"]}
        for sid in died_this_round & players.keys():
            if sid not in threw_this_round:
                players[sid].rounds_no_util += 1

        # ---- compact per-round row for the prompt -------------------------
        ref_kills = [e for e in events if e["atk"] == ref_sid]
        ref_death = next((e for e in events if e["vic"] == ref_sid), None)
        row = {
            "n": rnd.number,
            "side": SIDE_OF.get(ref_team),
            "k": len(ref_kills),
            "survived": ref_death is None,
        }
        # Whether the reference player was in this round's opening duel, and on
        # which end of it -- so a report criticising opening duels can cite the
        # rounds it actually happened in instead of guessing from death times.
        if events:
            if events[0]["atk"] == ref_sid:
                row["opening"] = "won"
            elif events[0]["vic"] == ref_sid:
                row["opening"] = "lost"
        missed = ((players[ref_sid].trade_chances - ref_before[0])
                  - (players[ref_sid].trades_made - ref_before[1]))
        if missed:
            row["missed_trades"] = missed
        if meta.get("winner"):
            row["won"] = meta["winner"] == SIDE_OF.get(ref_team)
        if ref_death:
            row["death_s"] = round(ref_death["sample"] / rate, 1)
            killer = by_sid.get(ref_death["atk"], {}).get("name")
            if killer:
                row["killed_by"] = killer
            if players[ref_sid].isolation:
                row["nearest_mate_u"] = round(players[ref_sid].isolation[-1])
            # Where it happened, in the words the player would use.
            spots = players[ref_sid].death_spots
            if spots and spots[-1][0] == rnd.number:
                zone = zones.zone_label(map_name, spots[-1][2], spots[-1][3],
                                        spots[-1][4])
                if zone:
                    row["zone"] = zone
        nades = [i.get("type") for i in rnd.header.get("utility", [])
                 if i.get("thrower") == ref_sid]
        if nades:
            row["util"] = nades
        bomb = meta.get("bomb")
        if bomb:
            row["plant_s"] = round(bomb.get("plant", 0) / rate, 1)
            if bomb.get("site"):
                row["site"] = bomb["site"]
        per_round.append(row)

    if ref_sid not in players:
        return None

    ref = players[ref_sid]
    ref_sum = ref.summary()
    lobby_sums = [p.summary() for p in players.values() if p.sid != ref_sid]

    comparison = {}
    for key, mine in ref_sum.items():
        if not isinstance(mine, (int, float)) or isinstance(mine, bool):
            continue
        direction = BETTER.get(key)
        if direction is None:                     # volume stat -- no good/bad
            continue
        # 1 = best in the server on this metric, 10 = worst.
        entry = rank_against(mine, [s.get(key) for s in lobby_sums], direction)
        if entry is not None:
            comparison[key] = entry

    return {
        "map": map_name,
        "rounds_analysed": len(per_round),
        "player": {"sid": ref_sid, "name": ref.name},
        "you": ref_sum,
        "vs_lobby": comparison,
        "death_hotspot": _hotspot(ref.death_spots, map_name),
        "rounds": per_round,
    }
