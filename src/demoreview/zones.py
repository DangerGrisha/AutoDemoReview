"""Map callout zones: turn a world position into the name players actually use.

Without this the coach can only say "five deaths in the same spot at
(-189, -720)". With it the same finding reads "five deaths at Palace" -- the
difference between a report a player can act on and one they have to decode.

The zone table (assets/zones.json) is not hand-drawn. It is extracted by
tools/extract_zones.py from the `env_cs_place` entities the maps themselves
ship: the official `place_name` plus the brush volume around it, which is the
very data the game uses to fill a player's `last_place_name`.

Accuracy was measured, not assumed. Parsing demos/1st.dem for `last_place_name`
gives the game's own answer for every player tick; over 218k sampled positions
on de_mirage, 94.0% fall inside a zone volume and **92.5% get exactly the
callout the game reports**. The residual is almost entirely overlapping
volumes (Palace Interior sits partly inside Bombsite A, and so on), which is
why `zone_of` resolves an overlap by picking the SMALLEST containing volume --
measured at 92.5% against 87.9% for "deepest inside" and 86.9% for "smallest
footprint". A guessed rectangle table would have had no way to know that.
"""

import json
import re
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets"
ZONES_PATH = ASSET_DIR / "zones.json"

# A position outside every volume is snapped to the nearest one, but only this
# far: past it the honest answer is "no idea", not a confident wrong callout.
# Deaths land outside a volume mainly by being on top of one (boosts, ledges).
MAX_SNAP_U = 256.0

_cache = None

# Callout names ship run-together ("PalaceInterior", "CTSpawn"). Two passes:
# lower->Upper, then an all-caps prefix before a capitalised word, so "CTSpawn"
# becomes "CT Spawn" rather than "CTS pawn".
_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

# Only for names the generic rules cannot fix.
_ALIASES = {
    "TopofMid": "Top of Mid",
    "BombsiteA": "Bombsite A",
    "BombsiteB": "Bombsite B",
}


def _load():
    global _cache
    if _cache is None:
        try:
            raw = json.loads(ZONES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache = {}
            return _cache
        _cache = {}
        for map_name, zones in (raw.get("maps") or {}).items():
            prepared = []
            for z in zones:
                lo, hi = z.get("min"), z.get("max")
                if not (lo and hi and len(lo) == 3 and len(hi) == 3):
                    continue
                volume = max((hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]), 0.0)
                prepared.append((z["name"], tuple(lo), tuple(hi), volume))
            if prepared:
                _cache[map_name] = prepared
    return _cache


def has_zones(map_name):
    return bool(_load().get(map_name))


def label(name):
    """A callout name as it should be read: 'PalaceInterior' -> 'Palace Interior'."""
    if not name:
        return None
    if name in _ALIASES:
        return _ALIASES[name]
    return _SPLIT_ACRONYM.sub(" ", _SPLIT_CAMEL.sub(" ", name))


def zone_of(map_name, x, y, z=None):
    """The official callout containing (x, y[, z]), or None.

    `z` is optional but worth passing on stacked maps -- Nuke's whole lower
    level sits underneath the upper one, so without it Ramp and Vents are the
    same rectangle.
    """
    zones = _load().get(map_name)
    if not zones:
        return None

    best = None
    best_volume = float("inf")
    for name, lo, hi, volume in zones:
        if not (lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]):
            continue
        if z is not None and not lo[2] <= z <= hi[2]:
            continue
        # Volumes overlap; the smallest one containing the point is the most
        # specific callout, which is what the game reports too.
        if volume < best_volume:
            best, best_volume = name, volume
    if best is not None:
        return best

    # Just outside everything (a ledge, a boost, a body sliding down a slope):
    # snap to the nearest volume, but only within MAX_SNAP_U.
    nearest, nearest_d2 = None, MAX_SNAP_U ** 2
    for name, lo, hi, _volume in zones:
        dx = max(lo[0] - x, 0.0, x - hi[0])
        dy = max(lo[1] - y, 0.0, y - hi[1])
        dz = 0.0 if z is None else max(lo[2] - z, 0.0, z - hi[2])
        d2 = dx * dx + dy * dy + dz * dz
        if d2 < nearest_d2:
            nearest, nearest_d2 = name, d2
    return nearest


def zone_label(map_name, x, y, z=None):
    """`zone_of` in reader-facing form, or None."""
    return label(zone_of(map_name, x, y, z))
