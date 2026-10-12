"""Arena updates: the roster, the battle periods and the kill feed.

The server pushes these to the recorder's Avatar entity as one method call,
`updateArena(updateType: u8, payload: blob)` in BigWorld terms -- method index
48 in both client versions we have (11.7.1 and 11.20.0). The blob carries a
BigWorld packed length (one byte, or 0xFF + u24le) and then protobuf.

Update types decoded so far (top-level protobuf field in brackets):

    1  vehicle list   [1] repeated {1 entity id, 2 compact descriptor,
                          3 nickname, 4 team, 7 account id, 8 clan tag,
                          10 platoon id}
    3  period         [3] {1 period, 2 length f64, 3 length s}
                          1 waiting, 2 pre-battle countdown, 3 battle, 4 after
    6  vehicle killed [6] {1 victim entity, 2 killer entity, 3 ?}
    8  base capture   [8] {1 team, 2 base id, 3 capture points AGAINST that
                          team, 0..100 -- i.e. the OTHER team's progress, 4 1
                          while the team's own tanks stand in the base}
                          Encounter only; one record per team, both at once
   12  point state    [11] {1 point id (A = 0), 2 owner team, 3 capturing team,
                          4 capture progress %, 5 ?, 6 ?} -- Supremacy only;
                          each update is the point's whole state (protobuf
                          leaves out zeros: an empty message is point A,
                          neutral, untouched)
   13  team score     [12] {1 team, 2 victory points} -- Supremacy only
   18  arena settings [17] {5 "regular", 10 victory points at the start (300),
                          11 victory points that win (1000), 9 ?, 12 ?}

The roster is what makes a replay WITHOUT battle_results.dat still readable:
it names every vehicle, its owner, team and tank. The compact descriptor's
first two bytes are the tank id (u16le: nation << 4 | 1, then the per-nation
vehicle index) -- checked against battle_results on 166/166 vehicles.

The capture updates were checked against the tracks: every capture start we
can see (the capturing team spotted) has one of its tanks inside the zone --
a Supremacy point's 15 m, the Encounter base's 25 m -- and on Rift the
progress of the team on the base came in the record of the team NOT on it.
They also tell the mode when battle_results.dat is missing: Supremacy sends
point states and scores from the start, Encounter the base records.
"""

from .packets import avatar_id, method_calls
from .pb import Message, PbError

ARENA_UPDATE_METHOD = 48

UT_VEHICLE_LIST = 1
UT_PERIOD = 3
UT_VEHICLE_KILLED = 6
UT_BASE_CAPTURE = 8
UT_POINT_STATE = 12
UT_TEAM_SCORE = 13
UT_ARENA_SETTINGS = 18

PERIOD_WAITING, PERIOD_PREBATTLE, PERIOD_BATTLE, PERIOD_AFTERBATTLE = 1, 2, 3, 4

MODE_ENCOUNTER, MODE_SUPREMACY = 0, 1  # the numbering battle_results uses
OTHER_TEAM = {1: 2, 2: 1}


def _blob(args, pos):
    """BigWorld packed-length bytes at `pos`: u8 size, or 0xFF + u24le size."""
    if pos >= len(args):
        return None
    size = args[pos]
    pos += 1
    if size == 0xFF:
        if pos + 3 > len(args):
            return None
        size = int.from_bytes(args[pos:pos + 3], "little")
        pos += 3
    if pos + size > len(args):
        return None
    return args[pos:pos + size]


def iter_updates(stream):
    """Yield (clock, update_type, Message) for every decodable arena update."""
    avatar = avatar_id(stream)
    for clock, _, _, args in method_calls(stream, avatar, ARENA_UPDATE_METHOD):
        if not args:
            continue
        blob = _blob(args, 1)
        if blob is None:
            continue
        try:
            yield clock, args[0], Message(blob)
        except PbError:
            continue  # an update type we cannot read must not sink the replay


def _vehicle(entry):
    descriptor = entry.raw(2, b"")
    return {
        "entity_id": entry.uint(1),
        "tank_id": descriptor[0] | (descriptor[1] << 8) if len(descriptor) >= 2 else 0,
        "nickname": entry.string(3),
        "team": entry.uint(4),
        "account_id": entry.uint(7),
        "clan_tag": entry.string(8) or None,
        "platoon_id": entry.uint(10) or None,
    }


def _no_objectives():
    return {"goal": None, "base": [], "points": [], "score": []}


def parse_empty():
    """What `parse` returns for a replay without a packet stream."""
    return {"roster": {}, "periods": [], "battle_start": None, "kills": [],
            "mode": None, "objectives": _no_objectives()}


def parse(stream):
    """Roster, periods, battle start clock, kill feed and the capture updates
    of one packet stream; `mode` is what those updates say (None if neither)."""
    roster, periods, kills = {}, [], []
    objectives = _no_objectives()
    for clock, utype, msg in iter_updates(stream):
        try:
            if utype == UT_VEHICLE_LIST:
                listing = msg.message(1)
                for entry in (listing.messages(1) if listing else ()):
                    vehicle = _vehicle(entry)
                    if vehicle["entity_id"]:
                        roster[vehicle["entity_id"]] = vehicle
            elif utype == UT_PERIOD:
                body = msg.message(3)
                if body is not None:
                    periods.append({"clock": round(clock, 3),
                                    "period": body.uint(1),
                                    "length": body.uint(3)})
            elif utype == UT_VEHICLE_KILLED:
                body = msg.message(6)
                if body is not None and body.uint(1):
                    kills.append({"clock": round(clock, 3),
                                  "victim": body.uint(1),
                                  "killer": body.uint(2) or None})
            elif utype == UT_BASE_CAPTURE:
                body = msg.message(8)
                if body is not None and body.uint(1) in OTHER_TEAM:
                    objectives["base"].append({"clock": round(clock, 3),
                                               "team": OTHER_TEAM[body.uint(1)],
                                               "points": body.uint(3)})
            elif utype == UT_POINT_STATE:
                body = msg.message(11)
                if body is not None:
                    objectives["points"].append({"clock": round(clock, 3), "id": body.uint(1),
                                                 "owner": body.uint(2), "capturer": body.uint(3),
                                                 "progress": body.uint(4)})
            elif utype == UT_TEAM_SCORE:
                body = msg.message(12)
                if body is not None and body.uint(1):
                    objectives["score"].append({"clock": round(clock, 3), "team": body.uint(1),
                                                "points": body.uint(2)})
            elif utype == UT_ARENA_SETTINGS:
                body = msg.message(17)
                if body is not None and body.uint(11):
                    objectives["goal"] = body.uint(11)
        except PbError:
            continue
    battle_start = next((p["clock"] for p in periods if p["period"] == PERIOD_BATTLE), None)
    if objectives["points"] or objectives["score"]:
        mode = MODE_SUPREMACY
    elif objectives["base"]:
        mode = MODE_ENCOUNTER
    else:
        mode = None
    return {"roster": roster, "periods": periods, "battle_start": battle_start,
            "kills": kills, "mode": mode, "objectives": objectives}
