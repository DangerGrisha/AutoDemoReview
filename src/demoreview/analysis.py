"""Per-player analysis for the client-side 'focus player' switch (Layers 4 & 5).

Everything here is computed for EVERY player and embedded in the page so the
front-end can re-spotlight any player with no reload:
  - a headline stat block (mirrors the "your match" strip),
  - Layer 4 breakdowns: win rate by buy type (per side), where the player dies
    (by round buy type, by opponent weapon), and a head-to-head duel record,
  - Layer 5 rule flags per round (risky opening death / over-extend / clutch win).

The heavy lifting (rating, KAST, clutch detection) already lives in stats.py;
this module only re-slices the raw kill sequence per player.
"""

from collections import defaultdict

SIDE_OF = {2: "T", 3: "CT"}
BUY_TYPES = ("eco", "force", "full")


def _headline(p):
    """The stat subset the 'your match' strip shows, for one finalized player."""
    multi = sum(p["multi"].values())
    kd = p["kills"] / p["deaths"] if p["deaths"] else float(p["kills"])
    return {
        "rating": round(p["rating"], 2), "adr": round(p["adr"], 0),
        "kast": round(p["kast"], 0), "kills": p["kills"], "deaths": p["deaths"],
        "assists": p["assists"], "kd": round(kd, 2), "hs_pct": round(p["hs_pct"], 0),
        "opening_k": p["opening_k"], "opening_d": p["opening_d"],
        "open_pct": round(p["open_pct"], 0), "multi": multi,
        "trade_kills": p["trade_kills"], "clutch": p["clutch"],
        "clutch_won": p["clutch_won"], "flash_assists": p["flash_assists"],
    }


def _focus_buytype(rounds, r, focus_side, ref_side):
    """The focus player's own team buy type for round r ('eco'/'force'/'full'/None)."""
    econ = rounds[r - 1].get("economy")
    if not econ or focus_side is None:
        return None
    side = econ["you"] if focus_side == ref_side else econ["opp"]
    return side["buytype"]


def _round_rules(rk, roster, winner_side, focus):
    """Layer-5 flags for `focus` in one round. Returns list of {kind, line}.

    `rk` is this round's kills in tick order (index-aligned with the rendered
    kill list). We replay deaths to track alive-per-side, mirroring stats.py.
    """
    flags = []
    fside = roster.get(focus)
    if fside is None:
        return flags

    # Risky opening death: focus is the very first player to die, while it drops
    # their side into a man-disadvantage with teammates still up.
    if rk and rk[0]["v_sid"] == focus:
        start_mine = sum(1 for s in roster.values() if s == fside)
        if start_mine >= 2:                       # a teammate is left behind
            flags.append({"kind": "risky-duel", "line": 0})

    # Replay the round; catch the moment focus becomes the LAST alive on their
    # side with 2+ enemies up (an over-extend / clutch attempt).
    alive = dict(roster)
    flagged_clutch = False
    for i, k in enumerate(rk):
        if k["v_sid"] in alive:
            del alive[k["v_sid"]]
        counts = defaultdict(int)
        for s in alive.values():
            counts[s] += 1
        mine = counts.get(fside, 0)
        enemies = sum(c for s, c in counts.items() if s != fside)
        if (not flagged_clutch and mine == 1 and focus in alive
                and enemies >= 2):
            flagged_clutch = True
            won = winner_side == fside
            flags.append({"kind": "clutch-won" if won else "clutch-attempt",
                          "line": i, "x": enemies})
    return flags


def build_analysis(kills_seq, rounds, players, roster_by_round,
                   ref_side_by_round, winners, names, your_team_ids, n_rounds):
    """Return the JSON-serialisable blob embedded for client-side refocus."""
    players_by_sid = {p["sid"]: p for p in players}
    team_a = set(your_team_ids)                    # the reference player's team

    # Per-round kill lists in tick order (index-aligned with the rendered cards).
    by_round = defaultdict(list)
    for k in kills_seq:
        by_round[k["round"]].append(k)
    for r in by_round:
        by_round[r].sort(key=lambda k: k["tick"])

    # Pairwise kill matrix for head-to-head (attacker -> victim -> count).
    pair = defaultdict(lambda: defaultdict(int))
    for k in kills_seq:
        a, v = k["a_sid"], k["v_sid"]
        if a and v:
            pair[a][v] += 1

    roster_meta = [{"sid": p["sid"], "name": p["name"],
                    "teamA": p["sid"] in team_a} for p in players]

    profiles = {}
    for p in players:
        f = p["sid"]
        f_in_a = f in team_a

        win_by_buy = {sl: {b: [0, 0] for b in BUY_TYPES} for sl in ("CT", "T")}
        deaths_by_type = {b: 0 for b in BUY_TYPES}
        deaths_by_weapon = defaultdict(int)

        for r in range(1, n_rounds + 1):
            fside = roster_by_round.get(r, {}).get(f)
            if fside is None:
                continue
            buytype = _focus_buytype(rounds, r, fside, ref_side_by_round.get(r))
            side_lbl = SIDE_OF.get(fside)
            won = winners.get(r) == fside
            if buytype and side_lbl:
                win_by_buy[side_lbl][buytype][0 if won else 1] += 1

        # Deaths of the focus player: by their team's buy type, and by weapon.
        for k in kills_seq:
            if k["v_sid"] != f:
                continue
            r = k["round"]
            fside = roster_by_round.get(r, {}).get(f)
            buytype = _focus_buytype(rounds, r, fside, ref_side_by_round.get(r))
            if buytype:
                deaths_by_type[buytype] += 1
            deaths_by_weapon[k.get("weapon") or "?"] += 1

        # Head-to-head vs each opponent (opposite persistent team).
        h2h = []
        for o in players:
            osid = o["sid"]
            if osid == f or (osid in team_a) == f_in_a:
                continue
            k = pair[f][osid]
            d = pair[osid][f]
            if k or d:
                h2h.append({"sid": osid, "name": o["name"],
                            "k": k, "d": d, "diff": k - d})
        h2h.sort(key=lambda x: (x["diff"], -x["d"]))   # worst matchup first

        profiles[f] = {
            "name": p["name"], "teamA": f_in_a,
            "stats": _headline(p),
            "winByBuy": win_by_buy,
            "deathsByType": deaths_by_type,
            "deathsByWeapon": sorted(deaths_by_weapon.items(),
                                     key=lambda kv: -kv[1]),
            "h2h": h2h,
        }

    # Layer-5 rule flags: round -> sid -> [{kind, line, ...}].
    rules = {}
    for r in range(1, n_rounds + 1):
        roster = roster_by_round.get(r, {})
        rk = by_round.get(r, [])
        winner_side = winners.get(r)
        per_player = {}
        for f in roster:
            fl = _round_rules(rk, roster, winner_side, f)
            if fl:
                per_player[f] = fl
        if per_player:
            rules[r] = per_player

    return {
        "roster": roster_meta,
        "profiles": profiles,
        "rules": rules,
    }
