"""Data-collection layer: parse a CS2 demo into per-round / per-player data.

`collect()` is the single entry point; it returns (summary, rounds, meta) built
from demoparser2 events and tick snapshots.
"""

import sys
from collections import defaultdict

from . import stats
from . import replay as replay_mod

try:
    import pandas as pd
    from demoparser2 import DemoParser
except ImportError:
    sys.exit(
        "demoparser2 is not installed.\n"
        "Activate your venv and run:  pip install -r requirements.txt"
    )

# In CS2 demos team_num 2 == Terrorists, 3 == Counter-Terrorists.
T_NUM, CT_NUM = 2, 3
SIDE_OF = {CT_NUM: "CT", T_NUM: "T"}

# Buy-type thresholds on a player's equipment value ($). Heuristic: a full rifle
# buy (rifle + armour + a little utility) lands around $3.5k+, a force/half buy
# sits in the SMG / upgraded-pistol range, and anything below is an eco/save.
ECO_MAX = 1500
FORCE_MAX = 3500

DEFAULT_HIGHLIGHT = "P1rat1C"


def value_or(cell, default):
    """Return a clean value for a DataFrame cell, or `default` if it's missing."""
    if cell is None or cell == "" or (isinstance(cell, float) and pd.isna(cell)):
        return default
    return cell


def clean_name(name) -> str:
    """Human-readable player name; some CS2 names are blank/invisible unicode."""
    text = str(value_or(name, "")).strip()
    return text if text else "(unnamed)"


def buy_type(equip_value: float) -> str:
    """Classify an equipment value into a buy type."""
    if equip_value < ECO_MAX:
        return "eco"
    if equip_value < FORCE_MAX:
        return "force"
    return "full"


def other_side(side_num):
    return T_NUM if side_num == CT_NUM else CT_NUM


def tick_to_round(tick, freeze_ticks, end_ticks):
    """1-indexed round whose (freeze_end, round_end] span holds `tick`, else None."""
    for i, (start, end) in enumerate(zip(freeze_ticks, end_ticks), start=1):
        if start <= tick <= end:
            return i
    return None


def snap_at(ticks_df, tick):
    return ticks_df[ticks_df["tick"] == tick]


def alive_counts(ticks_df, tick, side_num):
    """Alive players on `side_num` at `tick`."""
    snap = ticks_df[(ticks_df["tick"] == tick)
                    & (ticks_df["is_alive"])
                    & (ticks_df["team_num"] == side_num)]
    return int(len(snap))


def side_economy(ticks_df, buytime_tick, side_num, ref_sid):
    """Buy snapshot for one side at buytime end: dict with totals and players."""
    snap = ticks_df[(ticks_df["tick"] == buytime_tick)
                    & (ticks_df["team_num"] == side_num)]
    players = []
    for _, p in snap.sort_values("current_equip_value", ascending=False).iterrows():
        players.append({
            "name": clean_name(p.get("name")),
            "equip": int(value_or(p.get("current_equip_value"), 0)),
            "cash": int(value_or(p.get("balance"), 0)),
            "is_me": str(value_or(p.get("sid"), "")) == ref_sid,
        })
    total = sum(pl["equip"] for pl in players)
    avg = total / len(players) if players else 0
    return {"buytype": buy_type(avg), "total": total, "players": players}


def team_scores_at(ticks_df, tick):
    """Native per-side round-win totals (team_rounds_total) at `tick`."""
    snap = snap_at(ticks_df, tick)
    by_side = snap.groupby("team_num")["team_rounds_total"].first()
    return {int(k): int(v) for k, v in by_side.items()}


def find_sid(ticks_df, name):
    """String steamid for the first player matching `name`, else None."""
    if not name:
        return None
    match = ticks_df[ticks_df["name"] == name]
    return match["sid"].iloc[0] if not match.empty else None


def rel(side_num, my_side_num):
    """Relationship of `side_num` to the reference team: 'ally' / 'enemy' / ''."""
    if side_num is None or my_side_num is None:
        return ""
    return "ally" if int(side_num) == int(my_side_num) else "enemy"


def resolve_reference(ticks_df, freeze_ticks, highlight):
    """Find the highlighted player's steamid, or fall back to a CT-start player.

    Returns (steamid, display_name, found). Tracking by steamid keeps identity
    stable across the halftime swap even if a name changes.
    """
    for ft in freeze_ticks:
        snap = snap_at(ticks_df, ft)
        row = snap[snap["name"] == highlight]
        if not row.empty:
            return row["sid"].iloc[0], highlight, True
    # Fallback: any player on CT in the first round with a roster.
    for ft in freeze_ticks:
        snap = snap_at(ticks_df, ft)
        ct = snap[snap["team_num"] == CT_NUM]
        if not ct.empty:
            return ct["sid"].iloc[0], clean_name(ct["name"].iloc[0]), False
    return None, highlight, False


def collect(parser, highlight, rival):
    """Parse the demo and return (summary, rounds, meta)."""
    round_ends = parser.parse_event("round_end").sort_values("tick")
    freeze_ends = parser.parse_event("round_freeze_end").sort_values("tick")
    buytime_ends = parser.parse_event("buytime_ended").sort_values("tick")
    bomb_plants = parser.parse_event("bomb_planted").sort_values("tick")
    kills = parser.parse_event(
        "player_death",
        player=["team_num", "X", "Y"],
        other=["total_rounds_played", "is_warmup_period"],
    )
    hurts = parser.parse_event(
        "player_hurt",
        player=["team_num"],
        other=["total_rounds_played", "is_warmup_period"],
    )
    blinds = parser.parse_event(
        "player_blind",
        player=["team_num"],
        other=["total_rounds_played", "is_warmup_period"],
    )

    if round_ends is None or round_ends.empty:
        return None, [], {}

    try:
        map_name = parser.parse_header().get("map_name")
    except Exception:
        map_name = None

    freeze_ticks = freeze_ends["tick"].tolist()
    end_ticks = round_ends["tick"].tolist()
    n_rounds = len(end_ticks)

    def first_per_round(df):
        mapping = {}
        if df is not None and not df.empty:
            for tk in df["tick"]:
                r = tick_to_round(tk, freeze_ticks, end_ticks)
                if r and r not in mapping:
                    mapping[r] = tk
        return mapping

    buytime_of = first_per_round(buytime_ends)
    plant_of = first_per_round(bomb_plants)

    # One parse_ticks pass across every tick we need. team_rounds_total is the
    # engine's native per-team score (correct across the halftime swap).
    wanted_ticks = sorted(set(freeze_ticks) | set(end_ticks)
                          | set(buytime_of.values()) | set(plant_of.values()))
    ticks_df = parser.parse_ticks(
        ["current_equip_value", "balance", "is_alive",
         "team_num", "team_rounds_total"],
        ticks=wanted_ticks,
    )
    # parse_ticks returns steamid as uint64 but parse_event returns it as a
    # string; normalise to a string `sid` so identities compare across both.
    ticks_df["sid"] = ticks_df["steamid"].astype(str)

    ref_steamid, ref_name, found = resolve_reference(ticks_df, freeze_ticks, highlight)

    # Which side is the reference player on for round i (fallback: end tick,
    # then carry the previous round's side if they were dead/absent at freeze).
    def side_for_round(i, prev):
        for tk in (freeze_ticks[i], end_ticks[i]):
            snap = ticks_df[(ticks_df["tick"] == tk)
                            & (ticks_df["sid"] == ref_steamid)]
            if not snap.empty:
                return int(snap["team_num"].iloc[0])
        return prev

    # Reference player's teammates (share their side; invariant across the swap).
    your_team_ids = set()
    for i, ft in enumerate(freeze_ticks):
        snap = snap_at(ticks_df, ft)
        if ref_steamid in snap["sid"].values:
            pside = int(snap.loc[snap["sid"] == ref_steamid, "team_num"].iloc[0])
            your_team_ids = set(snap.loc[snap["team_num"] == pside, "sid"])
            break

    # Warmup kills don't belong to a scored round.
    if kills is not None and not kills.empty:
        if "is_warmup_period" in kills.columns:
            kills = kills[kills["is_warmup_period"] != True]  # noqa: E712
        kills = kills.dropna(subset=["total_rounds_played"]).sort_values("tick")

    # Rival for the "vs" filter. Explicit CLI name wins; otherwise auto-pick the
    # reference player's nemesis: the opponent who killed them the most times.
    rival_steamid, rival_name, rival_auto, rival_kills_on_me = None, rival, False, 0
    if rival:
        rival_steamid = find_sid(ticks_df, rival)
    elif kills is not None and not kills.empty:
        my_deaths = kills[kills["user_steamid"].astype(str) == ref_steamid]
        counts = my_deaths["attacker_steamid"].dropna().astype(str).value_counts()
        for sid, cnt in counts.items():
            if sid not in ("None", ref_steamid) and sid not in your_team_ids:
                names = my_deaths.loc[
                    my_deaths["attacker_steamid"].astype(str) == sid, "attacker_name"]
                rival_steamid = sid
                rival_name = clean_name(names.iloc[0]) if not names.empty else sid
                rival_auto, rival_kills_on_me = True, int(cnt)
                break

    # Damage (ADR) and flashes — enemy-only, non-warmup.
    def _enemy_rows(df):
        if df is None or df.empty:
            return None
        d = df
        if "is_warmup_period" in d.columns:
            d = d[d["is_warmup_period"] != True]  # noqa: E712
        d = d[d["attacker_steamid"].notna()
              & d["attacker_team_num"].notna() & d["user_team_num"].notna()]
        d = d[d["attacker_team_num"] != d["user_team_num"]].copy()
        d["a"] = d["attacker_steamid"].astype(str)
        return d

    damage_by, util_damage_by, blinds_by = {}, {}, {}
    hurt_rows = _enemy_rows(hurts)
    if hurt_rows is not None and not hurt_rows.empty:
        damage_by = hurt_rows.groupby("a")["dmg_health"].sum().astype(int).to_dict()
        util = hurt_rows[hurt_rows["weapon"].isin(stats.HE_WEAPONS | stats.FIRE_WEAPONS)]
        util_damage_by = util.groupby("a")["dmg_health"].sum().astype(int).to_dict()
    blind_rows = _enemy_rows(blinds)
    if blind_rows is not None and not blind_rows.empty:
        agg = blind_rows.groupby("a")["blind_duration"].agg(["count", "sum"])
        blinds_by = {sid: {"count": int(row["count"]), "duration": float(row["sum"])}
                     for sid, row in agg.iterrows()}

    # Per-round rosters (sid -> side) and display names from the freeze snapshot.
    roster_by_round = {}
    for rr in range(1, n_rounds + 1):
        snap = snap_at(ticks_df, freeze_ticks[rr - 1])
        roster_by_round[rr] = {row["sid"]: int(row["team_num"])
                               for _, row in snap.iterrows()}
    names = {}
    for _, row in ticks_df.drop_duplicates("sid").iterrows():
        names[row["sid"]] = clean_name(row["name"])

    player_stats = defaultdict(lambda: {"name": "", "kills": 0, "deaths": 0})
    rounds = []
    kills_seq = []          # flat, ordered kills for the stats layer
    map_kills = []          # kill/death world positions for the map layer
    winners = {}            # round -> winning team_num
    ref_side_by_round = {}  # round -> reference player's side (for replay roles)
    prev_side = CT_NUM
    my_final = opp_final = 0
    total_kills = 0

    for r in range(1, n_rounds + 1):
        row = round_ends.iloc[r - 1]
        winner_side = value_or(row.get("winner"), None)   # "CT" / "T"
        freeze_tick = freeze_ticks[r - 1]
        end_tick = end_ticks[r - 1]

        my_side = side_for_round(r - 1, prev_side)
        prev_side = my_side
        opp = other_side(my_side)
        my_side_label = SIDE_OF.get(my_side, "?")
        winners[r] = CT_NUM if winner_side == "CT" else T_NUM if winner_side == "T" else None
        ref_side_by_round[r] = my_side

        # Native running score, framed as your team vs opponent.
        scores = team_scores_at(ticks_df, end_tick)
        my_score = scores.get(my_side, my_final)
        opp_score = scores.get(opp, opp_final)
        my_final, opp_final = my_score, opp_score
        won = (winner_side == my_side_label)

        # Alive counts framed as (moment, your_alive, opp_alive).
        alive = [("start", alive_counts(ticks_df, freeze_tick, my_side),
                  alive_counts(ticks_df, freeze_tick, opp))]
        if r in plant_of:
            alive.append(("plant", alive_counts(ticks_df, plant_of[r], my_side),
                          alive_counts(ticks_df, plant_of[r], opp)))
        alive.append(("end", alive_counts(ticks_df, end_tick, my_side),
                      alive_counts(ticks_df, end_tick, opp)))

        economy = None
        if r in buytime_of:
            economy = {
                "you": side_economy(ticks_df, buytime_of[r], my_side, ref_steamid),
                "opp": side_economy(ticks_df, buytime_of[r], opp, ref_steamid),
            }

        round_kills = []
        my_kills = my_deaths = 0
        vs_rival = False
        if kills is not None and not kills.empty:
            for _, k in kills[kills["total_rounds_played"] == (r - 1)].iterrows():
                a_raw = value_or(k.get("attacker_steamid"), None)
                v_raw = value_or(k.get("user_steamid"), None)
                a_steam = str(a_raw) if a_raw is not None else None
                v_steam = str(v_raw) if v_raw is not None else None
                a_side = value_or(k.get("attacker_team_num"), None)
                v_side = value_or(k.get("user_team_num"), None)
                attacker_raw = value_or(k.get("attacker_name"), None)

                asst_raw = value_or(k.get("assister_steamid"), None)
                assister_sid = str(asst_raw) if asst_raw is not None else None
                assistedflash = bool(value_or(k.get("assistedflash"), False))
                headshot = bool(value_or(k.get("headshot"), False))

                attacker_is_me = a_steam == ref_steamid
                victim_is_me = v_steam == ref_steamid
                round_kills.append({
                    "attacker": clean_name(attacker_raw) if attacker_raw else "<world>",
                    "attacker_rel": rel(a_side, my_side),
                    "attacker_is_me": attacker_is_me,
                    "victim": clean_name(k.get("user_name")),
                    "victim_rel": rel(v_side, my_side),
                    "victim_is_me": victim_is_me,
                    "weapon": clean_name(k.get("weapon")),
                    "headshot": headshot,
                })
                kills_seq.append({
                    "round": r, "tick": int(k.get("tick")),
                    "a_sid": a_steam, "v_sid": v_steam,
                    "assister_sid": assister_sid, "assistedflash": assistedflash,
                    "headshot": headshot,
                })
                kx, ky = value_or(k.get("attacker_X"), None), value_or(k.get("attacker_Y"), None)
                vx, vy = value_or(k.get("user_X"), None), value_or(k.get("user_Y"), None)
                map_kills.append({
                    "round": r,
                    "kx": float(kx) if kx is not None else None,
                    "ky": float(ky) if ky is not None else None,
                    "vx": float(vx) if vx is not None else None,
                    "vy": float(vy) if vy is not None else None,
                    "killer": clean_name(attacker_raw) if attacker_raw else "<world>",
                    "victim": clean_name(k.get("user_name")),
                    "weapon": clean_name(k.get("weapon")),
                    "killer_is_me": attacker_is_me,
                    "victim_is_me": victim_is_me,
                })
                if attacker_is_me:
                    my_kills += 1
                if victim_is_me:
                    my_deaths += 1
                if rival_steamid is not None and (
                    (attacker_is_me and v_steam == rival_steamid)
                    or (a_steam == rival_steamid and victim_is_me)
                ):
                    vs_rival = True

                if v_steam is not None:
                    player_stats[v_steam]["deaths"] += 1
                    player_stats[v_steam]["name"] = clean_name(k.get("user_name"))
                if attacker_raw and a_steam is not None:
                    player_stats[a_steam]["kills"] += 1
                    player_stats[a_steam]["name"] = clean_name(attacker_raw)
                total_kills += 1

        rounds.append({
            "number": r,
            "my_side_label": my_side_label,
            "won": won,
            "reason": value_or(row.get("reason"), "unknown"),
            "my_score": my_score,
            "opp_score": opp_score,
            "economy": economy,
            "alive": alive,
            "kills": round_kills,
            "my_kills": my_kills,
            "my_deaths": my_deaths,
            "vs_rival": vs_rival,
        })

    scoreboard = []
    for steamid, s in player_stats.items():
        scoreboard.append({
            "name": s["name"] or "(unnamed)",
            "kills": s["kills"],
            "deaths": s["deaths"],
            "on_your_team": steamid in your_team_ids,
        })
    scoreboard.sort(key=lambda s: (-s["kills"], s["deaths"], s["name"].lower()))

    ref_kills = player_stats[ref_steamid]["kills"] if ref_steamid in player_stats else 0
    ref_deaths = player_stats[ref_steamid]["deaths"] if ref_steamid in player_stats else 0

    # Pro stats: per-player metrics + per-round insights (clutches, trades, ...).
    players, ref_stats, round_insights = stats.build(
        kills_seq, damage_by, util_damage_by, blinds_by, roster_by_round,
        names, your_team_ids, ref_steamid, winners, n_rounds)
    for rd, ins in zip(rounds, round_insights):
        rd["insights"] = ins

    # Replay: sampled positions per round (None if the map is unsupported).
    replay = replay_mod.build_replay(
        parser, map_name, freeze_ticks, end_ticks, roster_by_round,
        ref_side_by_round, ref_steamid, names, kills_seq)

    summary = {
        "n_rounds": n_rounds,
        "my_final": my_final,
        "opp_final": opp_final,
        "won_match": my_final > opp_final,
        "total_kills": total_kills,
        "ref_kills": ref_kills,
        "ref_deaths": ref_deaths,
        "scoreboard": scoreboard,
        "players": players,
        "ref_stats": ref_stats,
        "map_kills": map_kills,
        "replay": replay,
    }
    meta = {
        "ref_name": ref_name,
        "ref_sid": ref_steamid,
        "map_name": map_name,
        "found": found,
        "rival_name": rival_name,
        "rival_found": rival_steamid is not None,
        "rival_auto": rival_auto,
        "rival_kills_on_me": rival_kills_on_me,
    }
    return summary, rounds, meta
