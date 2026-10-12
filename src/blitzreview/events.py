"""In-battle events: health, shots, hits, damage and where each gun points.

All of it rides on the BigWorld entity packets of the stream (framing in
packets.py). The indexes below are the same in both client versions we have,
11.7.1 and 11.20.0:

Vehicle entity (entity type 2)
  create, packet 5   entity u32 | type u16 | space u32 | vehicle u32 |
                     position 3f | direction 3f | size u32 | count u8 |
                     count x (property index u8, value). The first four
                     values have fixed sizes: 0 u8, 1 u8, 2 gun angles u16,
                     3 health i16 -- the rest (public info, crew ...) is not
                     read
  property 2         gun angles u16: the turret's yaw relative to the hull in
                     the top 10 bits, yaw = (v >> 6) / 1023 * 2pi - pi, same
                     sense as the hull yaw; the gun's pitch in the low 6 bits
                     (not used)
  property 3         health i16 -- always followed by method 1 with the same
                     value, which also names the cause, so only creates and
                     method 1 are read
  method 0           shooting: burst size u8 (1, or 3 for burst guns)
  method 1           health changed: new health i16 (negative = overkill),
                     attacker u32, reason u8: 0 shell, 1 fire (ticks every
                     0.5 s), 2 ramming (both tanks hurt at once), 3 a fall --
                     the attacker is the vehicle itself
  method 8           hit: attacker u32, victim u32, 1 u8, hit point u64 whose
                     low byte is the kind, effects u8, u8, u8, u8. Kinds:
                     0 ricochet, the shell flies on (a second tracer with the
                     same shot id starts there, and what it hits next comes
                     as a second hit at the same moment); 1 ricochet and 2 not
                     pierced (never any damage); 3 pierced (always damage);
                     4 pierced with a critical hit (damage, or a module only)

Avatar (the recorder)
  method 29          tracer: shooter u32, shot id u32, effects u8, start 3f,
                     velocity 3f, gravity f -- one per shell the recorder saw
                     fired, and one more from every point it bounced off
  method 20          tracer stop: shot id u32, end point 3f. Mostly in the
                     same packet bundle as the tracer: the server sends the
                     whole flight at once. A stop can come without a tracer:
                     a hidden shooter's shell landing in view

What the checks in tests/test_wotb_events.py found on 13 battles: for every
tank of the recorder's team (always in view) the bursts add up to its shots,
the hits to its hits received and the health drops to its damage received --
exactly, when the recording runs to the end of the battle. Health drops
credited to a tank add up to its damage dealt, except for drops nobody saw:
an enemy hit or burning out of view shows up with less health when it is
spotted again (`unseen`). Hull yaw plus turret yaw points along the tracer of
every shot within a degree.

A vehicle the recorder never saw has no events at all; an enemy's health is
the last value seen.
"""

import math
import struct
from collections import defaultdict

from .packets import PT_ENTITY_CREATE, PT_ENTITY_METHOD, PT_ENTITY_PROPERTY, avatar_id

VEHICLE_TYPE = 2

VP_GUN_ANGLES = 2

VM_SHOOTING = 0
VM_HEALTH_CHANGED = 1
VM_HIT = 8

AM_TRACER = 29
AM_TRACER_STOP = 20

HIT_OTHER, HIT_RICOCHET, HIT_NOT_PIERCED, HIT_PIERCED, HIT_CRITICAL = 0, 1, 2, 3, 4
REASON_SHELL, REASON_FIRE, REASON_RAMMING, REASON_FALL = 0, 1, 2, 3

# A hit and the health change it causes, a tracer stop and the hit it ends
# in: these come in the same packet bundle, so "the same moment" is tight.
SAME_MOMENT = 0.02

_CALL = struct.Struct("<III")
_CREATE = struct.Struct("<IHII3f3fI")
_HEALTH_CHANGED = struct.Struct("<hIB")
_HIT = struct.Struct("<IIB8sBBBB")
_TRACER = struct.Struct("<IIB3f3ff")
_TRACER_STOP = struct.Struct("<I3f")
_U16 = struct.Struct("<H")
_I16 = struct.Struct("<h")


def turret_yaw(gun_angles):
    """Turret yaw relative to the hull, radians in -pi..pi, from property 2."""
    return (gun_angles >> 6) / 1023 * 2 * math.pi - math.pi


def vehicle_create(payload):
    """(entity id, position (x, y, z), health, gun angles) of a vehicle create
    packet, or None for any other entity or a layout we do not know."""
    if len(payload) < _CREATE.size + 11:
        return None
    entity, etype, _space, _vehicle, x, y, z, *_direction, _size = _CREATE.unpack_from(payload, 0)
    if etype != VEHICLE_TYPE:
        return None
    props = memoryview(payload)[_CREATE.size:]
    # count, then (index, value): 0 u8, 1 u8, 2 u16, 3 i16
    if (props[1], props[3], props[5], props[8]) != (0, 1, 2, 3):
        return None
    gun = _U16.unpack_from(props, 6)[0]
    health = _I16.unpack_from(props, 9)[0]
    return entity, (x, y, z), health, gun


def parse_empty():
    return {"health": {}, "turret": {}, "damage": [], "unseen": [], "hits": [],
            "bursts": [], "tracers": []}


def parse(stream, vehicle_ids):
    """Every event of the vehicles in `vehicle_ids`, in recording clock:

    health   {entity: [(clock, health)]} -- each value once, as it changes
    turret   {entity: [(clock, yaw)]} -- turret yaw relative to the hull
    damage   [{clock, victim, attacker, amount, reason, health}]
    unseen   [{clock, victim, amount}] -- health lost out of view
    hits     [{clock, attacker, victim, kind, damage}]
    bursts   [{clock, shooter, shells}]
    tracers  [{clock, shooter, start (x, z), end (x, z) or None, victim,
             kind, damage, bounce}] -- `victim` is whom the shell hit, with
             the hit's kind and damage (None, None, 0: a miss); `bounce` marks
             the flight on from a ricochet, not a new shot
    """
    vehicle_ids = set(vehicle_ids)
    avatar = avatar_id(stream)
    out = parse_empty()
    health, turret = defaultdict(list), defaultdict(list)
    known = {}       # entity -> health as last seen
    open_shots = {}  # shot id -> tracer
    stops = []       # (clock, shooter, tracer) of tracers that came to a stop

    def note_health(entity, clock, value):
        points = health[entity]
        if not points or points[-1][1] != value:
            points.append((clock, value))

    for ptype, clock, payload in stream.packets:
        if ptype == PT_ENTITY_CREATE:
            created = vehicle_create(payload)
            if created is None or created[0] not in vehicle_ids:
                continue
            entity, _pos, value, gun = created
            value = max(value, 0)
            if entity in known and value < known[entity]:
                out["unseen"].append({"clock": clock, "victim": entity,
                                      "amount": known[entity] - value})
            known[entity] = value
            note_health(entity, clock, value)
            turret[entity].append((clock, turret_yaw(gun)))
            continue
        if ptype not in (PT_ENTITY_PROPERTY, PT_ENTITY_METHOD) or len(payload) < _CALL.size:
            continue
        entity, index, size = _CALL.unpack_from(payload, 0)
        args = payload[_CALL.size:_CALL.size + size]
        if len(args) != size:
            continue
        if ptype == PT_ENTITY_PROPERTY:
            if entity in vehicle_ids and index == VP_GUN_ANGLES and size == 2:
                turret[entity].append((clock, turret_yaw(_U16.unpack(args)[0])))
        elif entity == avatar:
            if index == AM_TRACER and size == _TRACER.size:
                shooter, shot, _fx, x, _y, z, *_rest = _TRACER.unpack(args)
                before = open_shots.get(shot)
                if before is not None:  # a ricochet: the shell flies on from here
                    before["end"] = (x, z)
                    stops.append((clock, shooter, before))
                tracer = {"clock": clock, "shooter": shooter, "start": (x, z), "end": None,
                          "victim": None, "kind": None, "damage": 0,
                          "bounce": before is not None}
                out["tracers"].append(tracer)
                open_shots[shot] = tracer
            elif index == AM_TRACER_STOP and size == _TRACER_STOP.size:
                shot, x, _y, z = _TRACER_STOP.unpack(args)
                tracer = open_shots.pop(shot, None)
                if tracer is not None:
                    tracer["end"] = (x, z)
                    stops.append((clock, tracer["shooter"], tracer))
        elif entity in vehicle_ids:
            if index == VM_SHOOTING and size == 1:
                out["bursts"].append({"clock": clock, "shooter": entity, "shells": args[0]})
            elif index == VM_HEALTH_CHANGED and size == _HEALTH_CHANGED.size:
                value, attacker, reason = _HEALTH_CHANGED.unpack(args)
                value = max(value, 0)
                before = known.get(entity)
                if before is not None and value < before:
                    out["damage"].append({"clock": clock, "victim": entity, "attacker": attacker,
                                          "amount": before - value, "reason": reason,
                                          "health": value})
                known[entity] = value
                note_health(entity, clock, value)
            elif index == VM_HIT and size == _HIT.size:
                attacker, victim, _count, point, *_rest = _HIT.unpack(args)
                out["hits"].append({"clock": clock, "attacker": attacker, "victim": victim,
                                    "kind": point[0], "damage": 0})

    _join_damage(out["hits"], out["damage"])
    _join_stops(stops, out["hits"])
    out["health"] = dict(health)
    out["turret"] = dict(turret)
    return out


def _join_damage(hits, damage):
    """Credit each shell's health drop to the hit it came with: same moment,
    same attacker and victim, the last such hit (a pair of kind-0 records
    carries its damage in the second)."""
    by_key = defaultdict(list)
    for hit in hits:
        by_key[(hit["attacker"], hit["victim"])].append(hit)
    for drop in damage:
        if drop["reason"] != REASON_SHELL:
            continue
        same = [h for h in by_key.get((drop["attacker"], drop["victim"]), ())
                if abs(h["clock"] - drop["clock"]) <= SAME_MOMENT]
        if same:
            same[-1]["damage"] += drop["amount"]


def _join_stops(stops, hits):
    """A tracer that stops at the moment its shooter hits someone hit them;
    each hit ends one flight (a burst lands three shells at once)."""
    by_attacker = defaultdict(list)
    for hit in hits:
        by_attacker[hit["attacker"]].append(hit)
    taken = set()
    for clock, shooter, tracer in stops:
        for hit in by_attacker.get(shooter, ()):
            if id(hit) not in taken and abs(hit["clock"] - clock) <= SAME_MOMENT:
                taken.add(id(hit))
                tracer.update(victim=hit["victim"], kind=hit["kind"], damage=hit["damage"])
                break
