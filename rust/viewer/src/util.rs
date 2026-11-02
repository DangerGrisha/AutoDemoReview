//! Utility (smoke / molotov / HE / flash / decoy) visuals + throw tracers.
//! Smokes are churning multi-puff volumes, molotovs are flickering flame beds,
//! and HE/flash pops are an expanding core plus a ground shockwave — every item's
//! on-screen state is recomputed from the playback clock each frame so arbitrary
//! scrubbing stays correct. Meshes are built once per round load. Players render
//! x-ray on top of all of this (see lib.rs), so even a dense smoke never hides
//! them — you see everyone through it, like a demo with wallhack.

use bevy::prelude::*;
use serde_json::Value;

use crate::coords::world_to_scene;

/// Points a second on a recorded grenade flight's trail, so a bounce shows as a corner.
const PATH_POINTS_PER_S: f64 = 32.0;

pub const UTIL_COLORS: &[(&str, &str)] = &[
    ("smoke", "#d6dae1"),
    ("molotov", "#ff6a1a"),
    ("flash", "#ffffff"),
    ("he", "#ff5040"),
    ("decoy", "#9aa4b2"),
];

/// Soft puffs per smoke cloud (golden-angle scattered into a ball, then churned).
const SMOKE_PUFFS: usize = 12;
/// Flame tongues per molotov fire bed.
const MOLOTOV_FLAMES: usize = 15;

pub fn util_color(kind: &str) -> Color {
    let hex = UTIL_COLORS
        .iter()
        .find(|(k, _)| *k == kind)
        .map(|(_, c)| *c)
        .unwrap_or("#cccccc");
    hex_color(hex)
}

pub fn hex_color(hex: &str) -> Color {
    let h = hex.trim_start_matches('#');
    let p = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).unwrap_or(0xcc);
    Color::srgb_u8(p(0), p(2), p(4))
}

pub enum UtilKind {
    Smoke {
        /// individual puff spheres making up the volume
        puffs: Vec<Entity>,
        /// grow window in samples: max(1, 1.2*rate)
        grow: f64,
    },
    Molotov {
        disc: Entity,
        flames: Vec<Entity>,
    },
    /// HE and flash detonation pops: an expanding core sphere, plus (HE only) a
    /// flat ground shockwave ring that races outward and fades.
    Burst {
        mesh: Entity,
        ring: Option<Entity>,
        scale: f32,
        base: f32,
    },
    Decoy {
        mesh: Entity,
    },
}

pub struct Tracer {
    /// 41 precomputed parabola points in scene space (thrower -> detonation)
    pub pts: Vec<Vec3>,
    pub tf: f64,
    pub df: f64,
    pub fade: f64,
    pub color: Color,
}

pub struct UtilItem {
    pub kind: UtilKind,
    pub s0: f64,
    pub s1: f64,
    /// radius * SCENE_SCALE (meaning varies slightly per kind, as in the template)
    pub r_s: f32,
    /// scene-space detonation center — the anchor puffs/ring animate around
    pub pos: Vec3,
    pub tracer: Option<Tracer>,
}

/// Deterministic golden-angle point on the UNIT sphere (no RNG, reproducible
/// across scrubs). Smoke puffs are laid on a shell of radius 0.5*r_s and drawn
/// 0.5*r_s across, so the cloud's outer radius is exactly r_s (CS 1:1) and the
/// big puffs overlap into one coherent mass rather than distinct balloons.
pub fn puff_offset(i: usize, n: usize) -> Vec3 {
    let y = 1.0 - 2.0 * (i as f32 + 0.5) / n as f32; // -1 .. 1
    let r = (1.0 - y * y).max(0.0).sqrt();
    let theta = 2.399_963_2 * i as f32; // golden angle (radians)
    Vec3::new(r * theta.cos(), y, r * theta.sin())
}

/// Per-puff / per-flame animation phase so neighbours churn out of sync.
pub fn wobble_phase(i: usize) -> f32 {
    i as f32 * 1.7
}

fn f(v: &Value, key: &str) -> Option<f64> {
    v.get(key).and_then(Value::as_f64)
}

/// Build one utility item's entities from its header JSON dict. Mirrors buildUtility().
/// (Tracer label text is rendered by the page from header.utility[idx].thrower.)
pub fn build_util_item(
    commands: &mut Commands,
    meshes: &mut Assets<Mesh>,
    materials: &mut Assets<StandardMaterial>,
    u: &Value,
    s: f64,
    rate: f64,
) -> Option<UtilItem> {
    let utype = u.get("type").and_then(Value::as_str)?;
    let pos_arr = u.get("pos").and_then(Value::as_array)?;
    let p = world_to_scene(
        pos_arr.first().and_then(Value::as_f64)?,
        pos_arr.get(1).and_then(Value::as_f64)?,
        pos_arr.get(2).and_then(Value::as_f64).unwrap_or(0.0),
        s,
    );
    let p = Vec3::from_array(p);
    let r_s = (f(u, "radius").unwrap_or(0.0) * s) as f32;
    let s0 = f(u, "detSample").unwrap_or(0.0);
    let dur_s = f(u, "duration").unwrap_or(0.0);

    let blend = |color: Color, unlit: bool, double: bool| StandardMaterial {
        base_color: color.with_alpha(0.0),
        unlit,
        double_sided: double,
        cull_mode: if double { None } else { Some(bevy::render::render_resource::Face::Back) },
        alpha_mode: AlphaMode::Blend,
        perceptual_roughness: 1.0,
        ..default()
    };

    let mut item = match utype {
        "smoke" => {
            // a dozen lit puffs; each gets its own material so they can vary alpha
            // independently as the cloud churns.
            let mut puffs = Vec::with_capacity(SMOKE_PUFFS);
            for _ in 0..SMOKE_PUFFS {
                puffs.push(
                    commands
                        .spawn((
                            Mesh3d(meshes.add(Sphere::new(1.0))),
                            MeshMaterial3d(materials.add(blend(hex_color("#d9dde4"), false, false))),
                            Transform::from_translation(p).with_scale(Vec3::ZERO),
                            Visibility::Hidden,
                            crate::state::UtilTag,
                        ))
                        .id(),
                );
            }
            UtilItem {
                kind: UtilKind::Smoke { puffs, grow: (1.2 * rate).max(1.0) },
                s0,
                s1: s0 + (dur_s * rate).max(1.0),
                r_s,
                pos: p,
                tracer: None,
            }
        }
        "molotov" => {
            let disc = commands
                .spawn((
                    Mesh3d(meshes.add(Circle::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ff7a1e"), true, true))),
                    Transform::from_translation(p + Vec3::Y * 0.03)
                        .with_rotation(Quat::from_rotation_x(-std::f32::consts::FRAC_PI_2))
                        .with_scale(Vec3::splat(r_s.max(0.1))),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            // flame tongues scattered across the fire bed (golden angle so they
            // spread evenly), a mix of yellow cores and orange bodies.
            let mut flames = Vec::with_capacity(MOLOTOV_FLAMES);
            for k in 0..MOLOTOV_FLAMES {
                let a = 2.399_963_2 * k as f32;
                let rr = r_s * (0.12 + 0.82 * ((k as f32 + 0.5) / MOLOTOV_FLAMES as f32).sqrt());
                let col = if k % 3 == 0 { "#ffd24a" } else { "#ff6a1a" };
                flames.push(
                    commands
                        .spawn((
                            Mesh3d(meshes.add(Cone { radius: 0.17, height: 1.0 })),
                            MeshMaterial3d(materials.add(blend(hex_color(col), true, false))),
                            Transform::from_translation(
                                p + Vec3::new(a.cos() * rr, 0.35, a.sin() * rr),
                            ),
                            Visibility::Hidden,
                            crate::state::UtilTag,
                        ))
                        .id(),
                );
            }
            UtilItem {
                kind: UtilKind::Molotov { disc, flames },
                s0,
                s1: s0 + (dur_s * rate).max(1.0),
                r_s,
                pos: p,
                tracer: None,
            }
        }
        "he" => {
            let mesh = commands
                .spawn((
                    Mesh3d(meshes.add(Sphere::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ffd08a"), true, false))),
                    Transform::from_translation(p + Vec3::Y * (r_s * 0.3)).with_scale(Vec3::ZERO),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            // ground shockwave ring: a flat annulus that expands fast and fades.
            let ring = commands
                .spawn((
                    Mesh3d(meshes.add(Annulus::new(0.74, 1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ffe0a0"), true, true))),
                    Transform::from_translation(p + Vec3::Y * 0.05)
                        .with_rotation(Quat::from_rotation_x(-std::f32::consts::FRAC_PI_2))
                        .with_scale(Vec3::ZERO),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            UtilItem {
                kind: UtilKind::Burst { mesh, ring: Some(ring), scale: r_s * 0.6, base: 0.85 },
                s0,
                s1: s0 + (0.5 * rate).max(1.0),
                r_s,
                pos: p,
                tracer: None,
            }
        }
        "flash" => {
            let mesh = commands
                .spawn((
                    Mesh3d(meshes.add(Sphere::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ffffff"), true, false))),
                    Transform::from_translation(p + Vec3::Y * 0.6).with_scale(Vec3::splat(1.1)),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            UtilItem {
                kind: UtilKind::Burst { mesh, ring: None, scale: 1.1, base: 0.95 },
                s0,
                s1: s0 + (0.25 * rate).max(1.0),
                r_s,
                pos: p,
                tracer: None,
            }
        }
        "decoy" => {
            let mesh = commands
                .spawn((
                    Mesh3d(meshes.add(Sphere::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#9aa4b2"), true, false))),
                    Transform::from_translation(p + Vec3::Y * 0.5).with_scale(Vec3::splat(0.4)),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            UtilItem {
                kind: UtilKind::Decoy { mesh },
                s0,
                s1: s0 + (dur_s * rate).max(1.0),
                r_s,
                pos: p,
                tracer: None,
            }
        }
        _ => return None,
    };

    // throw tracer (only when we have a throw origin and det strictly after throw)
    if let (Some(tp), Some(tf), Some(df)) =
        (u.get("throwPos").and_then(Value::as_array), f(u, "throwSample"), f(u, "detSample"))
    {
        if df > tf && tp.len() >= 2 {
            let recorded = u
                .get("path")
                .and_then(Value::as_array)
                .map(|path| path.iter().filter_map(Value::as_f64).collect::<Vec<f64>>())
                .filter(|flat| flat.len() >= 8 && flat.len() % 4 == 0);
            let pts: Vec<Vec3> = if let Some(flat) = recorded {
                let n = (((df - tf) / rate * PATH_POINTS_PER_S).ceil() as usize).max(8);
                path_points(&flat, tf, df, n)
                    .into_iter()
                    .map(|[x, y, z]| Vec3::from_array(world_to_scene(x, y, z, s)))
                    .collect()
            } else {
                // A replay from before data version 2 knows only the two ends: an invented arc.
                let a = Vec3::from_array(world_to_scene(
                    tp[0].as_f64().unwrap_or(0.0),
                    tp[1].as_f64().unwrap_or(0.0),
                    tp.get(2).and_then(Value::as_f64).unwrap_or(0.0),
                    s,
                ));
                let b = p;
                let arc_h = (Vec2::new(b.x - a.x, b.z - a.z).length() * 0.18).clamp(0.8, 7.0);
                let n = 40;
                (0..=n)
                    .map(|k| {
                        let t = k as f32 / n as f32;
                        Vec3::new(
                            a.x + (b.x - a.x) * t,
                            a.y + (b.y - a.y) * t + arc_h * 4.0 * t * (1.0 - t),
                            a.z + (b.z - a.z) * t,
                        )
                    })
                    .collect()
            };
            item.tracer = Some(Tracer {
                pts,
                tf,
                df,
                fade: df + (1.5 * rate).max(1.0),
                color: util_color(utype),
            });
        }
    }
    Some(item)
}

/// A recorded flight (flat `[sample, x, y, z, ...]`, at least two points) as `n + 1` world
/// points at even times from `tf` to `df`, the spacing `tracer_frame` walks. Outside the
/// recording it holds the nearest end.
pub fn path_points(flat: &[f64], tf: f64, df: f64, n: usize) -> Vec<[f64; 3]> {
    let rows: Vec<&[f64]> = flat.chunks_exact(4).collect();
    let mut seg = 0;
    (0..=n)
        .map(|k| {
            let s = tf + (df - tf) * k as f64 / n as f64;
            while seg + 2 < rows.len() && rows[seg + 1][0] < s {
                seg += 1;
            }
            let (a, b) = (rows[seg], rows[seg + 1]);
            let t = if b[0] > a[0] { ((s - a[0]) / (b[0] - a[0])).clamp(0.0, 1.0) } else { 0.0 };
            [a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t]
        })
        .collect()
}

/// Per-frame animation state for one item (mirrors updateUtility()). Returns
/// per-part (visible, opacity, scale) via the closure-friendly struct below.
pub struct UtilFrame {
    pub in_window: bool,
    /// normalized progress through [s0, s1]
    pub p: f64,
}

pub fn util_frame(item: &UtilItem, s: f64) -> UtilFrame {
    let in_window = s >= item.s0 && s <= item.s1;
    let p = if item.s1 > item.s0 { (s - item.s0) / (item.s1 - item.s0) } else { 1.0 };
    UtilFrame { in_window, p }
}

/// Tracer per-frame state: (visible, draw_count, opacity, tip). Mirrors updateTracer().
pub fn tracer_frame(tr: &Tracer, s: f64) -> Option<(usize, f32, Vec3)> {
    if s < tr.tf || s > tr.fade {
        return None;
    }
    let prog = if tr.df > tr.tf { ((s - tr.tf) / (tr.df - tr.tf)).min(1.0) } else { 1.0 };
    let n = tr.pts.len() - 1;
    let count = ((prog * n as f64).floor() as usize + 1).max(2);
    let op = if s <= tr.df {
        0.95
    } else {
        (0.95 * (1.0 - (s - tr.df) / (tr.fade - tr.df))).max(0.0)
    } as f32;
    let tip = tr.pts[count.saturating_sub(1).min(n)];
    Some((count, op, tip))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_recorded_flight_is_walked_at_even_times() {
        // up to z=100 by sample 10, a bounce back to z=0 by sample 20
        let flat = [0.0, 0.0, 0.0, 0.0, 10.0, 10.0, 0.0, 100.0, 20.0, 20.0, 0.0, 0.0];
        let pts = path_points(&flat, 0.0, 20.0, 4);
        assert_eq!(pts.len(), 5);
        let zs: Vec<f64> = pts.iter().map(|p| p[2]).collect();
        assert_eq!(zs, [0.0, 50.0, 100.0, 50.0, 0.0], "the corner of the bounce stays");
        let xs: Vec<f64> = pts.iter().map(|p| p[0]).collect();
        assert_eq!(xs, [0.0, 5.0, 10.0, 15.0, 20.0]);
    }

    #[test]
    fn a_flight_outside_its_recording_holds_the_ends() {
        let flat = [5.0, 1.0, 2.0, 3.0, 10.0, 4.0, 5.0, 6.0];
        let pts = path_points(&flat, 0.0, 20.0, 4);
        assert_eq!(pts[0], [1.0, 2.0, 3.0], "before the first point");
        assert_eq!(pts[4], [4.0, 5.0, 6.0], "after the last, as a resting grenade");
    }

    #[test]
    fn two_points_at_one_moment_do_not_divide_by_zero() {
        let flat = [5.0, 1.0, 2.0, 3.0, 5.0, 4.0, 5.0, 6.0];
        assert!(path_points(&flat, 5.0, 5.0, 2).iter().all(|p| p.iter().all(|v| v.is_finite())));
    }
}
