//! What smoke, fire, explosions, hits and the bomb look like, as sprites for `fx.rs`.
//!
//! Every picture is worked out from the playback clock and from numbers fixed per round,
//! so a scrub shows what playing would. Distances are in Hammer units and in the map
//! file's frame (y up), as `space.rs` has them; a sprite goes to the scene times its scale.
//! Times are seconds of playback.

use std::collections::HashMap;
use std::f32::consts::{PI, TAU};

use bevy::prelude::*;
use serde_json::Value;

use crate::fx::{hash01, Fx, FxAssets, FxFrame, Sheet, Sprite, GLOW_MUZZLE, GLOW_WARM, GLOW_WHITE};
use crate::space::{Cell, MapSpace, SpaceAsset, SpaceGrid};
use crate::state::{lerp, MainCam, RoundState, Viewer};
use crate::textured_map::MapLight;
use crate::util::UtilKind;

// A smoke grenade's cloud. CS2's own is a voxel flood of about this size.
const SMOKE_RADIUS: f32 = 144.0;
/// How high it stands on open ground.
const SMOKE_UP: f32 = 112.0;
const SMOKE_DOWN: f32 = 48.0;
/// From the pop to the last corner filled.
const SMOKE_BLOOM_S: f32 = 1.2;
const SMOKE_FADE_S: f32 = 2.0;
/// A puff on every third cell each way, 48 units apart, each about twice that across.
const PUFF_STEP: i32 = 3;
/// Half the side of a puff's picture. The cloud in it fills about 70% of that.
const PUFF_SIZE: f32 = 70.0;
/// How much light a puff gives back.
const SMOKE_GREY: f32 = 0.55;

// The straight grey of each grey sheet where it is solid, measured on the atlases: CS2 tints
// them in its materials, so a tint here divides by it.
const SMOKE_SHEET_GREY: f32 = 0.32;
const DUST_SHEET_GREY: f32 = 0.105;
const BLOOD_SHEET_GREY: f32 = 0.6;
/// HE and bomb smoke, and the dust they throw along the floor.
const BLAST_SMOKE_GREY: f32 = 0.3;
const FIRE_SMOKE_GREY: f32 = 0.06;
const DUST_COLOR: Vec3 = Vec3::new(0.34, 0.30, 0.25);
const BLOOD_COLOR: Vec3 = Vec3::new(0.28, 0.014, 0.011);
const SMOKE_OPACITY: f32 = 0.85;

// A molotov's fire.
const FLAME_SIZE: f32 = 24.0;
const FIRE_SPREAD_S: f32 = 0.7;
const FIRE_DIE_S: f32 = 1.0;

/// How long an HE, a flashbang and the bomb leave something to see.
const HE_LIFE_S: f32 = 3.5;
const FLASH_LIFE_S: f32 = 0.45;
const C4_LIFE_S: f32 = 6.0;

/// A shot's flash and what a bullet leaves where it lands.
pub const MUZZLE_LIFE_S: f64 = 0.05;
pub const IMPACT_LIFE_S: f64 = 0.7;
const BLOOD_LIFE_S: f64 = 0.55;

// sprite groups: one mesh each, so Bevy sorts them against each other
const GROUP_MUZZLE: u32 = 1;
const GROUP_IMPACT: u32 = 2;
const GROUP_BLOOD: u32 = 3;
const GROUP_BOMB: u32 = 4;
const GROUP_IN_SMOKE: u32 = 8;
fn util_group(idx: usize, part: u32) -> u32 {
    16 + idx as u32 * 4 + part
}

/// A world (Hammer) point in the map file's frame.
pub fn world_to_map(x: f64, y: f64, z: f64) -> Vec3 {
    Vec3::new(x as f32, z as f32, -y as f32)
}

fn ease_out(x: f32) -> f32 {
    let x = x.clamp(0.0, 1.0);
    1.0 - (1.0 - x) * (1.0 - x)
}

/// Seed-dependent numbers for one sprite.
fn h(seed: u32, i: u32) -> f32 {
    hash01(seed, i)
}

/// What lights an effect: what a white surface there shows on the textured map, or an even
/// light on the plain map.
struct Light<'a> {
    map: Option<&'a MapLight>,
}

/// A white surface on the plain map, which Bevy lights with one sun and an ambient.
pub const PLAIN_MAP_LIGHT: f32 = 0.8;

impl Light<'_> {
    fn at(&self, p: Vec3) -> Vec3 {
        match self.map {
            Some(l) => l.on_screen(p),
            None => Vec3::splat(PLAIN_MAP_LIGHT),
        }
    }
}

struct Puff {
    pos: Vec3,
    size: f32,
    delay: f32,
    seed: u32,
}

struct SmokeShape {
    puffs: Vec<Puff>,
    /// filled cells and when the bloom reaches each: the camera is in the smoke in these
    cells: HashMap<Cell, f32>,
    /// the pop, for a dome without a grid
    at: Vec3,
}

struct Flame {
    pos: Vec3,
    delay: f32,
    seed: u32,
}

/// One hit on a player, from the round's `hurt` events.
struct Hit {
    t: f64,
    victim: usize,
    attacker: usize,
    height: f32,
}

/// The shapes worked out once per round, and again when the map's grid arrives.
#[derive(Resource, Default)]
pub struct FxCache {
    round: Option<(i64, usize)>,
    with_space: bool,
    smokes: HashMap<usize, SmokeShape>,
    fires: HashMap<usize, Vec<Flame>>,
    hits: Vec<Hit>,
}

impl FxCache {
    /// Forget what belongs to another round, or was worked out before the grid was in.
    fn refresh(&mut self, r: &RoundState, space: bool) {
        let id = (r.dec.round, r.dec.start_tick as usize);
        if self.round != Some(id) {
            self.round = Some(id);
            self.with_space = space;
            self.smokes.clear();
            self.fires.clear();
            self.hits = read_hits(&r.dec.header);
        } else if space && !self.with_space {
            self.with_space = true;
            self.smokes.clear();
            self.fires.clear();
        }
    }
}

/// How high on a standing player each hit group is.
fn hit_height(group: &str) -> f32 {
    match group {
        "head" => 62.0,
        "neck" => 56.0,
        "chest" | "left_arm" | "right_arm" => 50.0,
        "stomach" => 40.0,
        "left_leg" | "right_leg" => 20.0,
        _ => 45.0,
    }
}

/// Does damage from this weapon tag draw blood? Bullets, the knife, an HE and the bomb do;
/// burning, a fall and a grenade bouncing off a head do not.
fn bleeds(weapon: Option<&str>) -> bool {
    !matches!(weapon, None | Some("\u{1f525}" | "MOLLY" | "INC" | "SMOKE" | "DECOY" | "FLASH"))
}

fn read_hits(header: &Value) -> Vec<Hit> {
    let Some(events) = header.get("events").and_then(Value::as_array) else { return Vec::new() };
    events
        .iter()
        .filter(|e| e.get("k").and_then(Value::as_str) == Some("hurt"))
        .filter(|e| bleeds(e.get("w").and_then(Value::as_str)))
        .filter_map(|e| {
            Some(Hit {
                t: e.get("s")?.as_f64()?,
                victim: e.get("p")?.as_u64()? as usize,
                attacker: e.get("a")?.as_u64()? as usize,
                height: hit_height(e.get("hg").and_then(Value::as_str).unwrap_or("")),
            })
        })
        .collect()
}

/// The puffs of a smoke that pops at `at`. With a grid they fill what the flood reaches;
/// without one, a dome.
fn smoke_shape(space: Option<&SpaceGrid>, at: Vec3, seed: u32) -> SmokeShape {
    if let Some(g) = space {
        let cells = g.flood_smoke(at, SMOKE_RADIUS, SMOKE_UP, SMOKE_DOWN);
        if let Some(&(first, _)) = cells.first() {
            let far = cells.last().map_or(1.0, |c| c.1).max(1.0);
            let filled: HashMap<Cell, f32> = cells.iter().map(|&(c, d)| (c, d / far * SMOKE_BLOOM_S)).collect();
            let k = g.cell_size();
            let mut puffs = Vec::new();
            for (i, &(c, d)) in cells.iter().enumerate() {
                if (0..3).any(|a| (c[a] - first[a]).rem_euclid(PUFF_STEP) != 0) {
                    continue;
                }
                let n = i as u32;
                let jitter = Vec3::new(h(seed, n * 5) - 0.5, h(seed, n * 5 + 1) - 0.5, h(seed, n * 5 + 2) - 0.5) * k * 1.4;
                // where the cloud meets a wall its puffs are smaller, so less pokes through
                let open = [[2, 0, 0], [-2, 0, 0], [0, 2, 0], [0, -2, 0], [0, 0, 2], [0, 0, -2]]
                    .iter()
                    .all(|o| filled.contains_key(&[c[0] + o[0], c[1] + o[1], c[2] + o[2]]));
                let size = PUFF_SIZE * (0.8 + 0.4 * h(seed, n * 5 + 3)) * if open { 1.0 } else { 0.72 };
                puffs.push(Puff { pos: g.centre(c) + jitter, size, delay: d / far * SMOKE_BLOOM_S, seed: seed ^ n.wrapping_mul(0x9E37) });
            }
            return SmokeShape { puffs, cells: filled, at };
        }
    }
    let n = 44;
    let puffs = (0..n)
        .map(|i| {
            let u = h(seed, i * 3);
            let r = u.cbrt() * SMOKE_RADIUS * 0.75;
            let a = i as f32 * 2.399_963;
            let y = (i as f32 + 0.5) / n as f32;
            let flat = (1.0 - y * y).sqrt();
            let dir = Vec3::new(a.cos() * flat, y * SMOKE_UP / SMOKE_RADIUS, a.sin() * flat);
            Puff { pos: at + dir * r + Vec3::Y * 16.0, size: PUFF_SIZE, delay: r / SMOKE_RADIUS * SMOKE_BLOOM_S, seed: seed ^ i }
        })
        .collect();
    SmokeShape { puffs, cells: HashMap::new(), at }
}

/// How far into the dome a point is, 0 at its middle and 1 at its edge, for a smoke
/// without a grid.
fn in_dome(at: Vec3, p: Vec3) -> f32 {
    let d = p - at;
    let up = if d.y > 0.0 { d.y / SMOKE_UP } else { -d.y / SMOKE_DOWN };
    (Vec2::new(d.x, d.z).length_squared() / (SMOKE_RADIUS * SMOKE_RADIUS) + up * up).sqrt()
}

/// The flames of a molotov that lands at `at`: over the floor the flood reaches, or on a
/// disc without a grid.
fn fire_shape(space: Option<&SpaceGrid>, at: Vec3, radius: f32, seed: u32) -> Vec<Flame> {
    if let Some(g) = space {
        let floor = g.flood_floor(at, radius);
        if let Some(&(first, _, _)) = floor.first() {
            let k = g.cell_size();
            let mut flames = Vec::new();
            for (i, &(c, d, y)) in floor.iter().enumerate() {
                if (c[0] - first[0]).rem_euclid(2) != 0 || (c[2] - first[2]).rem_euclid(2) != 0 {
                    continue;
                }
                let n = i as u32;
                let centre = g.centre(c);
                let jx = (h(seed, n * 3) - 0.5) * k * 1.6;
                let jz = (h(seed, n * 3 + 1) - 0.5) * k * 1.6;
                flames.push(Flame {
                    pos: Vec3::new(centre.x + jx, y, centre.z + jz),
                    delay: d / radius * FIRE_SPREAD_S,
                    seed: seed ^ n.wrapping_mul(0x51ED),
                });
            }
            return flames;
        }
    }
    let n = ((PI * radius * radius) / (32.0 * 32.0)) as u32;
    (0..n)
        .map(|i| {
            let r = radius * ((i as f32 + 0.5) / n as f32).sqrt();
            let a = i as f32 * 2.399_963;
            Flame { pos: at + Vec3::new(a.cos() * r, 0.0, a.sin() * r), delay: r / radius * FIRE_SPREAD_S, seed: seed ^ i }
        })
        .collect()
}

struct Ctx<'a> {
    frame: &'a mut FxFrame,
    fx: &'a FxAssets,
    light: Light<'a>,
    /// scene units per Hammer unit
    s: f32,
    t: f32,
}

impl Ctx<'_> {
    #[allow(clippy::too_many_arguments)]
    fn push(&mut self, group: u32, kind: Fx, pos: Vec3, size: f32, rot: f32, uv: [f32; 4], rgb: Vec3, opacity: f32, cover: f32) {
        let sprite = Sprite::new(pos * self.s, size * self.s, rot, uv, rgb, opacity, cover);
        self.frame.push(group, kind, false, sprite);
    }

    fn sheet(&self, kind: Fx) -> Sheet {
        self.fx.sheet(kind)
    }
}

fn draw_smoke(c: &mut Ctx, group: u32, shape: &SmokeShape, t0: f32, t1: f32) {
    let sheet = c.sheet(Fx::Smoke);
    let t = c.t;
    let end = ((t1 - t) / SMOKE_FADE_S).clamp(0.0, 1.0);
    for p in &shape.puffs {
        let age = t - t0 - p.delay;
        if age < 0.0 {
            continue;
        }
        let grow = ease_out(age / 0.7);
        let appear = (age / 0.3).min(1.0);
        let size = p.size * (0.45 + 0.55 * grow) * (1.0 + 0.35 * (1.0 - end));
        let (a, b, d) = (h(p.seed, 1), h(p.seed, 2), h(p.seed, 3));
        let drift = Vec3::new((t * 0.31 + a * 6.0).sin(), 0.5 * (t * 0.23 + b * 6.0).sin(), (t * 0.27 + d * 6.0).cos()) * 3.0;
        let pos = p.pos + drift + Vec3::Y * 12.0 * (1.0 - end);
        let frame = sheet.frame_at(t + a * 20.0, 6.0, true);
        let rot = a * TAU + t * (b - 0.5) * 0.2;
        let light = c.light.at(pos).max(Vec3::splat(0.08));
        c.push(group, Fx::Smoke, pos, size, rot, sheet.uv(0, frame), light * (SMOKE_GREY / SMOKE_SHEET_GREY), SMOKE_OPACITY * appear * end, 1.0);
    }
}

/// A grey veil over the screen while the camera is inside a smoke, as in CS2: the puffs
/// close to the camera fade out, so without it the inside of a smoke would be clear.
fn draw_in_smoke(c: &mut Ctx, shape: &SmokeShape, space: Option<&SpaceGrid>, t0: f32, t1: f32, eye: Vec3, forward: Vec3) {
    let t = c.t;
    let reached = match space {
        Some(g) if !shape.cells.is_empty() => shape.cells.get(&g.cell_of(eye)).map(|d| t - t0 - d),
        _ => (in_dome(shape.at, eye) < 0.85).then(|| t - t0 - in_dome(shape.at, eye) * SMOKE_BLOOM_S),
    };
    let Some(age) = reached else { return };
    let thick = (age / 0.5).clamp(0.0, 1.0) * ((t1 - t) / SMOKE_FADE_S).clamp(0.0, 1.0);
    if thick <= 0.0 {
        return;
    }
    let sheet = c.sheet(Fx::Smoke);
    let [u0, v0, u1, v1] = sheet.uv(0, sheet.frame_at(t, 6.0, true));
    // the middle of a puff, where it is even
    let (du, dv) = ((u1 - u0) * 0.3, (v1 - v0) * 0.3);
    let uv = [u0 + du, v0 + dv, u1 - du, v1 - dv];
    let light = c.light.at(eye).max(Vec3::splat(0.08));
    let pos = (eye + forward * 6.0) * c.s;
    let sprite = Sprite::new(pos, -24.0 * c.s, 0.0, uv, light * (SMOKE_GREY / SMOKE_SHEET_GREY), 0.97 * thick, 1.0);
    c.frame.push(GROUP_IN_SMOKE, Fx::Smoke, false, sprite);
}

fn draw_fire(c: &mut Ctx, idx: usize, flames: &[Flame], at: Vec3, radius: f32, t0: f32, t1: f32) {
    let fire = c.sheet(Fx::Fire);
    let smoke = c.sheet(Fx::BlastSmoke);
    let t = c.t;
    let end = ((t1 - t) / FIRE_DIE_S).clamp(0.0, 1.0);
    for (i, f) in flames.iter().enumerate() {
        let age = t - t0 - f.delay;
        if age < 0.0 {
            continue;
        }
        let (a, b) = (h(f.seed, 1), h(f.seed, 2));
        let flicker = 0.88 + 0.12 * (t * 11.0 + a * 40.0).sin();
        let size = FLAME_SIZE * (0.75 + 0.5 * b) * ease_out(age / 0.35) * end.sqrt() * flicker;
        let frame = fire.frame_at(t + a * 5.0, 20.0, true);
        let pos = f.pos + Vec3::Y * size * 0.8;
        c.push(util_group(idx, 0), Fx::Fire, pos, size, 0.0, fire.uv((b * 4.0) as usize, frame), Vec3::splat(1.0), end.max(0.3), 0.8);
        // every third flame sends up a dark puff, one after another
        if i % 3 == 0 {
            let cycle = 2.4;
            let k = ((t + a * cycle) / cycle).fract();
            let up = f.pos + Vec3::Y * (24.0 + k * 70.0) + Vec3::new((a - 0.5) * 20.0, 0.0, (b - 0.5) * 20.0) * k;
            let fr = smoke.frame_at(k * cycle, 16.0 / cycle, false);
            let o = 0.3 * (PI * k).sin() * end;
            c.push(util_group(idx, 1), Fx::BlastSmoke, up, 22.0 + 26.0 * k, a * TAU, smoke.uv(i % 3, fr), Vec3::splat(FIRE_SMOKE_GREY / SMOKE_SHEET_GREY), o, 1.0);
        }
    }
    // the glow of the fire on everything near
    let glow = c.sheet(Fx::Glow);
    let o = 0.28 * end * ease_out((t - t0) / 0.5) * (0.9 + 0.1 * (t * 7.0).sin());
    c.push(util_group(idx, 2), Fx::Glow, at + Vec3::Y * 12.0, radius * 1.1, 0.0, glow.uv(GLOW_WARM, 0), Vec3::new(1.0, 0.75, 0.5), o, 0.0);
    // the bottle bursting
    let age = t - t0;
    if age < 0.6 {
        let fb = c.sheet(Fx::Fireball);
        for k in 0..3u32 {
            let d = Vec3::new(h(idx as u32, k) - 0.5, 0.4, h(idx as u32, k + 9) - 0.5) * 40.0;
            let fr = fb.frame_at(age, 16.0 / 0.6, false);
            c.push(util_group(idx, 2), Fx::Fireball, at + d + Vec3::Y * 20.0, 40.0 + 40.0 * ease_out(age / 0.6), k as f32 * 2.0, fb.uv(k as usize, fr), Vec3::splat(1.2), 1.0 - age / 0.6, 0.2);
        }
    }
}

/// An explosion: a flash, a fireball, smoke that rises and thins, dust thrown along the
/// floor. `scale` is 1 for an HE and larger for the bomb.
fn draw_blast(c: &mut Ctx, group: u32, at: Vec3, age: f32, life: f32, scale: f32, seed: u32) {
    let glow = c.sheet(Fx::Glow);
    let fb = c.sheet(Fx::Fireball);
    let smoke = c.sheet(Fx::BlastSmoke);
    let dust = c.sheet(Fx::Dust);
    if age < 0.15 {
        let k = 1.0 - age / 0.15;
        c.push(group + 2, Fx::Glow, at + Vec3::Y * 24.0 * scale, 220.0 * scale * k.sqrt(), 0.0, glow.uv(GLOW_WARM, 0), Vec3::new(1.0, 0.85, 0.6), k, 0.0);
    }
    let fire_life = 0.65 * scale.sqrt();
    if age < fire_life {
        let n = (5.0 * scale) as u32;
        for k in 0..n {
            let d = Vec3::new(h(seed, k) - 0.5, h(seed, k + 50) * 0.8, h(seed, k + 99) - 0.5) * 70.0 * scale;
            let fr = fb.frame_at(age, 16.0 / fire_life, false);
            let size = (55.0 + 75.0 * ease_out(age / fire_life)) * scale.sqrt() * (0.8 + 0.4 * h(seed, k + 7));
            let o = if age < fire_life * 0.6 { 1.0 } else { (fire_life - age) / (fire_life * 0.4) };
            c.push(group, Fx::Fireball, at + d * ease_out(age / 0.3) + Vec3::Y * 30.0 * scale, size, h(seed, k + 3) * TAU, fb.uv(k as usize, fr), Vec3::splat(1.3), o, 0.25);
        }
    }
    let n = (7.0 * scale) as u32;
    for k in 0..n {
        let start = 0.05 + 0.1 * h(seed, k + 200);
        let a = age - start;
        if a < 0.0 {
            continue;
        }
        let e = ease_out(a / (life * 0.45));
        let d = Vec3::new(h(seed, k + 300) - 0.5, h(seed, k + 400) * 0.6, h(seed, k + 500) - 0.5) * 2.0;
        let pos = at + d * (40.0 + 70.0 * e) * scale + Vec3::Y * (30.0 + 60.0 * a / life) * scale;
        let size = (45.0 + 95.0 * e) * scale.sqrt();
        let o = 0.75 * (1.0 - a / (life - start)).max(0.0).powf(1.5) * (a / 0.15).min(1.0);
        let fr = smoke.frame_at(a, 16.0 / life, false);
        let light = c.light.at(pos).max(Vec3::splat(0.08));
        c.push(group + 1, Fx::BlastSmoke, pos, size, h(seed, k + 600) * TAU + a * 0.2, smoke.uv(k as usize, fr), light * (BLAST_SMOKE_GREY / SMOKE_SHEET_GREY), o, 1.0);
    }
    if age < 1.2 {
        let n = (10.0 * scale) as u32;
        for k in 0..n {
            let ang = k as f32 / n as f32 * TAU + h(seed, k + 700);
            let r = (20.0 + 150.0 * ease_out(age / 0.5)) * scale.sqrt();
            let pos = at + Vec3::new(ang.cos() * r, 10.0 + 10.0 * h(seed, k + 800), ang.sin() * r);
            let o = 0.5 * (1.0 - age / 1.2);
            let light = c.light.at(pos).max(Vec3::splat(0.08));
            c.push(group + 1, Fx::Dust, pos, 28.0 + 30.0 * ease_out(age / 1.2), h(seed, k + 900) * TAU, dust.uv(k as usize, 0), light * DUST_COLOR / DUST_SHEET_GREY, o, 1.0);
        }
    }
}

fn draw_flash(c: &mut Ctx, group: u32, at: Vec3, age: f32) {
    let glow = c.sheet(Fx::Glow);
    let k = if age < 0.05 { 1.0 } else { (1.0 - (age - 0.05) / (FLASH_LIFE_S - 0.05)).max(0.0).powi(2) };
    let p = at + Vec3::Y * 12.0;
    c.push(group, Fx::Glow, p, 320.0 * (0.6 + 0.4 * k), 0.0, glow.uv(GLOW_WHITE, 0), Vec3::splat(1.0), k, 0.0);
    c.push(group, Fx::Glow, p, 50.0, 1.0, glow.uv(GLOW_WHITE, 0), Vec3::splat(1.0), k, 0.0);
}

/// Smoke, fire and explosions of the grenades this round.
#[allow(clippy::too_many_arguments)]
pub fn draw_utility(
    viewer: Res<Viewer>,
    fx: Res<FxAssets>,
    mut frame: ResMut<FxFrame>,
    mut cache: ResMut<FxCache>,
    spaces: Res<Assets<SpaceAsset>>,
    map_space: Res<MapSpace>,
    map_light: Option<Res<MapLight>>,
    q_cam: Query<&GlobalTransform, With<MainCam>>,
) {
    if !fx.ready() {
        return;
    }
    let (Some(r), Some(m)) = (viewer.round.as_ref(), viewer.manifest.as_ref()) else { return };
    let space = map_space.get(&spaces);
    cache.refresh(r, space.is_some());
    let s = m.scene_scale as f32;
    let t = viewer.clock as f32;
    let rate = r.rate as f32;
    let cam = q_cam.single().ok();
    let eye = cam.map(|c| c.translation() / s);
    let forward = cam.map_or(Vec3::NEG_Z, |c| c.forward().as_vec3());
    let cache = &mut *cache;
    let mut c = Ctx { frame: &mut frame, fx: &fx, light: Light { map: map_light.as_deref().filter(|_| viewer.show_map) }, s, t };
    for (idx, item) in r.utils.iter().enumerate() {
        let t0 = item.s0 as f32 / rate;
        let at = item.pos / s;
        let seed = idx as u32 * 7919 + 13;
        match &item.kind {
            UtilKind::Smoke { .. } => {
                let t1 = item.s1 as f32 / rate;
                if t < t0 || t > t1 {
                    continue;
                }
                let shape = cache.smokes.entry(idx).or_insert_with(|| smoke_shape(space, at, seed));
                draw_smoke(&mut c, util_group(idx, 0), shape, t0, t1);
                if let Some(eye) = eye {
                    draw_in_smoke(&mut c, shape, space, t0, t1, eye, forward);
                }
            }
            UtilKind::Molotov { .. } => {
                let t1 = item.s1 as f32 / rate;
                if t < t0 || t > t1 {
                    continue;
                }
                let radius = (item.r_s / s).max(60.0);
                let flames = cache.fires.entry(idx).or_insert_with(|| fire_shape(space, at, radius, seed));
                draw_fire(&mut c, idx, flames, at, radius, t0, t1);
            }
            UtilKind::Burst { ring: Some(_), .. } => {
                let age = t - t0;
                if (0.0..HE_LIFE_S).contains(&age) {
                    draw_blast(&mut c, util_group(idx, 0), at, age, HE_LIFE_S, 1.0, seed);
                }
            }
            UtilKind::Burst { ring: None, .. } => {
                let age = t - t0;
                if (0.0..FLASH_LIFE_S).contains(&age) {
                    draw_flash(&mut c, util_group(idx, 0), at, age);
                }
            }
            UtilKind::Decoy { .. } => {}
        }
    }
}

/// A player's feet at a fractional sample, in the map file's frame, and whether crouched.
pub fn feet_at(r: &RoundState, slot: usize, sample: f64) -> Option<(Vec3, bool)> {
    let tr = r.dec.tracks.get(slot)?;
    if r.n == 0 {
        return None;
    }
    let sc = sample.clamp(0.0, (r.n - 1) as f64);
    let i0 = sc.floor() as usize;
    let i1 = (i0 + 1).min(r.n - 1);
    let f = sc - i0 as f64;
    let p = world_to_map(lerp(tr.x[i0], tr.x[i1], f), lerp(tr.y[i0], tr.y[i1], f), lerp(tr.z[i0], tr.z[i1], f));
    Some((p, tr.flags[i0] & c2r3::F_CROUCH != 0))
}

/// Blood where a bullet hits a player, sprayed away from the shooter.
pub fn draw_hits(viewer: Res<Viewer>, fx: Res<FxAssets>, mut frame: ResMut<FxFrame>, cache: Res<FxCache>) {
    if !fx.ready() {
        return;
    }
    let (Some(r), Some(m)) = (viewer.round.as_ref(), viewer.manifest.as_ref()) else { return };
    if cache.round != Some((r.dec.round, r.dec.start_tick as usize)) {
        return;
    }
    let s = m.scene_scale as f32;
    let sheet = fx.sheet(Fx::Blood);
    let clock = viewer.clock;
    for (i, hit) in cache.hits.iter().enumerate() {
        let t = hit.t / r.rate;
        let age = clock - t;
        if !(0.0..BLOOD_LIFE_S).contains(&age) {
            continue;
        }
        let Some((feet, crouch)) = feet_at(r, hit.victim, hit.t) else { continue };
        let point = feet + Vec3::Y * hit.height * if crouch { 0.72 } else { 1.0 };
        let from = feet_at(r, hit.attacker, hit.t).map(|(p, c)| p + Vec3::Y * if c { 46.0 } else { 64.0 });
        let dir = from.map_or(Vec3::X, |a| (point - a).normalize_or(Vec3::X));
        let a = (age / BLOOD_LIFE_S) as f32;
        let seed = i as u32 * 31 + 7;
        for k in 0..4u32 {
            let spread = Vec3::new(h(seed, k) - 0.5, h(seed, k + 10) - 0.3, h(seed, k + 20) - 0.5) * 0.8;
            let travel = (6.0 + 30.0 * h(seed, k + 30)) * ease_out(a * 1.5);
            let pos = point + (dir + spread).normalize_or(dir) * travel - Vec3::Y * 20.0 * a * a;
            let size = 5.0 + 14.0 * ease_out(a * 2.0) * (0.6 + 0.6 * h(seed, k + 40));
            let fr = sheet.frame_at(age as f32, 16.0 / BLOOD_LIFE_S as f32, false);
            let sprite = Sprite::new(pos * s, size * s, h(seed, k + 50) * TAU, sheet.uv(k as usize, fr), BLOOD_COLOR / BLOOD_SHEET_GREY, 0.95 * (1.0 - a), 1.0);
            frame.push(GROUP_BLOOD, Fx::Blood, false, sprite);
        }
    }
}

/// A shot's flash at the muzzle: one of CS2's two pictures and a warm glow. `first_person`
/// puts it with the viewmodel, which has its own camera and depth, and its size is then
/// in the viewmodel's scene units.
pub fn push_muzzle_flash(frame: &mut FxFrame, fx: &FxAssets, muzzle: Vec3, size: f32, age: f64, shot: u32, first_person: bool) {
    let glow = fx.sheet(Fx::Glow);
    let k = (1.0 - age / MUZZLE_LIFE_S).clamp(0.0, 1.0) as f32;
    let pic = GLOW_MUZZLE[(shot % 2) as usize];
    let rot = hash01(shot, 1) * TAU;
    let size = if first_person { -size } else { size };
    frame.push(GROUP_MUZZLE, Fx::Glow, first_person, Sprite::new(muzzle, size * (0.8 + 0.3 * hash01(shot, 2)), rot, glow.uv(pic, 0), Vec3::splat(1.8), k, 0.0));
    frame.push(GROUP_MUZZLE, Fx::Glow, first_person, Sprite::new(muzzle, size * 2.2, 0.0, glow.uv(GLOW_WARM, 0), Vec3::new(1.0, 0.8, 0.5), 0.5 * k, 0.0));
}

/// Dust where a bullet lands in a wall or the floor. `at` is in the scene.
pub fn push_impact(frame: &mut FxFrame, fx: &FxAssets, at: Vec3, s: f32, age: f64, shot: u32, light: Vec3) {
    let dust = fx.sheet(Fx::Dust);
    let a = (age / IMPACT_LIFE_S) as f32;
    for k in 0..2u32 {
        let size = (4.0 + 12.0 * ease_out(a * 1.5)) * (0.7 + 0.5 * hash01(shot, k + 3)) * s;
        let lift = Vec3::Y * 6.0 * a * s;
        let sprite = Sprite::new(at + lift, size, hash01(shot, k + 5) * TAU, dust.uv((shot + k) as usize, 0), light * DUST_COLOR / DUST_SHEET_GREY, 0.8 * (1.0 - a), 1.0);
        frame.push(GROUP_IMPACT, Fx::Dust, false, sprite);
    }
    if a < 0.15 {
        let glow = fx.sheet(Fx::Glow);
        frame.push(GROUP_IMPACT, Fx::Glow, false, Sprite::new(at, 3.0 * s, 0.0, glow.uv(GLOW_WARM, 0), Vec3::new(1.0, 0.8, 0.5), 1.0 - a / 0.15, 0.0));
    }
}

/// The bomb of a round, as the manifest has it, in seconds and the map file's frame.
pub struct Bomb {
    pub plant: f64,
    pub at: Vec3,
    pub defused: Option<f64>,
    pub exploded: Option<f64>,
}

/// Interval between beeps by the share of the fuse that is left, as sfx.js plays them.
fn c4_interval(left: f64) -> f64 {
    (0.1 + 0.9 * left).max(0.15)
}
const C4_FUSE_S: f64 = 40.0;

/// When the last beep at or before `clock` was, if one was.
pub fn last_beep(bomb: &Bomb, clock: f64, round_end: f64) -> Option<f64> {
    let blow = bomb.exploded.unwrap_or(bomb.plant + C4_FUSE_S);
    let fuse = (blow - bomb.plant).max(1.0);
    let stop = bomb.defused.unwrap_or(if bomb.exploded.is_some() { blow } else { blow.min(round_end) });
    if clock < bomb.plant || clock >= stop {
        return None;
    }
    let mut t = bomb.plant;
    let mut last = t;
    while t <= clock {
        last = t;
        t += c4_interval((blow - t) / fuse);
    }
    Some(last)
}

/// The bomb's light blinking with its beeps, and its explosion.
#[allow(clippy::too_many_arguments)]
fn draw_bomb(c: &mut Ctx, bomb: &Bomb, clock: f64, round_end: f64, led: Vec3) {
    if let Some(beep) = last_beep(bomb, clock, round_end) {
        let k = 1.0 - ((clock - beep) / 0.12) as f32;
        if k > 0.0 {
            let glow = c.sheet(Fx::Glow);
            c.push(GROUP_BOMB, Fx::Glow, led, 5.0, 0.0, glow.uv(GLOW_WARM, 0), Vec3::new(1.0, 0.08, 0.03), k, 0.0);
        }
    }
    if let Some(blow) = bomb.exploded {
        let age = (clock - blow) as f32;
        if (0.0..C4_LIFE_S).contains(&age) {
            draw_blast(c, GROUP_BOMB + 1, bomb.at, age, C4_LIFE_S, 3.0, 0xC4);
        }
    }
}

/// This round's bomb: where the manifest says it was planted, or, in a replay from
/// before it said, where whoever held it stood when it went down.
pub fn round_bomb(r: &RoundState, m: &crate::state::ManifestData) -> Option<Bomb> {
    let fact = m.bombs.get(&r.dec.round)?;
    let at = match fact.pos {
        Some([x, y, z]) => world_to_map(x, y, z),
        None => {
            let before = (fact.plant as usize).saturating_sub(1).min(r.n.saturating_sub(1));
            let holder = (0..r.dec.tracks.len())
                .find(|&slot| crate::weapons_viz::current_tag(r, slot, before) == Some("\u{1f4a3}"))?;
            feet_at(r, holder, fact.plant)?.0
        }
    };
    Some(Bomb {
        plant: fact.plant / r.rate,
        at,
        defused: fact.defused.map(|s| s / r.rate),
        exploded: fact.exploded.map(|s| s / r.rate),
    })
}

/// Where on the bomb its light is: on top of the case lying on the floor.
pub const C4_LED: Vec3 = Vec3::new(0.0, 3.6, 0.0);

pub fn draw_round_bomb(
    viewer: Res<Viewer>,
    fx: Res<FxAssets>,
    mut frame: ResMut<FxFrame>,
    map_light: Option<Res<MapLight>>,
) {
    if !fx.ready() {
        return;
    }
    let (Some(r), Some(m)) = (viewer.round.as_ref(), viewer.manifest.as_ref()) else { return };
    let Some(bomb) = round_bomb(r, m) else { return };
    let s = m.scene_scale as f32;
    let mut c = Ctx { frame: &mut frame, fx: &fx, light: Light { map: map_light.as_deref().filter(|_| viewer.show_map) }, s, t: viewer.clock as f32 };
    draw_bomb(&mut c, &bomb, viewer.clock, r.duration, bomb.at + C4_LED);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::space::tests::grid;

    #[test]
    fn a_smoke_fills_the_corridor_it_pops_in() {
        let g = grid([80, 16, 40], |x, y, z| y == 0 || ((z == 18 || z == 22) && y < 12 && (2..78).contains(&x)));
        let at = Vec3::new(40.0 * 16.0, 16.0, 20.0 * 16.0 + 8.0);
        let shape = smoke_shape(Some(&g), at, 1);
        assert!(!shape.puffs.is_empty());
        assert!(shape.cells.keys().all(|c| (c[2] > 18 && c[2] < 22) || c[1] >= 12), "nothing behind the walls");
        assert!(shape.puffs.iter().all(|p| p.delay >= 0.0 && p.delay <= SMOKE_BLOOM_S + 1e-3));
        let along = shape.puffs.iter().map(|p| (p.pos.x - at.x).abs()).fold(0.0, f32::max);
        assert!(along > SMOKE_RADIUS, "runs down the corridor: {along}");
    }

    #[test]
    fn without_a_grid_a_smoke_is_a_dome() {
        let shape = smoke_shape(None, Vec3::ZERO, 1);
        assert!(shape.puffs.len() > 20);
        assert!(shape.puffs.iter().all(|p| p.pos.y >= 0.0 && in_dome(Vec3::ZERO, p.pos) <= 1.1));
        assert!(in_dome(Vec3::ZERO, Vec3::new(0.0, 50.0, 0.0)) < 0.85, "the camera inside it");
        assert!(in_dome(Vec3::ZERO, Vec3::new(300.0, 0.0, 0.0)) > 1.0);
    }

    #[test]
    fn fire_lies_on_the_floor_it_reaches() {
        let g = grid([40, 10, 40], |x, y, _| y == 0 || (x == 22 && y < 8));
        let at = Vec3::new(18.0 * 16.0, 17.0, 20.0 * 16.0);
        let flames = fire_shape(Some(&g), at, 150.0, 3);
        assert!(flames.len() > 20, "{}", flames.len());
        assert!(flames.iter().all(|f| f.pos.y == at.y), "on the grenade's floor");
        assert!(flames.iter().all(|f| f.pos.x < 22.0 * 16.0 + 16.0), "not past the wall");
        let disc = fire_shape(None, at, 150.0, 3);
        assert!(disc.iter().all(|f| (f.pos - at).length() <= 150.0 + 1e-3));
    }

    #[test]
    fn hits_come_from_the_hurt_events() {
        let header = serde_json::json!({"events": [
            {"k": "hurt", "s": 873.0, "p": 1, "a": 8, "d": 100, "hg": "head", "w": "P2000"},
            {"k": "pickup", "s": 13.0, "p": 9, "item": "vest"},
            {"k": "hurt", "s": 881.0, "p": 8, "a": 2, "d": 9, "hg": "right_leg", "w": "GLOCK"},
            {"k": "hurt", "s": 890.0, "p": 8, "a": 2, "d": 4, "hg": "generic", "w": "\u{1f525}"},
            {"k": "hurt", "s": 891.0, "p": 8, "a": 8, "d": 12, "hg": "generic"},
        ]});
        let hits = read_hits(&header);
        assert_eq!(hits.len(), 2, "burning and falling draw no blood");
        assert_eq!((hits[0].victim, hits[0].attacker, hits[0].height), (1, 8, 62.0));
        assert_eq!(hits[1].height, 20.0);
        assert!(read_hits(&serde_json::json!({})).is_empty(), "an old replay has none");
    }

    #[test]
    fn the_bomb_beeps_faster_towards_the_end() {
        let bomb = Bomb { plant: 10.0, at: Vec3::ZERO, defused: None, exploded: Some(50.0) };
        assert_eq!(last_beep(&bomb, 9.0, 99.0), None, "not yet planted");
        assert_eq!(last_beep(&bomb, 10.05, 99.0), Some(10.0));
        let early = last_beep(&bomb, 11.5, 99.0).unwrap();
        assert!((early - 11.0).abs() < 1e-9, "a second apart at first: {early}");
        assert_eq!(last_beep(&bomb, 50.0, 99.0), None, "it has blown");
        // near the end they come every 0.15 s
        let a = last_beep(&bomb, 49.9, 99.0).unwrap();
        assert!(49.9 - a < 0.16, "{a}");
        let defused = Bomb { defused: Some(20.0), exploded: None, ..bomb };
        assert_eq!(last_beep(&defused, 21.0, 99.0), None, "silent once defused");
    }

    #[test]
    fn hit_groups_sit_at_their_height() {
        assert!(hit_height("head") > hit_height("chest"));
        assert!(hit_height("chest") > hit_height("stomach"));
        assert!(hit_height("stomach") > hit_height("left_leg"));
        assert_eq!(hit_height("generic"), 45.0);
    }
}
