"""One replay -> one JSON-able battle dict: what the site stores and renders.

Joins three sources that overlap on purpose:

  * meta.json -- always there: map, recorder, tank, client version, duration;
  * the packet stream -- the roster (who drove what, platoons, clans), the
    battle periods (when the battle really started) and the kill feed;
  * battle_results.dat -- only if the recorder waited for the results: the
    final numbers of all 14 players (damage, shots, hp left, xp ...).

The stream also gives every visible vehicle's position, which becomes the
`replay` block -- the tracks of the 2D minimap replay (see replay.py) -- and
the capture updates, which become `objectives`: who captured what and when,
and the Supremacy score (see arena.py).

Vehicles are keyed by entity id: a battle result's id IS the vehicle's entity
id in the stream, so the sources join exactly, with no name matching. A broken
or missing results file degrades the report (no final numbers) instead of
rejecting the replay; the reason goes into `warnings`.
"""

from . import arena, battle_results, gamedata, replay
from .container import open_replay
from .packets import positions, read_stream

# 2: adds the `replay` block (vehicle tracks).
# 3: adds `objectives` (base and point captures, the Supremacy score) and takes
#    the mode of a battle without results from the stream.
# Battles stored in an older format are re-parsed when their replay is
# uploaded again (web/blitz.py).
FORMAT = 3

_STAT_FIELDS = ("damage", "kills", "shots", "hits", "penetrations", "assist",
                "damage_received", "hits_received", "nonpen_received",
                "pens_received", "damage_blocked", "enemies_damaged", "hp_left",
                "max_hp", "xp", "credits", "victory_points", "mm_rating")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_meta(raw):
    """meta.json mixes strings and numbers for ids; make the types predictable."""
    return {
        "version": str(raw.get("version") or ""),
        "map_key": str(raw.get("mapName") or ""),
        "map_id": _int(raw.get("mapId")),
        "player_name": str(raw.get("playerName") or ""),
        "account_id": _int(raw.get("dbid")),
        "arena_id": _int(raw.get("arenaUniqueId")),
        "started_at": _int(raw.get("battleStartTime")),
        "duration": _float(raw.get("battleDuration")),
        "tank_id": _int(raw.get("vehicleCompDescriptor")),
        "tank_key": str(raw.get("playerVehicleName") or ""),
        "bonus_type": _int(raw.get("arenaBonusType")),
    }


def load(src, lang="ru"):
    """Parse a replay (path or bytes) into the battle dict."""
    files = open_replay(src)
    warnings = []
    stream = read_stream(files.data) if files.data else None
    if stream is None:
        warnings.append("no packet stream in the replay")
    elif stream.truncated:
        warnings.append("packet stream is cut off")
    arena_info = arena.parse(stream) if stream else arena.parse_empty()

    results = None
    if files.battle_results is None:
        warnings.append("no battle results (the recorder left before they arrived)")
    else:
        try:
            results = battle_results.parse(files.battle_results)
        except battle_results.BattleResultsError as exc:
            warnings.append(f"battle results unreadable: {exc}")
    return build(normalize_meta(files.meta), stream, arena_info, results,
                 warnings=warnings, lang=lang)


def _vehicles(meta, arena_info, results):
    rows = {}
    for eid, entry in arena_info["roster"].items():
        rows[eid] = dict(entry, stats=None)
    if results:
        players = {p["account_id"]: p for p in results["players"]}
        for res in results["results"]:
            row = rows.setdefault(res["entity_id"], {"entity_id": res["entity_id"],
                                                     "stats": None})
            player = players.get(res["account_id"], {})
            for key, value in (("account_id", res["account_id"]), ("team", res["team"]),
                               ("tank_id", res["tank_id"]),
                               ("nickname", player.get("nickname")),
                               ("clan_tag", player.get("clan_tag")),
                               ("platoon_id", player.get("platoon_id"))):
                if not row.get(key) and value:
                    row[key] = value
            row["stats"] = {key: res[key] for key in _STAT_FIELDS}
            row["killer_id"] = res["killer_id"]
    return rows


def _objectives(info, start):
    """The capture updates in battle time, compact for storage, or None when
    the stream has none:

        base    [t, capturing team, capture points 0..100]   Encounter
        points  [t, point id, owner, capturing team, progress %]   Supremacy
        score   [t, team, victory points]   Supremacy; `goal` points win

    Team 0 means nobody. Each base / point entry is the whole state from t on.
    """
    if start is None or not (info["base"] or info["points"] or info["score"]):
        return None

    def t(entry):
        return round(entry["clock"] - start, 1)

    return {
        "goal": info["goal"],
        "base": [[t(e), e["team"], e["points"]] for e in info["base"]],
        "points": [[t(e), e["id"], e["owner"], e["capturer"], e["progress"]]
                   for e in info["points"]],
        "score": [[t(e), e["team"], e["points"]] for e in info["score"]],
    }


def build(meta, stream, arena_info, results, warnings=(), lang="ru"):
    rows = _vehicles(meta, arena_info, results)
    start = arena_info["battle_start"]

    author = next((r for r in rows.values() if r.get("account_id") == meta["account_id"]), None)
    author_team = (author or {}).get("team") or ((results or {}).get("author") or {}).get("team")

    death_time = {}
    for kill in arena_info["kills"]:
        death_time.setdefault(kill["victim"], kill)

    # Platoons get small per-battle numbers (1, 2, ...) instead of server ids.
    # A platoon id carried by ONE vehicle happens (the mate dropped before the
    # battle) and would draw a badge with nobody to match -- so it gets none.
    members = {}
    for row in rows.values():
        if row.get("platoon_id"):
            members[row["platoon_id"]] = members.get(row["platoon_id"], 0) + 1
    platoon_no = {}
    for row in sorted(rows.values(), key=lambda r: r["entity_id"]):
        pid = row.get("platoon_id")
        if pid and members[pid] > 1 and pid not in platoon_no:
            platoon_no[pid] = len(platoon_no) + 1

    vehicles = []
    for row in rows.values():
        stats = row.get("stats")
        kill = death_time.get(row["entity_id"])
        if stats is not None:
            alive = stats["hp_left"] > 0
        elif stream is not None:  # without results: alive when the recording stopped
            alive = kill is None
        else:
            alive = None
        killer = row.get("killer_id") or (kill or {}).get("killer")
        vehicles.append({
            "entity_id": row["entity_id"],
            "account_id": row.get("account_id"),
            "nickname": row.get("nickname") or f"#{row['entity_id']}",
            "clan_tag": row.get("clan_tag"),
            "platoon": platoon_no.get(row.get("platoon_id")),
            "team": row.get("team"),
            "ally": row.get("team") == author_team,
            "is_author": author is not None and row["entity_id"] == author["entity_id"],
            "tank": gamedata.vehicle(row.get("tank_id") or 0, lang),
            "alive": alive,
            "killer_id": killer if not alive else None,
            "death_time": (round(kill["clock"] - start, 1)
                           if kill and start is not None else None),
            "stats": stats,
        })
    vehicles.sort(key=lambda v: (not v["ally"],
                                 -((v["stats"] or {}).get("damage") or 0),
                                 v["nickname"].lower()))

    if results is None:
        outcome, winner = "unknown", None
    else:
        winner = results["winner_team"]
        outcome = "draw" if not winner else ("win" if winner == author_team else "loss")

    mode = results["mode"] if results else arena_info["mode"]
    end_clock = stream.end_clock if stream else None
    tracks = None
    if stream is not None and start is not None:
        tracks = replay.build(positions(stream), start,
                              [(v["entity_id"], v["death_time"]) for v in vehicles],
                              end_clock - start)
    return {
        "format": FORMAT,
        "client_version": meta["version"],
        "arena_id": meta["arena_id"] or (results or {}).get("arena_id"),
        "started_at": meta["started_at"],
        "duration": meta["duration"],
        "map": gamedata.map_info(meta["map_key"] or None, meta["map_id"], lang),
        "mode": {"id": mode, "name": gamedata.mode_name(mode, lang)} if mode is not None else None,
        "bonus_type": meta["bonus_type"],
        "outcome": outcome,
        "winner_team": winner,
        "author": {
            "account_id": meta["account_id"],
            "nickname": meta["player_name"],
            "entity_id": author["entity_id"] if author else None,
            "team": author_team,
            "tank": gamedata.vehicle(meta["tank_id"] or 0, lang),
            "totals": (results or {}).get("author"),
            "free_xp": (results or {}).get("free_xp"),
        },
        "battle_start_clock": start,
        "recording_end_clock": round(end_clock, 3) if end_clock is not None else None,
        "has_results": results is not None,
        "vehicles": vehicles,
        "kills": [{"t": round(k["clock"] - start, 1) if start is not None else None,
                   "victim": k["victim"], "killer": k["killer"]}
                  for k in arena_info["kills"]],
        "replay": tracks,
        "objectives": _objectives(arena_info["objectives"], start),
        "warnings": list(warnings),
    }
