"""The recorder's camera: where the player looked, and how far they zoomed.

The client keeps its own camera in the replay -- so the game can play the
battle back through the player's eyes -- in three packet types beside the
entity packets (framing in packets.py). The same in 11.7.1 and 11.20.0:

    23  sniper sight   u32: 0 = the player went into the sniper sight, 1 = back
                       to the arcade camera
    31  field of view  f32, degrees across the screen, every frame inside the
                       sight: from the arcade camera's 54 down to 27 (x2),
                       6.76 (x8) or anything between (the "smart zoom")
    39  camera         7 x f32, every frame (about 60 a second): the camera's
                       yaw and pitch in degrees -- yaw like the hull's (0 =
                       north, clockwise), pitch below the horizon -- then the
                       aim point x y z where the crosshair rests, the turret's
                       yaw relative to the hull (radians) and the gun's pitch
                       (radians, down positive)

What the checks found on 13 battles (tests/test_wotb_camera.py pins them on
the fixtures): the camera yaw points from the recorder's tank to the aim
point (median 0.1-0.7 degrees). In the sight the line of sight passes over
the tank at the gun's height from the tank's XML (T77 2.22 m against 2.23,
FV215b (183) 2.35 against 2.35): the sight sits on the gun. In the arcade
camera it passes about 4.8 m over the tank, on every tank -- the point that
camera orbits. The field of view only ever comes inside the sight. When the
recorder dies the camera leaves the sight and orbits the wreck (the aim
point stops) until the recording ends.

`build` keeps what the 3D view needs to look through the player's eyes:

    dir     [t, yaw, pitch, dt, dyaw, dpitch, ...] -- battle time in
            hundredths of a second, angles in tenths of a degree; the first
            sample absolute, the rest deltas (yaw deltas wrapped into
            -1800..1799, so the running sum turns smoothly through north).
            Thinned to the samples that straight lines between them keep
            within TOLERANCE degrees of the camera
    sniper  [[t_in, t_out], ...] -- seconds of battle time in the sight
    fov     [[t, degrees], ...] -- inside the sight, thinned the same way;
            outside it the field of view is ARCADE_FOV
"""

import math
import struct

from .replay import PREROLL

PT_SNIPER = 23
PT_FOV = 31
PT_CAMERA = 39

# optionsDesktop.yaml "Default fov" -- and where every zoom into the sight starts.
ARCADE_FOV = 54.0

TOLERANCE = 0.25      # degrees a thinned camera line may stray
FOV_TOLERANCE = 0.1   # degrees of field of view
STEP = 0.05           # seconds: at most 20 samples a second go into the thinning
MAX_GAP = 1.0         # seconds between kept samples, however still the camera

_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")
_ANGLES = struct.Struct("<2f")
CAMERA_SIZE = 28


def _wrap(value, half):
    return (value + half) % (2 * half) - half


def thin(samples, tolerance, max_gap):
    """The samples a polyline through them needs: (t, v1, v2, ...) tuples
    sorted by t; a sample is dropped when the straight line between its kept
    neighbours passes within `tolerance` of it in every value, and kept ones
    are at most `max_gap` apart. The first and the last always stay."""
    if len(samples) <= 2:
        return list(samples)
    kept, anchor, i = [samples[0]], 0, 2
    while i < len(samples):
        a, b = samples[anchor], samples[i]
        span = b[0] - a[0]
        fits = span <= max_gap
        k = anchor + 1
        while fits and k < i:
            s = samples[k]
            f = (s[0] - a[0]) / span if span > 0 else 0.0
            fits = all(abs(a[c] + (b[c] - a[c]) * f - s[c]) <= tolerance for c in range(1, len(s)))
            k += 1
        if fits:
            i += 1
        else:
            anchor = i - 1
            kept.append(samples[anchor])
            i = anchor + 2
    kept.append(samples[-1])
    return kept


def _encode(samples):
    flat, prev = [], None
    for t, yaw, pitch in samples:
        q = (round(t * 100), round(yaw * 10), round(pitch * 10))
        if prev is None:
            flat.extend((q[0], _wrap(q[1], 1800), q[2]))
        else:
            flat.extend((q[0] - prev[0], _wrap(q[1] - prev[1], 1800), q[2] - prev[2]))
        prev = q
    return flat


def decode_dir(flat):
    """[(t, yaw, pitch)] back from `dir`: seconds and degrees, yaw unwrapped
    (it keeps counting past 360 the way the camera turned)."""
    out, t, yaw, pitch = [], 0, 0, 0
    for i in range(0, len(flat) - 2, 3):
        if i == 0:
            t, yaw, pitch = flat[0:3]
        else:
            t, yaw, pitch = t + flat[i], yaw + flat[i + 1], pitch + flat[i + 2]
        out.append((t / 100, yaw / 10, pitch / 10))
    return out


def build(stream, start, end):
    """The recorder's camera over the replay's span (battle time -PREROLL
    .. `end`), or None when the stream has none (or no battle start)."""
    if stream is None or start is None or end is None:
        return None
    raw, fov_groups, sniper = [], [], []
    before = None      # the last sample before the window opens
    last_t = yaw_was = None
    inside = None      # battle time the sight was entered
    for ptype, clock, payload in stream.packets:
        t = clock - start
        if t > end:
            break
        if ptype == PT_CAMERA and len(payload) == CAMERA_SIZE:
            yaw, pitch = _ANGLES.unpack_from(payload, 0)
            if not (math.isfinite(yaw) and math.isfinite(pitch)):
                continue
            if yaw_was is not None:
                yaw = yaw_was + _wrap(yaw - yaw_was, 180.0)
            yaw_was = yaw
            if t < -PREROLL:
                before = (-PREROLL, yaw, pitch)
            elif last_t is None or t - last_t >= STEP:
                if not raw and before is not None and t > -PREROLL:
                    raw.append(before)
                raw.append((t, yaw, pitch))
                last_t = t
        elif ptype == PT_SNIPER and len(payload) == _U32.size:
            entering = _U32.unpack(payload)[0] == 0
            if entering and inside is None:
                inside = t
            elif not entering and inside is not None:
                sniper.append((inside, t))
                inside = None
        elif ptype == PT_FOV and len(payload) == _F32.size and t >= -PREROLL:
            value = _F32.unpack(payload)[0]
            if not (math.isfinite(value) and 0 < value < 180):
                continue
            # the sight sends every frame: a pause means a new visit to it
            if not fov_groups or t - fov_groups[-1][-1][0] > 0.5:
                fov_groups.append([])
            fov_groups[-1].append((t, value))
    if not raw and before is not None:
        raw.append(before)
    if not raw:
        return None
    if inside is not None:
        sniper.append((inside, end))
    sniper = [(max(t0, -PREROLL), t1) for t0, t1 in sniper if t1 > -PREROLL and t1 > t0]
    fov = [kf for group in fov_groups for kf in thin(group, FOV_TOLERANCE, 30.0)]
    return {"dir": _encode(thin(raw, TOLERANCE, MAX_GAP)),
            "sniper": [[round(t0, 2), round(t1, 2)] for t0, t1 in sniper],
            "fov": [[round(t, 2), round(v, 2)] for t, v in fov]}
