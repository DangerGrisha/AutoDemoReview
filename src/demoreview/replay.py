"""Position sampling for the 2D round replay.

Samples every player's position (and facing) a few times a second across each
round, projects to radar pixels, and packs it into a compact structure the
front-end canvas can animate. Returns None if the map is unsupported.
"""

from . import maps

STEP = 16          # sample every 16 ticks (~4 Hz at 64-tick) — smooth enough
DEAD = -1          # sentinel for a dead/absent player in a frame


def build_replay(parser, map_name, freeze_ticks, end_ticks, roster_by_round,
                 ref_side_by_round, ref_sid, names, kills_seq, step=STEP):
    info = maps.map_info(map_name)
    if not info:
        return None

    n_rounds = len(end_ticks)
    round_sample_ticks = []
    all_ticks = set()
    for i in range(n_rounds):
        ts = list(range(int(freeze_ticks[i]), int(end_ticks[i]) + 1, step))
        round_sample_ticks.append(ts)
        all_ticks.update(ts)

    df = parser.parse_ticks(["X", "Y", "yaw", "is_alive"], ticks=sorted(all_ticks))
    df["sid"] = df["steamid"].astype(str)

    # (tick, sid) -> (x, y, yaw, alive), built once for fast per-frame lookup.
    cell = {}
    for row in df.itertuples(index=False):
        cell[(row.tick, row.sid)] = (row.X, row.Y, row.yaw, row.is_alive)

    # Kills per round -> frame index, for on-timeline markers.
    kills_by_round = {}
    for k in kills_seq:
        kills_by_round.setdefault(k["round"], []).append(k)

    rounds_out = []
    for i in range(n_rounds):
        r = i + 1
        ts = round_sample_ticks[i]
        start = int(freeze_ticks[i])
        roster = roster_by_round.get(r, {})
        ref_side = ref_side_by_round.get(r)

        players_out = []
        for sid, side in roster.items():
            role = "you" if sid == ref_sid else ("ally" if side == ref_side else "enemy")
            frames = []
            for tk in ts:
                c = cell.get((tk, sid))
                if c is not None and c[3] and c[0] is not None and c[1] is not None:
                    px, py = maps.world_to_radar(c[0], c[1], info)
                    frames.extend([int(round(px)), int(round(py)), int(c[2]) % 360])
                else:
                    frames.extend([DEAD, DEAD, 0])
            players_out.append({"n": names.get(sid, "?"), "r": role, "f": frames})

        events = []
        for k in kills_by_round.get(r, []):
            frame = max(0, min(len(ts) - 1, (k["tick"] - start) // step))
            killer = names.get(k["a_sid"], "world")
            victim = names.get(k["v_sid"], "?")
            events.append([frame, "K", f"{killer} ▸ {victim}"])

        rounds_out.append({
            "n": r, "step": step, "nf": len(ts),
            "players": players_out, "events": events,
        })

    return {"size": info["size"], "rounds": rounds_out}
