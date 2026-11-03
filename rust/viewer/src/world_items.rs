//! Real CS2 models that lie or fly in the world on their own: grenades along their
//! recorded flight, the gun a player drops where they die, and the planted bomb. Also the
//! shells guns throw out, as small brass cylinders.
//!
//! Each frame works out which should be where from the clock alone, then spawns,
//! moves and despawns entities to match, so a scrub needs nothing remembered.

use std::collections::HashMap;
use std::f32::consts::{FRAC_PI_2, TAU};

use bevy::prelude::*;
use serde_json::Value;

use crate::effects::{feet_at, round_bomb};
use crate::state::{RoundState, Viewer};
use crate::weapons_viz::{current_tag, RealWeapons};

/// Models are in metres and the bone frames a player model gives them are in Hammer units.
const METRES_PER_UNIT: f32 = 0.0254;
/// The gap under what lies on the floor, in Hammer units, so it does not flicker into it.
const FLOOR_GAP: f32 = 0.2;
/// How far in front of a player their gun lands.
const DROP_AHEAD: f32 = 18.0;
/// A pickup this close to a dropped gun, at the moment it happens, takes it.
const PICKUP_REACH: f32 = 72.0;
/// Turns a second of a grenade in the air.
const GRENADE_SPIN: f32 = 2.2;
/// A flight's last point is a grenade's middle, this far above the floor it rests on.
const GRENADE_RADIUS: f32 = 1.5;

/// What a grenade type is held as, by the thrower's team for the two fire grenades.
fn grenade_tag(utype: &str, team: i64) -> Option<&'static str> {
    Some(match utype {
        "smoke" => "SMOKE",
        "flash" => "FLASH",
        "he" => "HE",
        "decoy" => "DECOY",
        "molotov" if team == crate::scene::TEAM_T => "MOLLY",
        "molotov" => "INC",
        _ => return None,
    })
}

/// What a dead player leaves on the floor: a gun, but no knife and no grenade.
fn drops(tag: &str) -> bool {
    !matches!(tag, "\u{1f52a}" | "\u{1f4a3}" | "HE" | "FLASH" | "SMOKE" | "MOLLY" | "INC" | "DECOY")
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum ItemKey {
    Grenade(usize),
    Drop(usize),
    Bomb,
    DroppedBomb(usize),
}

/// Where a model goes: along a flight as it is, or set down with its lowest point on a floor.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Place {
    At(Vec3),
    OnFloor(Vec3),
}

/// How far below its origin a model's lowest point is once turned, in Hammer units.
fn depth_below(q: Quat, (lo, hi): (Vec3, Vec3)) -> f32 {
    let lowest = (0..8)
        .map(|c| Vec3::new(if c & 1 == 0 { lo.x } else { hi.x }, if c & 2 == 0 { lo.y } else { hi.y }, if c & 4 == 0 { lo.z } else { hi.z }))
        .map(|corner| (q * corner).y)
        .fold(f32::INFINITY, f32::min);
    -lowest / METRES_PER_UNIT
}

#[derive(Component)]
pub struct WorldItem {
    key: ItemKey,
    file: &'static str,
    round: (i64, i64),
}

/// Lying on its right side, barrel along `yaw` (scene radians about +Y): the model's barrel
/// (+X) goes there, its left (+Y) up, its top (+Z) to the side.
fn lying(yaw: f32) -> Quat {
    let barrel = Vec3::new(yaw.cos(), 0.0, -yaw.sin());
    let side = barrel.cross(Vec3::Y);
    Quat::from_mat3(&Mat3::from_cols(barrel, Vec3::Y, side))
}

/// Upright, as the bomb is planted: the model's top (+Z) up.
fn upright(yaw: f32) -> Quat {
    Quat::from_rotation_y(yaw) * Quat::from_rotation_x(-FRAC_PI_2)
}

/// Survivors lose the alive flag on the round's last sample, so a death counts when the kill
/// list has it, or, for a round without one, when it is not that last sample.
fn really_died(kills: &[crate::state::KillFact], name: Option<&str>, death: usize, n: usize) -> bool {
    if kills.is_empty() {
        return death + 1 < n;
    }
    kills.iter().any(|k| Some(k.victim.as_str()) == name)
}

/// A pickup at `sample` by someone standing within reach of `at`.
fn picked_up(r: &RoundState, events: &[Value], kind: &str, from: f64, at: Vec3) -> Option<f64> {
    events
        .iter()
        .filter(|e| e.get("k").and_then(Value::as_str) == Some(kind))
        .filter_map(|e| Some((e.get("s")?.as_f64()?, e.get("p")?.as_u64()? as usize)))
        .filter(|&(s, _)| s > from)
        .find(|&(s, p)| feet_at(r, p, s).is_some_and(|(f, _)| f.distance(at) < PICKUP_REACH))
        .map(|(s, _)| s)
}

/// Where each model is this frame, in the map file's frame, with its turn.
fn wanted(r: &RoundState, m: &crate::state::ManifestData, real: &RealWeapons, clock: f64) -> Vec<(ItemKey, &'static str, Place, Quat)> {
    let mut out = Vec::new();
    let s = m.scene_scale as f32;
    let sample = clock * r.rate;
    let team_of = |sid: Option<&str>| {
        sid.and_then(|sid| r.dec.players.iter().find(|p| p.sid == sid)).map_or(0, |p| p.team)
    };

    // grenades in the air, and the ones that lie where they landed while they work
    for (idx, item) in r.utils.iter().enumerate() {
        let Some(tracer) = &item.tracer else { continue };
        let Some(tag) = grenade_tag(item.utype, team_of(item.thrower.as_deref())) else { continue };
        let Some(file) = real.ready_file(tag, 0) else { continue };
        let (tf, df) = (tracer.tf, tracer.df);
        if sample < tf {
            continue;
        }
        let lies = matches!(item.utype, "smoke" | "decoy");
        if sample > df && !(lies && sample <= item.s1) {
            continue;
        }
        let p = crate::util::along_flight(tracer, sample) / s;
        let flying = sample <= df;
        let turn = if flying {
            let age = ((sample - tf) / r.rate) as f32;
            Quat::from_rotation_y(idx as f32) * Quat::from_rotation_x(age * GRENADE_SPIN * TAU)
        } else {
            lying(idx as f32)
        };
        let place = if flying { Place::At(p) } else { Place::OnFloor(p - Vec3::Y * GRENADE_RADIUS) };
        out.push((ItemKey::Grenade(idx), file, place, turn));
    }

    let events: &[Value] = r.dec.header.get("events").and_then(Value::as_array).map_or(&[], Vec::as_slice);

    // the gun each dead player held, from the sample they died
    let kills = m.kills.get(&r.dec.round).map_or(&[][..], Vec::as_slice);
    for (slot, tr) in r.dec.tracks.iter().enumerate() {
        let Some(death) = (1..r.n).find(|&i| tr.flags[i - 1] & c2r3::F_ALIVE != 0 && tr.flags[i] & c2r3::F_ALIVE == 0) else {
            continue;
        };
        if (death as f64) > sample || !really_died(kills, r.dec.players.get(slot).map(|p| p.name.as_str()), death, r.n) {
            continue;
        }
        let Some(tag) = current_tag(r, slot, death - 1) else { continue };
        if !drops(tag) {
            continue;
        }
        let team = r.dec.players.get(slot).map_or(0, |p| p.team);
        let Some(file) = real.ready_file(tag, team) else { continue };
        let Some((feet, _)) = feet_at(r, slot, (death - 1) as f64) else { continue };
        let yaw = (tr.yaw[death - 1] as f32).to_radians();
        let ahead = Vec3::new(yaw.cos(), 0.0, -yaw.sin()) * DROP_AHEAD;
        let at = feet + ahead;
        if picked_up(r, events, "pickup", death as f64, at).is_some_and(|s| s <= sample) {
            continue;
        }
        out.push((ItemKey::Drop(slot), file, Place::OnFloor(at), lying(yaw + 0.6)));
    }

    // the bomb, dropped by whoever carried it and lying until picked up
    if let Some(file) = real.ready_file("\u{1f4a3}", 0) {
        for (i, e) in events.iter().enumerate() {
            if e.get("k").and_then(Value::as_str) != Some("bombDrop") {
                continue;
            }
            let (Some(at_s), Some(p)) = (e.get("s").and_then(Value::as_f64), e.get("p").and_then(Value::as_u64)) else { continue };
            if at_s > sample {
                continue;
            }
            let Some((feet, _)) = feet_at(r, p as usize, at_s) else { continue };
            let back = events
                .iter()
                .filter(|e| e.get("k").and_then(Value::as_str) == Some("bombPickup"))
                .filter_map(|e| e.get("s").and_then(Value::as_f64))
                .find(|&s| s > at_s);
            if back.is_some_and(|s| s <= sample) {
                continue;
            }
            out.push((ItemKey::DroppedBomb(i), file, Place::OnFloor(feet), upright(i as f32)));
        }
        // planted, until it blows
        if let Some(bomb) = round_bomb(r, m) {
            let gone = bomb.exploded.is_some_and(|t| clock >= t);
            if clock >= bomb.plant && !gone {
                out.push((ItemKey::Bomb, file, Place::OnFloor(bomb.at), upright(0.0)));
            }
        }
    }
    out
}

/// Spawn, move and despawn the world's loose models to match the clock.
pub fn update_world_items(
    mut commands: Commands,
    viewer: Res<Viewer>,
    real: Res<RealWeapons>,
    mut q_items: Query<(Entity, &WorldItem, &mut Transform)>,
) {
    let (Some(r), Some(m)) = (viewer.round.as_ref(), viewer.manifest.as_ref()) else {
        for (e, _, _) in &q_items {
            commands.entity(e).despawn();
        }
        return;
    };
    let round = (r.dec.round, r.dec.start_tick);
    let s = m.scene_scale as f32;
    let scale = Vec3::splat(s / METRES_PER_UNIT);
    let mut want: HashMap<ItemKey, (&'static str, Vec3, Quat)> = HashMap::new();
    for (key, file, place, q) in wanted(r, m, &real, viewer.clock) {
        let Some((_, _, bounds)) = real.mesh_of(file) else { continue };
        let p = match place {
            Place::At(p) => p,
            Place::OnFloor(floor) => floor + Vec3::Y * (depth_below(q, bounds) + FLOOR_GAP),
        };
        want.insert(key, (file, p, q));
    }
    for (e, item, mut tf) in &mut q_items {
        match want.get(&item.key) {
            Some(&(file, p, q)) if file == item.file && item.round == round => {
                tf.translation = p * s;
                tf.rotation = q;
                want.remove(&item.key);
            }
            _ => commands.entity(e).despawn(),
        }
    }
    for (key, (file, p, q)) in want {
        let Some((mesh, material, _)) = real.mesh_of(file) else { continue };
        commands.spawn((
            Mesh3d(mesh),
            MeshMaterial3d(material),
            Transform { translation: p * s, rotation: q, scale },
            WorldItem { key, file, round },
            crate::weapons_viz::WeaponPart,
        ));
    }
}

/// How long a shell flies and then lies on the floor.
const SHELL_LIFE_S: f64 = 2.0;
/// CS2's gravity, in Source units a second squared.
const GRAVITY: f32 = 800.0;
/// Shells further than this from the camera are too small to see.
const SHELL_REACH: f32 = 900.0;
/// At most this many shells at once.
const SHELL_POOL: usize = 64;
/// A rifle case: 1.5 Source units long and a little under half a unit across.
const SHELL_LENGTH: f32 = 1.5;
const SHELL_RADIUS: f32 = 0.22;

/// Where a shell is `age` seconds after its shot, in the map file's frame, and how it is
/// turned: out of the right side of the gun, up a little, then down onto the floor, where it
/// stays lying across the way it fell.
fn shell_at(eye: Vec3, aim: Vec3, floor: f32, seed: u32, age: f32) -> (Vec3, Quat) {
    let h = |k: u32| crate::fx::hash01(seed, k);
    let flat = Vec3::new(aim.x, 0.0, aim.z).normalize_or(Vec3::X);
    let right = flat.cross(Vec3::Y).normalize_or(Vec3::Z);
    let start = eye + aim * 14.0 + right * 5.0 - Vec3::Y * 7.0;
    let v = right * (100.0 + 50.0 * h(1)) + Vec3::Y * (60.0 + 40.0 * h(2)) - flat * (10.0 + 30.0 * h(3));
    let rest = floor + SHELL_RADIUS;
    // when it reaches the floor: start.y + v.y t - g t^2 / 2 = rest
    let drop = (start.y - rest).max(0.0);
    let land = (v.y + (v.y * v.y + 2.0 * GRAVITY * drop).sqrt()) / GRAVITY;
    let t = age.min(land);
    let mut p = start + v * t - Vec3::Y * (0.5 * GRAVITY * t * t);
    p.y = p.y.max(rest);
    let spin = Quat::from_axis_angle(flat, t * (25.0 + 15.0 * h(4))) * Quat::from_axis_angle(Vec3::Y, t * 8.0);
    // a cylinder stands along +Y: lay it across the aim, as it leaves the gun
    let across = Quat::from_rotation_arc(Vec3::Y, right);
    let turn = if age < land { spin * across } else { Quat::from_axis_angle(Vec3::Y, h(5) * TAU) * Quat::from_rotation_arc(Vec3::Y, Vec3::X) };
    (p, turn)
}

#[derive(Component)]
pub struct Shell;

/// Brass shells out of every gun fired in the last couple of seconds near the camera.
#[allow(clippy::too_many_arguments)]
pub fn update_shells(
    mut commands: Commands,
    viewer: Res<Viewer>,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
    mut look: Local<Option<(Handle<Mesh>, Handle<StandardMaterial>)>>,
    mut pool: Local<Vec<Entity>>,
    q_cam: Query<&GlobalTransform, With<crate::state::MainCam>>,
    mut q_shells: Query<(&mut Transform, &mut Visibility), With<Shell>>,
) {
    let (mesh, material) = look
        .get_or_insert_with(|| {
            (
                meshes.add(Cylinder::new(SHELL_RADIUS, SHELL_LENGTH)),
                materials.add(StandardMaterial {
                    base_color: Color::srgb(0.80, 0.62, 0.28),
                    perceptual_roughness: 0.35,
                    ..default()
                }),
            )
        })
        .clone();
    let mut used = 0;
    if let (Some(r), Some(m), Ok(cam)) = (viewer.round.as_ref(), viewer.manifest.as_ref(), q_cam.single()) {
        let s = m.scene_scale as f32;
        let cam = cam.translation() / s;
        let clock = viewer.clock;
        for (k, shot) in r.shots.iter().enumerate() {
            let age = clock - shot.t;
            if !(0.0..SHELL_LIFE_S).contains(&age) || used == SHELL_POOL {
                continue;
            }
            let eye = shot.a / s;
            if eye.distance(cam) > SHELL_REACH {
                continue;
            }
            let sample = shot.t * r.rate;
            let Some(tag) = current_tag(r, shot.slot, sample as usize) else { continue };
            if !drops(tag) || tag.starts_with("ZEUS") {
                continue;
            }
            let Some((feet, _)) = feet_at(r, shot.slot, sample) else { continue };
            let aim = (shot.b - shot.a).normalize_or(Vec3::X);
            let (p, q) = shell_at(eye, aim, feet.y, k as u32, age as f32);
            if pool.len() == used {
                let e = commands
                    .spawn((
                        Mesh3d(mesh.clone()),
                        MeshMaterial3d(material.clone()),
                        Transform { translation: p * s, rotation: q, scale: Vec3::splat(s) },
                        bevy::light::NotShadowCaster,
                        Shell,
                    ))
                    .id();
                pool.push(e);
            } else if let Ok((mut tf, mut vis)) = q_shells.get_mut(pool[used]) {
                tf.translation = p * s;
                tf.rotation = q;
                tf.scale = Vec3::splat(s);
                *vis = Visibility::Inherited;
            }
            used += 1;
        }
    }
    for &e in &pool[used..] {
        if let Ok((_, mut vis)) = q_shells.get_mut(e) {
            *vis = Visibility::Hidden;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_fire_grenade_is_the_one_its_side_carries() {
        assert_eq!(grenade_tag("molotov", crate::scene::TEAM_T), Some("MOLLY"));
        assert_eq!(grenade_tag("molotov", 3), Some("INC"));
        assert_eq!(grenade_tag("smoke", 2), Some("SMOKE"));
        assert_eq!(grenade_tag("nothing", 2), None);
    }

    #[test]
    fn knives_and_grenades_are_not_dropped() {
        assert!(drops("AK"));
        assert!(drops("AWP"));
        assert!(!drops("\u{1f52a}"));
        assert!(!drops("HE"));
        assert!(!drops("\u{1f4a3}"), "the bomb has its own drop event");
    }

    fn kill(victim: &str) -> crate::state::KillFact {
        crate::state::KillFact { sample: 10, attacker: "a".into(), victim: victim.into(), weapon: "AK".into(), headshot: false }
    }

    #[test]
    fn a_survivor_drops_nothing_when_the_round_ends() {
        let kills = [kill("bob")];
        assert!(really_died(&kills, Some("bob"), 10, 100));
        assert!(!really_died(&kills, Some("ann"), 99, 100), "ann was not killed");
        assert!(really_died(&[], Some("ann"), 50, 100), "no kill list: the flag decides");
        assert!(!really_died(&[], Some("ann"), 99, 100), "but not on the last sample");
    }

    #[test]
    fn a_model_is_set_down_by_its_lowest_corner() {
        let inch = METRES_PER_UNIT;
        // the bomb's box: its origin sits 4.6 units above its bottom when it lies flat
        let c4 = (Vec3::new(-5.3, 0.3, -4.6) * inch, Vec3::new(5.2, 7.7, -1.3) * inch);
        assert!((depth_below(upright(0.7), c4) - 4.6).abs() < 1e-3);
        // an AK on its side rests on its left, which is 1.6 units off its origin
        let ak = (Vec3::new(-14.3, -1.6, -9.2) * inch, Vec3::new(22.9, 1.2, 1.5) * inch);
        assert!((depth_below(lying(2.0), ak) - 1.6).abs() < 1e-3);
    }

    #[test]
    fn a_shell_flies_right_and_comes_to_rest_on_the_floor() {
        let eye = Vec3::new(0.0, 64.0, 0.0);
        let aim = Vec3::X;
        let (early, _) = shell_at(eye, aim, 0.0, 7, 0.05);
        let start = eye + aim * 14.0 + Vec3::Z * 5.0 - Vec3::Y * 7.0;
        // facing +X with +Y up, the right hand side is +Z
        assert!(early.z > start.z, "out to the right: {early:?}");
        let (late, _) = shell_at(eye, aim, 0.0, 7, 1.5);
        let (later, _) = shell_at(eye, aim, 0.0, 7, 1.9);
        assert!((late.y - SHELL_RADIUS).abs() < 1e-3, "on the floor: {late:?}");
        assert_eq!(late, later, "and still");
        assert!(late.distance(start) < 120.0, "not far: {late:?}");
    }

    #[test]
    fn a_dropped_gun_lies_on_its_side() {
        let q = lying(0.3);
        let up = q * Vec3::Y;
        assert!((up - Vec3::Y).length() < 1e-5, "its left side faces up");
        let barrel = q * Vec3::X;
        assert!(barrel.y.abs() < 1e-5, "its barrel is level");
        assert!((barrel - Vec3::new(0.3f32.cos(), 0.0, -0.3f32.sin())).length() < 1e-5);
        assert!((q.length() - 1.0).abs() < 1e-5, "a pure turn");
    }
}
