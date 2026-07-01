"""Map radar calibration + world->pixel projection, and radar image loading.

Calibration (pos_x, pos_y, scale) and radar images are sourced from awpy's map
data; they are consistent with each other. To support more maps, drop the radar
PNG in assets/ and add an entry here.
"""

import base64
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent / "assets"

# radar size is 1024x1024 for all these images.
MAPS = {
    "de_mirage": {"pos_x": -3230.0, "pos_y": 1713.0, "scale": 5.0,
                  "size": 1024, "asset": "de_mirage.png"},
}


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
