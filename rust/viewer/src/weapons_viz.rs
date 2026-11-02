//! Stylized low-poly weapon models — procedural (cuboids + a sphere), no external
//! assets, matching the untextured-map / tinted-soldier art style. One silhouette
//! per weapon CLASS (rifle / sniper / smg / pistol / shotgun / MG / knife / nade /
//! C4), resolved from the round's weaponTable tag each frame:
//!   * third person — a kit parented to each PlayerBody at chest height, pointing
//!     along the body yaw (+X), on the x-ray layer like the player meshes;
//!   * first person — the same kit as a viewmodel parented to the main camera
//!     (bottom-right, like in game), only while spectating in 1st person.
//! Kits swap only when the weapon class actually changes (cheap per frame).

use std::collections::HashMap;

use bevy::camera::visibility::RenderLayers;
use bevy::prelude::*;

use crate::player_anim::{class_for_tag, WeaponClass};
use crate::state::{sample_pos, CamMode, RoundState, Viewer};

// --- calibration one-liners (verify by eye) ---------------------------------
/// Third-person mount, in PlayerBody space (+X forward, +Y up, +Z player's right).
pub const MOUNT_POS: Vec3 = Vec3::new(0.24, 1.04, 0.10);
/// Third-person kit scale (kits are authored chunky for replay-zoom legibility).
pub const THIRD_SCALE: f32 = 0.8;
/// Viewmodel offset in camera space (+X right, +Y up, -Z forward) and scale.
pub const VM_POS: Vec3 = Vec3::new(0.17, -0.15, -0.44);
pub const VM_SCALE: f32 = 1.0;
/// Viewmodel yaw: rotate the kit's +X barrel to face away from the camera (-Z),
/// minus a slight inward cant so it reads like a held gun, not a screenshot prop.
pub const VM_YAW: f32 = std::f32::consts::FRAC_PI_2 - 0.08;

/// The CS2 skeleton's weapon bone. Its local +X runs along the barrel, +Y to the
/// player's left and +Z up, so a quarter turn about +X lines up a kit's +Y up / +Z right.
pub const WEAPON_BONE: &str = "wpn";
const BONE_KIT_ROLL: f32 = std::f32::consts::FRAC_PI_2;
/// Bones inherit the model's scale (0.787), so this matches THIRD_SCALE in the scene.
const BONE_KIT_SCALE: f32 = 1.016;

/// The kit's origin in `wpn` bone space (metres), measured so its grip lands on the
/// right hand in each clip set's idle pose: the bone sits between the hands, not on
/// the grip, and the knife set holds it at an angle.
fn bone_mount(class: WeaponClass) -> Transform {
    let pos = match class {
        WeaponClass::Rifle => Vec3::new(-0.23, 0.0, -0.02),
        WeaponClass::Pistol => Vec3::new(-0.13, 0.0, -0.04),
        WeaponClass::Knife => Vec3::new(-0.035, -0.016, -0.076),
    };
    Transform::from_translation(pos)
        .with_rotation(Quat::from_rotation_x(BONE_KIT_ROLL))
        .with_scale(Vec3::splat(BONE_KIT_SCALE))
}

/// Marker for spawned weapon part meshes — `tint_players` must SKIP these
/// (they'd get team-painted otherwise; guns stay gunmetal).
#[derive(Component)]
pub struct WeaponPart;

/// Weapon attachment point, one per player body (spawned in scene::spawn_players):
/// on the chest, or on the `wpn` bone when the model has one (`on_bone`).
/// `current` is the kit index currently spawned under it (None = empty hands).
#[derive(Component)]
pub struct WeaponMount {
    pub slot: usize,
    pub current: Option<usize>,
    pub on_bone: bool,
}

/// First-person viewmodel anchor, child of the main camera (spawned in setup).
#[derive(Component, Default)]
pub struct ViewModel {
    pub current: Option<usize>,
}

/// One primitive of a weapon kit.
pub struct Part {
    pub mesh: Handle<Mesh>,
    pub mat: Handle<StandardMaterial>,
    pub tf: Transform,
}

/// All kits + the weaponTable-tag -> kit index map. Built once at startup.
#[derive(Resource)]
pub struct WeaponAssets {
    kits: Vec<Vec<Part>>,
    by_tag: HashMap<&'static str, usize>,
}

impl WeaponAssets {
    pub fn kit_for_tag(&self, tag: &str) -> Option<usize> {
        self.by_tag.get(tag).copied()
    }
    pub fn parts(&self, kit: usize) -> &[Part] {
        &self.kits[kit]
    }
}

pub fn setup_weapon_assets(
    mut commands: Commands,
    mut meshes: ResMut<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    let metal = materials.add(StandardMaterial {
        base_color: Color::srgb(0.16, 0.17, 0.19),
        metallic: 0.6,
        perceptual_roughness: 0.55,
        ..default()
    });
    let blade = materials.add(StandardMaterial {
        base_color: Color::srgb(0.75, 0.78, 0.82),
        metallic: 0.9,
        perceptual_roughness: 0.25,
        ..default()
    });
    let c4_led = materials.add(StandardMaterial {
        base_color: Color::srgb(1.0, 0.2, 0.2),
        emissive: LinearRgba::rgb(2.5, 0.1, 0.1),
        ..default()
    });
    let mut nade_mat = |hex: Color| {
        let lin = hex.to_linear();
        materials.add(StandardMaterial {
            base_color: hex,
            emissive: LinearRgba::rgb(lin.red * 0.08, lin.green * 0.08, lin.blue * 0.08),
            perceptual_roughness: 0.7,
            ..default()
        })
    };

    // local frame: +X barrel/forward, +Y up, +Z right
    let mut kits: Vec<Vec<Part>> = Vec::new();
    let mut by_tag: HashMap<&'static str, usize> = HashMap::new();
    let cub = |m: &mut Assets<Mesh>, sx: f32, sy: f32, sz: f32| m.add(Cuboid::new(sx, sy, sz));

    // rifle (AK/M4/...)
    let rifle = vec![
        Part { mesh: cub(&mut meshes, 0.46, 0.075, 0.045), mat: metal.clone(), tf: Transform::from_xyz(0.03, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.30, 0.030, 0.030), mat: metal.clone(), tf: Transform::from_xyz(0.40, 0.008, 0.0) },
        Part { mesh: cub(&mut meshes, 0.055, 0.13, 0.035), mat: metal.clone(), tf: Transform::from_xyz(0.05, -0.095, 0.0).with_rotation(Quat::from_rotation_z(0.22)) },
        Part { mesh: cub(&mut meshes, 0.16, 0.09, 0.04), mat: metal.clone(), tf: Transform::from_xyz(-0.26, -0.015, 0.0) },
    ];
    // sniper (AWP/SCOUT/autos): longer + scope on top
    let sniper = vec![
        Part { mesh: cub(&mut meshes, 0.55, 0.08, 0.05), mat: metal.clone(), tf: Transform::from_xyz(0.05, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.38, 0.028, 0.028), mat: metal.clone(), tf: Transform::from_xyz(0.52, 0.01, 0.0) },
        Part { mesh: cub(&mut meshes, 0.20, 0.045, 0.045), mat: metal.clone(), tf: Transform::from_xyz(0.08, 0.075, 0.0) },
        Part { mesh: cub(&mut meshes, 0.05, 0.09, 0.03), mat: metal.clone(), tf: Transform::from_xyz(0.03, -0.075, 0.0) },
        Part { mesh: cub(&mut meshes, 0.18, 0.11, 0.045), mat: metal.clone(), tf: Transform::from_xyz(-0.30, -0.02, 0.0) },
    ];
    // smg: short, big mag
    let smg = vec![
        Part { mesh: cub(&mut meshes, 0.32, 0.075, 0.045), mat: metal.clone(), tf: Transform::from_xyz(0.02, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.12, 0.026, 0.026), mat: metal.clone(), tf: Transform::from_xyz(0.24, 0.005, 0.0) },
        Part { mesh: cub(&mut meshes, 0.045, 0.14, 0.03), mat: metal.clone(), tf: Transform::from_xyz(0.04, -0.10, 0.0) },
        Part { mesh: cub(&mut meshes, 0.10, 0.06, 0.03), mat: metal.clone(), tf: Transform::from_xyz(-0.19, 0.0, 0.0) },
    ];
    // pistol
    let pistol = vec![
        Part { mesh: cub(&mut meshes, 0.19, 0.05, 0.032), mat: metal.clone(), tf: Transform::from_xyz(0.05, 0.02, 0.0) },
        Part { mesh: cub(&mut meshes, 0.05, 0.11, 0.03), mat: metal.clone(), tf: Transform::from_xyz(-0.02, -0.055, 0.0).with_rotation(Quat::from_rotation_z(0.18)) },
    ];
    // shotgun: long, pump, no mag
    let shotgun = vec![
        Part { mesh: cub(&mut meshes, 0.48, 0.07, 0.045), mat: metal.clone(), tf: Transform::from_xyz(0.04, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.26, 0.032, 0.032), mat: metal.clone(), tf: Transform::from_xyz(0.40, 0.012, 0.0) },
        Part { mesh: cub(&mut meshes, 0.13, 0.045, 0.055), mat: metal.clone(), tf: Transform::from_xyz(0.22, -0.045, 0.0) },
        Part { mesh: cub(&mut meshes, 0.17, 0.09, 0.04), mat: metal.clone(), tf: Transform::from_xyz(-0.25, -0.015, 0.0) },
    ];
    // machine gun: fat body + ammo box
    let mg = vec![
        Part { mesh: cub(&mut meshes, 0.50, 0.11, 0.06), mat: metal.clone(), tf: Transform::from_xyz(0.02, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.30, 0.034, 0.034), mat: metal.clone(), tf: Transform::from_xyz(0.44, 0.01, 0.0) },
        Part { mesh: cub(&mut meshes, 0.10, 0.12, 0.06), mat: metal.clone(), tf: Transform::from_xyz(0.02, -0.11, 0.0) },
        Part { mesh: cub(&mut meshes, 0.14, 0.09, 0.05), mat: metal.clone(), tf: Transform::from_xyz(-0.27, -0.01, 0.0) },
    ];
    // knife: bright blade + dark handle
    let knife = vec![
        Part { mesh: cub(&mut meshes, 0.17, 0.030, 0.008), mat: blade.clone(), tf: Transform::from_xyz(0.10, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.10, 0.032, 0.024), mat: metal.clone(), tf: Transform::from_xyz(-0.04, 0.0, 0.0) },
    ];
    // C4: brick + blinking-red LED look (static emissive)
    let bomb = vec![
        Part { mesh: cub(&mut meshes, 0.13, 0.075, 0.05), mat: metal.clone(), tf: Transform::from_xyz(0.0, 0.0, 0.0) },
        Part { mesh: cub(&mut meshes, 0.02, 0.02, 0.012), mat: c4_led, tf: Transform::from_xyz(0.03, 0.045, 0.0) },
    ];

    let mut add_kit = |kit: Vec<Part>, tags: &[&'static str]| {
        let idx = kits.len();
        kits.push(kit);
        for t in tags {
            by_tag.insert(t, idx);
        }
    };
    add_kit(rifle, &["AK", "M4", "M4S", "AUG", "SG553", "FAMAS", "GALIL"]);
    add_kit(sniper, &["AWP", "SCOUT", "SCAR", "G3"]);
    add_kit(smg, &["MP9", "MP7", "MP5", "MAC10", "UMP", "P90", "BIZON"]);
    add_kit(pistol, &["GLOCK", "USP", "P2000", "P250", "FIVE7", "TEC9", "CZ", "DUALS", "DEAG", "R8"]);
    add_kit(shotgun, &["NOVA", "XM", "SAWED", "MAG7"]);
    add_kit(mg, &["M249", "NEGEV"]);
    add_kit(knife, &["🔪"]);
    add_kit(bomb, &["💣"]);
    // grenades in hand: one sphere, colored per type (matches the page's palette)
    let sphere = meshes.add(Sphere::new(0.048));
    for (tag, hex) in [
        ("HE", Color::srgb_u8(0xff, 0x50, 0x40)),
        ("FLASH", Color::srgb_u8(0xee, 0xee, 0xee)),
        ("SMOKE", Color::srgb_u8(0xd6, 0xda, 0xe1)),
        ("MOLLY", Color::srgb_u8(0xff, 0x6a, 0x1a)),
        ("INC", Color::srgb_u8(0xff, 0x6a, 0x1a)),
        ("DECOY", Color::srgb_u8(0x9a, 0xa4, 0xb2)),
    ] {
        let mat = nade_mat(hex);
        let kit = vec![Part {
            mesh: sphere.clone(),
            mat,
            tf: Transform::from_xyz(0.01, -0.01, 0.0),
        }];
        add_kit(kit, &[tag]);
    }

    commands.insert_resource(WeaponAssets { kits, by_tag });
}

/// `slot`'s held weaponTable tag at sample `i0` (None: dead/absent slot, WEAPON_NONE).
pub fn current_tag(r: &RoundState, slot: usize, i0: usize) -> Option<&str> {
    let tr = r.dec.tracks.get(slot)?;
    let widx = *tr.weapon.get(i0)? as usize;
    r.dec.weapon_table.get(widx).map(String::as_str)
}

/// The kit for `slot`'s held weapon at sample `i0` (None = nothing to draw:
/// dead/absent slot, WEAPON_NONE, or an unknown tag we'd rather not guess at).
fn current_kit(r: &RoundState, slot: usize, i0: usize, assets: &WeaponAssets) -> Option<usize> {
    assets.kit_for_tag(current_tag(r, slot, i0)?)
}

fn spawn_parts(
    commands: &mut Commands,
    parent: Entity,
    assets: &WeaponAssets,
    kit: usize,
    layers: RenderLayers,
) {
    commands.entity(parent).with_children(|p| {
        for part in assets.parts(kit) {
            p.spawn((
                Mesh3d(part.mesh.clone()),
                MeshMaterial3d(part.mat.clone()),
                part.tf,
                WeaponPart,
                layers.clone(),
            ));
        }
    });
}

/// Third person: keep every body's mount holding the weapon class the player
/// actually has this frame (kit swap only on class change).
pub fn update_weapon_mounts(
    mut commands: Commands,
    viewer: Res<Viewer>,
    assets: Option<Res<WeaponAssets>>,
    mut q_mounts: Query<(Entity, &mut WeaponMount, &mut Transform, Option<&Children>)>,
) {
    let Some(assets) = assets else { return };
    let Some(r) = viewer.round.as_ref() else { return };
    if r.n == 0 {
        return;
    }
    let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
    for (e, mut mount, mut tf, children) in &mut q_mounts {
        let kit = current_kit(r, mount.slot, i0, &assets);
        if mount.current == kit {
            continue;
        }
        mount.current = kit;
        if let Some(ch) = children {
            for c in ch.iter() {
                commands.entity(c).despawn();
            }
        }
        if mount.on_bone {
            if let Some(tag) = current_tag(r, mount.slot, i0) {
                *tf = bone_mount(class_for_tag(tag));
            }
        }
        if let Some(k) = kit {
            spawn_parts(&mut commands, e, &assets, k, RenderLayers::from_layers(&[0, crate::XRAY_LAYER]));
        }
    }
}

/// First person: show the followed player's weapon as a camera-space viewmodel
/// while spectating in 1st person; hidden otherwise. (The muzzle flash for the
/// own shots is already suppressed in draw_tracers.)
pub fn update_viewmodel(
    mut commands: Commands,
    viewer: Res<Viewer>,
    assets: Option<Res<WeaponAssets>>,
    mut q_vm: Query<(Entity, &mut ViewModel, &mut Visibility, Option<&Children>)>,
) {
    let Some(assets) = assets else { return };
    let fp_slot = match viewer.cam_mode {
        CamMode::Follow { slot, first_person: true } => Some(slot),
        _ => None,
    };
    let kit = match (fp_slot, viewer.round.as_ref()) {
        (Some(slot), Some(r)) if r.n > 0 => {
            let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
            current_kit(r, slot, i0, &assets)
        }
        _ => None,
    };
    for (e, mut vm, mut vis, children) in &mut q_vm {
        *vis = if kit.is_some() { Visibility::Visible } else { Visibility::Hidden };
        if vm.current == kit {
            continue;
        }
        vm.current = kit;
        if let Some(ch) = children {
            for c in ch.iter() {
                commands.entity(c).despawn();
            }
        }
        if let Some(k) = kit {
            spawn_parts(&mut commands, e, &assets, k, RenderLayers::layer(0));
        }
    }
}
