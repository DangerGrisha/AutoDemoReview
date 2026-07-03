"""Grenade extraction for the 2D views (replay + map heatmap).

Detonations come from the detonate game events (authoritative position + tick +
thrower); the flight path / throw direction comes from the per-tick projectile
trajectories in `parse_grenades()`. Everything is projected to radar pixels and
mapped onto the same per-round frame model the replay uses, so grenades animate
in lockstep with the players. Returns None if the map is unsupported.
"""

from . import maps
from .replay import STEP, TICKRATE

# Projectile entity class -> our normalized grenade type. Incendiary and molotov
# both fly as a "molotov" projectile; the fire is keyed off inferno_startburn.
PROJ_TYPE = {
    "CFlashbangProjectile": "flash",
    "CSmokeGrenadeProjectile": "smoke",
    "CHEGrenadeProjectile": "he",
    "CMolotovProjectile": "molotov",
    "CDecoyProjectile": "decoy",
}

# Detonate event -> normalized grenade type. inferno_startburn marks the fire.
DET_EVENTS = {
    "smokegrenade_detonate": "smoke",
    "hegrenade_detonate": "he",
    "flashbang_detonate": "flash",
    "inferno_startburn": "molotov",
    "decoy_started": "decoy",
}

# How long each effect stays on screen, in seconds (real durations, roughly).
DURATION_S = {"smoke": 18.0, "molotov": 7.0, "he": 0.6, "flash": 0.5, "decoy": 2.0}

TRAIL_TICKS = 6   # sample the flight path every N ticks (keeps the JSON small)


def _norm_sid(v) -> str:
    return str(int(v)) if not isinstance(v, str) else v


def build_grenades(parser, map_name, freeze_ticks, end_ticks,
                   roster_by_round, ref_side_by_round, ref_sid, names, step=STEP):
    info = maps.map_info(map_name)
    if not info:
        return None

    # Projectile trajectories: keep in-flight rows (have a position) only.
    proj = parser.parse_grenades()
    proj = proj[proj["grenade_type"].isin(PROJ_TYPE)
                & proj["x"].notna() & proj["y"].notna()].copy()
    proj["sid"] = proj["steamid"].map(_norm_sid)
    proj["gtype"] = proj["grenade_type"].map(PROJ_TYPE)

    # Detonations: one table across all five events, tagged with our type.
    dets = []
    for ev, gtype in DET_EVENTS.items():
        try:
            df = parser.parse_event(ev)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        for row in df.itertuples(index=False):
            x, y = getattr(row, "x", None), getattr(row, "y", None)
            if x is None or y is None:
                continue
            dets.append({"type": gtype, "tick": int(row.tick),
                         "x": float(x), "y": float(y),
                         "sid": _norm_sid(getattr(row, "user_steamid", ""))})

    n_rounds = len(end_ticks)
    rounds_out, map_out = [], []

    for i in range(n_rounds):
        r = i + 1
        start, end = int(freeze_ticks[i]), int(end_ticks[i])
        nf = len(range(start, end + 1, step))
        dur_frames = {t: max(1, round(s * TICKRATE / step))
                      for t, s in DURATION_S.items()}

        def frame(tick):
            return max(0, min(nf - 1, (tick - start) // step))

        roster = roster_by_round.get(r, {})
        ref_side = ref_side_by_round.get(r)

        def role_of(sid):
            if sid == ref_sid:
                return "you"
            return "ally" if roster.get(sid) == ref_side else "enemy"

        round_proj = proj[(proj["tick"] >= start) & (proj["tick"] <= end)]
        round_dets = [d for d in dets if start <= d["tick"] <= end]

        # Pre-group this round's projectiles by (type, thrower) for flight lookup.
        groups = {}
        for eid, sub in round_proj.groupby("grenade_entity_id"):
            sub = sub.sort_values("tick")
            key = (sub["gtype"].iloc[0], sub["sid"].iloc[0])
            groups.setdefault(key, []).append(sub)

        grenades = []
        for d in round_dets:
            det_px = maps.world_to_radar(d["x"], d["y"], info)
            # Flight for this detonation: the same-type/thrower projectile whose
            # flight started most recently before it (handles >1 of a type/round).
            best, best_start = None, None
            for sub in groups.get((d["type"], d["sid"]), []):
                tmin = int(sub["tick"].iloc[0])
                if tmin <= d["tick"] and (best_start is None or tmin > best_start):
                    best, best_start = sub, tmin

            trail, origin = [], det_px
            if best is not None:
                flight = best[best["tick"] <= d["tick"]]
                pts = flight.iloc[::TRAIL_TICKS]
                for row in pts.itertuples(index=False):
                    px, py = maps.world_to_radar(row.x, row.y, info)
                    trail.append([round(px, 1), round(py, 1)])
                if trail:
                    origin = trail[0]
                    tf = frame(int(flight["tick"].iloc[0]))
                else:
                    tf = frame(d["tick"])
            else:
                tf = frame(d["tick"])

            dxp, dyp = det_px
            oxp, oyp = origin
            mag = ((dxp - oxp) ** 2 + (dyp - oyp) ** 2) ** 0.5 or 1.0
            direction = [round((dxp - oxp) / mag, 3), round((dyp - oyp) / mag, 3)]

            dfr = frame(d["tick"])
            grenades.append({
                "t": d["type"], "r": role_of(d["sid"]),
                "by": names.get(d["sid"], "?"),
                "tf": min(tf, dfr), "df": dfr, "ef": dfr + dur_frames[d["type"]],
                "det": [round(dxp, 1), round(dyp, 1)],
                "org": [round(oxp, 1), round(oyp, 1)],
                "dir": direction, "trail": trail,
            })

            map_out.append({
                "t": d["type"], "me": d["sid"] == ref_sid, "round": r,
                "ox": round(oxp, 1), "oy": round(oyp, 1),
                "dx": round(dxp, 1), "dy": round(dyp, 1),
            })

        rounds_out.append(grenades)

    return {"rounds": rounds_out, "map": map_out}
