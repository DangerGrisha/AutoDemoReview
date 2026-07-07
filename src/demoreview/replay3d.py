"""Tick-level 3D replay extraction (Phase A — data only, no rendering).

Extends the demoparser2 usage of `replay.py` to true 3D at ~16 Hz: for each round it
samples every player's position (X/Y/Z), view angles (yaw/pitch) and state (alive, crouch,
scoped, defusing, health, weapon, flash) and collects fully-timed utility events. Output
is a list of neutral `round_model` dicts (see `binfmt`) — this module does NOT encode,
write files, or render. `build3d.py` drives encoding + reporting.

Tick rate is *detected*, not assumed: `game_time` is a per-tick prop advancing 1/tick_rate
seconds, so the native rate is `round(1 / median(Δgame_time per tick))`; the sample stride
is then `round(tick_rate / target_hz)`.
"""

from statistics import median

from . import maps
from .replay import weapon_tag

TARGET_HZ = 16.0
PAD_SECONDS = 3          # keep sampling a few seconds past round-end (mirror replay.py)

TICK_PROPS = ["X", "Y", "Z", "pitch", "yaw", "health", "is_alive",
              "active_weapon_name", "flash_duration", "ducked", "is_scoped",
              "is_defusing", "team_num"]

# flags bitfield
F_PRESENT, F_ALIVE, F_CROUCH, F_SCOPED, F_DEFUSING = 0x01, 0x02, 0x04, 0x08, 0x10

FLASH_FULL_S = 5.1       # flash_duration mapped onto 0..255 over this many seconds

# Utility gameplay constants — APPROXIMATE, world units / seconds. Flash uses the
# authoritative per-victim player_blind durations instead of a radius model.
UTIL_CONSTANTS = {
    "smoke":   {"radius": 144, "duration": 18.0},
    "molotov": {"radius": 150, "duration": 7.0},
    "he":      {"radius": 350, "duration": 0.5},
    "flash":   {"radius": 120, "duration": 0.0},
    "decoy":   {"radius": 0,   "duration": 15.0},
}

DET_EVENTS = {
    "smokegrenade_detonate": "smoke",
    "hegrenade_detonate": "he",
    "flashbang_detonate": "flash",
    "inferno_startburn": "molotov",
    "decoy_started": "decoy",
}


def _sanitize_name(name):
    """Readable name with lone surrogates stripped (they crash UTF-8 header encoding)."""
    text = "" if name is None else str(name).strip()
    text = "".join(c for c in text if not 0xD800 <= ord(c) <= 0xDFFF)
    return text if text else "(unnamed)"


def _norm_type(raw):
    t = str(raw).lower()
    if "flash" in t:
        return "flash"
    if "smoke" in t:
        return "smoke"
    if "molotov" in t or "incendiary" in t or "inferno" in t:
        return "molotov"
    if "decoy" in t:
        return "decoy"
    if "he" in t or "hegrenade" in t:
        return "he"
    return None


def detect_tickrate(parser):
    """Native tick rate = round(1 / median per-tick Δgame_time). Defaults to 64 on failure."""
    try:
        fe = parser.parse_event("round_freeze_end")
        t0 = int(fe["tick"].iloc[0])
        df = parser.parse_ticks(["game_time"], ticks=list(range(t0, t0 + 96)))
        sub = df[["tick", "game_time"]].drop_duplicates("tick").sort_values("tick")
        ticks = [int(t) for t in sub["tick"].tolist()]
        gt = [float(g) for g in sub["game_time"].tolist()]
        deltas = []
        for i in range(len(ticks) - 1):
            dt = ticks[i + 1] - ticks[i]
            if dt > 0:
                deltas.append((gt[i + 1] - gt[i]) / dt)
        if deltas:
            per_tick = median(deltas)
            if per_tick > 0:
                return int(round(1.0 / per_tick))
    except Exception:
        pass
    return 64


def _round_sample_ticks(freeze_ticks, end_ticks, stride, tickrate):
    """Per-round list of sample ticks + the sorted union across all rounds."""
    n = len(end_ticks)
    per_round = []
    all_ticks = set()
    for i in range(n):
        pad_end = int(end_ticks[i]) + PAD_SECONDS * tickrate
        if i + 1 < n:
            pad_end = min(pad_end, int(freeze_ticks[i + 1]) - stride)
        pad_end = max(pad_end, int(end_ticks[i]))
        ts = list(range(int(freeze_ticks[i]), pad_end + 1, stride))
        per_round.append(ts)
        all_ticks.update(ts)
    return per_round, sorted(all_ticks)


def _flash_byte(fd):
    if not fd or fd <= 0:
        return 0
    return min(255, int(round(min(fd, FLASH_FULL_S) / FLASH_FULL_S * 255)))


def _build_flights(parser):
    """entity_id -> {type, sid, start_tick, pos(x,y,z)} for grenade throw matching."""
    flights = {}
    try:
        proj = parser.parse_grenades()
    except Exception:
        return flights
    if proj is None or proj.empty:
        return flights
    proj = proj[proj["x"].notna() & proj["y"].notna()]
    for row in proj.itertuples(index=False):
        gtype = _norm_type(getattr(row, "grenade_type", ""))
        if gtype is None:
            continue
        eid = getattr(row, "grenade_entity_id", None)
        tick = int(row.tick)
        z = float(getattr(row, "z", 0.0) or 0.0)
        rec = flights.get(eid)
        if rec is None or tick < rec["start"]:
            flights[eid] = {"type": gtype, "sid": str(row.steamid), "start": tick,
                            "pos": [float(row.x), float(row.y), z]}
    return flights


def _match_throw(flights, gtype, sid, det_tick):
    """Best (start_tick, pos) for the flight of this type+thrower ending before det_tick."""
    best = None
    for rec in flights.values():
        if rec["type"] == gtype and rec["sid"] == sid and rec["start"] <= det_tick:
            if best is None or rec["start"] > best["start"]:
                best = rec
    return best


def _extract_utility(parser, freeze_ticks, end_ticks, stride):
    """Per-round list of utility event dicts (detonations + throws + flash victims)."""
    n = len(end_ticks)
    per_round = [[] for _ in range(n)]
    flights = _build_flights(parser)

    # flash victims grouped by detonation tick
    blind_by_tick = {}
    try:
        blinds = parser.parse_event("player_blind")
        if blinds is not None and not blinds.empty:
            for row in blinds.itertuples(index=False):
                blind_by_tick.setdefault(int(row.tick), []).append(
                    {"sid": str(getattr(row, "user_steamid", "")),
                     "blindDuration": float(getattr(row, "blind_duration", 0.0) or 0.0)})
    except Exception:
        pass

    def round_of(tick):
        for i in range(n):
            if int(freeze_ticks[i]) <= tick <= int(end_ticks[i]):
                return i
        return None

    for ev, gtype in DET_EVENTS.items():
        try:
            dets = parser.parse_event(ev)
        except Exception:
            continue
        if dets is None or dets.empty:
            continue
        for row in dets.itertuples(index=False):
            tick = int(row.tick)
            ri = round_of(tick)
            if ri is None:
                continue
            x = getattr(row, "x", None)
            y = getattr(row, "y", None)
            if x is None or y is None:
                continue
            z = float(getattr(row, "z", 0.0) or 0.0)
            sid = str(getattr(row, "user_steamid", ""))
            const = UTIL_CONSTANTS.get(gtype, {"radius": 0, "duration": 0.0})
            det_sample = max(0, (tick - int(freeze_ticks[ri])) // stride)
            item = {
                "type": gtype, "thrower": sid, "detTick": tick, "detSample": det_sample,
                "pos": [float(x), float(y), z],
                "radius": const["radius"], "duration": const["duration"],
            }
            flight = _match_throw(flights, gtype, sid, tick)
            if flight is not None:
                item["throwTick"] = flight["start"]
                item["throwSample"] = max(0, (flight["start"] - int(freeze_ticks[ri])) // stride)
                item["throwPos"] = flight["pos"]
            if gtype == "flash":
                victims = blind_by_tick.get(tick, [])
                if victims:
                    item["affected"] = victims
            per_round[ri].append(item)
    return per_round


def build_replay3d(parser, target_hz=TARGET_HZ):
    """Extract all rounds. Returns (rounds_models, meta).

    meta = {map, tickRate, sampleStride, sampleRate, nRounds}. `rounds_models` is a list of
    round_model dicts ready for binfmt.encode_round (or None if the map is unsupported —
    3D still extracts, but callers may want the radar calibration for context).
    """
    map_name = parser.parse_header().get("map_name")
    tickrate = detect_tickrate(parser)
    stride = max(1, int(round(tickrate / target_hz)))
    sample_rate = tickrate / stride

    freeze_ticks = parser.parse_event("round_freeze_end")["tick"].tolist()
    end_ticks = parser.parse_event("round_end")["tick"].tolist()
    n_rounds = len(end_ticks)

    per_round_ticks, all_ticks = _round_sample_ticks(freeze_ticks, end_ticks, stride, tickrate)

    df = parser.parse_ticks(TICK_PROPS, ticks=all_ticks)
    df["sid"] = df["steamid"].astype(str)

    # freeze tick -> round index, so we can capture the per-round roster in one pass
    freeze_to_round = {int(freeze_ticks[i]): i for i in range(n_rounds)}

    # (tick, sid) -> state tuple, best-known name per sid, and per-round roster {sid: team}
    cell = {}
    name_by_sid = {}
    roster_by_round = [dict() for _ in range(n_rounds)]
    for row in df.itertuples(index=False):
        tick = int(row.tick)
        cell[(tick, row.sid)] = row
        if row.sid not in name_by_sid:
            name_by_sid[row.sid] = _sanitize_name(getattr(row, "name", None))
        ri = freeze_to_round.get(tick)
        if ri is not None:
            roster_by_round[ri][row.sid] = int(row.team_num)

    utility_by_round = _extract_utility(parser, freeze_ticks, end_ticks, stride)

    models = []
    for i in range(n_rounds):
        ts = per_round_ticks[i]
        n = len(ts)
        freeze = int(freeze_ticks[i])

        # roster + per-round team captured from the freeze-tick snapshot above
        roster = roster_by_round[i]
        slots = sorted(roster.keys(), key=lambda s: (roster[s], s))

        weapon_table = [""]        # index 0 = none/empty tag
        weapon_idx = {"": 0}

        def windex(name):
            tag = weapon_tag(name)
            if not tag:
                return 255
            idx = weapon_idx.get(tag)
            if idx is None:
                idx = len(weapon_table)
                weapon_idx[tag] = idx
                weapon_table.append(tag)
            return idx if idx < 255 else 255

        tracks = []
        players = []
        for slot, sid in enumerate(slots):
            # first pass: raw per-sample state
            raw = []
            first_coords = None
            for tk in ts:
                c = cell.get((tk, sid))
                if c is None:
                    raw.append(None)                  # absent (no tick row)
                    continue
                alive = bool(c.is_alive) and c.X is not None and c.Y is not None
                coords = (float(c.X), float(c.Y), float(c.Z or 0.0),
                          float(c.yaw or 0.0), float(c.pitch or 0.0)) if alive else None
                if alive and first_coords is None:
                    first_coords = coords
                raw.append({"present": True, "alive": alive, "coords": coords, "c": c})

            X = [0.0] * n
            Y = [0.0] * n
            Z = [0.0] * n
            yaw = [0.0] * n
            pitch = [0.0] * n
            flags = [0] * n
            health = [0] * n
            weapon = [255] * n
            flash = [0] * n

            last = first_coords if first_coords is not None else (0.0, 0.0, 0.0, 0.0, 0.0)
            for j in range(n):
                r = raw[j]
                if r is not None and r["alive"]:
                    last = r["coords"]
                    c = r["c"]
                    fl = F_PRESENT | F_ALIVE
                    if bool(getattr(c, "ducked", False)):
                        fl |= F_CROUCH
                    if bool(getattr(c, "is_scoped", False)):
                        fl |= F_SCOPED
                    if bool(getattr(c, "is_defusing", False)):
                        fl |= F_DEFUSING
                    flags[j] = fl
                    health[j] = max(0, min(255, int(c.health or 0)))
                    weapon[j] = windex(getattr(c, "active_weapon_name", None))
                    flash[j] = _flash_byte(getattr(c, "flash_duration", 0.0))
                elif r is not None:
                    flags[j] = F_PRESENT           # present but dead
                # else absent: flags 0
                X[j], Y[j], Z[j], yaw[j], pitch[j] = last

            tracks.append({"X": X, "Y": Y, "Z": Z, "yaw": yaw, "pitch": pitch,
                           "flags": flags, "health": health, "weapon": weapon,
                           "flash": flash})
            players.append({"slot": slot, "sid": sid,
                            "name": name_by_sid.get(sid, "(unnamed)"),
                            "team": roster[sid]})

        models.append({
            "map": map_name, "round": i + 1, "tickRate": tickrate,
            "sampleStride": stride, "sampleRate": sample_rate, "nSamples": n,
            "startTick": freeze, "endTick": int(end_ticks[i]),
            "players": players, "weaponTable": weapon_table,
            "utility": utility_by_round[i], "tracks": tracks,
        })

    meta = {"map": map_name, "tickRate": tickrate, "sampleStride": stride,
            "sampleRate": sample_rate, "nRounds": n_rounds,
            "mapSupported": maps.map_info(map_name) is not None}
    return models, meta
