//! Greybox construction + player entity pool. All geometry/color constants ported
//! 1:1 from the Three.js template (buildGreybox / player meshes in build3d_app.py).

use std::collections::HashMap;

use bevy::prelude::*;
use c2r3::DecodedRound;
use serde_json::Value;

use bevy::gltf::{GltfAssetLabel, GltfLoaderSettings};

use crate::coords::{radar_ground_rect, world_to_scene, Calibration};
use crate::state::{GreyboxTag, ManifestData, MapTag, PlayerBody, PlayerNose};
use crate::util::hex_color;

pub const CAP_H: f32 = 0.35 * 2.0 + 1.0; // capsule total height (r 0.35, cylinder 1.0)

pub fn parse_manifest(json: &str) -> Result<ManifestData, String> {
    let v: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
    let cal_v = v.get("calibration").ok_or("manifest missing calibration")?;
    let g = |k: &str| cal_v.get(k).and_then(Value::as_f64).ok_or(format!("calibration.{k}"));
    let cal = Calibration {
        pos_x: g("pos_x")?,
        pos_y: g("pos_y")?,
        scale: g("scale")?,
        size: g("size")?,
    };
    let scene_scale = v.get("sceneScale").and_then(Value::as_f64).unwrap_or(0.02);
    let mut team_colors = HashMap::new();
    if let Some(tc) = v.get("teamColors").and_then(Value::as_object) {
        for (k, hex) in tc {
            if let (Ok(team), Some(h)) = (k.parse::<i64>(), hex.as_str()) {
                team_colors.insert(team, hex_color(h));
            }
        }
    }
    let elevation_world_z: Vec<f64> = v
        .get("elevationWorldZ")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_f64).collect())
        .unwrap_or_default();
    let ground_y = (elevation_world_z.first().copied().unwrap_or(0.0) * scene_scale) as f32;
    let radar_asset = v
        .get("radarAsset")
        .and_then(Value::as_str)
        .unwrap_or("de_mirage.png")
        .to_string();
    let map_asset = v
        .get("mapAsset")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string);
    Ok(ManifestData {
        scene_scale,
        cal,
        team_colors,
        elevation_world_z,
        ground_y,
        radar_asset,
        map_asset,
    })
}

/// Build the static greybox: radar ground plane, elevation planes, perimeter walls.
/// (The grid is drawn per-frame with gizmos; see draw_grid.) Returns the scene-space
/// center the camera should orbit. `visible` is false when the real map geometry
/// is shown instead (the JS Map/Radar toggle flips it back).
pub fn build_greybox(
    commands: &mut Commands,
    meshes: &mut Assets<Mesh>,
    materials: &mut Assets<StandardMaterial>,
    asset_server: &AssetServer,
    m: &ManifestData,
    visible: bool,
) -> Vec3 {
    let s = m.scene_scale;
    let rect = radar_ground_rect(&m.cal, s);
    let ground_y = m.ground_y;
    let vis = if visible { Visibility::Visible } else { Visibility::Hidden };

    // radar-textured ground plane (MeshBasicMaterial equivalent: unlit)
    let radar_tex: Handle<Image> = asset_server.load(m.radar_asset.clone());
    commands.spawn((
        Mesh3d(meshes.add(Plane3d::default().mesh().size(rect.half_x * 2.0, rect.half_z * 2.0))),
        MeshMaterial3d(materials.add(StandardMaterial {
            base_color_texture: Some(radar_tex),
            unlit: true,
            ..default()
        })),
        Transform::from_xyz(rect.center[0], ground_y - 0.02, rect.center[2]),
        GreyboxTag,
        vis,
    ));

    // translucent elevation planes at each match-wide level (opacity .09, no depth write)
    for wz in &m.elevation_world_z {
        commands.spawn((
            Mesh3d(meshes.add(
                Plane3d::default().mesh().size(rect.half_x * 1.6, rect.half_z * 1.6),
            )),
            MeshMaterial3d(materials.add(StandardMaterial {
                base_color: hex_color("#5b6472").with_alpha(0.09),
                unlit: true,
                double_sided: true,
                cull_mode: None,
                alpha_mode: AlphaMode::Blend,
                ..default()
            })),
            Transform::from_xyz(rect.center[0], (*wz * s) as f32, rect.center[2]),
            GreyboxTag,
            vis,
        ));
    }

    // perimeter walls around the full radar rect, height 200 world units
    let wall_mat = materials.add(StandardMaterial {
        base_color: hex_color("#3a4152"),
        perceptual_roughness: 1.0,
        ..default()
    });
    let (min_x, max_x) = (m.cal.pos_x, m.cal.pos_x + m.cal.size * m.cal.scale);
    let (min_y, max_y) = (m.cal.pos_y - m.cal.size * m.cal.scale, m.cal.pos_y);
    let h = (200.0 * s) as f32;
    let corners = [
        world_to_scene(min_x, min_y, 0.0, s),
        world_to_scene(max_x, min_y, 0.0, s),
        world_to_scene(max_x, max_y, 0.0, s),
        world_to_scene(min_x, max_y, 0.0, s),
    ];
    for i in 0..4 {
        let a = corners[i];
        let d = corners[(i + 1) % 4];
        let (dx, dz) = (d[0] - a[0], d[2] - a[2]);
        let len = (dx * dx + dz * dz).sqrt();
        commands.spawn((
            Mesh3d(meshes.add(Cuboid::new(len, h, 0.3))),
            MeshMaterial3d(wall_mat.clone()),
            Transform::from_xyz((a[0] + d[0]) / 2.0, ground_y + h / 2.0, (a[2] + d[2]) / 2.0)
                .with_rotation(Quat::from_rotation_y(-dz.atan2(dx))),
            GreyboxTag,
            vis,
        ));
    }

    let center = world_to_scene(
        (min_x + max_x) / 2.0,
        (min_y + max_y) / 2.0,
        ground_y as f64 / s,
        s,
    );
    Vec3::from_array(center)
}

/// Spawn the real map world exported by Source2Viewer (glTF/GLB, Y-up, Hammer
/// units). Its axis convention (x, z, -y of Source) is exactly `world_to_scene`
/// with s = 1, so a uniform sceneScale registers it with the player positions —
/// no rotation or recalibration. Children spawn under the root and inherit its
/// Visibility, so the Map/Radar toggle only touches this entity.
pub fn spawn_map(commands: &mut Commands, asset_server: &AssetServer, m: &ManifestData) {
    let Some(path) = m.map_asset.clone() else {
        return;
    };
    let scene: Handle<WorldAsset> = asset_server
        .load_builder()
        .with_settings(|s: &mut GltfLoaderSettings| {
            // Source2Viewer emits non-standard attribute semantics; skip strict
            // gltf-crate validation (unknown attributes are warn-and-skip anyway).
            s.validate = false;
            s.load_cameras = false;
            s.load_lights = false;
            s.load_animations = false;
        })
        .load(GltfAssetLabel::Scene(0).from_asset(path));
    commands.spawn((
        WorldAssetRoot(scene),
        Transform::from_scale(Vec3::splat(m.scene_scale as f32)),
        Visibility::Visible,
        MapTag,
    ));
}

/// Source 2 editor/tool/effect materials that are invisible in-game but export
/// as opaque sheets (clips, triggers, block-light, effect planes). `extras` is
/// the raw material-extras JSON, which carries the vmat path + shader name.
pub fn is_hidden_material(name: &str, extras: Option<&str>) -> bool {
    let n = name.to_ascii_lowercase();
    // start-anchored so real assets like "bucket_tools" survive; no broad
    // "skybox" match — that would hide the 3D skybox's distant buildings.
    if n.starts_with("tools")
        || n == "black_simple"
        || n.contains("trigger")
        || n.contains("nodraw")
        || n.contains("occluder")
        || n.contains("playerclip")
        || n.contains("invisible")
    {
        return true;
    }
    if let Some(raw) = extras {
        if raw.contains("materials/tools/") || raw.contains("csgo_effects.vfx") {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod hidden_material_tests {
    use super::is_hidden_material;

    #[test]
    fn tool_brushes_hidden() {
        assert!(is_hidden_material("toolsclip", None));
        assert!(is_hidden_material("toolsnodraw", None));
        assert!(is_hidden_material("toolsskybox", None));
        assert!(is_hidden_material("black_simple", None));
        assert!(is_hidden_material("de_mirage_playerclip", None));
        assert!(is_hidden_material("ok_name", Some(r#"{"vmat":"materials/tools/toolsblock_los.vmat"}"#)));
        assert!(is_hidden_material("dust_sheet", Some(r#"{"shader":"csgo_effects.vfx"}"#)));
    }

    #[test]
    fn real_assets_visible() {
        assert!(!is_hidden_material("bucket_tools", None));
        assert!(!is_hidden_material("skybox_dust_hotel_a", None));
        assert!(!is_hidden_material("de_mirage_ground", Some(r#"{"vmat":"materials/world/ground.vmat"}"#)));
    }
}

/// Spawn the per-round player entities (body capsule + nose cone), team-colored.
pub fn spawn_players(
    commands: &mut Commands,
    meshes: &mut Assets<Mesh>,
    materials: &mut Assets<StandardMaterial>,
    m: &ManifestData,
    dec: &DecodedRound,
) {
    let body_mesh = meshes.add(Capsule3d::new(0.35, 1.0));
    let nose_mesh = meshes.add(Cone { radius: 0.18, height: 0.5 });
    for (slot, player) in dec.players.iter().enumerate() {
        let color = m
            .team_colors
            .get(&player.team)
            .copied()
            .unwrap_or(Color::srgb_u8(0xcc, 0xcc, 0xcc));
        let mat = materials.add(StandardMaterial {
            base_color: color,
            perceptual_roughness: 0.7,
            ..default()
        });
        commands.spawn((
            Mesh3d(body_mesh.clone()),
            MeshMaterial3d(mat.clone()),
            Transform::default(),
            Visibility::Hidden,
            PlayerBody { slot },
        ));
        commands.spawn((
            Mesh3d(nose_mesh.clone()),
            MeshMaterial3d(mat),
            Transform::default(),
            Visibility::Hidden,
            PlayerNose { slot },
        ));
    }
}
