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

pub fn orbit_camera(
    mut orbit: ResMut<OrbitCam>,
    buttons: Res<ButtonInput<MouseButton>>,
    mut motion: MessageReader<MouseMotion>,
    mut wheel: MessageReader<MouseWheel>,
    mut cam: Query<&mut Transform, With<Camera3d>>,
) {
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
