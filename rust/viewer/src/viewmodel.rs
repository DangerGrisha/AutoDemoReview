//! First person as CS2 draws it: the player's own arms and a weapon that moves, on their own
//! camera, so they have their own field of view and never go through a wall.
//!
//! The arms come from each side's model and the weapons from `viewmodel/<file>.glb`, built by
//! `tools/prepare_viewmodel.py` and loaded the first time someone looks through a player's eyes.
//! While any of it is missing `weapons_viz::update_viewmodel` keeps showing the still weapon.
//!
//! Which clip is on is worked out from the replay's data alone with no memory of earlier frames,
//! like `player_actions`, so a scrub or a pause lands on the right pose.

use std::collections::{HashMap, HashSet};

use bevy::animation::graph::AnimationGraphHandle;
use bevy::animation::{AnimatedBy, AnimationPlayer, AnimationTargetId};
use bevy::asset::LoadState;
use bevy::camera::visibility::{NoFrustumCulling, RenderLayers};
use bevy::gltf::{Gltf, GltfLoaderSettings, GltfMesh, GltfNode};
use bevy::prelude::*;
use c2r3::{DecodedRound, Track};
use serde_json::Value;

use crate::player_actions;
use crate::player_rig::held_time;
use crate::state::{sample_pos, CamMode, KillFact, MainCam, RoundState, Viewer, XrayCam};
use crate::textured_map::MapLight;
use crate::weapons_viz::{current_tag, RealWeapons, WeaponPart};

/// Render layer of the arms and the weapon, and of the camera that draws them.
pub const FP_LAYER: usize = 2;

const FP_DIR: &str = "models/cs2_v1/viewmodel";
/// The weapon's bones hang from this bone of the arms' skeleton.
const WEAPON_BONE: &str = "wpn";
/// The names from the file's root down to `wpn`: what Bevy hashes into a bone's target.
const WEAPON_BONE_PATH: [&str; 3] = ["viewmodel", "root_motion", WEAPON_BONE];
/// Children of `wpn` with this start are the arms' skeleton's and not the weapon's.
const ARM_SIDE_PREFIX: &str = "wpn";
const METRES_PER_SOURCE_UNIT: f32 = 0.0254;

/// `fov_cs_debug` and `viewmodel_fov`. CS2 gives them for a 4:3 screen, and on a wider one the
/// height stays and the sides grow, so what counts is the vertical angle these make at 4:3.
const WORLD_FOV_4_3: f32 = 90.0;
const VIEWMODEL_FOV_4_3: f32 = 60.0;
/// `viewmodel_offset_x`, `_y`, `_z`: right, forward and up, in Source units.
const VIEWMODEL_OFFSET: Vec3 = Vec3::new(1.0, 1.0, -1.0);
/// The arms reach back past the eye, closer than the world camera's near plane.
const FP_NEAR: f32 = 0.02;

/// What the page's settings may ask for, CS2's own limits: `viewmodel_fov` 54-68,
/// `viewmodel_offset_x` -2 to 2.5, `_y` and `_z` -2 to 2.
pub const VIEWMODEL_FOV_RANGE: (f32, f32) = (54.0, 68.0);
pub const VIEWMODEL_OFFSET_MIN: Vec3 = Vec3::new(-2.0, -2.0, -2.0);
pub const VIEWMODEL_OFFSET_MAX: Vec3 = Vec3::new(2.5, 2.0, 2.0);

/// The arms as the page's settings want them: field of view at 4:3, offset (right,
/// forward, up, in Source units) and whether to draw them at all.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct VmSettings {
    pub fov_4_3: f32,
    pub offset: Vec3,
    pub visible: bool,
}

impl Default for VmSettings {
    fn default() -> Self {
        Self { fov_4_3: VIEWMODEL_FOV_4_3, offset: VIEWMODEL_OFFSET, visible: true }
    }
}

impl VmSettings {
    /// Within CS2's limits; what is not a number keeps the default.
    pub fn fitted(self) -> Self {
        let d = Self::default();
        let fov = if self.fov_4_3.is_finite() { self.fov_4_3 } else { d.fov_4_3 };
        let offset = if self.offset.is_finite() { self.offset } else { d.offset };
        Self {
            fov_4_3: fov.clamp(VIEWMODEL_FOV_RANGE.0, VIEWMODEL_FOV_RANGE.1),
            offset: offset.clamp(VIEWMODEL_OFFSET_MIN, VIEWMODEL_OFFSET_MAX),
            visible: self.visible,
        }
    }
}

/// Set by the page (`set_viewmodel`), taken by `sync_fp_view` on the next frame.
pub static PENDING_VM: std::sync::Mutex<Option<VmSettings>> = std::sync::Mutex::new(None);

/// The grenade leaves the hand this long after the throw clip starts.
const THROW_RELEASE_S: f32 = 0.1;
/// Pulling the pin comes this long before the throw, which is the clip's length.
const PIN_LEAD_S: f32 = 0.97;
/// Crossfade between two clips.
const CLIP_FADE_S: f32 = 0.06;
/// How long a crouch moves the eye.
pub const CROUCH_S: f64 = 0.2;
/// `m_flZoomTime0` in the game's `scripts/weapons.vdata`.
const ZOOM_S: f64 = 0.05;
/// With this much of a flash left the screen is fully white, and below it the white falls off
/// with the square, so a short flash starts off partly white. As in CS:GO's published client
/// code; CS2's own is not public, so this is close and not known to be exact.
const FLASH_FULL_LEFT_S: f64 = 3.0;
/// The screen goes white over this: 255 / 45 per frame at 60 frames a second.
const FLASH_BUILD_UP_S: f64 = 0.094;

fn vertical_fov(fov_4_3_degrees: f32) -> f32 {
    2.0 * ((fov_4_3_degrees / 2.0).to_radians().tan() * 0.75).atan()
}

/// The view angle a scope gives at 4:3: `m_nZoomFOV1` and `m_nZoomFOV2` in the game's
/// `scripts/weapons.vdata`. None for a weapon that does not zoom.
fn scope_fov_4_3(tag: &str, level: u8) -> Option<f32> {
    let (first, second) = match tag {
        "AWP" => (40.0, 10.0),
        "SCOUT" | "SCAR" | "G3" => (40.0, 15.0),
        "AUG" | "SG553" => (45.0, 45.0),
        _ => return None,
    };
    Some(if level >= 2 { second } else { first })
}

/// `m_bHideViewModelWhenZoomed`: a sniper rifle's scope covers the screen. The AUG and the SG 553
/// only bring their sight up, and show the gun as usual.
fn scope_hides_the_gun(tag: &str) -> bool {
    matches!(tag, "AWP" | "SCOUT" | "SCAR" | "G3")
}

/// Is `slot` looking through a scope that covers the screen?
fn sniper_scope_up(r: &RoundState, slot: usize, i0: usize) -> bool {
    r.dec.tracks.get(slot).is_some_and(|tr| tr.flags[i0] & c2r3::F_SCOPED != 0)
        && current_tag(r, slot, i0).is_some_and(scope_hides_the_gun)
}

/// How much a flag has taken effect, 0 to 1, given it takes `seconds` either way.
pub fn flag_share(flags: &[u8], mask: u8, i0: usize, rate: f64, seconds: f64, clock: f64) -> f32 {
    let Some(&now) = flags.get(i0) else {
        return 0.0;
    };
    let on = now & mask != 0;
    let window = (seconds * rate).ceil() as usize + 1;
    let mut share = if on { 1.0 } else { 0.0 };
    for back in 0..window.min(i0) {
        if (flags[i0 - back - 1] & mask != 0) != on {
            let changed = (i0 - back) as f64 / rate;
            let done = ((clock - changed) / seconds).clamp(0.0, 1.0);
            share = if on { done } else { 1.0 - done };
            break;
        }
    }
    (share * share * (3.0 - 2.0 * share)) as f32
}

/// How white the screen is for someone flashed. The channel holds one value per flash from the
/// moment it lands, in seconds * 50, and stays up 1.4 times longer than that says, so the time
/// comes from the value and not from where the channel ends. Dead players see nothing.
pub fn flash_alpha(tr: &Track, i0: usize, rate: f64, clock: f64) -> f32 {
    let byte = tr.flash.get(i0).copied().unwrap_or(0);
    if byte == 0 || tr.flags[i0] & c2r3::F_ALIVE == 0 {
        return 0.0;
    }
    let start = (0..i0).rev().take_while(|&k| tr.flash[k] == byte).last().unwrap_or(i0);
    let elapsed = (clock - start as f64 / rate).max(0.0);
    let left = byte as f64 / player_actions::FLASH_BYTE_PER_S as f64 - elapsed;
    if left <= 0.0 {
        return 0.0;
    }
    let fade = if left >= FLASH_FULL_LEFT_S { 1.0 } else { (left / FLASH_FULL_LEFT_S).powi(2) };
    (fade * (elapsed / FLASH_BUILD_UP_S).min(1.0)) as f32
}

// The gun trails the aim and sways with the steps, by the sums in Source's published
// `CalcViewModelLag` and `CalcViewmodelBob`. CS2's own are not public, so this is close and not
// known to be exact. Both are worked out from the track, so a pause holds them still.
/// `cl_wpn_sway_scale`: Source units of trail per radian the aim is ahead.
const TRAIL_UNITS_PER_RADIAN: f64 = 1.6;
/// The trailing aim catches up by this share of the difference per second.
const TRAIL_CATCH_UP_PER_S: f64 = 5.0;
const TRAIL_LOOK_BACK_S: f64 = 0.5;
const TRAIL_MAX_RADIANS: f64 = 1.5;
/// `cl_bobcycle`: one left-to-right sway, and the up-and-down goes twice as fast.
const SWAY_CYCLE_S: f64 = 0.98;
const SWAY_PER_SPEED: f64 = 0.005;
const SWAY_MAX_SPEED: f64 = 250.0;

fn wrapped_degrees(deg: f64) -> f64 {
    (deg + 180.0).rem_euclid(360.0) - 180.0
}

/// How far the gun is off its place: `(right, up, forward)` in Source units and
/// `(pitch, yaw, roll)` in degrees, as Source counts them.
fn gun_motion(tr: &Track, n: usize, i0: usize, rate: f64, clock: f64) -> (Vec3, Vec3) {
    let (mut ahead_yaw, mut ahead_pitch, mut weights) = (0.0, 0.0, 0.0);
    let reach = (TRAIL_LOOK_BACK_S * rate).ceil() as usize;
    for back in 0..=reach.min(i0) {
        let weight = (-TRAIL_CATCH_UP_PER_S * back as f64 / rate).exp();
        ahead_yaw += weight * wrapped_degrees(tr.yaw[i0] - tr.yaw[i0 - back]);
        ahead_pitch += weight * (tr.pitch[i0] - tr.pitch[i0 - back]);
        weights += weight;
    }
    let trail = |ahead: f64| {
        (ahead / weights).to_radians().clamp(-TRAIL_MAX_RADIANS, TRAIL_MAX_RADIANS) * TRAIL_UNITS_PER_RADIAN
    };

    let (speed, _, _) = crate::player_anim::ground_motion(tr, i0, n, rate);
    let amount = speed.min(SWAY_MAX_SPEED) * SWAY_PER_SPEED;
    let wave = |cycle: f64| amount * (0.3 + 0.7 * (std::f64::consts::TAU * clock / cycle).sin());
    let (vertical, lateral) = (wave(SWAY_CYCLE_S / 2.0), wave(SWAY_CYCLE_S));

    let offset = Vec3::new(
        (trail(ahead_yaw) + lateral * 0.8) as f32,
        (trail(ahead_pitch * crate::PITCH_SIGN as f64) + vertical * 0.1) as f32,
        (vertical * 0.1) as f32,
    );
    let turn = Vec3::new((-vertical * 0.4) as f32, (-lateral * 0.3) as f32, (vertical * 0.5) as f32);
    (offset, turn)
}

// --- which clip is on -------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum ClipKind {
    Draw,
    Idle,
    Shoot,
    Shoot2,
    Reload,
}

impl ClipKind {
    const ALL: [ClipKind; 5] = [Self::Draw, Self::Idle, Self::Shoot, Self::Shoot2, Self::Reload];

    fn file_name(self) -> &'static str {
        match self {
            Self::Draw => "draw",
            Self::Idle => "idle",
            Self::Shoot => "shoot",
            Self::Shoot2 => "shoot2",
            Self::Reload => "reload",
        }
    }
}

/// A clip that starts at a moment.
#[derive(Clone, Copy, PartialEq, Debug)]
struct VmEvent {
    t: f32,
    kind: ClipKind,
}

/// One clip and how far along it is.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct VmClip {
    kind: ClipKind,
    time: f32,
    weight: f32,
}

/// One player's round.
#[derive(Debug, Default)]
struct FpTimeline {
    /// by start
    events: Vec<VmEvent>,
    /// When the held weapon last changed: what came before is another weapon's.
    draws: Vec<f32>,
    /// `(t, level)` for each scope change
    scope: Vec<(f32, u8)>,
}

impl FpTimeline {
    fn build(dec: &DecodedRound, slot: usize, kills: &[KillFact]) -> Self {
        let Some(tr) = dec.tracks.get(slot) else {
            return Self::default();
        };
        let (n, rate) = (dec.n_samples, dec.sample_rate);
        if n == 0 || rate <= 0.0 {
            return Self::default();
        }
        let seconds = |s: f64| (s / rate) as f32;
        let alive = |i: usize| tr.flags[i] & c2r3::F_ALIVE != 0;
        let mut events = Vec::new();
        let mut draws = Vec::new();
        for i in 1..n {
            if tr.weapon[i] != tr.weapon[i - 1] && alive(i) && alive(i - 1) {
                let t = seconds(i as f64);
                draws.push(t);
                events.push(VmEvent { t, kind: ClipKind::Draw });
            }
        }
        // Shots take turns on two clips: the Dual Berettas have one per hand, and on any other
        // gun it is the same clip twice, so that a burst can crossfade from shot to shot.
        let shots = player_actions::shot_samples(dec, slot).into_iter().filter(|s| {
            let tag = dec.weapon_table.get(tr.weapon[(*s as usize).min(n - 1)] as usize);
            tag.is_some_and(|tag| !matches!(player_actions::anim_tag(tag), "KNIFE" | "GRENADE" | "MOLOTOV" | "C4"))
        });
        for (k, s) in shots.enumerate() {
            let kind = if k % 2 == 0 { ClipKind::Shoot } else { ClipKind::Shoot2 };
            events.push(VmEvent { t: seconds(s), kind });
        }
        for s in player_actions::knife_hit_samples(dec, slot, kills) {
            let t = (seconds(s) - player_actions::KNIFE_HIT_S).max(0.0);
            events.push(VmEvent { t, kind: ClipKind::Shoot });
        }
        for s in player_actions::throw_samples(dec, slot) {
            let t = (seconds(s) - THROW_RELEASE_S).max(0.0);
            events.push(VmEvent { t: (t - PIN_LEAD_S).max(0.0), kind: ClipKind::Reload });
            events.push(VmEvent { t, kind: ClipKind::Shoot });
        }
        let mut scope = Vec::new();
        for e in dec.header.get("events").and_then(Value::as_array).into_iter().flatten() {
            if e.get("p").and_then(Value::as_u64) != Some(slot as u64) {
                continue;
            }
            let Some(s) = e.get("s").and_then(Value::as_f64) else { continue };
            let i = (s as usize).min(n - 1);
            match e.get("k").and_then(Value::as_str) {
                Some("reload") => events.push(VmEvent { t: seconds(s), kind: ClipKind::Reload }),
                Some("plant") => {
                    events.push(VmEvent { t: seconds(s), kind: ClipKind::Shoot });
                    // Moving away gives the plant up, and a real one ends in a weapon change.
                    let moved = (i + 1..n).find(|&j| {
                        (tr.x[j] - tr.x[i]).hypot(tr.y[j] - tr.y[i]) > player_actions::PLANT_ABORT_UNITS
                    });
                    if let Some(j) = moved {
                        events.push(VmEvent { t: seconds(j as f64), kind: ClipKind::Idle });
                    }
                }
                Some("zoom") => {
                    // The flag moves within a few samples of the event.
                    let around = (0.15 * rate) as usize;
                    let scoped = |k: usize| tr.flags[k.min(n - 1)] & c2r3::F_SCOPED != 0;
                    let level = match (scoped(i.saturating_sub(around)), scoped(i + around)) {
                        (true, true) => 2,
                        (_, true) => 1,
                        _ => 0,
                    };
                    scope.push((seconds(s), level));
                }
                _ => {}
            }
        }
        events.sort_by(|a, b| a.t.total_cmp(&b.t));
        Self { events, draws, scope }
    }

    fn scope_level(&self, t: f32) -> u8 {
        let after = self.scope.partition_point(|e| e.0 <= t);
        self.scope[..after].last().map_or(1, |e| e.1.max(1))
    }

    /// The clip on at `t`, how far along, and the moment it became the one on. A clip this weapon
    /// lacks does not count, and `Idle` loops from the draw on.
    fn state_at(&self, t: f32, length: &dyn Fn(ClipKind) -> Option<f32>) -> (ClipKind, f32, f32) {
        let after = self.draws.partition_point(|d| *d <= t);
        let since = self.draws[..after].last().copied().unwrap_or(0.0);
        let started = self.events.partition_point(|e| e.t <= t);
        let last = self.events[..started]
            .iter()
            .rev()
            .take_while(|e| e.t >= since)
            .find_map(|e| length(e.kind).map(|len| (e, len)));
        let idle = |switched: f32| {
            let looped = length(ClipKind::Idle).filter(|l| *l > 0.0).map_or(0.0, |l| (t - since).rem_euclid(l));
            (ClipKind::Idle, looped, switched)
        };
        match last {
            Some((e, len)) if t - e.t < len => (e.kind, t - e.t, e.t),
            Some((e, len)) => idle(e.t + len),
            None => idle(since),
        }
    }

    /// The clips on at `t`: one, or two while the last switch is fading. A draw has no fade, since
    /// what came before it was another weapon.
    fn clips_at(&self, t: f32, length: &dyn Fn(ClipKind) -> Option<f32>) -> [Option<VmClip>; 2] {
        let (kind, time, switched) = self.state_at(t, length);
        let age = t - switched;
        let current = VmClip { kind, time, weight: 1.0 };
        if age >= CLIP_FADE_S || kind == ClipKind::Draw || switched <= 0.0 {
            return [Some(current), None];
        }
        let (old, old_time, _) = self.state_at(switched - 1e-3, length);
        if old == kind {
            return [Some(current), None];
        }
        let weight = age / CLIP_FADE_S;
        let previous = VmClip { kind: old, time: old_time + age, weight: 1.0 - weight };
        [Some(previous), Some(VmClip { weight, ..current })]
    }
}

// --- assets -----------------------------------------------------------------------------------

/// One bone of a weapon's own skeleton.
struct WeaponBone {
    /// From the file's root, which is what Bevy finds an animated bone by.
    path: Vec<Name>,
    parent: Option<usize>,
    transform: Transform,
    mesh: Option<Handle<Mesh>>,
}

struct VmWeapon {
    graph: Handle<AnimationGraph>,
    /// Graph node and length of each clip the file has.
    clips: HashMap<ClipKind, (AnimationNodeIndex, f32)>,
    bones: Vec<WeaponBone>,
    /// A copy of the third-person material that the light can tint, once it is shown.
    material: Option<Handle<StandardMaterial>>,
}

enum WeaponState {
    Loading(Handle<Gltf>),
    Ready(VmWeapon),
    Failed,
}

/// The arms of one side, spawned under the anchor once their file is in.
struct Rig {
    player: Option<Entity>,
    wpn: Option<Entity>,
    material: Option<Handle<StandardMaterial>>,
    weapon: Option<&'static str>,
    weapon_bones: Vec<Entity>,
}

enum ArmsState {
    NotAsked,
    Loading(Handle<Gltf>),
    Spawned(Rig),
    Failed,
}

/// Under the main camera. Everything first person hangs from it.
#[derive(Component)]
pub struct FpAnchor;

/// The camera that draws the arms and the weapon over everything else.
#[derive(Component)]
pub struct FpCam;

#[derive(Component)]
pub struct FpArmsRoot {
    side: usize,
}

#[derive(Resource)]
pub struct FpState {
    enabled: bool,
    /// The real arms and weapon are on screen, so the still weapon stays away.
    pub active: bool,
    arms: [ArmsState; 2],
    weapons: HashMap<&'static str, WeaponState>,
    /// The round, the slot, and that slot's timeline.
    timeline: Option<(i64, usize, FpTimeline)>,
    light_level: Option<[u8; 3]>,
    /// What the world camera shows now: a scope narrows it, and `muzzle_on_screen` needs it.
    world_fov: f32,
    /// A sniper rifle's scope is up, for the page's overlay.
    pub scope_overlay: bool,
    active_side: usize,
    /// The page's settings for the arms.
    pub vm: VmSettings,
}

const SIDE_T: usize = 0;
const SIDE_CT: usize = 1;

fn side_of(team: i64) -> usize {
    if team == crate::scene::TEAM_T {
        SIDE_T
    } else {
        SIDE_CT
    }
}

fn arms_file(side: usize) -> String {
    format!("{FP_DIR}/{}.glb", if side == SIDE_T { "arms_t" } else { "arms_ct" })
}

impl FpState {
    pub fn new(enabled: bool) -> Self {
        Self {
            enabled,
            active: false,
            arms: [ArmsState::NotAsked, ArmsState::NotAsked],
            weapons: HashMap::new(),
            timeline: None,
            light_level: None,
            world_fov: vertical_fov(WORLD_FOV_4_3),
            scope_overlay: false,
            active_side: SIDE_T,
            vm: VmSettings::default(),
        }
    }

    fn ask_for_weapon(&mut self, file: &'static str, asset_server: &AssetServer) {
        self.weapons.entry(file).or_insert_with(|| {
            WeaponState::Loading(
                asset_server
                    .load_builder()
                    .with_settings(|s: &mut GltfLoaderSettings| {
                        s.load_cameras = false;
                        s.load_lights = false;
                    })
                    .load(format!("{FP_DIR}/{file}.glb")),
            )
        });
    }

    /// The muzzle of the gun in the hands, where the first-person camera draws it.
    pub fn muzzle_in_hands(&self, real: &RealWeapons, q_tf: &Query<&GlobalTransform>) -> Option<Vec3> {
        if !self.active {
            return None;
        }
        let ArmsState::Spawned(rig) = &self.arms[self.active_side] else {
            return None;
        };
        let muzzle = real.muzzle_of(rig.weapon?)?;
        Some(q_tf.get(rig.wpn?).ok()?.transform_point(muzzle))
    }

    /// Where the shot leaves the gun, in the world, at the spot on screen where the muzzle shows.
    /// The gun is drawn with its own field of view, so its real place in the world is elsewhere.
    pub fn muzzle_on_screen(
        &self,
        real: &RealWeapons,
        q_tf: &Query<&GlobalTransform>,
        cam: &GlobalTransform,
    ) -> Option<Vec3> {
        if !self.active {
            return None;
        }
        let ArmsState::Spawned(rig) = &self.arms[self.active_side] else {
            return None;
        };
        let muzzle = real.muzzle_of(rig.weapon?)?;
        let at = q_tf.get(rig.wpn?).ok()?.transform_point(muzzle);
        let view = cam.affine().inverse().transform_point3(at);
        let spread = (self.world_fov / 2.0).tan() / (vertical_fov(self.vm.fov_4_3) / 2.0).tan();
        Some(cam.transform_point(Vec3::new(view.x * spread, view.y * spread, view.z)))
    }
}

/// The weapon's own bones from the file, or None until all of it is in.
fn read_weapon_bones(
    gltf: &Gltf,
    nodes: &Assets<GltfNode>,
    meshes: &Assets<GltfMesh>,
) -> Option<Vec<WeaponBone>> {
    let wpn = nodes.get(gltf.named_nodes.get(WEAPON_BONE)?)?;
    let base: Vec<Name> = WEAPON_BONE_PATH.iter().map(|n| Name::new(*n)).collect();
    let mut bones = Vec::new();
    let mut stack: Vec<(Handle<GltfNode>, Option<usize>, Vec<Name>)> =
        wpn.children.iter().rev().map(|c| (c.clone(), None, base.clone())).collect();
    while let Some((handle, parent, above)) = stack.pop() {
        let node = nodes.get(&handle)?;
        if parent.is_none() && node.name.starts_with(ARM_SIDE_PREFIX) {
            continue;
        }
        let mut path = above;
        path.push(Name::new(node.name.clone()));
        let mesh = match &node.mesh {
            Some(m) => Some(meshes.get(m)?.primitives.first()?.mesh.clone()),
            None => None,
        };
        let index = bones.len();
        bones.push(WeaponBone { path: path.clone(), parent, transform: node.transform, mesh });
        for child in node.children.iter().rev() {
            stack.push((child.clone(), Some(index), path.clone()));
        }
    }
    Some(bones)
}

/// Publish each weapon rig as its file arrives. One that fails stays out, and whoever holds it
/// keeps the still weapon.
fn finish_weapons(
    state: &mut FpState,
    asset_server: &AssetServer,
    gltfs: &Assets<Gltf>,
    nodes: &Assets<GltfNode>,
    meshes: &Assets<GltfMesh>,
    clips: &Assets<AnimationClip>,
    graphs: &mut Assets<AnimationGraph>,
) {
    for (file, weapon) in &mut state.weapons {
        let WeaponState::Loading(handle) = weapon else { continue };
        if matches!(asset_server.load_state(&*handle), LoadState::Failed(_)) {
            warn!("first-person rig for {file} did not load; keeping the still weapon");
            *weapon = WeaponState::Failed;
            continue;
        }
        let Some(gltf) = gltfs.get(&*handle) else { continue };
        let Some(bones) = read_weapon_bones(gltf, nodes, meshes) else { continue };
        let lengths: Option<Vec<_>> = ClipKind::ALL
            .iter()
            .filter_map(|kind| gltf.named_animations.get(kind.file_name()).map(|h| (*kind, h)))
            .map(|(kind, h)| clips.get(h).map(|clip| (kind, h.clone(), clip.duration())))
            .collect();
        let Some(lengths) = lengths else { continue };
        if !lengths.iter().any(|(kind, _, _)| *kind == ClipKind::Idle) {
            warn!("first-person rig for {file} has no idle clip; keeping the still weapon");
            *weapon = WeaponState::Failed;
            continue;
        }
        // One small graph per weapon, because Bevy walks every node for every animated bone.
        let mut graph = AnimationGraph::new();
        let blend = graph.add_blend(1.0, graph.root);
        let mut found = HashMap::new();
        for (kind, clip, length) in &lengths {
            found.insert(*kind, (graph.add_clip(clip.clone(), 1.0, blend), *length));
        }
        if !found.contains_key(&ClipKind::Shoot2) {
            if let Some((_, clip, length)) = lengths.iter().find(|(kind, _, _)| *kind == ClipKind::Shoot) {
                found.insert(ClipKind::Shoot2, (graph.add_clip(clip.clone(), 1.0, blend), *length));
            }
        }
        *weapon = WeaponState::Ready(VmWeapon { graph: graphs.add(graph), clips: found, bones, material: None });
    }
}

/// Hook up the arms as their scene appears: the player, the weapon bone and the mesh's layer.
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn setup_fp_rig(
    mut commands: Commands,
    mut state: ResMut<FpState>,
    q_new_players: Query<Entity, Added<AnimationPlayer>>,
    q_new_meshes: Query<
        (Entity, &MeshMaterial3d<StandardMaterial>),
        (Added<MeshMaterial3d<StandardMaterial>>, Without<WeaponPart>),
    >,
    q_childof: Query<&ChildOf>,
    q_roots: Query<&FpArmsRoot>,
    q_children: Query<&Children>,
    q_names: Query<&Name>,
) {
    if !state.enabled {
        return;
    }
    let side_of_entity = |mut e: Entity| -> Option<usize> {
        loop {
            if let Ok(root) = q_roots.get(e) {
                return Some(root.side);
            }
            e = q_childof.get(e).ok()?.parent();
        }
    };
    for e in &q_new_players {
        let Some(side) = side_of_entity(e) else { continue };
        let ArmsState::Spawned(rig) = &mut state.arms[side] else { continue };
        rig.player = Some(e);
        rig.wpn = q_children
            .iter_descendants(e)
            .find(|b| q_names.get(*b).is_ok_and(|n| n.as_str() == WEAPON_BONE));
    }
    for (e, material) in &q_new_meshes {
        let Some(side) = side_of_entity(e) else { continue };
        let ArmsState::Spawned(rig) = &mut state.arms[side] else { continue };
        commands.entity(e).insert((RenderLayers::layer(FP_LAYER), NoFrustumCulling, bevy::light::NotShadowCaster));
        rig.material = Some(material.0.clone());
    }
}

fn spawn_weapon(commands: &mut Commands, rig: &mut Rig, weapon: &VmWeapon, material: &Handle<StandardMaterial>) {
    let (Some(player), Some(wpn)) = (rig.player, rig.wpn) else {
        return;
    };
    let mut spawned: Vec<Entity> = Vec::with_capacity(weapon.bones.len());
    for bone in &weapon.bones {
        let parent = bone.parent.map_or(wpn, |p| spawned[p]);
        let name = bone.path.last().cloned().unwrap_or_else(|| Name::new("bone"));
        let id = commands
            .spawn((
                bone.transform,
                Visibility::Inherited,
                name,
                AnimationTargetId::from_names(bone.path.iter()),
                AnimatedBy(player),
                ChildOf(parent),
            ))
            .id();
        if let Some(mesh) = &bone.mesh {
            commands.spawn((
                Mesh3d(mesh.clone()),
                MeshMaterial3d(material.clone()),
                Transform::IDENTITY,
                WeaponPart,
                RenderLayers::layer(FP_LAYER),
                bevy::light::NotShadowCaster,
                ChildOf(id),
            ));
        }
        spawned.push(id);
    }
    rig.weapon_bones = weapon
        .bones
        .iter()
        .zip(&spawned)
        .filter(|(b, _)| b.parent.is_none())
        .map(|(_, e)| *e)
        .collect();
}

/// Timed clips have speed 0 and get their time written every frame from the replay clock.
fn play_clips(want: &[Option<VmClip>; 2], weapon: &VmWeapon, player: &mut AnimationPlayer) {
    for (kind, (node, length)) in &weapon.clips {
        // the newer of the two, in case a clip follows itself
        match want.iter().rev().flatten().find(|clip| clip.kind == *kind) {
            Some(clip) => {
                player
                    .play(*node)
                    .repeat()
                    .set_speed(0.0)
                    .set_seek_time(held_time(clip.time, *length))
                    .set_weight(clip.weight.max(1e-3));
            }
            None => {
                player.stop(*node);
            }
        }
    }
}

/// Everything first person does in a frame: load what the followed player needs, then show it.
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn drive_first_person(
    mut commands: Commands,
    viewer: Res<Viewer>,
    mut state: ResMut<FpState>,
    real: Res<RealWeapons>,
    map_light: Option<Res<MapLight>>,
    asset_server: Res<AssetServer>,
    gltfs: Res<Assets<Gltf>>,
    gltf_nodes: Res<Assets<GltfNode>>,
    gltf_meshes: Res<Assets<GltfMesh>>,
    clips: Res<Assets<AnimationClip>>,
    mut graphs: ResMut<Assets<AnimationGraph>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut q_anchor: Query<(Entity, &mut Transform, &mut Visibility), With<FpAnchor>>,
    mut q_roots: Query<(&FpArmsRoot, &mut Visibility), Without<FpAnchor>>,
    mut q_cam: Query<&mut Camera, With<FpCam>>,
    mut q_players: Query<&mut AnimationPlayer>,
) {
    let state = &mut *state;
    let Ok((anchor, mut anchor_tf, mut anchor_vis)) = q_anchor.single_mut() else {
        return;
    };
    finish_weapons(state, &asset_server, &gltfs, &gltf_nodes, &gltf_meshes, &clips, &mut graphs);

    let followed = match (viewer.cam_mode, viewer.round.as_ref()) {
        (CamMode::Follow { slot, first_person: true }, Some(r)) if state.enabled && state.vm.visible && r.n > 0 => {
            Some((slot, r))
        }
        _ => None,
    };
    let mut shown = false;
    if let Some((slot, r)) = followed {
        let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
        let team = r.dec.players.get(slot).map_or(0, |p| p.team);
        let side = side_of(team);
        let tr = r.dec.tracks.get(slot);
        let alive = tr.is_some_and(|tr| tr.flags[i0] & c2r3::F_ALIVE != 0);
        let tag = current_tag(r, slot, i0);

        // the arms, the first time this side is looked through
        match &state.arms[side] {
            ArmsState::NotAsked => {
                state.arms[side] = ArmsState::Loading(
                    asset_server
                        .load_builder()
                        .with_settings(|s: &mut GltfLoaderSettings| {
                            s.load_cameras = false;
                            s.load_lights = false;
                        })
                        .load(arms_file(side)),
                );
            }
            ArmsState::Loading(handle) => {
                if matches!(asset_server.load_state(handle), LoadState::Failed(_)) {
                    warn!("first-person arms did not load; keeping the still weapon");
                    state.arms[side] = ArmsState::Failed;
                } else if let Some(scene) = gltfs.get(handle).and_then(|g| g.scenes.first().cloned()) {
                    commands.spawn((
                        WorldAssetRoot(scene),
                        Transform::IDENTITY,
                        Visibility::Hidden,
                        FpArmsRoot { side },
                        ChildOf(anchor),
                    ));
                    state.arms[side] = ArmsState::Spawned(Rig {
                        player: None,
                        wpn: None,
                        material: None,
                        weapon: None,
                        weapon_bones: Vec::new(),
                    });
                }
            }
            _ => {}
        }

        // the round's timeline, and every weapon this player will hold
        let fresh = state.timeline.as_ref().is_none_or(|(round, s, _)| *round != r.dec.round || *s != slot);
        if fresh {
            let kills = viewer
                .manifest
                .as_ref()
                .and_then(|m| m.kills.get(&r.dec.round))
                .map_or(&[][..], Vec::as_slice);
            state.timeline = Some((r.dec.round, slot, FpTimeline::build(&r.dec, slot, kills)));
            let held: HashSet<u8> = tr.map_or(HashSet::new(), |tr| tr.weapon.iter().copied().collect());
            for w in held {
                if let Some(file) = r.dec.weapon_table.get(w as usize).and_then(|tag| real.file_for(tag, team)) {
                    state.ask_for_weapon(file, &asset_server);
                }
            }
        }

        let file = tag.and_then(|tag| real.ready_file(tag, team));
        let rig_ready = matches!(
            &state.arms[side],
            ArmsState::Spawned(rig) if rig.player.is_some() && rig.wpn.is_some() && rig.material.is_some()
        );
        let weapon_ready = file.is_some_and(|f| matches!(state.weapons.get(f), Some(WeaponState::Ready(_))));

        if let (Some(file), true, true, true) = (file, rig_ready, weapon_ready, alive) {
            let ArmsState::Spawned(rig) = &mut state.arms[side] else { unreachable!() };
            let Some(WeaponState::Ready(weapon)) = state.weapons.get_mut(file) else { unreachable!() };
            let mut player = rig.player.and_then(|p| q_players.get_mut(p).ok());
            if rig.weapon != Some(file) {
                for bone in rig.weapon_bones.drain(..) {
                    commands.entity(bone).despawn();
                }
                let material = weapon
                    .material
                    .get_or_insert_with(|| {
                        let base = real.material_of(file).and_then(|h| materials.get(&h).cloned()).unwrap_or_default();
                        materials.add(base)
                    })
                    .clone();
                spawn_weapon(&mut commands, rig, weapon, &material);
                rig.weapon = Some(file);
                if let (Some(entity), Some(player)) = (rig.player, player.as_mut()) {
                    player.stop_all();
                    commands.entity(entity).insert(AnimationGraphHandle(weapon.graph.clone()));
                }
                state.light_level = None;
            }

            // the clips, from the replay's clock
            if let (Some((_, _, timeline)), Some(player)) = (state.timeline.as_ref(), player.as_mut()) {
                let length = |kind: ClipKind| weapon.clips.get(&kind).map(|c| c.1);
                let want = timeline.clips_at(viewer.clock as f32, &length);
                play_clips(&want, weapon, player);
            }

            // the light where the player stands, on both materials
            let scale = viewer.manifest.as_ref().map_or(0.02, |m| m.scene_scale);
            let light = match (&map_light, tr) {
                (Some(l), Some(tr)) if viewer.show_map => {
                    let feet = Vec3::from_array(crate::world_to_scene(tr.x[i0], tr.y[i0], tr.z[i0], scale));
                    l.at(feet / scale as f32 + Vec3::Y * crate::EYE_STAND * 0.5)
                }
                _ => Vec3::ONE,
            };
            let level = [light.x, light.y, light.z].map(|c| (c.max(0.0).sqrt() * 16.0).round() as u8);
            if state.light_level != Some(level) {
                state.light_level = Some(level);
                let tint = Color::linear_rgb(light.x, light.y, light.z);
                for handle in [rig.material.as_ref(), weapon.material.as_ref()].into_iter().flatten() {
                    if let Some(mut mat) = materials.get_mut(handle) {
                        mat.base_color = tint;
                    }
                }
            }

            let scale = scale as f32;
            let (moved, turned) =
                tr.map_or((Vec3::ZERO, Vec3::ZERO), |tr| gun_motion(tr, r.n, i0, r.rate, viewer.clock));
            let at = state.vm.offset + moved;
            // Source's angles in the camera's frame: pitch is down, yaw is left, roll is clockwise.
            let tilt = Quat::from_rotation_y(turned.y.to_radians())
                * Quat::from_rotation_x(-turned.x.to_radians())
                * Quat::from_rotation_z(-turned.z.to_radians());
            let place = Transform {
                translation: Vec3::new(at.x, at.z, -at.y) * scale,
                // The file's frame has +Z forward and +X to the left, so half a turn faces it
                // away from the camera.
                rotation: tilt * Quat::from_rotation_y(std::f32::consts::PI),
                scale: Vec3::splat(scale / METRES_PER_SOURCE_UNIT),
            };
            if *anchor_tf != place {
                *anchor_tf = place;
            }
            let hide_gun = sniper_scope_up(r, slot, i0);
            for (root, mut vis) in &mut q_roots {
                let want_vis = if root.side == side && !hide_gun { Visibility::Inherited } else { Visibility::Hidden };
                if *vis != want_vis {
                    *vis = want_vis;
                }
            }
            state.active_side = side;
            shown = true;
        }
    }

    // nothing to show: stop the clips and let the still weapon back
    if !shown && state.active {
        for arms in &state.arms {
            if let ArmsState::Spawned(rig) = arms {
                if let Some(mut player) = rig.player.and_then(|p| q_players.get_mut(p).ok()) {
                    player.stop_all();
                }
            }
        }
    }
    state.active = shown;
    let vis = if shown { Visibility::Visible } else { Visibility::Hidden };
    if *anchor_vis != vis {
        *anchor_vis = vis;
    }
    if let Ok(mut cam) = q_cam.single_mut() {
        if cam.is_active != shown {
            cam.is_active = shown;
        }
    }
}

/// Give the world camera its field of view, and keep the first-person camera on it.
/// Runs after whatever moved the world camera this frame.
#[allow(clippy::type_complexity)]
pub fn sync_fp_view(
    viewer: Res<Viewer>,
    mut state: ResMut<FpState>,
    q_main: Query<(&Transform, &AmbientLight), (With<MainCam>, Without<FpCam>)>,
    mut q_fp: Query<(&mut Transform, &mut AmbientLight, &mut Projection), (With<FpCam>, Without<MainCam>, Without<XrayCam>)>,
    mut q_proj: Query<&mut Projection, Or<(With<MainCam>, With<XrayCam>)>>,
) {
    if let Some(vm) = PENDING_VM.lock().ok().and_then(|mut p| p.take()) {
        state.vm = vm.fitted();
    }
    let vm_fov = vertical_fov(state.vm.fov_4_3);
    for (_, _, mut projection) in &mut q_fp {
        if let Projection::Perspective(p) = &mut *projection {
            if p.fov != vm_fov {
                p.fov = vm_fov;
            }
        }
    }
    let mut scope_overlay = false;
    let world_fov = match (viewer.cam_mode, viewer.round.as_ref()) {
        (CamMode::Follow { slot, first_person: true }, Some(r)) if r.n > 0 => {
            let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
            scope_overlay = sniper_scope_up(r, slot, i0);
            let open = vertical_fov(WORLD_FOV_4_3);
            let zoomed = r.dec.tracks.get(slot).and_then(|tr| {
                let share = flag_share(&tr.flags, c2r3::F_SCOPED, i0, r.rate, ZOOM_S, viewer.clock);
                if share <= 0.0 {
                    return None;
                }
                let level = state.timeline.as_ref().map_or(1, |(_, s, timeline)| {
                    if *s == slot {
                        timeline.scope_level(viewer.clock as f32)
                    } else {
                        1
                    }
                });
                let fov = scope_fov_4_3(current_tag(r, slot, i0)?, level)?;
                Some(open + (vertical_fov(fov) - open) * share)
            });
            zoomed.unwrap_or(open)
        }
        _ => crate::OVERVIEW_FOV_DEGREES.to_radians(),
    };
    state.world_fov = world_fov;
    state.scope_overlay = scope_overlay;
    for mut projection in &mut q_proj {
        if let Projection::Perspective(p) = &mut *projection {
            if p.fov != world_fov {
                p.fov = world_fov;
            }
        }
    }
    if !state.active {
        return;
    }
    let (Ok((main_tf, main_ambient)), Ok((mut tf, mut ambient, _))) = (q_main.single(), q_fp.single_mut()) else {
        return;
    };
    if *tf != *main_tf {
        *tf = *main_tf;
    }
    if ambient.brightness != main_ambient.brightness || ambient.color != main_ambient.color {
        ambient.brightness = main_ambient.brightness;
        ambient.color = main_ambient.color;
    }
}

/// The camera that draws the arms, off until someone looks through a player's eyes.
pub fn fp_camera_bundle() -> impl Bundle {
    (
        Camera3d::default(),
        Camera {
            order: 2,
            is_active: false,
            clear_color: ClearColorConfig::None,
            ..default()
        },
        Projection::from(PerspectiveProjection {
            fov: vertical_fov(VIEWMODEL_FOV_4_3),
            near: FP_NEAR,
            far: 100.0,
            ..default()
        }),
        Msaa::Sample2, // keep in lockstep with the main camera
        bevy::core_pipeline::tonemapping::Tonemapping::None,
        AmbientLight::default(),
        RenderLayers::layer(FP_LAYER),
        FpCam,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lengths(kind: ClipKind) -> Option<f32> {
        match kind {
            ClipKind::Draw => Some(1.0),
            ClipKind::Idle => Some(2.0),
            ClipKind::Shoot => Some(0.8),
            ClipKind::Shoot2 => None,
            ClipKind::Reload => Some(2.5),
        }
    }

    fn timeline(events: &[(f32, ClipKind)], draws: &[f32]) -> FpTimeline {
        FpTimeline {
            events: events.iter().map(|&(t, kind)| VmEvent { t, kind }).collect(),
            draws: draws.to_vec(),
            scope: Vec::new(),
        }
    }

    #[test]
    fn with_nothing_happening_it_idles_and_loops() {
        let tl = timeline(&[], &[]);
        let (kind, time, _) = tl.state_at(5.0, &lengths);
        assert_eq!(kind, ClipKind::Idle);
        assert!((time - 1.0).abs() < 1e-5, "{time}");
    }

    #[test]
    fn a_clip_plays_from_its_start_and_gives_way_to_idle_when_it_ends() {
        let tl = timeline(&[(3.0, ClipKind::Shoot)], &[]);
        let (kind, time, switched) = tl.state_at(3.2, &lengths);
        assert_eq!((kind, switched), (ClipKind::Shoot, 3.0));
        assert!((time - 0.2).abs() < 1e-5);
        let (kind, _, switched) = tl.state_at(4.0, &lengths);
        assert_eq!(kind, ClipKind::Idle);
        assert!((switched - 3.8).abs() < 1e-5);
    }

    #[test]
    fn firing_cuts_a_reload_short_and_the_reload_does_not_come_back() {
        let tl = timeline(&[(1.0, ClipKind::Reload), (2.0, ClipKind::Shoot)], &[]);
        assert_eq!(tl.state_at(1.5, &lengths).0, ClipKind::Reload);
        assert_eq!(tl.state_at(2.1, &lengths).0, ClipKind::Shoot);
        assert_eq!(tl.state_at(3.0, &lengths).0, ClipKind::Idle);
    }

    #[test]
    fn what_came_before_a_weapon_change_is_forgotten() {
        let tl = timeline(&[(1.0, ClipKind::Reload), (2.0, ClipKind::Draw)], &[2.0]);
        assert_eq!(tl.state_at(2.5, &lengths).0, ClipKind::Draw);
        let (kind, _, switched) = tl.state_at(3.5, &lengths);
        assert_eq!(kind, ClipKind::Idle);
        assert!((switched - 3.0).abs() < 1e-5);
    }

    #[test]
    fn a_clip_the_weapon_lacks_plays_nothing() {
        let tl = timeline(&[(1.0, ClipKind::Shoot2)], &[]);
        assert_eq!(tl.state_at(1.1, &lengths).0, ClipKind::Idle);
    }

    #[test]
    fn a_switch_fades_over_and_the_weights_add_up() {
        let tl = timeline(&[(3.0, ClipKind::Shoot)], &[]);
        let [a, b] = tl.clips_at(3.03, &lengths);
        let (a, b) = (a.unwrap(), b.unwrap());
        assert_eq!((a.kind, b.kind), (ClipKind::Idle, ClipKind::Shoot));
        assert!((a.weight + b.weight - 1.0).abs() < 1e-5);
        assert!((b.weight - 0.5).abs() < 1e-3, "{}", b.weight);
        let [a, b] = tl.clips_at(3.2, &lengths);
        assert_eq!(a.unwrap().kind, ClipKind::Shoot);
        assert!(b.is_none());
    }

    #[test]
    fn a_burst_fades_from_one_shot_clip_to_the_other() {
        let with_two = |kind: ClipKind| if kind == ClipKind::Shoot2 { Some(0.8) } else { lengths(kind) };
        let tl = timeline(&[(3.0, ClipKind::Shoot), (3.1, ClipKind::Shoot2)], &[]);
        let [a, b] = tl.clips_at(3.13, &with_two);
        let (a, b) = (a.unwrap(), b.unwrap());
        assert_eq!((a.kind, b.kind), (ClipKind::Shoot, ClipKind::Shoot2));
        assert!((a.time - 0.13).abs() < 2e-3, "{}", a.time);
        assert!((b.time - 0.03).abs() < 2e-3, "{}", b.time);
    }

    #[test]
    fn a_draw_does_not_fade_from_the_weapon_before_it() {
        let tl = timeline(&[(2.0, ClipKind::Draw)], &[2.0]);
        let [a, b] = tl.clips_at(2.01, &lengths);
        assert_eq!(a.unwrap().kind, ClipKind::Draw);
        assert!(b.is_none());
    }

    #[test]
    fn the_scope_level_follows_the_zoom_events() {
        let mut tl = timeline(&[], &[]);
        assert_eq!(tl.scope_level(5.0), 1);
        tl.scope = vec![(1.0, 1), (2.0, 2), (3.0, 0), (4.0, 1)];
        assert_eq!(tl.scope_level(1.5), 1);
        assert_eq!(tl.scope_level(2.5), 2);
        assert_eq!(tl.scope_level(4.5), 1);
    }

    #[test]
    fn a_flag_takes_effect_gradually_and_both_ways() {
        let flags = [0u8, 0, 0, 0, 4, 4, 4, 4, 4, 4, 0, 0, 0, 0, 0, 0];
        let rate = 10.0;
        assert_eq!(flag_share(&flags, 4, 2, rate, 0.2, 0.2), 0.0);
        let mid = flag_share(&flags, 4, 5, rate, 0.2, 0.5);
        assert!((mid - 0.5).abs() < 1e-5, "{mid}");
        assert_eq!(flag_share(&flags, 4, 8, rate, 0.2, 0.8), 1.0);
        let falling = flag_share(&flags, 4, 11, rate, 0.2, 1.1);
        assert!((falling - 0.5).abs() < 1e-5, "{falling}");
        assert_eq!(flag_share(&flags, 4, 15, rate, 0.2, 1.5), 0.0);
    }

    fn blank_track(n: usize) -> Track {
        Track {
            x: vec![0.0; n],
            y: vec![0.0; n],
            z: vec![0.0; n],
            yaw: vec![0.0; n],
            pitch: vec![0.0; n],
            flags: vec![c2r3::F_ALIVE; n],
            health: vec![100; n],
            weapon: vec![0; n],
            flash: vec![0; n],
        }
    }

    #[test]
    fn a_flash_is_white_until_three_seconds_are_left_and_then_falls_away() {
        let rate = 10.0;
        let mut tr = blank_track(100);
        // 4 s by its value, and the channel stays up 1.4 times that
        tr.flash[10..66].iter_mut().for_each(|f| *f = 200);
        assert_eq!(flash_alpha(&tr, 5, rate, 0.5), 0.0);
        assert_eq!(flash_alpha(&tr, 15, rate, 1.5), 1.0);
        let quarter = flash_alpha(&tr, 35, rate, 3.5);
        assert!((quarter - 0.25).abs() < 1e-5, "{quarter}");
        assert_eq!(flash_alpha(&tr, 55, rate, 5.5), 0.0);
    }

    #[test]
    fn a_short_flash_starts_off_partly_white_after_it_builds_up() {
        let rate = 10.0;
        let mut tr = blank_track(100);
        // 1.5 s by its value
        tr.flash[10..31].iter_mut().for_each(|f| *f = 75);
        assert_eq!(flash_alpha(&tr, 10, rate, 1.0), 0.0);
        let early = flash_alpha(&tr, 11, rate, 1.1);
        let left: f64 = 1.4;
        assert!((early as f64 - (left / 3.0).powi(2)).abs() < 1e-5, "{early}");
    }

    #[test]
    fn a_second_flash_starts_over() {
        let rate = 10.0;
        let mut tr = blank_track(100);
        tr.flash[10..30].iter_mut().for_each(|f| *f = 200);
        tr.flash[30..60].iter_mut().for_each(|f| *f = 100);
        // 2 s from the moment it landed, so 1 s is left at 4 s
        let share = flash_alpha(&tr, 40, rate, 4.0);
        assert!((share as f64 - (1.0f64 / 3.0).powi(2)).abs() < 1e-5, "{share}");
    }

    #[test]
    fn nobody_dead_is_blinded() {
        let mut tr = blank_track(50);
        tr.flash.iter_mut().for_each(|f| *f = 200);
        tr.flags.iter_mut().for_each(|f| *f = 0);
        assert_eq!(flash_alpha(&tr, 5, 10.0, 0.5), 0.0);
    }

    #[test]
    fn a_gun_on_a_still_player_stays_put() {
        let tr = blank_track(100);
        let (moved, turned) = gun_motion(&tr, 100, 50, 64.0, 0.8);
        assert_eq!(moved, Vec3::ZERO);
        assert_eq!(turned, Vec3::ZERO);
    }

    #[test]
    fn the_gun_trails_the_aim_and_catches_up() {
        let mut tr = blank_track(200);
        let rate = 64.0;
        // turning left at 180 degrees a second for a quarter of a second, across the wrap
        for i in 0..200 {
            let turning = (i as f64 - 50.0).clamp(0.0, 16.0) * 180.0 / rate;
            tr.yaw[i] = (170.0 + turning + 180.0).rem_euclid(360.0) - 180.0;
        }
        let (during, _) = gun_motion(&tr, 200, 66, rate, 1.0);
        assert!(during.x > 0.1 && during.x < 2.5, "{}", during.x);
        let (after, _) = gun_motion(&tr, 200, 199, rate, 3.0);
        assert!(after.x.abs() < 1e-5, "{}", after.x);
    }

    #[test]
    fn a_gun_sways_with_the_steps_and_more_at_a_run() {
        let rate = 64.0;
        let moving = |speed: f64| {
            let mut tr = blank_track(200);
            for i in 0..200 {
                tr.x[i] = speed * i as f64 / rate;
            }
            tr
        };
        let widest = |speed: f64| {
            let tr = moving(speed);
            (60..190)
                .map(|i| gun_motion(&tr, 200, i, rate, i as f64 / rate).0.x.abs())
                .fold(0.0, f32::max)
        };
        assert!(widest(250.0) > 0.5, "{}", widest(250.0));
        assert!(widest(100.0) < widest(250.0));
    }

    #[test]
    fn the_views_match_cs2_at_its_defaults() {
        assert!((vertical_fov(WORLD_FOV_4_3).to_degrees() - 73.74).abs() < 0.01);
        assert!((vertical_fov(VIEWMODEL_FOV_4_3).to_degrees() - 46.83).abs() < 0.01);
    }

    #[test]
    fn the_settings_for_the_arms_stay_within_cs2s_limits() {
        assert_eq!(VmSettings::default().fitted(), VmSettings::default(), "the defaults are allowed");
        let wild = VmSettings { fov_4_3: 90.0, offset: Vec3::new(9.0, -9.0, f32::NAN), visible: false }.fitted();
        assert_eq!(wild.fov_4_3, VIEWMODEL_FOV_RANGE.1);
        assert_eq!(wild.offset, VIEWMODEL_OFFSET, "an offset that is not a number keeps the default");
        let far = VmSettings { fov_4_3: f32::NAN, offset: Vec3::new(9.0, -9.0, 0.5), visible: false }.fitted();
        assert_eq!(far.fov_4_3, VIEWMODEL_FOV_4_3);
        assert_eq!(far.offset, Vec3::new(2.5, -2.0, 0.5));
        assert!(!far.visible);
    }

    #[test]
    fn only_a_sniper_rifle_scope_hides_the_gun() {
        assert!(scope_hides_the_gun("AWP"));
        assert!(!scope_hides_the_gun("AUG"));
        assert_eq!(scope_fov_4_3("AK", 1), None);
        assert_eq!(scope_fov_4_3("AWP", 2), Some(10.0));
        assert_eq!(scope_fov_4_3("SCOUT", 2), Some(15.0));
    }
}
