"""Pro-level stat computations from raw kill / damage / utility data.

`build()` turns the raw per-kill sequence and per-player damage/flash tallies
(gathered in parsing.collect) into:
  - a per-player stat block (K/D/A, ADR, KAST%, HLTV 2.0 approx rating, HS%,
    multi-kills, opening duels, trades, clutches, utility)
  - per-round insights (clutches, trade kills, entry, best multi-kill)
"""

from collections import defaultdict

# CS2 GOTV demos record at 64 tick; the trade window is ~5 seconds.
TICK_RATE = 64
TRADE_WINDOW = 5 * TICK_RATE

HE_WEAPONS = {"hegrenade"}
FIRE_WEAPONS = {"inferno", "molotov", "incgrenade"}


def _new_player():
    return {
        "kills": 0, "deaths": 0, "assists": 0, "flash_assists": 0, "hs": 0,
        "rounds_in": 0, "kast_rounds": 0, "trade_kills": 0, "traded_deaths": 0,
        "multi": {2: 0, 3: 0, 4: 0, 5: 0}, "opening_k": 0, "opening_d": 0,
        "clutch": 0, "clutch_won": 0,
    }


def _kills_by_round(kills_seq):
    by_round = defaultdict(list)
    for k in kills_seq:
        by_round[k["round"]].append(k)
    for r in by_round:
        by_round[r].sort(key=lambda k: k["tick"])
    return by_round


def build(kills_seq, damage_by, util_damage_by, blinds_by, roster_by_round,
          names, your_team_ids, ref_sid, winners, n_rounds):
    """Return (players_sorted, ref_stats, round_insights)."""
    by_round = _kills_by_round(kills_seq)
    P = defaultdict(_new_player)

    # Rounds each player took part in (from the freeze-time roster).
    for r in range(1, n_rounds + 1):
        for sid in roster_by_round.get(r, {}):
            P[sid]["rounds_in"] += 1

    round_insights = []

    for r in range(1, n_rounds + 1):
        rk = by_round.get(r, [])
        roster = roster_by_round.get(r, {})          # sid -> side_num
        winner_side = winners.get(r)                  # team_num or None

        kc = defaultdict(int)                         # kills per player this round
        victims = set()
        assisters = set()
        for k in rk:
            a, v, asid = k["a_sid"], k["v_sid"], k["assister_sid"]
            if a and a in roster:
                kc[a] += 1
                P[a]["kills"] += 1
                if k["headshot"]:
                    P[a]["hs"] += 1
            if v:
                P[v]["deaths"] += 1
                victims.add(v)
            if asid and asid in roster:
                P[asid]["assists"] += 1
                assisters.add(asid)
                if k["assistedflash"]:
                    P[asid]["flash_assists"] += 1

        for sid, c in kc.items():
            if c >= 2:
                P[sid]["multi"][min(c, 5)] += 1
        best_multi = max(((c, sid) for sid, c in kc.items() if c >= 2), default=None)

        # Opening duel = first kill of the round.
        entry = None
        if rk:
            first = rk[0]
            if first["a_sid"] and first["a_sid"] in roster:
                P[first["a_sid"]]["opening_k"] += 1
            if first["v_sid"]:
                P[first["v_sid"]]["opening_d"] += 1
            entry = {
                "killer": first["a_sid"], "victim": first["v_sid"],
                "killer_side": roster.get(first["a_sid"]),
            }

        # Trade kills: a death avenged by a teammate killing the killer in-window.
        traded_victims = set()
        for i, k in enumerate(rk):
            v, a, t = k["v_sid"], k["a_sid"], k["tick"]
            vside = roster.get(v)
            if not a or vside is None:
                continue
            for k2 in rk[i + 1:]:
                if k2["tick"] - t > TRADE_WINDOW:
                    break
                if k2["v_sid"] == a and roster.get(k2["a_sid"]) == vside:
                    traded_victims.add(v)
                    if k2["a_sid"]:
                        P[k2["a_sid"]]["trade_kills"] += 1
                    break
        for v in traded_victims:
            P[v]["traded_deaths"] += 1

        # Clutches: replay the death order, watch for a side dropping to 1 alive.
        alive = dict(roster)
        recorded = set()
        clutches = []
        for k in rk:
            if k["v_sid"] in alive:
                del alive[k["v_sid"]]
            counts = defaultdict(int)
            for s in alive.values():
                counts[s] += 1
            for side, cnt in counts.items():
                if cnt == 1 and side not in recorded:
                    lone = next((sid for sid, s in alive.items() if s == side), None)
                    enemies = sum(c for s, c in counts.items() if s != side)
                    if lone and enemies >= 1:
                        recorded.add(side)
                        won = winner_side == side
                        clutches.append({"sid": lone, "name": names.get(lone, "?"),
                                         "x": enemies, "won": won})
                        P[lone]["clutch"] += 1
                        if won:
                            P[lone]["clutch_won"] += 1

        # KAST: rounds with a Kill, Assist, Survived, or Traded death.
        for sid in roster:
            if (kc.get(sid, 0) > 0 or sid in assisters
                    or sid not in victims or sid in traded_victims):
                P[sid]["kast_rounds"] += 1

        round_insights.append({
            "clutches": clutches,
            "trade_kills": sum(1 for v in traded_victims),
            "best_multi": ({"sid": best_multi[1], "name": names.get(best_multi[1], "?"),
                            "count": best_multi[0]} if best_multi else None),
            "entry": entry,
        })

    players = [_finalize(sid, s, damage_by, util_damage_by, blinds_by,
                         names, your_team_ids, n_rounds)
               for sid, s in P.items()]
    players.sort(key=lambda p: (-p["rating"], -p["kills"], p["deaths"]))
    ref_stats = next((p for p in players if p["sid"] == ref_sid), None)
    return players, ref_stats, round_insights


def _finalize(sid, s, damage_by, util_damage_by, blinds_by, names, your_team_ids, n_rounds):
    ri = s["rounds_in"] or n_rounds
    kpr, dpr, apr = s["kills"] / ri, s["deaths"] / ri, s["assists"] / ri
    adr = damage_by.get(sid, 0) / ri
    kast = 100.0 * s["kast_rounds"] / ri
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    rating = (0.0073 * kast + 0.3591 * kpr - 0.5329 * dpr
              + 0.2372 * impact + 0.0032 * adr + 0.1587)
    hs_pct = 100.0 * s["hs"] / s["kills"] if s["kills"] else 0.0
    open_total = s["opening_k"] + s["opening_d"]
    open_pct = 100.0 * s["opening_k"] / open_total if open_total else 0.0
    blind = blinds_by.get(sid, {"count": 0, "duration": 0.0})
    return {
        "sid": sid,
        "name": names.get(sid, "(unnamed)"),
        "on_your_team": sid in your_team_ids,
        "rounds_in": s["rounds_in"],          # rounds this player was rostered for
        "hs": s["hs"],                        # raw headshot count (for career HS% = hs/kills)
        "kills": s["kills"], "deaths": s["deaths"], "assists": s["assists"],
        "flash_assists": s["flash_assists"],
        "adr": adr, "kast": kast, "rating": rating, "hs_pct": hs_pct,
        "kpr": kpr, "dpr": dpr,
        "multi": s["multi"],
        "opening_k": s["opening_k"], "opening_d": s["opening_d"], "open_pct": open_pct,
        "trade_kills": s["trade_kills"], "traded_deaths": s["traded_deaths"],
        "clutch": s["clutch"], "clutch_won": s["clutch_won"],
        "enemy_blinds": blind["count"], "blind_time": blind["duration"],
        "util_damage": util_damage_by.get(sid, 0),
    }
