//! The free camera, in two ways of moving. Orbit: drag to turn around a point, wheel to
//! zoom, right-drag to pan, as the Three.js page had. Flight: WASD and the mouse, as CS2's
//! own free camera, turning around the camera itself.
//!
//! Both are kept as one orbit (a point, the angles around it, a distance), so switching
//! never moves the camera: in flight the point is only somewhere ahead, and going back to
//! the orbit puts it on what the camera looks at.

use bevy::input::mouse::{MouseMotion, MouseScrollUnit, MouseWheel};
use bevy::prelude::*;

use crate::state::{CamMode, MainCam, Viewer};

#[derive(Resource)]
pub struct OrbitCam {
    pub target: Vec3,
    pub yaw: f32,
    pub pitch: f32,
    pub dist: f32,
    /// Moving as CS2's free camera does, rather than around `target`.
    pub flying: bool,
    /// What the page holds down: forward, right and up, each -1 to 1.
    pub input: Vec3,
    pub fast: bool,
    /// Asked for by the page, done on the next frame, where the camera's place is known.
    pub pending: Option<Switch>,
    /// The middle of the map, which the first view looks at.
    pub home: Vec3,
    /// The page's settings: flight speed and mouse sensitivity as multiples of the
    /// defaults, and the mouse's up and down the other way round.
    pub speed: f32,
    pub sensitivity: f32,
    pub invert_y: bool,
}

/// A change of the free camera that needs to know where the camera is now.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Switch {
    /// Fly on from the view on screen, whatever drove it.
    Fly,
    /// Orbit around what the view on screen is looking at.
    Orbit,
    /// The first view of the map, from above.
    Home,
}

impl Default for OrbitCam {
    fn default() -> Self {
        // template: camera at target + (0, 58, 58) -> yaw 0, pitch 45deg, dist 58*sqrt(2)
        OrbitCam {
            target: Vec3::ZERO,
            yaw: 0.0,
            pitch: std::f32::consts::FRAC_PI_4,
            dist: 58.0 * std::f32::consts::SQRT_2,
            flying: false,
            input: Vec3::ZERO,
            fast: false,
            pending: None,
            home: Vec3::ZERO,
            speed: 1.0,
            sensitivity: 1.0,
            invert_y: false,
        }
    }
}

/// Hammer units a second, and how much faster with Shift held.
const FLY_SPEED: f32 = 700.0;
const FLY_FAST: f32 = 2.5;
/// How far ahead the orbit's point sits while flying, in scene units.
const FLY_AHEAD: f32 = 5.0;
/// Scene units one pixel of the wheel moves the camera in flight.
const FLY_WHEEL: f32 = 0.05;
/// Radians one pixel of the mouse turns the view.
const TURN_PER_PIXEL: f32 = 0.005;
/// What the page may ask for: speed and sensitivity multiples.
pub const SPEED_RANGE: (f32, f32) = (0.25, 4.0);
pub const SENSITIVITY_RANGE: (f32, f32) = (0.25, 3.0);
/// Without anything to look at, the orbit goes around a point this far ahead.
const ORBIT_FALLBACK: f32 = 20.0;
const DIST_MIN: f32 = 3.0;
const DIST_MAX: f32 = 400.0;
const PITCH_MAX: f32 = 1.54;

impl OrbitCam {
    /// The first view of the map: its middle from above at 45 degrees.
    pub fn go_home(&mut self) {
        let keep = OrbitCam {
            home: self.home,
            input: self.input,
            fast: self.fast,
            speed: self.speed,
            sensitivity: self.sensitivity,
            invert_y: self.invert_y,
            ..OrbitCam::default()
        };
        *self = keep;
        self.target = self.home;
    }

    /// An orbit that puts the camera at `eye`, looking along Source's pitch and yaw.
    pub fn look_from(&mut self, eye: Vec3, pitch: f32, yaw: f32) {
        let forward = Vec3::new(pitch.cos() * yaw.cos(), -pitch.sin(), -pitch.cos() * yaw.sin());
        self.target = eye + forward * FLY_AHEAD;
        self.pitch = pitch.clamp(-PITCH_MAX, PITCH_MAX);
        self.yaw = (-yaw.cos()).atan2(yaw.sin());
        self.dist = FLY_AHEAD;
    }

    /// Where the camera is.
    pub fn eye(&self) -> Vec3 {
        self.target - self.forward() * self.dist
    }

    /// Which way it looks.
    pub fn forward(&self) -> Vec3 {
        let (yc, ys) = (self.yaw.cos(), self.yaw.sin());
        let (pc, ps) = (self.pitch.cos(), self.pitch.sin());
        -Vec3::new(pc * ys, ps, pc * yc)
    }

    /// Level, to the right of the view.
    fn right(&self) -> Vec3 {
        Vec3::new(self.yaw.cos(), 0.0, -self.yaw.sin())
    }

    /// The orbit that shows exactly this camera, with its point `dist` ahead.
    pub fn take_view(&mut self, eye: Vec3, forward: Vec3, dist: f32) {
        let f = forward.normalize_or(Vec3::NEG_Z);
        self.pitch = (-f.y).clamp(-1.0, 1.0).asin().clamp(-PITCH_MAX, PITCH_MAX);
        self.yaw = (-f.x).atan2(-f.z);
        self.dist = dist;
        self.target = eye + self.forward() * dist;
    }

    /// The page's settings, kept within what they may be.
    pub fn set_feel(&mut self, speed: f32, sensitivity: f32, invert_y: bool) {
        let fit = |v: f32, (lo, hi): (f32, f32)| if v.is_finite() { v.clamp(lo, hi) } else { 1.0 };
        self.speed = fit(speed, SPEED_RANGE);
        self.sensitivity = fit(sensitivity, SENSITIVITY_RANGE);
        self.invert_y = invert_y;
    }

    /// Turn by a mouse movement in pixels: around the point when orbiting, around the
    /// camera itself in flight. The mouse to the right turns the view right, down looks
    /// down, unless the settings turn that round.
    pub fn turn(&mut self, delta: Vec2) {
        let eye = self.eye();
        let k = TURN_PER_PIXEL * self.sensitivity;
        let down = if self.invert_y { -delta.y } else { delta.y };
        self.yaw -= delta.x * k;
        self.pitch = (self.pitch + down * k).clamp(-PITCH_MAX, PITCH_MAX);
        if self.flying {
            self.target = eye + self.forward() * self.dist;
        }
    }

    /// Move for `dt` seconds with what is held. Forward follows the view up and down, as
    /// CS2's free camera does; up is the world's.
    pub fn fly(&mut self, dt: f32, scene_scale: f32) {
        if self.input == Vec3::ZERO {
            return;
        }
        let speed = FLY_SPEED * self.speed * if self.fast { FLY_FAST } else { 1.0 } * scene_scale;
        let way = self.forward() * self.input.x + self.right() * self.input.y + Vec3::Y * self.input.z;
        self.target += way.normalize_or_zero() * speed * dt;
    }

    pub fn camera(&self) -> Transform {
        Transform::from_translation(self.eye()).looking_at(self.target, Vec3::Y)
    }
}

/// How far ahead the camera's view meets something: the map's solid space when there is
/// one, else the ground, else nothing. Scene units.
pub fn looked_at(eye: Vec3, forward: Vec3, scale: f32, ground: f32, space: Option<&crate::space::SpaceGrid>) -> Option<f32> {
    const REACH: f32 = 8000.0;
    if let Some(d) = space.and_then(|g| g.ray(eye / scale, forward, REACH, 0.0)) {
        return Some(d * scale);
    }
    if forward.y < -0.05 && eye.y > ground {
        return Some((ground - eye.y) / forward.y);
    }
    None
}

#[allow(clippy::too_many_arguments)]
pub fn orbit_camera(
    mut viewer: ResMut<Viewer>,
    mut orbit: ResMut<OrbitCam>,
    time: Res<Time>,
    buttons: Res<ButtonInput<MouseButton>>,
    mut motion: MessageReader<MouseMotion>,
    mut wheel: MessageReader<MouseWheel>,
    mut cam: Query<&mut Transform, With<MainCam>>,
    spaces: Res<Assets<crate::space::SpaceAsset>>,
    map_space: Res<crate::space::MapSpace>,
) {
    let Ok(mut tf) = cam.single_mut() else { return };
    let (scale, ground) = viewer.manifest.as_ref().map_or((0.02, 0.0), |m| (m.scene_scale as f32, m.ground_y));
    if let Some(switch) = orbit.pending.take() {
        let (eye, forward) = (tf.translation, tf.forward().as_vec3());
        match switch {
            Switch::Fly => {
                orbit.take_view(eye, forward, FLY_AHEAD);
                orbit.flying = true;
            }
            Switch::Orbit => {
                let dist = looked_at(eye, forward, scale, ground, map_space.get(&spaces)).unwrap_or(ORBIT_FALLBACK);
                orbit.take_view(eye, forward, dist.clamp(DIST_MIN, DIST_MAX));
                orbit.flying = false;
            }
            Switch::Home => orbit.go_home(),
        }
        viewer.cam_mode = CamMode::Free;
    }
    // Holding a key to fly takes the camera from whoever had it.
    if orbit.input != Vec3::ZERO && (viewer.cam_mode != CamMode::Free || !orbit.flying) {
        if viewer.cam_mode != CamMode::Free {
            orbit.take_view(tf.translation, tf.forward().as_vec3(), FLY_AHEAD);
            viewer.cam_mode = CamMode::Free;
        }
        orbit.flying = true;
    }
    // While spectating a player, `spectator_camera` owns the transform.
    if viewer.cam_mode != CamMode::Free {
        motion.clear();
        wheel.clear();
        return;
    }
    let mut delta = Vec2::ZERO;
    for ev in motion.read() {
        delta += ev.delta;
    }
    let pan = buttons.pressed(MouseButton::Right) && !orbit.flying;
    if delta != Vec2::ZERO && (buttons.pressed(MouseButton::Left) || (buttons.pressed(MouseButton::Right) && orbit.flying)) {
        orbit.turn(delta);
    } else if pan && delta != Vec2::ZERO {
        // pan in the camera's ground plane, scaled by distance
        let k = orbit.dist * 0.0012;
        let right = orbit.right();
        let fwd = Vec3::new(-orbit.yaw.sin(), 0.0, -orbit.yaw.cos());
        orbit.target -= right * delta.x * k;
        orbit.target += fwd * delta.y * k;
    }
    for ev in wheel.read() {
        let dy = match ev.unit {
            MouseScrollUnit::Line => ev.y * 40.0,
            MouseScrollUnit::Pixel => ev.y,
        };
        if orbit.flying {
            let step = orbit.forward() * dy * FLY_WHEEL * orbit.speed;
            orbit.target += step;
        } else {
            orbit.dist = (orbit.dist * (1.0 - dy * 0.002)).clamp(DIST_MIN, DIST_MAX);
        }
    }
    // a hidden tab comes back with one long frame
    orbit.fly(time.delta_secs().min(0.1), scale);
    let want = orbit.camera();
    if *tf != want {
        *tf = want;
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
        assert!((o.eye() - eye).length() < 1e-4, "{}", o.eye());
        assert!((o.forward() - Vec3::new(0.0, -0.5, -0.866)).length() < 1e-3, "{}", o.forward());
    }

    #[test]
    fn taking_a_view_keeps_the_camera_where_it_was() {
        let mut o = OrbitCam::default();
        let (eye, forward) = (Vec3::new(4.0, 3.0, -2.0), Vec3::new(1.0, -0.4, 0.3).normalize());
        o.take_view(eye, forward, 12.0);
        assert!((o.eye() - eye).length() < 1e-4);
        assert!((o.forward() - forward).length() < 1e-4);
        let shown = o.camera();
        assert!((shown.translation - eye).length() < 1e-4);
        assert!((shown.forward().as_vec3() - forward).length() < 1e-4);
    }

    #[test]
    fn in_flight_the_camera_turns_on_the_spot() {
        let mut o = OrbitCam::default();
        o.take_view(Vec3::new(0.0, 5.0, 0.0), Vec3::NEG_Z, FLY_AHEAD);
        o.flying = true;
        o.turn(Vec2::new(120.0, 40.0));
        assert!((o.eye() - Vec3::new(0.0, 5.0, 0.0)).length() < 1e-4, "{}", o.eye());
        let f = o.forward();
        assert!(f.x > 0.3, "mouse to the right turns right: {f}");
        assert!(f.y < -0.1, "mouse down looks down: {f}");
        // the orbit turns around its point instead, so the camera itself moves
        o.flying = false;
        let before = o.eye();
        o.turn(Vec2::new(120.0, 0.0));
        assert!((o.eye() - before).length() > 1.0);
    }

    #[test]
    fn flight_goes_where_the_view_looks_at_its_speed() {
        let mut o = OrbitCam::default();
        o.take_view(Vec3::ZERO, Vec3::new(0.0, -1.0, -1.0).normalize(), FLY_AHEAD);
        o.flying = true;
        o.input = Vec3::new(1.0, 0.0, 0.0);
        o.fly(1.0, 0.02);
        let moved = o.eye();
        assert!((moved.length() - FLY_SPEED * 0.02).abs() < 1e-3, "{moved}");
        assert!(moved.y < 0.0 && moved.z < 0.0, "forward follows the view down: {moved}");
        // up is the world's, whatever the view does
        o.input = Vec3::new(0.0, 0.0, 1.0);
        let before = o.eye();
        o.fly(0.5, 0.02);
        let up = o.eye() - before;
        assert!(up.x.abs() < 1e-5 && up.z.abs() < 1e-5 && up.y > 0.0, "{up}");
        // and strafing stays level
        o.input = Vec3::new(0.0, 1.0, 0.0);
        let before = o.eye();
        o.fly(0.5, 0.02);
        let side = o.eye() - before;
        assert!(side.y.abs() < 1e-5 && side.x > 0.0, "right of a view down -z is +x: {side}");
    }

    #[test]
    fn the_settings_change_speed_turning_and_up_and_down() {
        let mut o = OrbitCam::default();
        o.take_view(Vec3::ZERO, Vec3::NEG_Z, FLY_AHEAD);
        o.flying = true;
        o.set_feel(2.0, 0.5, true);
        o.input = Vec3::new(1.0, 0.0, 0.0);
        o.fly(1.0, 0.02);
        assert!((o.eye().length() - 2.0 * FLY_SPEED * 0.02).abs() < 1e-3, "twice as fast");
        let pitch = o.pitch;
        o.turn(Vec2::new(0.0, 100.0));
        assert!((o.pitch - (pitch - 100.0 * TURN_PER_PIXEL * 0.5)).abs() < 1e-5, "half as far, and up");
        // what the page cannot mean stays sane, and the first view keeps the settings
        o.set_feel(f32::NAN, 99.0, false);
        assert_eq!((o.speed, o.sensitivity), (1.0, SENSITIVITY_RANGE.1));
        o.go_home();
        assert_eq!(o.sensitivity, SENSITIVITY_RANGE.1);
    }

    #[test]
    fn the_orbit_goes_around_the_ground_ahead_without_a_map_grid() {
        let eye = Vec3::new(0.0, 10.0, 0.0);
        let down = Vec3::new(0.0, -1.0, -1.0).normalize();
        let d = looked_at(eye, down, 0.02, 2.0, None).unwrap();
        assert!((eye + down * d).y - 2.0 < 1e-4, "on the ground");
        assert!(looked_at(eye, Vec3::NEG_Z, 0.02, 2.0, None).is_none(), "looking level meets no ground");
        assert!(looked_at(Vec3::new(0.0, 1.0, 0.0), down, 0.02, 2.0, None).is_none(), "under the ground");
    }
}
