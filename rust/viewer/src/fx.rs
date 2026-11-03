//! Effects drawn with CS2's own particle textures, built by `tools/convert_fx.ps1` into
//! one atlas per kind (`tools/fx_sheets.txt`).
//!
//! The particle systems themselves are Valve's and are not ported: what moves and how is
//! worked out here, as a function of the playback clock, so scrubbing lands on the same
//! picture as playing does. Each frame the effects push sprites into [`FxFrame`], and
//! [`flush_fx`] turns every group of them into one mesh, sorted back to front. A group is
//! one entity, so Bevy sorts groups against each other and the rest of the scene.
//!
//! Without the atlases, or when one fails to load, [`FxAssets::ready`] stays false and
//! the viewer keeps its simple shapes.

use std::collections::HashMap;

use bevy::asset::{embedded_asset, LoadState, RenderAssetUsages};
use bevy::camera::visibility::{NoFrustumCulling, RenderLayers};
use bevy::image::{
    ImageAddressMode, ImageFilterMode, ImageLoaderSettings, ImageSampler, ImageSamplerDescriptor,
};
use bevy::mesh::{Indices, MeshVertexAttribute, MeshVertexBufferLayoutRef, PrimitiveTopology};
use bevy::pbr::{MaterialPipeline, MaterialPipelineKey};
use bevy::prelude::*;
use bevy::render::render_resource::{
    AsBindGroup, RenderPipelineDescriptor, SpecializedMeshPipelineError, VertexFormat,
};
use bevy::shader::ShaderRef;

use crate::state::MainCam;

const SHADER: &str = "embedded://viewer/fx.wgsl";
const SHEET_LIST: &str = include_str!("../../../tools/fx_sheets.txt");
/// Where `tools/convert_fx.ps1` puts the atlases, under the asset root.
pub const FX_DIR: &str = "fx_v1";
/// Must match COLUMNS in tools/prepare_fx.py.
const COLUMNS: usize = 8;

pub const ATTRIBUTE_FX_CORNER: MeshVertexAttribute =
    MeshVertexAttribute::new("FxCorner", 0x4658_0001, VertexFormat::Float32x4);

pub struct FxPlugin;

impl Plugin for FxPlugin {
    fn build(&self, app: &mut App) {
        embedded_asset!(app, "fx.wgsl");
        app.add_plugins(MaterialPlugin::<FxMaterial>::default())
            .init_resource::<FxFrame>()
            .init_resource::<FxPool>();
    }
}

#[derive(Asset, TypePath, AsBindGroup, Clone)]
pub struct FxMaterial {
    #[texture(0)]
    #[sampler(1)]
    pub atlas: Handle<Image>,
}

impl Material for FxMaterial {
    fn vertex_shader() -> ShaderRef {
        SHADER.into()
    }
    fn fragment_shader() -> ShaderRef {
        SHADER.into()
    }
    fn alpha_mode(&self) -> AlphaMode {
        AlphaMode::Premultiplied
    }
    fn enable_prepass() -> bool {
        false
    }
    fn enable_shadows() -> bool {
        false
    }
    fn specialize(
        _pipeline: &MaterialPipeline,
        descriptor: &mut RenderPipelineDescriptor,
        layout: &MeshVertexBufferLayoutRef,
        _key: MaterialPipelineKey<Self>,
    ) -> Result<(), SpecializedMeshPipelineError> {
        let vertex = layout.0.get_layout(&[
            Mesh::ATTRIBUTE_POSITION.at_shader_location(0),
            ATTRIBUTE_FX_CORNER.at_shader_location(1),
            Mesh::ATTRIBUTE_UV_0.at_shader_location(2),
            Mesh::ATTRIBUTE_COLOR.at_shader_location(3),
        ])?;
        descriptor.vertex.buffers = vec![vertex];
        descriptor.primitive.cull_mode = None;
        Ok(())
    }
}

/// The atlases, by their name in `fx_sheets.txt`.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum Fx {
    Smoke,
    Fireball,
    BlastSmoke,
    Fire,
    Blood,
    Dust,
    Glow,
}

impl Fx {
    pub const ALL: [Fx; 7] = [Fx::Smoke, Fx::Fireball, Fx::BlastSmoke, Fx::Fire, Fx::Blood, Fx::Dust, Fx::Glow];

    pub fn name(self) -> &'static str {
        match self {
            Fx::Smoke => "smoke",
            Fx::Fireball => "fireball",
            Fx::BlastSmoke => "blast_smoke",
            Fx::Fire => "fire",
            Fx::Blood => "blood",
            Fx::Dust => "dust",
            Fx::Glow => "glow",
        }
    }
}

/// The pictures of the glow atlas, in `fx_sheets.txt` order.
pub const GLOW_MUZZLE: [usize; 2] = [0, 1];
pub const GLOW_WHITE: usize = 2;
pub const GLOW_WARM: usize = 3;

/// How an atlas is laid out: `sequences` runs of `frames` pictures, row by row.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sheet {
    pub sequences: usize,
    pub frames: usize,
    pub columns: usize,
    pub rows: usize,
    pub cell: usize,
}

impl Sheet {
    /// The same layout `layout()` in tools/prepare_fx.py writes.
    pub fn new(sequences: usize, frames: usize, cell: usize) -> Self {
        let count = (sequences * frames).max(1);
        let columns = COLUMNS.min(count.next_power_of_two());
        let rows = count.div_ceil(columns).next_power_of_two();
        Sheet { sequences: sequences.max(1), frames: frames.max(1), columns, rows, cell }
    }

    /// (u0, v0, u1, v1) of a picture. The sequence wraps and the frame holds at the last.
    /// A texel in from the cell's edge, so the mips do not pull in the next picture.
    pub fn uv(&self, sequence: usize, frame: usize) -> [f32; 4] {
        let i = (sequence % self.sequences) * self.frames + frame.min(self.frames - 1);
        let (c, r) = ((i % self.columns) as f32, (i / self.columns) as f32);
        let (w, h) = (1.0 / self.columns as f32, 1.0 / self.rows as f32);
        let inset_u = 2.0 / (self.columns * self.cell) as f32;
        let inset_v = 2.0 / (self.rows * self.cell) as f32;
        [c * w + inset_u, r * h + inset_v, (c + 1.0) * w - inset_u, (r + 1.0) * h - inset_v]
    }

    /// The frame `age` seconds into a sequence played at `fps`.
    pub fn frame_at(&self, age: f32, fps: f32, looping: bool) -> usize {
        let f = (age.max(0.0) * fps) as usize;
        if looping {
            f % self.frames
        } else {
            f.min(self.frames - 1)
        }
    }
}

/// `fx_sheets.txt`: (name, layout) per line.
pub fn parse_sheets(text: &str) -> Result<Vec<(String, Sheet)>, String> {
    let mut out = Vec::new();
    for (number, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        let [name, textures, sequences, frames, cell] = parts[..] else {
            return Err(format!("line {}: want 5 columns, got {}", number + 1, parts.len()));
        };
        let num = |s: &str| s.parse::<usize>().map_err(|e| format!("line {}: {s}: {e}", number + 1));
        let sequences = if sequences == "-" {
            textures.split('+').count()
        } else {
            sequences.split(',').count()
        };
        out.push((name.to_string(), Sheet::new(sequences, num(frames)?, num(cell)?)));
    }
    Ok(out)
}

/// One sprite of a frame.
#[derive(Clone, Copy, Debug)]
pub struct Sprite {
    pub pos: Vec3,
    /// half the side, in scene units; negative keeps it from fading near the camera
    pub size: f32,
    pub rot: f32,
    pub uv: [f32; 4],
    /// premultiplied: rgb the light it gives, a how much it hides
    pub color: [f32; 4],
}

impl Sprite {
    /// `rgb` is linear light, `opacity` scales all of it, and `cover` is how much of what
    /// is behind it the sprite hides where it is solid: 1 for smoke, 0 for a flash.
    pub fn new(pos: Vec3, size: f32, rot: f32, uv: [f32; 4], rgb: Vec3, opacity: f32, cover: f32) -> Self {
        let o = opacity.clamp(0.0, 1.0);
        Sprite { pos, size, rot, uv, color: [rgb.x * o, rgb.y * o, rgb.z * o, o * cover.clamp(0.0, 1.0)] }
    }
}

/// What is drawn this frame: sprites by (group, atlas, first person).
#[derive(Resource, Default)]
pub struct FxFrame {
    lists: HashMap<(u32, Fx, bool), Vec<Sprite>>,
}

impl FxFrame {
    pub fn push(&mut self, group: u32, fx: Fx, first_person: bool, sprite: Sprite) {
        if sprite.color.iter().all(|c| *c <= 0.0) {
            return;
        }
        self.lists.entry((group, fx, first_person)).or_default().push(sprite);
    }
}

#[derive(PartialEq)]
enum Load {
    Off,
    Loading,
    Ready,
    Failed,
}

/// The atlases and their materials.
#[derive(Resource)]
pub struct FxAssets {
    sheets: HashMap<Fx, Sheet>,
    images: Vec<Handle<Image>>,
    materials: HashMap<Fx, Handle<FxMaterial>>,
    load: Load,
}

impl FxAssets {
    pub fn ready(&self) -> bool {
        self.load == Load::Ready
    }

    pub fn sheet(&self, fx: Fx) -> Sheet {
        self.sheets.get(&fx).copied().unwrap_or(Sheet::new(1, 1, 1))
    }
}

/// The page passes whether the atlases exist, as it does for the other CS2 files.
#[derive(Resource)]
pub struct UseCs2Fx(pub bool);

/// Start loading every atlas.
pub fn setup_fx(
    mut commands: Commands,
    enabled: Res<UseCs2Fx>,
    asset_server: Res<AssetServer>,
    mut materials: ResMut<Assets<FxMaterial>>,
) {
    let parsed = match parse_sheets(SHEET_LIST) {
        Ok(p) => p,
        Err(e) => {
            warn!("fx_sheets.txt: {e}");
            Vec::new()
        }
    };
    let mut sheets = HashMap::new();
    for fx in Fx::ALL {
        if let Some((_, sheet)) = parsed.iter().find(|(name, _)| name == fx.name()) {
            sheets.insert(fx, *sheet);
        }
    }
    let mut out = FxAssets { sheets, images: Vec::new(), materials: HashMap::new(), load: Load::Off };
    if enabled.0 && out.sheets.len() == Fx::ALL.len() {
        for fx in Fx::ALL {
            let image: Handle<Image> = asset_server
                .load_builder()
                .with_settings(|s: &mut ImageLoaderSettings| {
                    s.is_srgb = true;
                    s.sampler = ImageSampler::Descriptor(ImageSamplerDescriptor {
                        address_mode_u: ImageAddressMode::ClampToEdge,
                        address_mode_v: ImageAddressMode::ClampToEdge,
                        mag_filter: ImageFilterMode::Linear,
                        min_filter: ImageFilterMode::Linear,
                        mipmap_filter: ImageFilterMode::Linear,
                        ..default()
                    });
                })
                .load(format!("{FX_DIR}/{}.ktx2", fx.name()));
            out.materials.insert(fx, materials.add(FxMaterial { atlas: image.clone() }));
            out.images.push(image);
        }
        out.load = Load::Loading;
    }
    commands.insert_resource(out);
}

/// Ready once every atlas is in; one that fails keeps the simple shapes for good.
pub fn track_fx(asset_server: Res<AssetServer>, mut fx: ResMut<FxAssets>) {
    if fx.load != Load::Loading {
        return;
    }
    let mut all = true;
    for image in &fx.images {
        match asset_server.load_state(image) {
            LoadState::Loaded => {}
            LoadState::Failed(e) => {
                warn!("effect atlas failed to load ({e}); keeping the simple effects");
                fx.load = Load::Failed;
                return;
            }
            _ => all = false,
        }
    }
    if all {
        fx.load = Load::Ready;
    }
}

/// The entity and mesh of each group ever drawn. They stay for later frames and rounds.
#[derive(Resource, Default)]
pub struct FxPool {
    emitters: HashMap<(u32, Fx, bool), (Entity, Handle<Mesh>)>,
}

const CORNERS: [(f32, f32); 4] = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)];

/// Back to front from `eye`, then into `mesh` around `centre`.
fn write_mesh(mesh: &mut Mesh, sprites: &mut [Sprite], centre: Vec3, eye: Vec3) {
    sprites.sort_by(|a, b| b.pos.distance_squared(eye).total_cmp(&a.pos.distance_squared(eye)));
    let n = sprites.len() * 4;
    let mut position = Vec::with_capacity(n);
    let mut corner = Vec::with_capacity(n);
    let mut uv = Vec::with_capacity(n);
    let mut color = Vec::with_capacity(n);
    let mut index = Vec::with_capacity(sprites.len() * 6);
    for (i, s) in sprites.iter().enumerate() {
        let local = (s.pos - centre).to_array();
        let [u0, v0, u1, v1] = s.uv;
        for (cx, cy) in CORNERS {
            position.push(local);
            corner.push([cx, cy, s.size, s.rot]);
            uv.push([if cx < 0.0 { u0 } else { u1 }, if cy < 0.0 { v1 } else { v0 }]);
            color.push(s.color);
        }
        let b = (i * 4) as u32;
        index.extend_from_slice(&[b, b + 1, b + 2, b, b + 2, b + 3]);
    }
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, position);
    mesh.insert_attribute(ATTRIBUTE_FX_CORNER, corner);
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uv);
    mesh.insert_attribute(Mesh::ATTRIBUTE_COLOR, color);
    mesh.insert_indices(Indices::U32(index));
}

/// Turn this frame's sprites into meshes, and hide the groups that have none.
pub fn flush_fx(
    mut commands: Commands,
    mut frame: ResMut<FxFrame>,
    mut pool: ResMut<FxPool>,
    fx: Res<FxAssets>,
    mut meshes: ResMut<Assets<Mesh>>,
    q_cam: Query<&GlobalTransform, With<MainCam>>,
    mut q_emitter: Query<(&mut Transform, &mut Visibility)>,
) {
    let eye = q_cam.single().map_or(Vec3::ZERO, |c| c.translation());
    let ready = fx.ready();
    for (key, list) in frame.lists.iter_mut() {
        if list.is_empty() || !ready {
            if let Some((e, _)) = pool.emitters.get(key) {
                if let Ok((_, mut vis)) = q_emitter.get_mut(*e) {
                    if *vis != Visibility::Hidden {
                        *vis = Visibility::Hidden;
                    }
                }
            }
            list.clear();
            continue;
        }
        let centre = list.iter().map(|s| s.pos).sum::<Vec3>() / list.len() as f32;
        match pool.emitters.get(key) {
            Some((e, handle)) => {
                if let Some(mut mesh) = meshes.get_mut(handle) {
                    write_mesh(&mut mesh, list, centre, eye);
                }
                if let Ok((mut tf, mut vis)) = q_emitter.get_mut(*e) {
                    tf.translation = centre;
                    *vis = Visibility::Visible;
                }
            }
            None => {
                let Some(material) = fx.materials.get(&key.1) else {
                    list.clear();
                    continue;
                };
                let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, RenderAssetUsages::default());
                write_mesh(&mut mesh, list, centre, eye);
                let handle = meshes.add(mesh);
                let layer = if key.2 { crate::viewmodel::FP_LAYER } else { 0 };
                let e = commands
                    .spawn((
                        Mesh3d(handle.clone()),
                        MeshMaterial3d(material.clone()),
                        Transform::from_translation(centre),
                        Visibility::Visible,
                        NoFrustumCulling,
                        RenderLayers::layer(layer),
                    ))
                    .id();
                pool.emitters.insert(*key, (e, handle));
            }
        }
        list.clear();
    }
}

/// A repeatable pseudo-random number in [0, 1) for `(seed, i)`: the same sprite gets the
/// same size and turn every frame, and on every scrub.
pub fn hash01(seed: u32, i: u32) -> f32 {
    let mut x = seed.wrapping_mul(0x9E37_79B9) ^ i.wrapping_mul(0x85EB_CA6B);
    x ^= x >> 16;
    x = x.wrapping_mul(0x7FEB_352D);
    x ^= x >> 15;
    x = x.wrapping_mul(0x846C_A68B);
    x ^= x >> 16;
    (x >> 8) as f32 / (1u32 << 24) as f32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_kind_has_a_line_in_the_sheet_list() {
        let sheets = parse_sheets(SHEET_LIST).unwrap();
        for fx in Fx::ALL {
            assert!(sheets.iter().any(|(n, _)| n == fx.name()), "{} is not in fx_sheets.txt", fx.name());
        }
        assert_eq!(sheets.len(), Fx::ALL.len(), "a line no effect uses");
    }

    #[test]
    fn the_layout_matches_what_prepare_fx_writes() {
        // prepare_fx.py: smoke 32 frames 1024x512, fireball 48 frames 1024x1024, glow 4 pictures 1024x256
        let sheets: HashMap<String, Sheet> = parse_sheets(SHEET_LIST).unwrap().into_iter().collect();
        let size = |s: &Sheet| (s.columns * s.cell, s.rows * s.cell);
        assert_eq!(size(&sheets["smoke"]), (1024, 512));
        assert_eq!(size(&sheets["fireball"]), (1024, 1024));
        assert_eq!(size(&sheets["glow"]), (1024, 256));
        assert_eq!(size(&sheets["dust"]), (1024, 128));
        assert_eq!(sheets["glow"].sequences, 4);
        assert_eq!(sheets["fire"].sequences * sheets["fire"].frames, 64);
    }

    #[test]
    fn a_frame_finds_its_cell() {
        let sheet = Sheet::new(3, 16, 128); // 48 pictures, 8 x 8
        let [u0, v0, u1, v1] = sheet.uv(1, 2); // picture 18: column 2, row 2
        assert!((u0 - 0.25).abs() < 0.01 && (u1 - 0.375).abs() < 0.01, "{u0} {u1}");
        assert!((v0 - 0.25).abs() < 0.01 && (v1 - 0.375).abs() < 0.01, "{v0} {v1}");
        assert!(u0 > 0.25 && u1 < 0.375, "kept inside its cell");
        assert_eq!(sheet.uv(4, 0), sheet.uv(1, 0), "the sequence wraps");
        assert_eq!(sheet.uv(0, 99), sheet.uv(0, 15), "the frame holds at the last");
    }

    #[test]
    fn frames_loop_or_hold() {
        let sheet = Sheet::new(1, 16, 64);
        assert_eq!(sheet.frame_at(0.5, 10.0, true), 5);
        assert_eq!(sheet.frame_at(2.0, 10.0, true), 4);
        assert_eq!(sheet.frame_at(2.0, 10.0, false), 15);
        assert_eq!(sheet.frame_at(-1.0, 10.0, false), 0);
    }

    #[test]
    fn a_flash_adds_light_and_smoke_covers() {
        let flash = Sprite::new(Vec3::ZERO, 1.0, 0.0, [0.0; 4], Vec3::ONE, 0.5, 0.0);
        assert_eq!(flash.color, [0.5, 0.5, 0.5, 0.0]);
        let smoke = Sprite::new(Vec3::ZERO, 1.0, 0.0, [0.0; 4], Vec3::splat(0.4), 0.5, 1.0);
        assert_eq!(smoke.color, [0.2, 0.2, 0.2, 0.5]);
    }

    #[test]
    fn sprites_are_written_back_to_front() {
        let near = Sprite::new(Vec3::new(0.0, 0.0, 1.0), 1.0, 0.0, [0.0; 4], Vec3::ONE, 1.0, 1.0);
        let far = Sprite::new(Vec3::new(0.0, 0.0, 9.0), 2.0, 0.0, [0.0; 4], Vec3::ONE, 1.0, 1.0);
        let mut list = vec![near, far];
        let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, RenderAssetUsages::default());
        write_mesh(&mut mesh, &mut list, Vec3::ZERO, Vec3::ZERO);
        assert_eq!(list[0].size, 2.0, "the far one first");
        assert_eq!(mesh.count_vertices(), 8);
        assert_eq!(mesh.indices().unwrap().len(), 12);
    }

    #[test]
    fn the_random_numbers_repeat_and_spread() {
        assert_eq!(hash01(7, 3), hash01(7, 3));
        let v: Vec<f32> = (0..1000).map(|i| hash01(11, i)).collect();
        assert!(v.iter().all(|x| (0.0..1.0).contains(x)));
        let mean = v.iter().sum::<f32>() / v.len() as f32;
        assert!((mean - 0.5).abs() < 0.05, "{mean}");
    }
}
