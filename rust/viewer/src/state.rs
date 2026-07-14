//! Shared viewer state: the JS->Bevy control queue and the main resources.

use std::collections::HashMap;
use std::sync::Mutex;

use bevy::prelude::*;
use c2r3::DecodedRound;

use crate::coords::Calibration;

/// Messages pushed by the wasm-bindgen exports (JS thread) and drained by a Bevy
/// system each frame. Wasm is single-threaded so contention is impossible; the
/// Mutex just satisfies Sync.
pub enum ControlMsg {
    LoadManifest(String),
    LoadRound(Box<DecodedRound>),
    Seek(f64),
    SetPlaying(bool),
    SetSpeed(f64),
    SetFlashFocus(Option<String>),
}

pub static QUEUE: Mutex<Vec<ControlMsg>> = Mutex::new(Vec::new());

pub fn push_control(msg: ControlMsg) {
    QUEUE.lock().unwrap().push(msg);
}

pub struct ManifestData {
    pub scene_scale: f64,
    pub cal: Calibration,
    /// team_num -> color (from manifest.teamColors)
    pub team_colors: HashMap<i64, Color>,
    pub elevation_world_z: Vec<f64>,
    /// groundY = elevationWorldZ[0] * S (scene units) — the template's reference height
    pub ground_y: f32,
}

pub struct RoundState {
    pub dec: DecodedRound,
    pub n: usize,
    pub rate: f64,
    pub duration: f64,
    pub utils: Vec<crate::util::UtilItem>,
}

#[derive(Resource)]
pub struct Viewer {
    pub manifest: Option<ManifestData>,
    pub round: Option<RoundState>,
    pub clock: f64,
    pub playing: bool,
    pub speed: f64,
    pub flash_focus_name: Option<String>,
    /// resolved slot index in the current round whose flash channel drives the overlay
    pub flash_focus: Option<usize>,
}

impl Default for Viewer {
    fn default() -> Self {
        Viewer {
            manifest: None,
            round: None,
            clock: 0.0,
            playing: true,
            speed: 1.0,
            flash_focus_name: None,
            flash_focus: None,
        }
    }
}

/// Marker components for scene entities.
#[derive(Component)]
pub struct GreyboxTag;

#[derive(Component)]
pub struct PlayerBody {
    pub slot: usize,
}

#[derive(Component)]
pub struct PlayerNose {
    pub slot: usize,
}

#[derive(Component)]
pub struct UtilTag;

/// The template's interpolation formulas (shared by players + flash overlay).
pub fn sample_pos(clock: f64, rate: f64, n: usize) -> (usize, usize, f64, f64) {
    let s = (clock * rate).clamp(0.0, (n as f64 - 1.0).max(0.0));
    let i0 = s.floor() as usize;
    let i1 = (i0 + 1).min(n.saturating_sub(1));
    (i0, i1, s - i0 as f64, s)
}

pub fn lerp(a: f64, b: f64, f: f64) -> f64 {
    a + (b - a) * f
}

/// Shortest-path yaw interpolation in degrees (template's angLerp).
pub fn ang_lerp(a: f64, b: f64, f: f64) -> f64 {
    let d = (b - a + 540.0).rem_euclid(360.0) - 180.0;
    a + d * f
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ang_lerp_shortest_path() {
        // 170 -> -170 must go THROUGH 180 (+20 total), not backwards through 0
        assert!((ang_lerp(170.0, -170.0, 0.5) - 180.0).abs() < 1e-9);
        // -170 -> 170 goes through -180
        assert!((ang_lerp(-170.0, 170.0, 0.5) - (-180.0)).abs() < 1e-9);
        // plain case
        assert!((ang_lerp(10.0, 30.0, 0.5) - 20.0).abs() < 1e-9);
        // exactly opposite: JS ((b-a+540)%360)-180 with b-a=180 gives d=+180... wait:
        // (180+540)%360 = 0 -> d = -180 (the convention the JS decoder uses)
        assert!((ang_lerp(0.0, 180.0, 0.5) - (-90.0)).abs() < 1e-9);
    }

    #[test]
    fn sample_clamping() {
        let (i0, i1, f, _s) = sample_pos(0.0, 16.0, 100);
        assert_eq!((i0, i1), (0, 1));
        assert_eq!(f, 0.0);
        // past the end: clamps to n-1
        let (i0, i1, f, _s) = sample_pos(1e9, 16.0, 100);
        assert_eq!((i0, i1), (99, 99));
        assert_eq!(f, 0.0);
        // n = 1
        let (i0, i1, _f, _s) = sample_pos(5.0, 16.0, 1);
        assert_eq!((i0, i1), (0, 0));
    }
}
