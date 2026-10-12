"""data.wotreplay: the header and the BigWorld packet stream.

    magic 0x12345678 u32le | rest_size u32le (= file size - 8) | header_size u32le
    header: u8 len + client hash (hex) | u8 len + version ("11.20.0") | u8 (?)
    packets until EOF: size u32le | type u32le | clock f32le | payload[size]

`clock` is seconds since the recording started -- NOT battle time: the battle
starts when the arena enters its BATTLE period (see arena.py), about 20 s in.
The recording stops when the player leaves (often right after dying), so the
stream can end long before the battle does.

Packet types we decode (the rest are kept raw for tools/wotb_probe.py):

    0   base player create   payload starts with the Avatar entity id u32le
    7   entity property      entity u32 | property u32 | size u32 | value
    8   entity method        entity u32 | method u32 | size u32 | args
    10  position (49 bytes)  entity u32 | space u32 | vehicle u32 | xyz 3f |
                             position error 3f | yaw pitch roll 3f | flag u8
        x/z are the ground plane in metres, y is height, yaw in radians.
        Enemies only appear while they are spotted by the recorder's team.
"""

import struct
from dataclasses import dataclass, field

from .container import ReplayError

MAGIC = 0x12345678
_HEAD = struct.Struct("<III")
_PACKET = struct.Struct("<IIf")
_CALL = struct.Struct("<III")
_POSITION = struct.Struct("<III3f3f3fB")

PT_BASE_PLAYER_CREATE = 0
PT_ENTITY_CREATE = 5
PT_ENTITY_PROPERTY = 7
PT_ENTITY_METHOD = 8
PT_POSITION = 10
PT_END = 0xFFFFFFFF


@dataclass
class Stream:
    version: str
    client_hash: str
    packets: list = field(default_factory=list)  # (type, clock, payload) tuples
    truncated: bool = False

    @property
    def end_clock(self):
        return max((clock for _, clock, _ in self.packets), default=0.0)


def _pascal(buf, pos):
    if pos >= len(buf):
        raise ReplayError("data.wotreplay: truncated header")
    size = buf[pos]
    text = buf[pos + 1:pos + 1 + size]
    if len(text) != size:
        raise ReplayError("data.wotreplay: truncated header")
    return text.decode("ascii", "replace"), pos + 1 + size


def read_stream(data):
    """Split data.wotreplay into packets. A cut-off tail sets `truncated`."""
    if len(data) < _HEAD.size:
        raise ReplayError("data.wotreplay: too short")
    magic, rest_size, header_size = _HEAD.unpack_from(data, 0)
    if magic != MAGIC:
        raise ReplayError("data.wotreplay: bad magic")
    start = _HEAD.size + header_size
    if start > len(data):
        raise ReplayError("data.wotreplay: header overruns the file")
    header = data[_HEAD.size:start]
    client_hash, pos = _pascal(header, 0)
    version, _ = _pascal(header, pos)

    stream = Stream(version=version, client_hash=client_hash,
                    truncated=rest_size + 8 != len(data))
    pos, end = start, len(data)
    while pos + _PACKET.size <= end:
        size, ptype, clock = _PACKET.unpack_from(data, pos)
        body = pos + _PACKET.size
        if body + size > end:
            stream.truncated = True
            break
        stream.packets.append((ptype, clock, data[body:body + size]))
        pos = body + size
    if pos != end:
        stream.truncated = True
    return stream


def avatar_id(stream):
    """Entity id of the recorder's Avatar (receiver of the arena updates)."""
    for ptype, _, payload in stream.packets:
        if ptype == PT_BASE_PLAYER_CREATE and len(payload) >= 4:
            return struct.unpack_from("<I", payload, 0)[0]
    return None


def method_calls(stream, entity_id=None, method_id=None):
    """Yield (clock, entity, method, args) for entity method packets."""
    for ptype, clock, payload in stream.packets:
        if ptype != PT_ENTITY_METHOD or len(payload) < _CALL.size:
            continue
        entity, method, size = _CALL.unpack_from(payload, 0)
        if entity_id is not None and entity != entity_id:
            continue
        if method_id is not None and method != method_id:
            continue
        yield clock, entity, method, payload[_CALL.size:_CALL.size + size]


def positions(stream):
    """{entity_id: [(clock, x, y, z, yaw), ...]} from every position packet."""
    tracks = {}
    for ptype, clock, payload in stream.packets:
        if ptype != PT_POSITION or len(payload) != _POSITION.size:
            continue
        (entity, _space, _vehicle, x, y, z, _ex, _ey, _ez,
         yaw, _pitch, _roll, _flag) = _POSITION.unpack(payload)
        tracks.setdefault(entity, []).append((clock, x, y, z, yaw))
    return tracks
