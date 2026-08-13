//! Port of `src/demoreview/maps.py`: radar calibration + world->pixel projection.
//! Calibration is sourced from awpy's map data and matches the 1024x1024 radar
//! PNGs in `src/demoreview/assets/`. Only single-level maps with no radar
//! rotation/zoom are listed (multi-level maps use their UPPER radar).

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MapInfo {
    pub pos_x: f64,
    pub pos_y: f64,
    pub scale: f64,
    pub size: i64,
    pub asset: &'static str,
}

pub fn map_info(map_name: &str) -> Option<MapInfo> {
    let (pos_x, pos_y, scale, asset) = match map_name {
        "de_mirage" => (-3230.0, 1713.0, 5.0, "de_mirage.png"),
        "de_inferno" => (-2087.0, 3870.0, 4.9, "de_inferno.png"),
        "de_nuke" => (-3453.0, 2887.0, 7.0, "de_nuke.png"),
        "de_overpass" => (-4831.0, 1781.0, 5.2, "de_overpass.png"),
        "de_ancient" => (-2953.0, 2164.0, 5.0, "de_ancient.png"),
        "de_anubis" => (-2796.0, 3328.0, 5.22, "de_anubis.png"),
        "de_vertigo" => (-3168.0, 1762.0, 4.0, "de_vertigo.png"),
        "de_train" => (-2308.0, 2078.0, 4.082077, "de_train.png"),
        _ => return None,
    };
    Some(MapInfo { pos_x, pos_y, scale, size: 1024, asset })
}

/// World (x, y) -> radar pixel (px, py) in the 1024x1024 image's coordinate space.
pub fn world_to_radar(x: f64, y: f64, info: &MapInfo) -> (f64, f64) {
    ((x - info.pos_x) / info.scale, (info.pos_y - y) / info.scale)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The 8 active-duty maps from src/demoreview/maps.py all resolve.
    #[test]
    fn all_eight_maps_calibrated() {
        for name in [
            "de_mirage", "de_inferno", "de_nuke", "de_overpass",
            "de_ancient", "de_anubis", "de_vertigo", "de_train",
        ] {
            let info = map_info(name).expect(name);
            assert_eq!(info.size, 1024);
            assert_eq!(info.asset, format!("{name}.png"));
        }
        assert!(map_info("de_dust2").is_none()); // rotated radar: unsupported
    }

    /// de_mirage constants must stay in lockstep with the viewer's coords tests.
    #[test]
    fn mirage_matches_viewer_calibration() {
        let m = map_info("de_mirage").unwrap();
        assert_eq!((m.pos_x, m.pos_y, m.scale), (-3230.0, 1713.0, 5.0));
    }

    /// Radar projection corners for a non-mirage map: world (pos_x, pos_y) is
    /// pixel (0, 0); one image-span east/south is pixel (1024, 1024).
    #[test]
    fn inferno_radar_corners() {
        let m = map_info("de_inferno").unwrap();
        assert_eq!(world_to_radar(m.pos_x, m.pos_y, &m), (0.0, 0.0));
        let span = m.size as f64 * m.scale;
        assert_eq!(world_to_radar(m.pos_x + span, m.pos_y - span, &m), (1024.0, 1024.0));
    }
}
