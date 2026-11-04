//! What players hold, resolved from the round's weaponTable tag each frame:
//!   * third person — parented to each PlayerBody, on the x-ray layer like the player
//!     meshes;
//!   * first person — a viewmodel parented to the main camera (bottom-right, like in
//!     game), only while spectating in 1st person.
//! Two kinds of model can fill either spot, and they swap only when the held weapon
//! actually changes (cheap per frame):
//!   * the real CS2 weapon, built from a local CS2 install by tools/convert_weapons.ps1
//!     and never committed. Its own frame is the `wpn` bone's, so it needs no offset;
//!   * a procedural kit (cuboids + a sphere) per weapon CLASS, while a real one is still
//!     loading, if its file is missing or broken, or on the fallback soldier.

use std::collections::HashMap;

use bevy::asset::LoadState;
use bevy::camera::visibility::RenderLayers;
use bevy::gltf::{Gltf, GltfLoaderSettings, GltfMaterial, GltfMesh, GltfNode};
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

/// A held model that casts a shadow only when what holds it does: a gun in a player's hand
/// does, the fallback first-person kit at the eye does not.
#[derive(Component)]
pub struct ShadowFromParent;

/// Copy the parent's no-shadow mark onto held models as they appear.
pub fn mark_held_shadows(
    mut commands: Commands,
    q_new: Query<(Entity, &ChildOf), Added<ShadowFromParent>>,
    q_no_shadow: Query<(), With<bevy::light::NotShadowCaster>>,
) {
    for (e, parent) in &q_new {
        if q_no_shadow.contains(parent.parent()) {
            commands.entity(e).insert(bevy::light::NotShadowCaster);
        }
    }
}

/// The model spawned under a mount.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Held {
    /// Index of a procedural kit.
    Kit(usize),
    /// A real model, by its `weapon_models.txt` file.
    Real(&'static str),
}

/// Weapon attachment point, one per player body (spawned in scene::spawn_players):
/// on the chest, or on the `wpn` bone when the model has one (`on_bone`).
/// `current` is what is spawned under it now (None = empty hands).
#[derive(Component)]
pub struct WeaponMount {
    pub slot: usize,
    pub current: Option<Held>,
    pub on_bone: bool,
}

/// First-person viewmodel anchor, child of the main camera (spawned in setup).
#[derive(Component, Default)]
pub struct ViewModel {
    pub current: Option<Held>,
}

/// Where the real viewmodel sits, in camera space: (+X right, +Y up, -Z forward).
const VM_REAL_POS: Vec3 = Vec3::new(0.15, -0.11, -0.34);
/// Bones inherit the player model's scale; a viewmodel has no bone to inherit it from.
const VM_REAL_SCALE: f32 = 0.02 / 0.0254;

/// The anchor's transform for the model it holds.
fn viewmodel_transform(held: Held) -> Transform {
    match held {
        Held::Kit(_) => Transform::from_translation(VM_POS)
            .with_rotation(Quat::from_rotation_y(VM_YAW))
            .with_scale(Vec3::splat(VM_SCALE)),
        // The bone's frame in the camera's: barrel +X to -Z, left +Y to -X, up +Z to +Y.
        Held::Real(_) => Transform::from_translation(VM_REAL_POS)
            .with_rotation(Quat::from_mat3(&Mat3::from_cols(Vec3::NEG_Z, Vec3::NEG_X, Vec3::Y)))
            .with_scale(Vec3::splat(VM_REAL_SCALE)),
    }
}

/// `tools/convert_weapons.ps1` builds one model per line, and the viewer reads the
/// tag -> file half of it from here so the two cannot drift apart.
const WEAPON_LIST: &str = include_str!("weapon_models.txt");
const WEAPON_DIR: &str = "models/cs2_v1/weapons";
/// The empty node prepare_weapon.py leaves at the muzzle.
const MUZZLE_NODE: &str = "muzzle_flash";

/// `(tag, file)` of each line of `weapon_models.txt`.
fn weapon_list() -> impl Iterator<Item = (&'static str, &'static str)> {
    WEAPON_LIST.lines().filter(|l| !l.is_empty() && !l.starts_with('#')).filter_map(|l| {
        let mut words = l.split_whitespace();
        Some((words.next()?, words.next()?))
    })
}

/// A weaponTable tag's name in `weapon_models.txt`, where knives are per team and the
/// emoji and truncated tags spell out. `weapon_tag` in rust/extract writes the Zeus as
/// the first 6 characters of its name, since it has no row in WEAPON_TAGS.
fn list_tag(tag: &str, team: i64) -> &str {
    match tag {
        "🔪" if team == crate::scene::TEAM_T => "KNIFE_T",
        "🔪" => "KNIFE_CT",
        "💣" => "C4",
        "ZEUS X" => "ZEUS",
        other => other,
    }
}

enum RealState {
    Loading(Handle<Gltf>),
    Ready(RealModel),
    Failed,
}

struct RealModel {
    mesh: Handle<Mesh>,
    material: Handle<StandardMaterial>,
    /// In the model's frame. None for what has no barrel: knives, grenades, the bomb.
    muzzle: Option<Vec3>,
    /// The mesh's box in its own frame, in metres, to set it down on a floor.
    bounds: (Vec3, Vec3),
}

/// The real weapon models. Each loads when a round's weapon table first names it, and
/// the handle kept here means later rounds don't fetch it again. Empty when the page
/// did not ask for them.
#[derive(Resource, Default)]
pub struct RealWeapons {
    /// `weapon_models.txt` tag -> file.
    files: HashMap<&'static str, &'static str>,
    models: HashMap<&'static str, RealState>,
    loading: usize,
}

impl RealWeapons {
    pub fn new(enabled: bool) -> Self {
        let files = if enabled { weapon_list().collect() } else { HashMap::new() };
        RealWeapons { files, ..default() }
    }

    pub(crate) fn file_for(&self, tag: &str, team: i64) -> Option<&'static str> {
        self.files.get(list_tag(tag, team)).copied()
    }

    /// Start fetching everything this round can show, both knives included: a player's
    /// switch to a weapon should not be the moment its model starts downloading.
    pub fn request(&mut self, weapon_table: &[String], asset_server: &AssetServer) {
        for tag in weapon_table {
            for team in [crate::scene::TEAM_T, 0] {
                let Some(file) = self.file_for(tag, team) else { continue };
                if self.models.contains_key(file) {
                    continue;
                }
                let handle = asset_server
                    .load_builder()
                    .with_settings(|s: &mut GltfLoaderSettings| {
                        s.load_cameras = false;
                        s.load_lights = false;
                        s.load_animations = false;
                    })
                    .load(format!("{WEAPON_DIR}/{file}.glb"));
                self.models.insert(file, RealState::Loading(handle));
                self.loading += 1;
            }
        }
    }

    fn ready(&self, file: &str) -> Option<&RealModel> {
        match self.models.get(file) {
            Some(RealState::Ready(model)) => Some(model),
            _ => None,
        }
    }

    pub(crate) fn ready_file(&self, tag: &str, team: i64) -> Option<&'static str> {
        self.file_for(tag, team).filter(|file| self.ready(file).is_some())
    }

    /// A ready model's mesh, material and box in metres, to place it in the world on its own.
    pub(crate) fn mesh_of(&self, file: &str) -> Option<(Handle<Mesh>, Handle<StandardMaterial>, (Vec3, Vec3))> {
        self.ready(file).map(|model| (model.mesh.clone(), model.material.clone(), model.bounds))
    }

    /// The material a ready model has: its first-person rig has the same UVs.
    pub(crate) fn material_of(&self, file: &str) -> Option<Handle<StandardMaterial>> {
        self.ready(file).map(|model| model.material.clone())
    }

    /// A ready model's muzzle in its own frame, which is the `wpn` bone's.
    pub(crate) fn muzzle_of(&self, file: &str) -> Option<Vec3> {
        self.ready(file)?.muzzle
    }

    /// A held model's muzzle in world space, given where its mount is. None for a kit,
    /// and for what has no barrel: the caller then keeps the eye as the shot's origin.
    pub fn muzzle_world(&self, held: Option<Held>, mount: &GlobalTransform) -> Option<Vec3> {
        let Held::Real(file) = held? else { return None };
        Some(mount.transform_point(self.ready(file)?.muzzle?))
    }
}

/// Publish each real weapon as its file arrives. A failed one stays out for good, so
/// whoever holds it keeps the kit.
pub fn finish_real_weapons(
    mut real: ResMut<RealWeapons>,
    asset_server: Res<AssetServer>,
    gltfs: Res<Assets<Gltf>>,
    gltf_meshes: Res<Assets<GltfMesh>>,
    gltf_nodes: Res<Assets<GltfNode>>,
    gltf_materials: Res<Assets<GltfMaterial>>,
    meshes: Res<Assets<Mesh>>,
    mut materials: ResMut<Assets<StandardMaterial>>,
) {
    if real.loading == 0 {
        return;
    }
    let real = &mut *real;
    for (file, state) in &mut real.models {
        let RealState::Loading(handle) = state else { continue };
        let done = if matches!(asset_server.load_state(&*handle), LoadState::Failed(_)) {
            warn!("real weapon {file} failed to load; keeping the simple one");
            RealState::Failed
        } else if let Some(gltf) = gltfs.get(&*handle) {
            let part = gltf
                .meshes
                .first()
                .and_then(|m| gltf_meshes.get(m))
                .and_then(|m| m.primitives.first())
                .and_then(|p| Some((p.mesh.clone(), gltf_materials.get(p.material.as_ref()?)?)));
            match part {
                Some((mesh, src)) => RealState::Ready(RealModel {
                    bounds: meshes
                        .get(&mesh)
                        .and_then(bevy::camera::primitives::MeshAabb::compute_aabb)
                        .map_or((Vec3::ZERO, Vec3::ZERO), |b| (b.min().into(), b.max().into())),
                    mesh,
                    // Not metallic: the viewer has no environment map for metal to reflect,
                    // and a metal surface with nothing to reflect renders black.
                    material: materials.add(StandardMaterial {
                        base_color_texture: src.base_color_texture.clone(),
                        normal_map_texture: src.normal_map_texture.clone(),
                        metallic: 0.0,
                        perceptual_roughness: src.perceptual_roughness,
                        ..default()
                    }),
                    muzzle: gltf
                        .named_nodes
                        .get(MUZZLE_NODE)
                        .and_then(|n| gltf_nodes.get(n))
                        .map(|n| n.transform.translation),
                }),
                None => {
                    warn!("real weapon {file} has no mesh; keeping the simple one");
                    RealState::Failed
                }
            }
        } else {
            continue;
        };
        *state = done;
        real.loading -= 1;
    }
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

/// What to show for `slot`'s held weapon at sample `i0` (None = nothing to draw:
/// dead/absent slot, WEAPON_NONE, or an unknown tag we'd rather not guess at).
fn current_held(
    r: &RoundState,
    slot: usize,
    i0: usize,
    assets: &WeaponAssets,
    real: Option<&RealWeapons>,
) -> Option<Held> {
    let tag = current_tag(r, slot, i0)?;
    let team = r.dec.players.get(slot).map_or(0, |p| p.team);
    real.and_then(|real| real.ready_file(tag, team))
        .map(Held::Real)
        .or_else(|| assets.kit_for_tag(tag).map(Held::Kit))
}

fn spawn_held(
    commands: &mut Commands,
    parent: Entity,
    assets: &WeaponAssets,
    real: &RealWeapons,
    held: Held,
    layers: RenderLayers,
) {
    commands.entity(parent).with_children(|p| match held {
        Held::Kit(kit) => {
            for part in assets.parts(kit) {
                p.spawn((
                    Mesh3d(part.mesh.clone()),
                    MeshMaterial3d(part.mat.clone()),
                    part.tf,
                    WeaponPart,
                    layers.clone(),
                    ShadowFromParent,
                ));
            }
        }
        Held::Real(file) => {
            if let Some(model) = real.ready(file) {
                p.spawn((
                    Mesh3d(model.mesh.clone()),
                    MeshMaterial3d(model.material.clone()),
                    Transform::IDENTITY,
                    WeaponPart,
                    layers,
                    ShadowFromParent,
                ));
            }
        }
    });
}

/// Third person: keep every body's mount holding the weapon the player actually has
/// this frame. The real model only goes on a bone: on the fallback soldier's chest it
/// would hang by its own origin, which is not its grip.
pub fn update_weapon_mounts(
    mut commands: Commands,
    viewer: Res<Viewer>,
    assets: Option<Res<WeaponAssets>>,
    real: Res<RealWeapons>,
    mut q_mounts: Query<(Entity, &mut WeaponMount, &mut Transform, Option<&Children>)>,
) {
    let Some(assets) = assets else { return };
    let Some(r) = viewer.round.as_ref() else { return };
    if r.n == 0 {
        return;
    }
    let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
    for (e, mut mount, mut tf, children) in &mut q_mounts {
        let held = current_held(r, mount.slot, i0, &assets, mount.on_bone.then_some(&*real));
        if mount.current == held {
            continue;
        }
        mount.current = held;
        if let Some(ch) = children {
            for c in ch.iter() {
                commands.entity(c).despawn();
            }
        }
        if mount.on_bone {
            match held {
                Some(Held::Real(_)) => *tf = Transform::IDENTITY,
                _ => {
                    if let Some(tag) = current_tag(r, mount.slot, i0) {
                        *tf = bone_mount(class_for_tag(tag));
                    }
                }
            }
        }
        if let Some(held) = held {
            let layers = RenderLayers::from_layers(&[0, crate::XRAY_LAYER]);
            spawn_held(&mut commands, e, &assets, &real, held, layers);
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
    real: Res<RealWeapons>,
    real_arms: Res<crate::viewmodel::FpState>,
    mut q_vm: Query<(Entity, &mut ViewModel, &mut Transform, &mut Visibility, Option<&Children>)>,
) {
    let Some(assets) = assets else { return };
    let fp_slot = match viewer.cam_mode {
        // the real arms and weapon are up, or a scope covers the screen
        // the settings asked for no arms and weapon at all
        _ if real_arms.active || real_arms.scope_overlay || !real_arms.vm.visible => None,
        CamMode::Follow { slot, first_person: true } => Some(slot),
        _ => None,
    };
    let held = match (fp_slot, viewer.round.as_ref()) {
        (Some(slot), Some(r)) if r.n > 0 => {
            let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
            current_held(r, slot, i0, &assets, Some(&*real))
        }
        _ => None,
    };
    for (e, mut vm, mut tf, mut vis, children) in &mut q_vm {
        *vis = if held.is_some() { Visibility::Visible } else { Visibility::Hidden };
        if vm.current == held {
            continue;
        }
        vm.current = held;
        if let Some(ch) = children {
            for c in ch.iter() {
                commands.entity(c).despawn();
            }
        }
        if let Some(held) = held {
            *tf = viewmodel_transform(held);
            spawn_held(&mut commands, e, &assets, &real, held, RenderLayers::layer(0));
            commands.entity(e).insert(bevy::light::NotShadowCaster);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_list_names_each_tag_once_and_each_file_once() {
        let rows: Vec<_> = weapon_list().collect();
        assert_eq!(rows.len(), 44);
        let tags: std::collections::HashSet<_> = rows.iter().map(|r| r.0).collect();
        let files: std::collections::HashSet<_> = rows.iter().map(|r| r.1).collect();
        assert_eq!((tags.len(), files.len()), (rows.len(), rows.len()));
        assert!(rows.contains(&("AK", "ak47")));
    }

    #[test]
    fn knives_go_by_team_and_the_odd_tags_spell_out() {
        let real = RealWeapons::new(true);
        assert_eq!(real.file_for("🔪", crate::scene::TEAM_T), Some("knife_t"));
        assert_eq!(real.file_for("🔪", 3), Some("knife_ct"));
        assert_eq!(real.file_for("💣", 3), Some("c4"));
        assert_eq!(real.file_for("ZEUS X", 3), Some("taser"));
        assert_eq!(real.file_for("not a weapon", 3), None);
    }

    #[test]
    fn disabled_means_nothing_is_ever_asked_for() {
        assert_eq!(RealWeapons::new(false).file_for("AK", 3), None);
    }

    #[test]
    fn the_real_viewmodel_points_away_from_the_camera_with_its_top_up() {
        let tf = viewmodel_transform(Held::Real("ak47"));
        assert!(tf.rotation.mul_vec3(Vec3::X).distance(Vec3::NEG_Z) < 1e-5);
        assert!(tf.rotation.mul_vec3(Vec3::Z).distance(Vec3::Y) < 1e-5);
        assert!(tf.rotation.mul_vec3(Vec3::Y).distance(Vec3::NEG_X) < 1e-5);
    }

    #[test]
    fn a_kit_has_no_muzzle_and_neither_does_an_unloaded_model() {
        let real = RealWeapons::new(true);
        let at = GlobalTransform::IDENTITY;
        assert_eq!(real.muzzle_world(None, &at), None);
        assert_eq!(real.muzzle_world(Some(Held::Kit(0)), &at), None);
        assert_eq!(real.muzzle_world(Some(Held::Real("ak47")), &at), None);
    }
}
