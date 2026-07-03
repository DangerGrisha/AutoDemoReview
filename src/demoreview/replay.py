"""Position sampling for the 2D round replay.

Samples every player's position (and facing) a few times a second across each
round, projects to radar pixels, and packs it into a compact structure the
front-end canvas can animate. Returns None if the map is unsupported.
"""

from . import maps

TICKRATE = 64      # CS2 demos are 64-tick; drives the frame<->seconds math
STEP = 16          # sample every 16 ticks (~4 Hz at 64-tick) — smooth enough
DEAD = -1          # sentinel for a dead/absent player in a frame
PAD_SECONDS = 3    # keep sampling this many seconds past round-end (see the aftermath)


# active_weapon_name -> short in-game tag shown by each player on the radar.
# Covers the full CS2 arsenal (not just what a given demo happens to contain).
WEAPON_TAGS = {
    # rifles
    "AK-47": "AK", "M4A4": "M4", "M4A1-S": "M4S", "AUG": "AUG",
    "SG 553": "SG553", "FAMAS": "FAMAS", "Galil AR": "GALIL",
    # snipers
    "AWP": "AWP", "SSG 08": "SCOUT", "SCAR-20": "SCAR", "G3SG1": "G3",
    # smgs
    "MP9": "MP9", "MP7": "MP7", "MP5-SD": "MP5", "MAC-10": "MAC10",
    "UMP-45": "UMP", "P90": "P90", "PP-Bizon": "BIZON",
    # pistols
    "Glock-18": "GLOCK", "USP-S": "USP", "P2000": "P2000", "P250": "P250",
    "Five-SeveN": "FIVE7", "Tec-9": "TEC9", "CZ75-Auto": "CZ", "Dual Berettas": "DUALS",
    "Desert Eagle": "DEAG", "R8 Revolver": "R8",
    # heavy
    "Nova": "NOVA", "XM1014": "XM", "Sawed-Off": "SAWED", "MAG-7": "MAG7",
    "M249": "M249", "Negev": "NEGEV",
    # grenades in hand
    "High Explosive Grenade": "HE", "Flashbang": "FLASH", "Smoke Grenade": "SMOKE",
    "Molotov": "MOLLY", "Incendiary Grenade": "INC", "Decoy Grenade": "DECOY",
}


def weapon_tag(name):
    """Short display tag for a held weapon, or None when there's nothing to show."""
    if not name:
        return None
    if name in WEAPON_TAGS:
        return WEAPON_TAGS[name]
    n = str(name).lower()
    if "c4" in n:
        return "💣"
    if "knife" in n or "bayonet" in n or n in ("knife", "knife_t") or any(
        w in n for w in ("karambit", "daggers", "talon", "ursus", "navaja",
                         "stiletto", "skeleton", "huntsman", "falchion", "bowie",
                         "shadow", "paracord", "nomad", "survival", "gut", "classic")):
        return "🔪"
    return str(name).upper()[:6]                     # unknown: show the raw name


def build_replay(parser, map_name, freeze_ticks, end_ticks, roster_by_round,
                 ref_side_by_round, ref_sid, names, kills_seq, step=STEP):
    info = maps.map_info(map_name)
    if not info:
        return None

    n_rounds = len(end_ticks)
    round_sample_ticks = []
    all_ticks = set()
    for i in range(n_rounds):
        # Sample a few real seconds past round-end so the deciding kill and its
        # killfeed entry stay on screen instead of cutting off on the last tick.
        pad_end = int(end_ticks[i]) + PAD_SECONDS * TICKRATE
        if i + 1 < n_rounds:                    # ...but don't bleed into next round
            pad_end = min(pad_end, int(freeze_ticks[i + 1]) - step)
        pad_end = max(pad_end, int(end_ticks[i]))
        ts = list(range(int(freeze_ticks[i]), pad_end + 1, step))
        round_sample_ticks.append(ts)
        all_ticks.update(ts)

    df = parser.parse_ticks(
        ["X", "Y", "yaw", "is_alive", "active_weapon_name", "flash_duration",
         "health"],
        ticks=sorted(all_ticks))
    df["sid"] = df["steamid"].astype(str)

    # (tick, sid) -> (x, y, yaw, alive, weapon, flash, health), per-frame lookup.
    cell = {}
    for row in df.itertuples(index=False):
        cell[(row.tick, row.sid)] = (row.X, row.Y, row.yaw, row.is_alive,
                                     row.active_weapon_name, row.flash_duration,
                                     row.health)

    # Kills per round -> frame index, for on-timeline markers.
    kills_by_round = {}
    for k in kills_seq:
        kills_by_round.setdefault(k["round"], []).append(k)

    # Bomb plants -> round -> (frame, radar px). The pre-plant carrier is shown
    # via the bomb emoji on their name; this is the planted position + timer.
    bomb_by_round = _bomb_plants(parser, freeze_ticks, end_ticks, info, step)

    # Weapon registry: each frame stores one char indexing into `wreg`; '.' = none.
    ALPHA = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz")           # 62 safe chars, no quote/backslash
    wreg, widx = [], {}

    def wchar(name, alive):
        if not alive:
            return "."
        tag = weapon_tag(name)
        if tag is None:
            return "."
        i = widx.get(tag)
        if i is None:
            i = widx[tag] = len(wreg)
            wreg.append(tag)
        return ALPHA[i] if i < len(ALPHA) else "."

    rounds_out = []
    for i in range(n_rounds):
        r = i + 1
        ts = round_sample_ticks[i]
        n_real = len(ts)
        start = int(freeze_ticks[i])
        roster = roster_by_round.get(r, {})
        ref_side = ref_side_by_round.get(r)

        def role_of(sid):
            if sid == ref_sid:
                return "you"
            if sid in roster:
                return "ally" if roster[sid] == ref_side else "enemy"
            return ""          # world / unknown -> neutral

        players_out = []
        frames_by_sid = {}          # for locating where each victim died
        for sid, side in roster.items():
            frames, wcodes, blind, health = [], [], [], []
            for tk in ts:
                c = cell.get((tk, sid))
                if c is not None and c[3] and c[0] is not None and c[1] is not None:
                    px, py = maps.world_to_radar(c[0], c[1], info)
                    frames.extend([int(round(px)), int(round(py)), int(c[2]) % 360])
                    wcodes.append(wchar(c[4], True))
                    # flash_duration (s) -> 0..9 blindness level (~4.5s = full).
                    fl = c[5] if c[5] is not None else 0.0
                    blind.append(str(min(9, int(fl / 4.5 * 9 + 0.5))))
                    # health 0..100 -> one char (index into ALPHA; 61 = full HP).
                    hp = int(c[6]) if c[6] is not None else 0
                    health.append(ALPHA[max(0, min(61, int(round(hp / 100 * 61))))])
                else:
                    frames.extend([DEAD, DEAD, 0])
                    wcodes.append(".")
                    blind.append("0")
                    health.append(".")
            frames_by_sid[sid] = frames
            players_out.append({"n": names.get(sid, "?"), "r": role_of(sid),
                                "sid": sid, "side": side, "f": frames,
                                "w": "".join(wcodes), "bl": "".join(blind),
                                "hp": "".join(health)})

        def death_pos(vsid, frame):
            """Radar px where `vsid` last stood alive at/before `frame`, or None."""
            vf = frames_by_sid.get(vsid)
            if not vf:
                return None
            j = min(frame, len(vf) // 3 - 1)
            while j >= 0 and vf[3 * j] == DEAD:
                j -= 1
            return [vf[3 * j], vf[3 * j + 1]] if j >= 0 else None

        events = []
        for k in kills_by_round.get(r, []):
            frame = max(0, min(n_real - 1, (k["tick"] - start) // step))
            ev = {
                "f": frame,
                "k": names.get(k["a_sid"], "world"), "kr": role_of(k["a_sid"]),
                "ks": k["a_sid"],
                "v": names.get(k["v_sid"], "?"), "vr": role_of(k["v_sid"]),
                "vs": k["v_sid"],
                "hs": bool(k.get("headshot")),
                "d": death_pos(k["v_sid"], frame),
            }
            events.append(ev)

        rounds_out.append({
            "n": r, "step": step, "nf": n_real,
            "players": players_out, "events": events,
            "bomb": bomb_by_round.get(r),
        })

    return {"size": info["size"], "weapons": wreg, "rounds": rounds_out}


def _bomb_plants(parser, freeze_ticks, end_ticks, info, step):
    """round -> {'pf': plant_frame, 'x': px, 'y': py} from bomb_planted events."""
    try:
        bp = parser.parse_event("bomb_planted", player=["X", "Y"])
    except Exception:
        return {}
    out = {}
    if bp is None or bp.empty:
        return out
    for row in bp.itertuples(index=False):
        tick = int(row.tick)
        for i in range(len(end_ticks)):
            if int(freeze_ticks[i]) <= tick <= int(end_ticks[i]):
                r = i + 1
                if r in out:
                    break
                x = getattr(row, "user_X", None)
                y = getattr(row, "user_Y", None)
                if x is None or y is None:
                    break
                px, py = maps.world_to_radar(x, y, info)
                pf = max(0, (tick - int(freeze_ticks[i])) // step)
                out[r] = {"pf": pf, "x": round(px, 1), "y": round(py, 1)}
                break
    return out
