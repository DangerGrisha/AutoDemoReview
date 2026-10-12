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

The roster is what makes a replay WITHOUT battle_results.dat still readable:
it names every vehicle, its owner, team and tank. The compact descriptor's
first two bytes are the tank id (u16le: nation << 4 | 1, then the per-nation
vehicle index) -- checked against battle_results on 166/166 vehicles.
"""

from .packets import avatar_id, method_calls
from .pb import Message, PbError

ARENA_UPDATE_METHOD = 48

UT_VEHICLE_LIST = 1
UT_PERIOD = 3
UT_VEHICLE_KILLED = 6

PERIOD_WAITING, PERIOD_PREBATTLE, PERIOD_BATTLE, PERIOD_AFTERBATTLE = 1, 2, 3, 4


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


def parse_empty():
    """What `parse` returns for a replay without a packet stream."""
    return {"roster": {}, "periods": [], "battle_start": None, "kills": []}


def parse(stream):
    """Roster, periods, battle start clock and kill feed of one packet stream."""
    roster, periods, kills = {}, [], []
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
        except PbError:
            continue
    battle_start = next((p["clock"] for p in periods if p["period"] == PERIOD_BATTLE), None)
    return {"roster": roster, "periods": periods,
            "battle_start": battle_start, "kills": kills}
