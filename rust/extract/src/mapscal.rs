//! Port of `src/demoreview/maps.py`: radar calibration + world->pixel projection.
//! Calibration is sourced from awpy's map data; only de_mirage is calibrated.

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MapInfo {
    pub pos_x: f64,
    pub pos_y: f64,
    pub scale: f64,
    pub size: i64,
    pub asset: &'static str,
}

pub fn map_info(map_name: &str) -> Option<MapInfo> {
    match map_name {
        "de_mirage" => Some(MapInfo {
            pos_x: -3230.0,
            pos_y: 1713.0,
            scale: 5.0,
            size: 1024,
            asset: "de_mirage.png",
        }),
        _ => None,
    }
}

/// World (x, y) -> radar pixel (px, py) in the 1024x1024 image's coordinate space.
pub fn world_to_radar(x: f64, y: f64, info: &MapInfo) -> (f64, f64) {
    ((x - info.pos_x) / info.scale, (info.pos_y - y) / info.scale)
}
