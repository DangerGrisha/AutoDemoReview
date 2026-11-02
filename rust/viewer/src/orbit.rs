//! Minimal orbit camera (drag to orbit, wheel to zoom, right-drag to pan) —
//! stands in for Three.js OrbitControls with the same initial framing.

use bevy::input::mouse::{MouseMotion, MouseScrollUnit, MouseWheel};
use bevy::prelude::*;

#[derive(Resource)]
pub struct OrbitCam {
    pub target: Vec3,
    pub yaw: f32,
    pub pitch: f32,
    pub dist: f32,
}

impl Default for OrbitCam {
    fn default() -> Self {
        // template: camera at target + (0, 58, 58) -> yaw 0, pitch 45deg, dist 58*sqrt(2)
        OrbitCam {
            target: Vec3::ZERO,
            yaw: 0.0,
            pitch: std::f32::consts::FRAC_PI_4,
            dist: 58.0 * std::f32::consts::SQRT_2,
        }
    }
}

impl OrbitCam {
    /// An orbit that puts the camera at `eye`, looking along Source's pitch and yaw.
    pub fn look_from(&mut self, eye: Vec3, pitch: f32, yaw: f32) {
        const DIST: f32 = 5.0;
        let forward = Vec3::new(pitch.cos() * yaw.cos(), -pitch.sin(), -pitch.cos() * yaw.sin());
        self.target = eye + forward * DIST;
        self.pitch = pitch.clamp(-1.54, 1.54);
        self.yaw = (-yaw.cos()).atan2(yaw.sin());
        self.dist = DIST;
    }
}

pub fn orbit_camera(
    viewer: Res<crate::state::Viewer>,
    mut orbit: ResMut<OrbitCam>,
    buttons: Res<ButtonInput<MouseButton>>,
    mut motion: MessageReader<MouseMotion>,
    mut wheel: MessageReader<MouseWheel>,
    mut cam: Query<&mut Transform, With<crate::state::MainCam>>,
) {
    // While spectating a player, `spectator_camera` owns the transform.
    if viewer.cam_mode != crate::state::CamMode::Free {
        return;
    }
    let mut delta = Vec2::ZERO;
    for ev in motion.read() {
        delta += ev.delta;
    }
    if buttons.pressed(MouseButton::Left) && delta != Vec2::ZERO {
        orbit.yaw -= delta.x * 0.005;
        orbit.pitch = (orbit.pitch + delta.y * 0.005).clamp(-1.54, 1.54);
    } else if buttons.pressed(MouseButton::Right) && delta != Vec2::ZERO {
        // pan in the camera's ground plane, scaled by distance
        let k = orbit.dist * 0.0012;
        let right = Vec3::new(orbit.yaw.cos(), 0.0, -orbit.yaw.sin());
        let fwd = Vec3::new(-orbit.yaw.sin(), 0.0, -orbit.yaw.cos());
        let (dx, dy) = (delta.x, delta.y);
        orbit.target -= right * dx * k;
        orbit.target += fwd * dy * k;
    }
    for ev in wheel.read() {
        let dy = match ev.unit {
            MouseScrollUnit::Line => ev.y * 40.0,
            MouseScrollUnit::Pixel => ev.y,
        };
        orbit.dist = (orbit.dist * (1.0 - dy * 0.002)).clamp(3.0, 400.0);
    }

    if let Ok(mut tf) = cam.single_mut() {
        let (yc, ys) = (orbit.yaw.cos(), orbit.yaw.sin());
        let (pc, ps) = (orbit.pitch.cos(), orbit.pitch.sin());
        let offset = Vec3::new(pc * ys, ps, pc * yc) * orbit.dist;
        *tf = Transform::from_translation(orbit.target + offset)
            .looking_at(orbit.target, Vec3::Y);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_look_puts_the_camera_at_the_eye_facing_along_the_angles() {
        let mut o = OrbitCam::default();
        let eye = Vec3::new(1.0, 2.0, 3.0);
        // yaw 90 in Source is +y, which the scene calls -z; pitch 30 looks down
        o.look_from(eye, 30_f32.to_radians(), 90_f32.to_radians());
        let (yc, ys) = (o.yaw.cos(), o.yaw.sin());
        let (pc, ps) = (o.pitch.cos(), o.pitch.sin());
        let camera = o.target + Vec3::new(pc * ys, ps, pc * yc) * o.dist;
        assert!((camera - eye).length() < 1e-4, "{camera}");
        let forward = (o.target - camera).normalize();
        assert!((forward - Vec3::new(0.0, -0.5, -0.866)).length() < 1e-3, "{forward}");
    }
}
