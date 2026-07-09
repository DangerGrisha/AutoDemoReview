//! THE coordinate transform — Rust port of `web/coords3d.mjs` (single source of truth).
//!
//! Source engine: X=east, Y=north, Z=up, right-handed, units ~inches.
//! Bevy (like Three.js): X=right, Y=up, Z=toward viewer, right-handed.
//! `scene = (x, z, -y) * SCENE_SCALE` — a pure axis relabel; the `-y` keeps the basis
//! right-handed so callouts are never mirrored.
//!
//! Derivation check (not a blind port): mapping (east, north, up) -> (right, up, toward
//! viewer) sends east->+X, up->+Y, north->-Z. det(basis) = +1 (rotation, no reflection):
//! e1=(1,0,0), e2=(0,0,-1), e3=(0,1,0) => det = e1 . (e2 x e3) = (1,0,0).((0,0,-1)x(0,1,0))
//! = (1,0,0).(1,0,0) = +1. Chirality preserved.

/// Uniform scene scale; the manifest's `sceneScale` field is the runtime source of
/// truth — this constant is only the compiled-in default and must match it.
pub const SCENE_SCALE: f64 = 0.02;

pub fn world_to_scene(x: f64, y: f64, z: f64, s: f64) -> [f32; 3] {
    [(x * s) as f32, (z * s) as f32, (-y * s) as f32]
}

pub fn world_dir_to_scene(dx: f64, dy: f64, dz: f64) -> [f32; 3] {
    [dx as f32, dz as f32, (-dy) as f32]
}

/// Rotation about scene +Y (radians) for a mesh whose local forward is +X.
/// World yaw is degrees CCW from +X(east): forward = (cos yaw, sin yaw, 0) which maps
/// to scene (cos yaw, 0, -sin yaw) = +X rotated by +yaw about +Y in a right-handed
/// basis, so the angle passes through unchanged (degrees -> radians only).
pub fn yaw_to_scene_rotation_y(yaw_deg: f64) -> f32 {
    (yaw_deg * std::f64::consts::PI / 180.0) as f32
}

#[derive(Clone, Copy, Debug)]
pub struct Calibration {
    pub pos_x: f64,
    pub pos_y: f64,
    pub scale: f64,
    pub size: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct GroundRect {
    /// scene-space center of the radar rect (y = 0)
    pub center: [f32; 3],
    /// scene-space half extent along +X (world X span)
    pub half_x: f32,
    /// scene-space half extent along +Z (world Y span)
    pub half_z: f32,
}

/// The radar image covers world X in [pos_x, pos_x + size*scale] and
/// world Y in [pos_y - size*scale, pos_y]. (Image +Y/top = world north = scene -Z.)
pub fn radar_ground_rect(cal: &Calibration, s: f64) -> GroundRect {
    let span_x = cal.size * cal.scale;
    let span_y = cal.size * cal.scale;
    let mid_x = cal.pos_x + span_x / 2.0;
    let mid_y = cal.pos_y - span_y / 2.0;
    GroundRect {
        center: world_to_scene(mid_x, mid_y, 0.0, s),
        half_x: (span_x / 2.0 * s) as f32,
        half_z: (span_y / 2.0 * s) as f32,
    }
}

/// World (x, y) -> radar pixel in the 1024x1024 image (for numeric verification).
pub fn world_to_radar(x: f64, y: f64, cal: &Calibration) -> (f64, f64) {
    ((x - cal.pos_x) / cal.scale, (cal.pos_y - y) / cal.scale)
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIRAGE: Calibration = Calibration {
        pos_x: -3230.0,
        pos_y: 1713.0,
        scale: 5.0,
        size: 1024.0,
    };

    #[test]
    fn axis_relabel() {
        // east
        assert_eq!(world_to_scene(100.0, 0.0, 0.0, 0.02), [2.0, 0.0, 0.0]);
        // north -> scene -Z
        assert_eq!(world_to_scene(0.0, 100.0, 0.0, 0.02), [0.0, 0.0, -2.0]);
        // up -> scene +Y
        assert_eq!(world_to_scene(0.0, 0.0, 100.0, 0.02), [0.0, 2.0, 0.0]);
    }

    #[test]
    fn chirality_preserved() {
        // east x north = up must hold after the transform (right-handed, no mirror)
        let e = world_dir_to_scene(1.0, 0.0, 0.0);
        let n = world_dir_to_scene(0.0, 1.0, 0.0);
        let u = world_dir_to_scene(0.0, 0.0, 1.0);
        let cross = [
            e[1] * n[2] - e[2] * n[1],
            e[2] * n[0] - e[0] * n[2],
            e[0] * n[1] - e[1] * n[0],
        ];
        assert_eq!(cross, u);
    }

    #[test]
    fn yaw_rotation() {
        // yaw 0 (facing east/+X): rotating scene +X by the result must give +X
        let r0 = yaw_to_scene_rotation_y(0.0);
        assert_eq!(r0, 0.0);
        // yaw 90 (facing north): forward should be scene (0, 0, -1)
        let r = yaw_to_scene_rotation_y(90.0) as f64;
        let fx = r.cos();
        let fz = -r.sin();
        // f32 precision: cos(pi/2) is ~4e-8, not exactly 0
        assert!((fx - 0.0).abs() < 1e-6 && (fz - (-1.0)).abs() < 1e-6);
    }

    #[test]
    fn mirage_ground_rect() {
        // world span = 1024 * 5 = 5120 per axis
        let rect = radar_ground_rect(&MIRAGE, 0.02);
        assert!((rect.half_x - 51.2).abs() < 1e-6);
        assert!((rect.half_z - 51.2).abs() < 1e-6);
        // center: world (-3230 + 2560, 1713 - 2560) = (-670, -847)
        assert!((rect.center[0] - (-670.0 * 0.02) as f32).abs() < 1e-6);
        assert!((rect.center[2] - (847.0 * 0.02) as f32).abs() < 1e-6);
    }

    #[test]
    fn radar_projection_matches_2d_pipeline() {
        // Corner checks: world (pos_x, pos_y) is radar pixel (0, 0)
        assert_eq!(world_to_radar(-3230.0, 1713.0, &MIRAGE), (0.0, 0.0));
        // Far corner is (1024, 1024)
        assert_eq!(world_to_radar(-3230.0 + 5120.0, 1713.0 - 5120.0, &MIRAGE), (1024.0, 1024.0));
        // A scene position must project back consistently: scene->world->radar
        let world = (-670.0, -847.0, 0.0); // rect center
        let (px, py) = world_to_radar(world.0, world.1, &MIRAGE);
        assert_eq!((px, py), (512.0, 512.0));
    }
}
