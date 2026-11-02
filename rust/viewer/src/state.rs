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
    SetShowMap(bool),
    /// Toggle the x-ray overlay (players drawn through walls + smoke, wallhack-style).
    SetXray(bool),
    /// Release the camera back to free orbit.
    SetCamFree,
    /// Put the free camera at a place in Source units, looking along pitch and yaw in degrees.
    SetCamLook { pos: [f32; 3], pitch: f32, yaw: f32 },
    /// Lock the camera to a player slot in first- (true) or third-person (false).
    SetCamFollow { slot: usize, first_person: bool },
}

/// Spectator camera mode. `Free` = the orbit controls; `Follow` locks onto a
/// player slot and drives the camera from their interpolated eye pose — the
/// CS2-demo POV feature (first-person through their eyes, or a third-person chase).
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum CamMode {
    Free,
    Follow { slot: usize, first_person: bool },
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
    /// radar PNG for the greybox ground plane (manifest.radarAsset)
    pub radar_asset: String,
    /// optional glTF/GLB world geometry, relative to the asset root (manifest.mapAsset)
    pub map_asset: Option<String>,
    /// textured CS2 map, relative to the asset root (manifest.mapV2). The page leaves it
    /// out for the "light map" choice and on GPUs without BC textures.
    pub map_v2: Option<String>,
    /// who killed whom in each round, by the round's number (manifest.rounds[].kills)
    pub kills: HashMap<i64, Vec<KillFact>>,
}

/// One kill, as the manifest has it.
#[derive(Clone, Debug)]
pub struct KillFact {
    pub sample: usize,
    pub attacker: String,
    pub victim: String,
    pub weapon: String,
    pub headshot: bool,
}

/// One precomputed shot tracer (built at round load from a `weapon_fire` record
/// plus the shooter's interpolated eye pose): a bright line `a`→`b` flashed
/// briefly at time `t`. Fixed in world space (it does not follow the shooter).
pub struct ShotViz {
    /// shot time, seconds from round start
    pub t: f64,
    /// shooter slot — lets the renderer drop the muzzle flash in that player's own
    /// first-person POV (where the muzzle sits right on the camera).
    pub slot: usize,
    /// muzzle / eye position (scene space)
    pub a: Vec3,
    /// tracer endpoint along the aim (scene space)
    pub b: Vec3,
    pub color: Color,
}

pub struct RoundState {
    pub dec: DecodedRound,
    pub n: usize,
    pub rate: f64,
    pub duration: f64,
    pub utils: Vec<crate::util::UtilItem>,
    pub shots: Vec<ShotViz>,
    /// What each player does besides walking. Waits for the clip file, which loads at its own
    /// pace, and stays None for a model that has no such clips.
    pub anim: Option<crate::player_actions::RoundAnim>,
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
    /// real map geometry shown (true) vs the radar greybox (false)
    pub show_map: bool,
    /// spectator camera mode (free orbit, or following a player's POV)
    pub cam_mode: CamMode,
    /// x-ray overlay active: players drawn on top of walls + smoke (wallhack-style)
    pub xray: bool,
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
            show_map: false,
            cam_mode: CamMode::Free,
            xray: true,
        }
    }
}

/// Marker components for scene entities.
#[derive(Component)]
pub struct GreyboxTag;

/// Root of the spawned glTF map world (children inherit its Visibility).
#[derive(Component)]
pub struct MapTag;

/// The primary scene camera: orbit / spectator drive its transform and it renders
/// the world layer (0). Distinct from the x-ray overlay camera.
#[derive(Component)]
pub struct MainCam;

/// The x-ray overlay camera: renders only the player layer (1) on top of the main
/// view with a cleared depth buffer, so players stay visible through walls + smoke.
#[derive(Component)]
pub struct XrayCam;

#[derive(Component)]
pub struct PlayerBody {
    pub slot: usize,
}

/// Placed on the glTF `AnimationPlayer` entity (a descendant of a `PlayerBody`),
/// linking it back to the player slot so `drive_player_anim` can pick a clip.
#[derive(Component)]
pub struct PlayerAnimSlot(pub usize);

/// Gait + direction memory for `drive_player_anim` (both have hysteresis).
#[derive(Component, Default)]
pub struct PlayerLoco(pub crate::player_anim::Locomotion);

/// On a textured player mesh: its team colour and how far `fade_team_colour` has painted
/// it, so the material is only rewritten when that changes.
#[derive(Component)]
pub struct TeamFade {
    pub team: Color,
    pub level: u8,
    /// how much of the map's light is on it, in steps of its own
    pub light_level: u8,
}

/// Clips one player can be fading between at once. A new clip reuses the oldest node,
/// so running out only cuts a fade short, and that takes three clip changes in one fade.
pub const CLIP_NODES: usize = 4;

/// The walking and whole-body part of one player's own small animation graph (see
/// player_rig.rs). The next clip is written into the next node rather than each graph holding
/// them all: Bevy walks every graph node for every animated bone every frame, and 78 nodes x
/// 80 bones x 10 players was 2.6 ms a frame in wasm.
#[derive(Component)]
pub struct PlayerClipNodes {
    pub nodes: [AnimationNodeIndex; CLIP_NODES],
    /// Index into `nodes` of the clip playing now.
    pub current: usize,
    pub clip: AssetId<AnimationClip>,
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
