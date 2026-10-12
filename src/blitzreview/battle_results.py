"""battle_results.dat -> every player's final results.

The file is a Python-2 pickle of the tuple `(arena_unique_id, protobuf_bytes)`.
It is parsed BY HAND below and never handed to `pickle.loads`: replays arrive
through the upload form, and unpickling attacker-controlled bytes runs
attacker-chosen code. The wrapper is five opcodes; anything else is rejected.

Field numbers: the baseline is eigenein/wotbreplay-parser (MIT, Rust); the rest
were worked out against our replays and are pinned by self-consistency checks
in tests/test_wotb_battle_results.py (e.g. "kills == number of victims naming
you as killer" holds for 168/168 player rows). Confidence per field is in
docs/WOTB_FORMAT.md. Unknown fields are simply not read.
"""

import struct

from .pb import Message, PbError


class BattleResultsError(ValueError):
    """battle_results.dat is not the pickle-wrapped protobuf we expect."""


# --------------------------------------------------------------------------- #
# the pickle wrapper
# --------------------------------------------------------------------------- #

_PROTO = 0x80
_LONG1, _LONG4 = 0x8A, 0x8B
_BININT, _BININT1, _BININT2 = ord("J"), ord("K"), ord("M")
_BINSTRING, _SHORT_BINSTRING = ord("T"), ord("U")
_BINBYTES, _SHORT_BINBYTES = ord("B"), ord("C")
_TUPLE2, _BINPUT, _LONG_BINPUT, _STOP = 0x86, ord("q"), ord("r"), ord(".")


class _Cursor:
    def __init__(self, raw):
        self.raw, self.pos = raw, 0

    def take(self, n):
        if self.pos + n > len(self.raw):
            raise BattleResultsError("truncated pickle wrapper")
        chunk = self.raw[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def byte(self):
        return self.take(1)[0]


def unwrap(raw):
    """Return (arena_unique_id, protobuf_bytes) without unpickling anything."""
    cur = _Cursor(raw)
    if cur.byte() != _PROTO or cur.byte() != 2:
        raise BattleResultsError("not a protocol-2 pickle")

    op = cur.byte()
    if op == _LONG1:
        arena_id = int.from_bytes(cur.take(cur.byte()), "little", signed=True)
    elif op == _LONG4:
        (size,) = struct.unpack("<I", cur.take(4))
        arena_id = int.from_bytes(cur.take(size), "little", signed=True)
    elif op == _BININT:
        (arena_id,) = struct.unpack("<i", cur.take(4))
    elif op == _BININT1:
        arena_id = cur.byte()
    elif op == _BININT2:
        (arena_id,) = struct.unpack("<H", cur.take(2))
    else:
        raise BattleResultsError(f"unexpected opcode {op:#04x} for the arena id")

    op = cur.byte()
    if op in (_BINSTRING, _BINBYTES):
        (size,) = struct.unpack("<I", cur.take(4))
    elif op in (_SHORT_BINSTRING, _SHORT_BINBYTES):
        size = cur.byte()
    else:
        raise BattleResultsError(f"unexpected opcode {op:#04x} for the payload")
    payload = cur.take(size)

    if cur.byte() != _TUPLE2:
        raise BattleResultsError("expected a 2-tuple")
    op = cur.byte()
    if op == _BINPUT:
        cur.take(1)
        op = cur.byte()
    elif op == _LONG_BINPUT:
        cur.take(4)
        op = cur.byte()
    if op != _STOP:
        raise BattleResultsError("unexpected trailing opcodes")
    return arena_id, payload


# --------------------------------------------------------------------------- #
# the protobuf
# --------------------------------------------------------------------------- #

MODE_ENCOUNTER, MODE_SUPREMACY = 0, 1

# Top level.
F_MODE_MAP = 1        # low 16 bits = map id, bits 16..23 = game mode
F_TIMESTAMP = 2
F_WINNER_TEAM = 3     # absent on a draw
F_AUTHOR = 8
F_ROOM_TYPE = 9       # 1 = random battle (NOT the recorder's team)
F_FREE_XP = 137
F_PLAYERS = 201
F_RESULTS = 301

# Author block (the recorder's own totals, premium included).
A_HP_LEFT = 1         # int32; -2 = auto-destroyed for inactivity
A_CREDITS = 2
A_XP = 3
A_SHOTS = 4
A_HITS = 5
A_SPLASHES = 6
A_PENETRATIONS = 7
A_DAMAGE = 8
A_ACCOUNT_ID = 101
A_TEAM = 102
A_TANK_ID = 103

# Player (#201) -> info (#2).
P_ACCOUNT_ID = 1
P_INFO = 2
PI_NICKNAME = 1
PI_PLATOON_ID = 2     # shared by platoon mates, absent for solo players
PI_TEAM = 3
PI_CLAN_TAG = 5

# Result (#301) -> info (#2).
R_ENTITY_ID = 1       # == the vehicle's entity id in data.wotreplay
R_INFO = 2
RI_HP_LEFT = 1        # int32: 0 or negative = destroyed (overkill shows as -N)
RI_CREDITS = 2
RI_XP = 3             # base XP, before multipliers
RI_SHOTS = 4
RI_HITS = 5
RI_PENETRATIONS = 7
RI_DAMAGE = 8
RI_ASSIST_A = 9       # two kinds of assisted damage (spotting / tracking),
RI_ASSIST_B = 10      # not yet told apart
RI_DAMAGE_RECEIVED = 11
RI_HITS_RECEIVED = 12
RI_NONPEN_RECEIVED = 13
RI_PENS_RECEIVED = 15
RI_ENEMIES_DAMAGED = 17
RI_KILLS = 18
RI_KILLER_ID = 25     # entity id of whoever destroyed this vehicle
RI_VP_EARNED = 32     # supremacy victory points
RI_VP_SEIZED = 33
RI_ACCOUNT_ID = 101
RI_TEAM = 102
RI_TANK_ID = 103
RI_MM_RATING = 107    # float; display rating = 3000 + 10 * value; often absent
RI_DAMAGE_BLOCKED = 117
RI_MAX_HP = 125       # only written by newer clients (11.20 yes, 11.7 no)


def _author(msg):
    if msg is None:
        return None
    return {
        "account_id": msg.uint(A_ACCOUNT_ID),
        "team": msg.uint(A_TEAM),
        "tank_id": msg.uint(A_TANK_ID),
        "hp_left": msg.sint(A_HP_LEFT),
        "credits": msg.uint(A_CREDITS),
        "xp": msg.uint(A_XP),
        "shots": msg.uint(A_SHOTS),
        "hits": msg.uint(A_HITS),
        "splashes": msg.uint(A_SPLASHES),
        "penetrations": msg.uint(A_PENETRATIONS),
        "damage": msg.uint(A_DAMAGE),
    }


def _player(msg):
    info = msg.message(P_INFO)
    if info is None:
        return None
    return {
        "account_id": msg.uint(P_ACCOUNT_ID),
        "nickname": info.string(PI_NICKNAME),
        "platoon_id": info.uint(PI_PLATOON_ID) or None,
        "team": info.uint(PI_TEAM),
        "clan_tag": info.string(PI_CLAN_TAG) or None,
    }


def _result(msg):
    info = msg.message(R_INFO)
    if info is None:
        return None
    mm = info.float32(RI_MM_RATING)
    return {
        "entity_id": msg.uint(R_ENTITY_ID),
        "account_id": info.uint(RI_ACCOUNT_ID),
        "team": info.uint(RI_TEAM),
        "tank_id": info.uint(RI_TANK_ID),
        "hp_left": info.sint(RI_HP_LEFT),
        "max_hp": info.uint(RI_MAX_HP) or None,
        "damage": info.uint(RI_DAMAGE),
        "kills": info.uint(RI_KILLS),
        "shots": info.uint(RI_SHOTS),
        "hits": info.uint(RI_HITS),
        "penetrations": info.uint(RI_PENETRATIONS),
        "assist": info.uint(RI_ASSIST_A) + info.uint(RI_ASSIST_B),
        "damage_received": info.uint(RI_DAMAGE_RECEIVED),
        "hits_received": info.uint(RI_HITS_RECEIVED),
        "nonpen_received": info.uint(RI_NONPEN_RECEIVED),
        "pens_received": info.uint(RI_PENS_RECEIVED),
        "damage_blocked": info.uint(RI_DAMAGE_BLOCKED),
        "enemies_damaged": info.uint(RI_ENEMIES_DAMAGED),
        "killer_id": info.uint(RI_KILLER_ID) or None,
        "xp": info.uint(RI_XP),
        "credits": info.uint(RI_CREDITS),
        "victory_points": info.uint(RI_VP_EARNED) + info.uint(RI_VP_SEIZED),
        "mm_rating": round(mm, 2) if mm is not None else None,
    }


def parse(raw):
    """Decode battle_results.dat into a plain dict (see the F_/R_ tables above)."""
    arena_id, payload = unwrap(raw)
    try:
        top = Message(payload)
        mode_map = top.uint(F_MODE_MAP)
        players = [p for p in map(_player, top.messages(F_PLAYERS)) if p]
        results = [r for r in map(_result, top.messages(F_RESULTS)) if r]
        author = _author(top.message(F_AUTHOR))
        return {
            "arena_id": arena_id,
            "map_id": mode_map & 0xFFFF,
            "mode": (mode_map >> 16) & 0xFF,
            "timestamp": top.sint(F_TIMESTAMP),
            "winner_team": top.uint(F_WINNER_TEAM),  # 0 = draw
            "room_type": top.uint(F_ROOM_TYPE),
            "free_xp": top.uint(F_FREE_XP),
            "author": author,
            "players": players,
            "results": results,
        }
    except PbError as exc:
        raise BattleResultsError(f"malformed results protobuf: {exc}") from exc
