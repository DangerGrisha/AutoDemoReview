"""Map radar calibration + world->pixel projection, and radar image loading.

Calibration (pos_x, pos_y, scale) and radar images are sourced from awpy's map
data; they are consistent with each other. To support more maps, drop the radar
PNG in assets/ and add an entry here.
"""

import base64
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets"

# radar size is 1024x1024 for all these images. Calibration (pos_x, pos_y,
# scale) comes straight from awpy's map-data.json and matches these radar PNGs.
# Only single-level maps with no radar rotation/zoom are listed: the projection
# below is a flat world->pixel map, so it can't handle de_dust2's rotated radar
# (rotate=1, zoom=1.1). Multi-level maps (nuke/vertigo/train) use their UPPER
# radar; lower-level positions are plotted on the upper image (good enough for
# the kill/death heatmap, slightly off for the lower bombsite).
MAPS = {
    "de_mirage":   {"pos_x": -3230.0, "pos_y": 1713.0, "scale": 5.000000},
    "de_inferno":  {"pos_x": -2087.0, "pos_y": 3870.0, "scale": 4.900000},
    "de_nuke":     {"pos_x": -3453.0, "pos_y": 2887.0, "scale": 7.000000},
    "de_overpass": {"pos_x": -4831.0, "pos_y": 1781.0, "scale": 5.200000},
    "de_ancient":  {"pos_x": -2953.0, "pos_y": 2164.0, "scale": 5.000000},
    "de_anubis":   {"pos_x": -2796.0, "pos_y": 3328.0, "scale": 5.220000},
    "de_vertigo":  {"pos_x": -3168.0, "pos_y": 1762.0, "scale": 4.000000},
    "de_train":    {"pos_x": -2308.0, "pos_y": 2078.0, "scale": 4.082077},
}
# Every image is 1024x1024 and named "<map>.png"; fill in the shared fields.
for _name, _cal in MAPS.items():
    _cal.setdefault("size", 1024)
    _cal.setdefault("asset", f"{_name}.png")


def map_info(map_name):
    """Calibration dict for `map_name`, or None if we don't have that map."""
    return MAPS.get(map_name)


def world_to_radar(x, y, info):
    """World (x, y) -> radar pixel (px, py) in the image's coordinate space."""
    px = (x - info["pos_x"]) / info["scale"]
    py = (info["pos_y"] - y) / info["scale"]
    return px, py


def radar_data_uri(info):
    """Base64 data: URI for the map's radar image, or None if the file is absent."""
    path = ASSET_DIR / info["asset"]
    if not path.is_file():
        return None
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"
