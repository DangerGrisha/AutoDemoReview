//! Utility (smoke / molotov / HE / flash / decoy) visuals + throw tracers.
//! All constants ported 1:1 from the Three.js template in build3d_app.py; every
//! item's on-screen state is recomputed from the playback clock each frame so
//! arbitrary scrubbing stays correct. Meshes are built once per round load.

use bevy::prelude::*;
use serde_json::Value;

use crate::coords::world_to_scene;

pub const UTIL_COLORS: &[(&str, &str)] = &[
    ("smoke", "#d6dae1"),
    ("molotov", "#ff6a1a"),
    ("flash", "#ffffff"),
    ("he", "#ff5040"),
    ("decoy", "#9aa4b2"),
];

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
        mesh: Entity,
        /// grow window in samples: max(1, 1.2*rate)
        grow: f64,
    },
    Molotov {
        disc: Entity,
        flames: Vec<Entity>,
    },
    /// HE and flash detonation pops share the same expand+fade behavior.
    Burst {
        mesh: Entity,
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
    pub tracer: Option<Tracer>,
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
            let mesh = commands
                .spawn((
                    Mesh3d(meshes.add(Sphere::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#d6dae1"), false, false))),
                    Transform::from_translation(p + Vec3::Y * (r_s * 0.55)).with_scale(Vec3::ZERO),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            UtilItem {
                kind: UtilKind::Smoke { mesh, grow: (1.2 * rate).max(1.0) },
                s0,
                s1: s0 + (dur_s * rate).max(1.0),
                r_s,
                tracer: None,
            }
        }
        "molotov" => {
            let disc = commands
                .spawn((
                    Mesh3d(meshes.add(Circle::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ff6a1a"), true, true))),
                    Transform::from_translation(p + Vec3::Y * 0.03)
                        .with_rotation(Quat::from_rotation_x(-std::f32::consts::FRAC_PI_2))
                        .with_scale(Vec3::splat(r_s.max(0.1))),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            let mut flames = Vec::with_capacity(7);
            for k in 0..7 {
                let a = k as f32 / 7.0 * 6.2832;
                let rr = r_s * 0.5;
                flames.push(
                    commands
                        .spawn((
                            Mesh3d(meshes.add(Cone { radius: 0.22, height: 1.0 })),
                            MeshMaterial3d(materials.add(blend(hex_color("#ff9a2a"), true, false))),
                            Transform::from_translation(
                                p + Vec3::new(a.cos() * rr, 0.4, a.sin() * rr),
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
                tracer: None,
            }
        }
        "he" => {
            let mesh = commands
                .spawn((
                    Mesh3d(meshes.add(Sphere::new(1.0))),
                    MeshMaterial3d(materials.add(blend(hex_color("#ffb060"), true, false))),
                    Transform::from_translation(p + Vec3::Y * (r_s * 0.3)).with_scale(Vec3::ZERO),
                    Visibility::Hidden,
                    crate::state::UtilTag,
                ))
                .id();
            UtilItem {
                kind: UtilKind::Burst { mesh, scale: r_s * 0.5, base: 0.7 },
                s0,
                s1: s0 + (0.45 * rate).max(1.0),
                r_s,
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
                kind: UtilKind::Burst { mesh, scale: 1.1, base: 0.95 },
                s0,
                s1: s0 + (0.25 * rate).max(1.0),
                r_s,
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
            let a = Vec3::from_array(world_to_scene(
                tp[0].as_f64().unwrap_or(0.0),
                tp[1].as_f64().unwrap_or(0.0),
                tp.get(2).and_then(Value::as_f64).unwrap_or(0.0),
                s,
            ));
            let b = p;
            let arc_h = (Vec2::new(b.x - a.x, b.z - a.z).length() * 0.18).clamp(0.8, 7.0);
            let n = 40;
            let pts: Vec<Vec3> = (0..=n)
                .map(|k| {
                    let t = k as f32 / n as f32;
                    Vec3::new(
                        a.x + (b.x - a.x) * t,
                        a.y + (b.y - a.y) * t + arc_h * 4.0 * t * (1.0 - t),
                        a.z + (b.z - a.z) * t,
                    )
                })
                .collect();
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
    let n = tr.pts.len() - 1; // 40
    let count = ((prog * n as f64).floor() as usize + 1).max(2);
    let op = if s <= tr.df {
        0.95
    } else {
        (0.95 * (1.0 - (s - tr.df) / (tr.fade - tr.df))).max(0.0)
    } as f32;
    let tip = tr.pts[count.saturating_sub(1).min(n)];
    Some((count, op, tip))
}
