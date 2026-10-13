"""Vehicle tracks for the 2D minimap replay.

The stream carries a position packet for every vehicle the recorder can see,
about 10 a second (packets.positions): allies all battle long, enemies only
while somebody on the recorder's team spots them. That is exactly what the
player saw on the minimap, so the replay shows enemies the same way -- live
while spotted, then a fading "last seen" marker.

Per vehicle we keep:
  * samples at least STEP apart (about 4 a second) -- plenty for a minimap;
  * always the last sample before a silence longer than GAP, so the "last
    seen" spot is exact (and the first one after it, which STEP keeps anyway);
  * nothing after the vehicle's death: a wreck still slides a little and keeps
    sending packets, but the replay draws it where it died. Every kill in our
    replays has a sample within 0.15 s before it, so that spot is exact too.

Times are battle time (0 = the BATTLE period starts, see arena.py), from
PREROLL seconds before it, when the tanks stand at their spawns.

Encoding -- a battle's tracks stay around 60-150 KB of JSON: one flat list of
integers per vehicle, [t, x, z, yaw, turret, pitch, dt, dx, dz, dyaw, ...].
The first sample is absolute, every next one is a delta to the previous:

    t       battle time, tenths of a second (negative before the battle starts)
    x z     metres x 10; x grows to the east, z to the north
    yaw     hull heading in whole degrees: 0 = north, 90 = east (clockwise seen
            from above, i.e. on the minimap); deltas wrap into -180..179
    turret  where the turret points relative to the hull, whole degrees, the
            same sense (events.py); hull + turret = where the gun looks
    pitch   how far the gun points below the hull's level (up negative),
            tenths of a degree (events.gun_pitch) -- with the slope under the
            tank, what a view from the gun looks along

`decode` sums them back up (the report's script does the same in JS). Older
battles carry older formats: 1 has no turret and no pitch (four numbers a
sample), 2 no pitch (five).
"""

import bisect
import math

STEP = 0.25
GAP = 1.5
PREROLL = 3.0
DEATH_TAIL = 0.2

FORMAT = 3  # of the encoded track list
WIDTH = {1: 4, 2: 5, 3: 6}  # numbers per sample


def _wrap(degrees):
    return (degrees + 180) % 360 - 180


def _last_sent(values, default):
    """clock -> the value last sent at or before it, `default` before the
    first one. `values` are [(clock, value)] sorted by clock."""
    times = [clock for clock, _ in values]

    def at(clock):
        i = bisect.bisect_right(times, clock) - 1
        return values[i][1] if i >= 0 else default
    return at


def decimate(points, start, end, turret=None, pitch=None):
    """Battle-time samples (t, x, z, yaw, turret, pitch) kept from one raw
    track, angles in degrees.

    `points` are (clock, x, y, z, yaw_radians) sorted by clock; `start` is the
    battle start clock, `end` the last battle time worth keeping; `turret` is
    the vehicle's [(clock, yaw_radians)] from events.py (none: straight
    ahead), `pitch` its [(clock, degrees)] (none: level).
    """
    turret_at = _last_sent(turret or [], 0.0)
    pitch_at = _last_sent(pitch or [], 0.0)
    out = []
    last_t = None
    for i, (clock, x, _y, z, yaw) in enumerate(points):
        t = clock - start
        if t < -PREROLL:
            continue
        if t > end:
            break
        nxt = points[i + 1][0] - start if i + 1 < len(points) else None
        before_gap = nxt is None or nxt > end or nxt - t > GAP
        if last_t is None or t - last_t >= STEP - 1e-6 or before_gap:
            out.append((t, x, z, math.degrees(yaw), math.degrees(turret_at(clock)),
                        pitch_at(clock)))
            last_t = t
    return out


def encode(samples):
    """(t, x, z, yaw, turret, pitch) samples, angles in degrees -> the flat
    delta-coded integer list."""
    flat, prev = [], None
    for t, x, z, yaw, turret, pitch in samples:
        q = (round(t * 10), round(x * 10), round(z * 10), round(_wrap(yaw)),
             round(_wrap(turret)), round(pitch * 10))
        if prev is None:
            flat.extend(q)
        else:
            flat.extend((q[0] - prev[0], q[1] - prev[1], q[2] - prev[2],
                         _wrap(q[3] - prev[3]), _wrap(q[4] - prev[4]), q[5] - prev[5]))
        prev = q
    return flat


def decode(flat, track_format=FORMAT):
    """The inverse of encode: [(t, x, z, yaw, turret, pitch)], yaws in degrees
    -180..179, pitch in degrees. What an older format lacks comes back as 0:
    the turret straight ahead (format 1), the gun level (formats 1 and 2)."""
    width = WIDTH[track_format]
    out = []
    t = x = z = yaw = turret = pitch = 0
    for i in range(0, len(flat) - width + 1, width):
        if i == 0:
            t, x, z, yaw = flat[0:4]
            turret = flat[4] if width > 4 else 0
            pitch = flat[5] if width > 5 else 0
        else:
            t += flat[i]
            x += flat[i + 1]
            z += flat[i + 2]
            yaw = _wrap(yaw + flat[i + 3])
            if width > 4:
                turret = _wrap(turret + flat[i + 4])
            if width > 5:
                pitch += flat[i + 5]
        out.append((t / 10, x / 10, z / 10, yaw, turret, pitch / 10))
    return out


def build(raw_tracks, start, vehicles, end, turrets=None, pitches=None):
    """The replay block of the battle dict, or None without a battle start.

    raw_tracks  packets.positions(stream)
    start       battle start clock (arena.parse()["battle_start"])
    vehicles    [(entity_id, death_time or None)] -- only these get tracks (the
                Avatar has a position track too, stuck at the origin)
    end         battle time where the recording stops
    turrets     events.parse()["turret"]: {entity_id: [(clock, yaw)]}
    pitches     {entity_id: [(clock, degrees)]} -- the gun's pitch (battle.py
                reads events.parse()["pitch"] over each tank's limits)
    """
    turrets = turrets or {}
    pitches = pitches or {}
    if start is None or end is None:
        return None
    tracks = []
    for entity_id, death_time in vehicles:
        points = raw_tracks.get(entity_id)
        if not points:
            continue
        stop = min(end, death_time + DEATH_TAIL) if death_time is not None else end
        samples = decimate(points, start, stop, turrets.get(entity_id), pitches.get(entity_id))
        if samples:
            tracks.append({"id": entity_id, "p": encode(samples)})
    return {"format": FORMAT, "step": STEP, "gap": GAP, "start": -PREROLL,
            "end": round(end, 1), "tracks": tracks}
