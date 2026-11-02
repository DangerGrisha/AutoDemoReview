//! The textured CS2 map: loads `world.c2map` (built by tools/prepare_map_textured.py),
//! spawns one entity per chunk, and swaps textures in as they arrive.
//!
//! The geometry appears first in flat colours, and each texture replaces its colour when
//! it is ready, instead of the map waiting for 350 downloads. A texture that fails to
//! load just stays flat. A `world.c2map` that fails falls back to the plain map.

use bevy::asset::io::Reader;
use bevy::asset::{AssetLoader, LoadContext, LoadState, RenderAssetUsages};
use bevy::camera::primitives::Aabb;
use bevy::camera::visibility::NoFrustumCulling;
use bevy::image::{
    CompressedImageFormatSupport, CompressedImageFormats, ImageAddressMode, ImageFilterMode,
    ImageLoaderSettings, ImageSampler, ImageSamplerDescriptor,
};
use bevy::mesh::{Indices, PrimitiveTopology, VertexAttributeValues};
use bevy::prelude::*;
use serde_json::Value;

use crate::map_material::{
    MapLayeredMaterial, MapMaterial, MapParams, ATTRIBUTE_MAP_COLOR, ATTRIBUTE_MAP_LIGHT_UV,
    ATTRIBUTE_MAP_NORMAL,
};
use crate::state::{MainCam, ManifestData, MapTag, Viewer};

const MAGIC: &[u8; 8] = b"C2MAP2\0\0";
/// Must match FORMAT_VERSION in tools/prepare_map_textured.py.
const FORMAT_VERSION: i64 = 1;
/// Pulls decals in front of the wall they lie on.
const DECAL_DEPTH_BIAS: f32 = 8.0;
/// In scene units: well inside the camera's far plane of 4000.
const SKY_RADIUS: f32 = 3000.0;

// ---------------------------------------------------------------------------
// file
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Default, PartialEq)]
pub struct MaterialDef {
    pub alpha: u8,
    pub cutoff: f32,
    pub two_sided: bool,
    pub unlit: bool,
    pub decal: bool,
    pub tint: [f32; 3],
    pub flat: [f32; 3],
    pub opacity: f32,
    pub color: Option<String>,
    pub color2: Option<String>,
    pub blend: Option<String>,
}

/// Floor light per cell of a coarse grid over the map, in Hammer units.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LightGrid {
    pub origin: [f32; 3],
    pub cell: [f32; 3],
    pub dims: [usize; 3],
    /// sqrt-coded rgb, ordered x, then y, then z
    pub data: Vec<u8>,
    pub range: f32,
}

impl LightGrid {
    /// Linear light at a point. Outside the grid it reads the nearest edge.
    pub fn sample(&self, p: Vec3) -> Vec3 {
        if self.data.is_empty() {
            return Vec3::ONE;
        }
        let cell = |axis: usize, v: f32| -> usize {
            let i = ((v - self.origin[axis]) / self.cell[axis]).floor();
            (i.max(0.0) as usize).min(self.dims[axis] - 1)
        };
        let (x, y, z) = (cell(0, p.x), cell(1, p.y), cell(2, p.z));
        let at = 3 * ((x * self.dims[1] + y) * self.dims[2] + z);
        let code = |i: usize| {
            let c = f32::from(self.data[at + i]) / 255.0;
            c * c * self.range
        };
        Vec3::new(code(0), code(1), code(2))
    }
}

pub struct ParsedChunk {
    pub material: usize,
    pub lo: [f32; 3],
    pub hi: [f32; 3],
    pub position: Vec<[f32; 3]>,
    pub normal: Vec<[i8; 4]>,
    pub uv: Vec<[f32; 2]>,
    pub light_uv: Vec<[u16; 2]>,
    pub color: Vec<[u8; 4]>,
    pub index: Vec<u16>,
}

pub struct ParsedWorld {
    pub materials: Vec<MaterialDef>,
    pub chunks: Vec<ParsedChunk>,
    pub lightmap: Option<String>,
    pub sky: Option<String>,
    pub sun_dir: [f32; 3],
    pub sun_rgb: [f32; 3],
    pub tone: Tone,
    pub light_range: f32,
    pub grid: LightGrid,
}

fn floats<const N: usize>(v: Option<&Value>) -> Result<[f32; N], String> {
    let a = v.and_then(Value::as_array).ok_or("expected an array of numbers")?;
    if a.len() < N {
        return Err(format!("expected {N} numbers, got {}", a.len()));
    }
    let mut out = [0.0; N];
    for (slot, x) in out.iter_mut().zip(a) {
        *slot = x.as_f64().ok_or("expected a number")? as f32;
    }
    Ok(out)
}

fn take<'a>(bytes: &'a [u8], at: &mut usize, n: usize) -> Result<&'a [u8], String> {
    let end = at
        .checked_add(n)
        .filter(|e| *e <= bytes.len())
        .ok_or_else(|| format!("file ends early: {n} bytes at {at}"))?;
    let out = &bytes[*at..end];
    *at = end + (4 - n % 4) % 4; // every stream is padded to four bytes
    Ok(out)
}

fn parse_material(m: &Value) -> Result<MaterialDef, String> {
    let text = |k: &str| m.get(k).and_then(Value::as_str).map(str::to_string);
    Ok(MaterialDef {
        alpha: m.get("alpha").and_then(Value::as_u64).unwrap_or(0) as u8,
        cutoff: m.get("cutoff").and_then(Value::as_f64).unwrap_or(0.5) as f32,
        two_sided: m.get("two_sided").and_then(Value::as_bool).unwrap_or(false),
        unlit: m.get("unlit").and_then(Value::as_bool).unwrap_or(false),
        decal: m.get("decal").and_then(Value::as_bool).unwrap_or(false),
        tint: floats(m.get("tint")).unwrap_or([1.0; 3]),
        flat: floats(m.get("flat")).unwrap_or([0.5; 3]),
        opacity: m.get("opacity").and_then(Value::as_f64).unwrap_or(1.0) as f32,
        color: text("color"),
        color2: text("color2"),
        blend: text("blend"),
    })
}

/// Everything here returns an error instead of panicking: a damaged download has to end
/// in the plain map, not in a dead page.
pub fn parse_world(bytes: &[u8]) -> Result<ParsedWorld, String> {
    if bytes.len() < 16 || &bytes[..8] != MAGIC {
        return Err("not a c2map file".into());
    }
    let json_len = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let binary_at = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
    let json_end = 16usize.checked_add(json_len).filter(|e| *e <= bytes.len()).ok_or("bad header length")?;
    if binary_at > bytes.len() || binary_at % 4 != 0 {
        return Err("bad binary offset".into());
    }
    let head: Value = serde_json::from_slice(&bytes[16..json_end]).map_err(|e| e.to_string())?;
    let version = head.get("version").and_then(Value::as_i64).unwrap_or(0);
    if version != FORMAT_VERSION {
        return Err(format!("format version {version}, this viewer reads {FORMAT_VERSION}"));
    }

    let materials = head
        .get("materials")
        .and_then(Value::as_array)
        .ok_or("no materials")?
        .iter()
        .map(parse_material)
        .collect::<Result<Vec<_>, _>>()?;

    let binary = &bytes[binary_at..];
    let mut chunks = Vec::new();
    for c in head.get("chunks").and_then(Value::as_array).ok_or("no chunks")? {
        let num = |k: &str| c.get(k).and_then(Value::as_u64).map(|n| n as usize).ok_or(format!("chunk.{k}"));
        let material = num("m")?;
        if material >= materials.len() {
            return Err(format!("chunk uses material {material} of {}", materials.len()));
        }
        let (verts, indices) = (num("v")?, num("i")?);
        let mut at = num("o")?;
        let position = take(binary, &mut at, verts * 12)?
            .chunks_exact(12)
            .map(|b| {
                [
                    f32::from_le_bytes(b[0..4].try_into().unwrap()),
                    f32::from_le_bytes(b[4..8].try_into().unwrap()),
                    f32::from_le_bytes(b[8..12].try_into().unwrap()),
                ]
            })
            .collect();
        let normal = take(binary, &mut at, verts * 4)?
            .chunks_exact(4)
            .map(|b| [b[0] as i8, b[1] as i8, b[2] as i8, 0])
            .collect();
        let uv = take(binary, &mut at, verts * 8)?
            .chunks_exact(8)
            .map(|b| {
                [
                    f32::from_le_bytes(b[0..4].try_into().unwrap()),
                    f32::from_le_bytes(b[4..8].try_into().unwrap()),
                ]
            })
            .collect();
        let light_uv = take(binary, &mut at, verts * 4)?
            .chunks_exact(4)
            .map(|b| {
                [
                    u16::from_le_bytes(b[0..2].try_into().unwrap()),
                    u16::from_le_bytes(b[2..4].try_into().unwrap()),
                ]
            })
            .collect();
        let color = take(binary, &mut at, verts * 4)?
            .chunks_exact(4)
            .map(|b| [b[0], b[1], b[2], b[3]])
            .collect();
        let index: Vec<u16> = take(binary, &mut at, indices * 2)?
            .chunks_exact(2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect();
        if index.iter().any(|i| usize::from(*i) >= verts) {
            return Err("index past the chunk's vertices".into());
        }
        chunks.push(ParsedChunk {
            material,
            lo: floats(c.get("lo"))?,
            hi: floats(c.get("hi"))?,
            position,
            normal,
            uv,
            light_uv,
            color,
            index,
        });
    }

    let light_range = head.get("light_range").and_then(Value::as_f64).unwrap_or(4.0) as f32;
    let grid = {
        let g = head.get("grid").ok_or("no grid")?;
        let d = floats::<3>(g.get("dims"))?;
        let dims = [d[0] as usize, d[1] as usize, d[2] as usize];
        let mut at = g.get("offset").and_then(Value::as_u64).ok_or("grid.offset")? as usize;
        let cells = dims[0].checked_mul(dims[1]).and_then(|n| n.checked_mul(dims[2])).ok_or("grid size")?;
        if cells == 0 {
            return Err("empty light grid".into());
        }
        let data = take(binary, &mut at, cells.checked_mul(3).ok_or("grid size")?)?.to_vec();
        LightGrid {
            origin: floats(g.get("origin"))?,
            cell: floats(g.get("cell"))?,
            dims,
            data,
            range: light_range,
        }
    };

    let sun = head.get("sun").ok_or("no sun")?;
    Ok(ParsedWorld {
        materials,
        chunks,
        lightmap: head.get("lightmap").and_then(Value::as_str).map(str::to_string),
        sky: head.get("sky").and_then(Value::as_str).map(str::to_string),
        sun_dir: floats(sun.get("dir"))?,
        sun_rgb: floats(sun.get("rgb"))?,
        tone: Tone::from_header(head.get("tone")),
        light_range,
        grid,
    })
}

// ---------------------------------------------------------------------------
// asset
// ---------------------------------------------------------------------------

pub struct ChunkRef {
    pub mesh: Handle<Mesh>,
    pub material: usize,
    pub aabb: Aabb,
}

#[derive(Asset, TypePath)]
pub struct MapGeo {
    pub materials: Vec<MaterialDef>,
    pub chunks: Vec<ChunkRef>,
    pub lightmap: Option<String>,
    pub sky: Option<String>,
    pub sun_dir: [f32; 3],
    pub sun_rgb: [f32; 3],
    pub tone: Tone,
    pub light_range: f32,
    pub grid: LightGrid,
}

fn chunk_mesh(c: ParsedChunk) -> Mesh {
    // the main world never reads the map, so the CPU copy goes once it is uploaded
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, RenderAssetUsages::RENDER_WORLD);
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, c.position);
    mesh.insert_attribute(ATTRIBUTE_MAP_NORMAL, VertexAttributeValues::Snorm8x4(c.normal));
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, c.uv);
    mesh.insert_attribute(ATTRIBUTE_MAP_LIGHT_UV, VertexAttributeValues::Unorm16x2(c.light_uv));
    mesh.insert_attribute(ATTRIBUTE_MAP_COLOR, VertexAttributeValues::Unorm8x4(c.color));
    mesh.insert_indices(Indices::U16(c.index));
    mesh
}

#[derive(Default, TypePath)]
pub struct MapGeoLoader;

impl AssetLoader for MapGeoLoader {
    type Asset = MapGeo;
    type Settings = ();
    type Error = std::io::Error;

    async fn load(
        &self,
        reader: &mut dyn Reader,
        _settings: &(),
        load_context: &mut LoadContext<'_>,
    ) -> Result<MapGeo, Self::Error> {
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes).await?;
        let world = parse_world(&bytes)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        drop(bytes);
        let mut chunks = Vec::with_capacity(world.chunks.len());
        for (i, c) in world.chunks.into_iter().enumerate() {
            let (material, lo, hi) = (c.material, c.lo, c.hi);
            let mesh = load_context.add_labeled_asset(format!("chunk{i}"), chunk_mesh(c));
            chunks.push(ChunkRef {
                mesh,
                material,
                aabb: Aabb::from_min_max(Vec3::from_array(lo), Vec3::from_array(hi)),
            });
        }
        Ok(MapGeo {
            materials: world.materials,
            chunks,
            lightmap: world.lightmap,
            sky: world.sky,
            sun_dir: world.sun_dir,
            sun_rgb: world.sun_rgb,
            tone: world.tone,
            light_range: world.light_range,
            grid: world.grid,
        })
    }

    fn extensions(&self) -> &[&str] {
        &["c2map"]
    }
}

// ---------------------------------------------------------------------------
// viewer
// ---------------------------------------------------------------------------

/// A textured map on its way in. Removed once it is spawned or has failed.
#[derive(Resource)]
pub struct PendingMap {
    geo: Handle<MapGeo>,
    /// asset path of the folder holding the textures
    dir: String,
    scene_scale: f32,
    fallback: Option<String>,
}

/// The map's textures still downloading. Empty when all of them have arrived or failed.
#[derive(Resource, Default)]
pub struct TextureQueue(Vec<QueuedTexture>);

struct QueuedTexture {
    image: Handle<Image>,
    target: MatRef,
    slot: Slot,
    /// the material's real tint, set when the flat stand-in colour goes
    tint: [f32; 3],
}

#[derive(Clone)]
enum MatRef {
    Plain(Handle<MapMaterial>),
    Layered(Handle<MapLayeredMaterial>),
}

#[derive(Clone, Copy, PartialEq)]
enum Slot {
    Color,
    Color2,
    Blend,
    Light,
}

/// The map's tone curve: Valve's filmic one, with the numbers from its post-processing file.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Tone {
    pub exposure: f32,
    pub white: f32,
    /// shoulder, linear strength, linear angle, toe strength, toe numerator, toe denominator
    pub curve: [f32; 6],
}

impl Tone {
    /// These numbers make the curve a straight line up to the white point.
    const STRAIGHT: [f32; 6] = [0.0, 0.001, 0.001, 1.0, 1.0, 1.0];
    /// What the engine multiplies an exposed picture by before the curve. Without it, and with
    /// the white point read as a plain number, de_mirage came out 1.5 times too dark.
    const BEFORE_CURVE: f32 = 2.8;

    fn from_header(header: Option<&Value>) -> Self {
        let number = |key: &str, fallback: f32| {
            header.and_then(|t| t.get(key)).and_then(Value::as_f64).map_or(fallback, |v| v as f32)
        };
        let mut tone = Self {
            exposure: number("exposure", 1.0),
            // a file without tone settings shows its colours as they are
            white: number("white", Self::BEFORE_CURVE.log2()),
            curve: floats::<6>(header.and_then(|t| t.get("curve"))).unwrap_or(Self::STRAIGHT),
        };
        // numbers that divide by zero would turn the whole map black
        if !(tone.bend(tone.white_point()).abs() > 1e-6) {
            tone.curve = Self::STRAIGHT;
        }
        tone
    }

    fn bend(&self, x: f32) -> f32 {
        let [a, b, c, d, e, f] = self.curve;
        (x * (a * x + c * b) + d * e) / (x * (a * x + b) + d * f) - e / f
    }

    /// The file gives the white point in stops.
    fn white_point(&self) -> f32 {
        2.0_f32.powf(self.white)
    }

    /// What light is multiplied by on its way into the curve.
    fn scale(&self) -> f32 {
        self.exposure * Self::BEFORE_CURVE
    }

    /// What the screen gets for this much light.
    fn apply(&self, light: f32) -> f32 {
        self.bend((light * self.scale()).min(self.white_point())) / self.bend(self.white_point())
    }

    /// The two vectors the shader reads the curve from.
    fn uniforms(&self) -> (Vec4, Vec4) {
        let [a, b, c, d, e, f] = self.curve;
        let white = self.white_point();
        (Vec4::new(a, b, c, d), Vec4::new(e, f, white, 1.0 / self.bend(white)))
    }
}

/// What players are lit with on the textured map, and how much of it reaches their feet.
#[derive(Resource)]
pub struct MapLight {
    pub grid: LightGrid,
    /// the light on a player in the open, averaged over all the ways a surface can face
    open_player: Vec3,
    sun_color: Color,
    sun_illuminance: f32,
    ambient: f32,
}

impl MapLight {
    /// The numbers that make Bevy's PBR land where the map's shader does. Bevy multiplies by
    /// its exposure and takes the albedo over pi on a directional light, the map by its tone
    /// curve and takes no pi, so the same sun gets the same pixels. Bevy's side is a straight
    /// line, so this matches the curve where a lit surface mostly sits: at a light of 1.
    fn new(grid: LightGrid, sun_dir: [f32; 3], sun_rgb: [f32; 3], tone: Tone) -> Self {
        /// Bevy's default camera exposure, EV100 9.7.
        const BEVY_EXPOSURE: f32 = 0.001_002;
        /// What a floor in the open reads from the sky on these maps, whatever the sun does.
        const SKY_ON_FLOOR: f32 = 0.5;
        /// A surface turned every way at once gets a quarter of a light it could face.
        const SUN_ON_AVERAGE: f32 = 0.25;
        let sun = Vec3::from_array(sun_rgb);
        let scale = tone.apply(1.0);
        let on_screen = sun * scale;
        let strongest = on_screen.max_element().max(0.01);
        let tint = on_screen / strongest;
        Self {
            grid,
            open_player: sun * SUN_ON_AVERAGE + Vec3::splat(SKY_ON_FLOOR),
            sun_color: Color::linear_rgb(tint.x, tint.y, tint.z),
            sun_illuminance: strongest * std::f32::consts::PI / BEVY_EXPOSURE,
            ambient: SKY_ON_FLOOR * scale / BEVY_EXPOSURE,
        }
    }

    /// The light at this point, in Hammer units, as a multiplier on a player already lit for
    /// the open. It compares with the average over a player's surfaces and not with a floor
    /// facing the sun: shade comes from all around, and the viewer's one sun only reaches the
    /// side facing it, so the floor's number left a player in the shade black from behind.
    /// The minimum keeps them from going fully black: a dark room is dark, a player is still
    /// something you need to find.
    pub fn at(&self, p: Vec3) -> Vec3 {
        const DARKEST: f32 = 0.06;
        (self.grid.sample(p) / self.open_player).clamp(Vec3::splat(DARKEST), Vec3::ONE)
    }
}

/// While the textured map shows, the viewer's one sun and ambient are the map's.
pub fn light_players_like_the_map(
    viewer: Res<Viewer>,
    map_light: Option<Res<MapLight>>,
    mut q_sun: Query<&mut DirectionalLight>,
    mut q_ambient: Query<&mut AmbientLight, With<MainCam>>,
) {
    let Some(light) = map_light else {
        return;
    };
    let (color, illuminance) = if viewer.show_map {
        (light.sun_color, light.sun_illuminance)
    } else {
        (Color::WHITE, crate::DEFAULT_SUN)
    };
    for mut sun in &mut q_sun {
        if sun.illuminance != illuminance {
            sun.illuminance = illuminance;
            sun.color = color;
        }
    }
    if viewer.show_map {
        for mut amb in &mut q_ambient {
            if amb.brightness != light.ambient {
                amb.brightness = light.ambient;
            }
        }
    }
}

#[derive(Component)]
pub struct SkyDome;

/// Start loading the textured map if there is one and this GPU can show it.
/// Returns false when the plain map should load instead.
pub fn begin(
    commands: &mut Commands,
    asset_server: &AssetServer,
    formats: Option<&CompressedImageFormatSupport>,
    m: &ManifestData,
) -> bool {
    let Some(path) = m.map_v2.clone() else {
        return false;
    };
    let bc = formats.is_some_and(|f| f.0.contains(CompressedImageFormats::BC));
    if !bc {
        warn!("no BC texture support here: staying on the plain map");
        return false;
    }
    let dir = path.rsplit_once('/').map(|(d, _)| d.to_string()).unwrap_or_default();
    commands.insert_resource(PendingMap {
        geo: asset_server.load(path),
        dir,
        scene_scale: m.scene_scale as f32,
        fallback: m.map_asset.clone(),
    });
    true
}

fn texture_settings(linear_only_lightmap: bool) -> ImageSamplerDescriptor {
    if linear_only_lightmap {
        // charts sit right next to each other: no wrapping and no anisotropy reaching over
        ImageSamplerDescriptor {
            address_mode_u: ImageAddressMode::ClampToEdge,
            address_mode_v: ImageAddressMode::ClampToEdge,
            mag_filter: ImageFilterMode::Linear,
            min_filter: ImageFilterMode::Linear,
            mipmap_filter: ImageFilterMode::Linear,
            ..default()
        }
    } else {
        ImageSamplerDescriptor {
            address_mode_u: ImageAddressMode::Repeat,
            address_mode_v: ImageAddressMode::Repeat,
            mag_filter: ImageFilterMode::Linear,
            min_filter: ImageFilterMode::Linear,
            mipmap_filter: ImageFilterMode::Linear,
            anisotropy_clamp: 8,
            ..default()
        }
    }
}

/// What a texture holds, which decides how it is read.
#[derive(Clone, Copy, PartialEq, Debug)]
enum TextureKind {
    Color,
    Mask,
    Lightmap,
}

impl TextureKind {
    /// Bevy ignores the format written in a KTX2 file and reads it as colour unless told
    /// otherwise. A lightmap read as colour keeps the sun, which is in its alpha, and loses
    /// ten to twenty times the light in the shade, so rooms go black and streets look right.
    fn is_srgb(self) -> bool {
        self == Self::Color
    }
}

fn load_texture(asset_server: &AssetServer, path: String, kind: TextureKind) -> Handle<Image> {
    asset_server
        .load_builder()
        .with_settings(move |s: &mut ImageLoaderSettings| {
            s.is_srgb = kind.is_srgb();
            s.sampler = ImageSampler::Descriptor(texture_settings(kind == TextureKind::Lightmap));
        })
        .load(path)
}

fn map_params(def: &MaterialDef, geo: &MapGeo, tint: [f32; 3]) -> MapParams {
    let (curve, curve2) = geo.tone.uniforms();
    MapParams {
        tint: Vec4::new(tint[0], tint[1], tint[2], def.opacity),
        sun_dir: Vec4::new(geo.sun_dir[0], geo.sun_dir[1], geo.sun_dir[2], geo.light_range),
        sun_rgb: Vec4::new(geo.sun_rgb[0], geo.sun_rgb[1], geo.sun_rgb[2], geo.tone.scale()),
        misc: Vec4::new(if def.unlit { 1.0 } else { 0.0 }, 0.0, def.cutoff, 0.0),
        curve,
        curve2,
    }
}

fn alpha_mode(def: &MaterialDef) -> AlphaMode {
    match def.alpha {
        1 => AlphaMode::Mask(def.cutoff),
        2 => AlphaMode::Blend,
        _ => AlphaMode::Opaque,
    }
}

/// A UV sphere with the map's vertex layout, so the sky uses the map's material.
fn sky_mesh() -> Mesh {
    const STACKS: u32 = 24;
    const SLICES: u32 = 48;
    let n = ((STACKS + 1) * (SLICES + 1)) as usize;
    let mut position = Vec::with_capacity(n);
    let mut uv = Vec::with_capacity(n);
    for s in 0..=STACKS {
        let v = s as f32 / STACKS as f32;
        let polar = v * std::f32::consts::PI;
        for q in 0..=SLICES {
            let u = q as f32 / SLICES as f32;
            let az = u * std::f32::consts::TAU;
            position.push([polar.sin() * az.cos(), polar.cos(), polar.sin() * az.sin()]);
            uv.push([u, v]);
        }
    }
    let mut index = Vec::with_capacity((STACKS * SLICES * 6) as usize);
    for s in 0..STACKS {
        for q in 0..SLICES {
            let a = (s * (SLICES + 1) + q) as u16;
            let b = a + SLICES as u16 + 1;
            index.extend_from_slice(&[a, a + 1, b, b, a + 1, b + 1]);
        }
    }
    let mut mesh = Mesh::new(PrimitiveTopology::TriangleList, RenderAssetUsages::RENDER_WORLD);
    mesh.insert_attribute(Mesh::ATTRIBUTE_POSITION, position);
    mesh.insert_attribute(ATTRIBUTE_MAP_NORMAL, VertexAttributeValues::Snorm8x4(vec![[0, 127, 0, 0]; n]));
    mesh.insert_attribute(Mesh::ATTRIBUTE_UV_0, uv);
    mesh.insert_attribute(ATTRIBUTE_MAP_LIGHT_UV, VertexAttributeValues::Unorm16x2(vec![[0, 0]; n]));
    mesh.insert_attribute(ATTRIBUTE_MAP_COLOR, VertexAttributeValues::Unorm8x4(vec![[0, 0, 0, 0]; n]));
    mesh.insert_indices(Indices::U16(index));
    mesh
}

/// Waits for `world.c2map`, then spawns the map, or the plain map if it failed.
#[allow(clippy::too_many_arguments)]
pub fn finish_map_load(
    mut commands: Commands,
    pending: Option<Res<PendingMap>>,
    geos: Res<Assets<MapGeo>>,
    asset_server: Res<AssetServer>,
    mut plain: ResMut<Assets<MapMaterial>>,
    mut layered: ResMut<Assets<MapLayeredMaterial>>,
    mut meshes: ResMut<Assets<Mesh>>,
    viewer: Res<Viewer>,
    mut q_sun: Query<&mut Transform, With<DirectionalLight>>,
) {
    let Some(pending) = pending else {
        return;
    };
    if matches!(asset_server.get_load_state(&pending.geo), Some(LoadState::Failed(_))) {
        warn!("textured map failed to load: falling back to the plain map");
        if let Some(path) = pending.fallback.clone() {
            crate::scene::spawn_plain_map(&mut commands, &asset_server, path, pending.scene_scale);
        }
        commands.remove_resource::<PendingMap>();
        return;
    }
    let Some(geo) = geos.get(&pending.geo) else {
        return;
    };

    let visibility = if viewer.show_map { Visibility::Visible } else { Visibility::Hidden };
    let root = commands
        .spawn((Transform::from_scale(Vec3::splat(pending.scene_scale)), visibility, MapTag))
        .id();

    let path_of = |rel: &str| {
        if pending.dir.is_empty() {
            rel.to_string()
        } else {
            format!("{}/{rel}", pending.dir)
        }
    };
    let lightmap = geo
        .lightmap
        .as_ref()
        .map(|rel| load_texture(&asset_server, path_of(rel), TextureKind::Lightmap));

    let mut queue = TextureQueue::default();
    let mut refs = Vec::with_capacity(geo.materials.len());
    for def in &geo.materials {
        let alpha = alpha_mode(def);
        let bias = if def.decal { DECAL_DEPTH_BIAS } else { 0.0 };
        // flat stand-in until the texture is here; a material without one just stays so
        let flat = [def.tint[0] * def.flat[0], def.tint[1] * def.flat[1], def.tint[2] * def.flat[2]];
        let layers = def.color2.is_some() && def.blend.is_some();
        let target = if layers {
            MatRef::Layered(layered.add(MapLayeredMaterial {
                params: map_params(def, geo, flat),
                color: None,
                lightmap: None,
                color2: None,
                blend: None,
                alpha,
                bias,
                two_sided: def.two_sided,
            }))
        } else {
            MatRef::Plain(plain.add(MapMaterial {
                params: map_params(def, geo, flat),
                color: None,
                lightmap: None,
                alpha,
                bias,
                two_sided: def.two_sided,
            }))
        };
        for (rel, slot) in [(&def.color, Slot::Color), (&def.color2, Slot::Color2), (&def.blend, Slot::Blend)] {
            if let Some(rel) = rel {
                if slot != Slot::Color && !layers {
                    continue;
                }
                let kind = if slot == Slot::Blend { TextureKind::Mask } else { TextureKind::Color };
                queue.0.push(QueuedTexture {
                    image: load_texture(&asset_server, path_of(rel), kind),
                    target: target.clone(),
                    slot,
                    tint: def.tint,
                });
            }
        }
        if let Some(image) = &lightmap {
            queue.0.push(QueuedTexture {
                image: image.clone(),
                target: target.clone(),
                slot: Slot::Light,
                tint: def.tint,
            });
        }
        refs.push(target);
    }

    commands.entity(root).with_children(|p| {
        for c in &geo.chunks {
            let mut e = p.spawn((Mesh3d(c.mesh.clone()), Transform::default(), c.aabb));
            match &refs[c.material] {
                MatRef::Plain(h) => {
                    e.insert(MeshMaterial3d(h.clone()));
                }
                MatRef::Layered(h) => {
                    e.insert(MeshMaterial3d(h.clone()));
                }
            }
        }
    });

    if let Some(rel) = &geo.sky {
        let (curve, curve2) = geo.tone.uniforms();
        let material = plain.add(MapMaterial {
            params: MapParams {
                tint: Vec4::ONE,
                sun_rgb: Vec4::new(0.0, 0.0, 0.0, geo.tone.scale()),
                misc: Vec4::new(1.0, 0.0, 0.0, 0.0),
                curve,
                curve2,
                ..default()
            },
            color: None,
            lightmap: None,
            alpha: AlphaMode::Opaque,
            bias: 0.0,
            two_sided: true,
        });
        queue.0.push(QueuedTexture {
            image: load_texture(&asset_server, path_of(rel), TextureKind::Color),
            target: MatRef::Plain(material.clone()),
            slot: Slot::Color,
            tint: [1.0; 3],
        });
        commands.spawn((
            Mesh3d(meshes.add(sky_mesh())),
            MeshMaterial3d(material),
            Transform::from_scale(Vec3::splat(SKY_RADIUS)),
            visibility,
            NoFrustumCulling,
            SkyDome,
            MapTag,
        ));
    }

    // the real sun, so players are lit from where the map's shadows say it is
    let dir = Vec3::from_array(geo.sun_dir);
    for mut tf in &mut q_sun {
        *tf = Transform::from_translation(dir * 100.0).looking_at(Vec3::ZERO, Vec3::Y);
    }

    commands.insert_resource(MapLight::new(geo.grid.clone(), geo.sun_dir, geo.sun_rgb, geo.tone));
    commands.insert_resource(queue);
    commands.remove_resource::<PendingMap>();
}

/// Each frame, hands every texture that has arrived to its material.
pub fn apply_map_textures(
    queue: Option<ResMut<TextureQueue>>,
    images: Res<Assets<Image>>,
    asset_server: Res<AssetServer>,
    mut plain: ResMut<Assets<MapMaterial>>,
    mut layered: ResMut<Assets<MapLayeredMaterial>>,
) {
    let Some(mut queue) = queue else {
        return;
    };
    if queue.0.is_empty() {
        return;
    }
    queue.0.retain(|q| {
        if !images.contains(&q.image) {
            // a damaged texture stays flat, nothing more can come of it
            return !matches!(asset_server.get_load_state(&q.image), Some(LoadState::Failed(_)));
        }
        match &q.target {
            MatRef::Plain(h) => {
                if let Some(mut m) = plain.get_mut(h) {
                    match q.slot {
                        Slot::Color => {
                            m.color = Some(q.image.clone());
                            m.params.tint = Vec4::new(q.tint[0], q.tint[1], q.tint[2], m.params.tint.w);
                        }
                        Slot::Light => {
                            m.lightmap = Some(q.image.clone());
                            m.params.misc.y = 1.0;
                        }
                        Slot::Color2 | Slot::Blend => {}
                    }
                }
            }
            MatRef::Layered(h) => {
                if let Some(mut m) = layered.get_mut(h) {
                    match q.slot {
                        Slot::Color => {
                            m.color = Some(q.image.clone());
                            m.params.tint = Vec4::new(q.tint[0], q.tint[1], q.tint[2], m.params.tint.w);
                        }
                        Slot::Color2 => m.color2 = Some(q.image.clone()),
                        Slot::Blend => m.blend = Some(q.image.clone()),
                        Slot::Light => {
                            m.lightmap = Some(q.image.clone());
                            m.params.misc.y = 1.0;
                        }
                    }
                }
            }
        }
        false
    });
}

/// The sky has no position: it stays centred on the camera.
pub fn sky_follow_camera(
    q_cam: Query<&Transform, (With<MainCam>, Without<SkyDome>)>,
    mut q_sky: Query<&mut Transform, With<SkyDome>>,
) {
    let Ok(cam) = q_cam.single() else {
        return;
    };
    for mut tf in &mut q_sky {
        tf.translation = cam.translation;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The same bytes tools/prepare_map_textured.py writes, for one triangle.
    fn sample_file() -> Vec<u8> {
        let mut bin = Vec::new();
        for p in [[0.0f32, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]] {
            for x in p {
                bin.extend_from_slice(&x.to_le_bytes());
            }
        }
        bin.extend_from_slice(&[0, 127, 0, 0, 0, 127, 0, 0, 0, 127, 0, 0]);
        for x in [0.0f32, 0.0, 1.0, 0.0, 0.0, 1.0] {
            bin.extend_from_slice(&x.to_le_bytes());
        }
        for x in [0u16, 0, 65535, 0, 0, 65535] {
            bin.extend_from_slice(&x.to_le_bytes());
        }
        bin.extend_from_slice(&[0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255]);
        for x in [0u16, 1, 2] {
            bin.extend_from_slice(&x.to_le_bytes());
        }
        bin.extend_from_slice(&[0, 0]); // indices pad to four bytes
        let grid_at = bin.len();
        bin.extend_from_slice(&[255, 128, 0, 0, 0, 0]); // a 2x1x1 grid
        let json = format!(
            r#"{{"version":{FORMAT_VERSION},"materials":[{{"alpha":1,"cutoff":0.25,"two_sided":true,"color":"tex/a.ktx2","tint":[1,1,1],"flat":[0.1,0.2,0.3],"opacity":1}}],
            "chunks":[{{"m":0,"v":3,"i":3,"o":0,"lo":[0,0,0],"hi":[1,1,0]}}],
            "lightmap":"lightmap.ktx2","light_range":4.0,"sky":null,
            "sun":{{"dir":[0,1,0],"rgb":[3,2,1]}},
            "grid":{{"origin":[0,0,0],"cell":[10,10,10],"dims":[2,1,1],"offset":{grid_at}}}}}"#
        );
        let mut head = json.into_bytes();
        while (head.len() + 16) % 4 != 0 {
            head.push(b' ');
        }
        let mut out = MAGIC.to_vec();
        out.extend_from_slice(&(head.len() as u32).to_le_bytes());
        out.extend_from_slice(&((16 + head.len()) as u32).to_le_bytes());
        out.extend_from_slice(&head);
        out.extend_from_slice(&bin);
        out
    }

    #[test]
    fn parses_what_the_bake_writes() {
        let w = parse_world(&sample_file()).unwrap();
        assert_eq!(w.chunks.len(), 1);
        let c = &w.chunks[0];
        assert_eq!(c.position[1], [1.0, 0.0, 0.0]);
        assert_eq!(c.normal[2], [0, 127, 0, 0]);
        assert_eq!(c.uv[2], [0.0, 1.0]);
        assert_eq!(c.light_uv[1], [65535, 0]);
        assert_eq!(c.color[0], [0, 0, 0, 255]);
        assert_eq!(c.index, vec![0, 1, 2]);
        assert_eq!(w.materials[0].alpha, 1);
        assert!(w.materials[0].two_sided);
        assert_eq!(w.materials[0].color.as_deref(), Some("tex/a.ktx2"));
        assert_eq!(w.sun_rgb, [3.0, 2.0, 1.0]);
        // a file without tone settings shows its colours as they are
        assert!((w.tone.apply(0.5) - 0.5).abs() < 0.001);
    }

    #[test]
    fn damaged_files_are_errors_not_panics() {
        assert!(parse_world(b"").is_err());
        assert!(parse_world(b"not a map at all, and long enough").is_err());
        let good = sample_file();
        // cut anywhere: it must fail cleanly or still parse, never panic
        for cut in 0..good.len() {
            let _ = parse_world(&good[..cut]);
        }
        let mut other_version = good.clone();
        let at = other_version.windows(10).position(|w| w == b"\"version\":").unwrap() + 10;
        other_version[at] = b'9';
        assert!(parse_world(&other_version).err().unwrap().contains("version"));
    }

    #[test]
    fn a_bad_index_is_refused() {
        let mut f = sample_file();
        let binary_at = u32::from_le_bytes(f[12..16].try_into().unwrap()) as usize;
        // indices start after 3 vertices of position, normal, uv, light uv and colour
        let index_at = binary_at + 3 * (12 + 4 + 8 + 4 + 4);
        f[index_at + 2] = 200;
        assert!(parse_world(&f).is_err());
    }

    #[test]
    fn grid_reads_the_cell_and_clamps_outside() {
        let w = parse_world(&sample_file()).unwrap();
        let g = &w.grid;
        let first = g.sample(Vec3::new(5.0, 5.0, 5.0));
        assert!((first.x - 4.0).abs() < 1e-4, "{first}");
        assert!(g.sample(Vec3::new(15.0, 5.0, 5.0)).length() < 1e-4);
        // past the far edge reads the last cell, before the start reads the first
        assert!(g.sample(Vec3::new(1e6, 0.0, 0.0)).length() < 1e-4);
        assert!((g.sample(Vec3::splat(-1e6)).x - 4.0).abs() < 1e-4);
    }

    #[test]
    fn players_are_lit_relative_to_an_open_floor() {
        let grid = |code: u8| LightGrid {
            origin: [0.0; 3],
            cell: [64.0, 96.0, 64.0],
            dims: [1, 1, 1],
            data: vec![code; 3],
            range: 4.0,
        };
        let tone = Tone { exposure: 0.9, white: 1.5, curve: Tone::STRAIGHT };
        // the average over a player in the open is 0.75 + 0.5, 0.5 + 0.5 and 0.25 + 0.5
        let bright = MapLight::new(grid(255), [0.0, 1.0, 0.0], [3.0, 2.0, 1.0], tone);
        assert_eq!(bright.at(Vec3::ZERO), Vec3::ONE);
        let dark = MapLight::new(grid(0), [0.0, 1.0, 0.0], [3.0, 2.0, 1.0], tone);
        assert_eq!(dark.at(Vec3::ZERO), Vec3::splat(0.06));
        // shade of 0.5 all around, which the grid codes as sqrt(0.5 / 4)
        let shade = MapLight::new(grid(90), [0.0, 1.0, 0.0], [3.0, 2.0, 1.0], tone);
        let at = shade.at(Vec3::ZERO);
        assert!((at - Vec3::new(0.4, 0.5, 0.665)).length() < 0.01, "{at}");
        // full sun on an albedo of 1 gives what the map's shader gives: 3 * 0.9 * 2.8 / 2^1.5
        let pixel = bright.sun_illuminance / std::f32::consts::PI * 0.001_002;
        assert!((pixel - 2.675).abs() < 0.01, "{pixel}");
    }

    #[test]
    fn the_tone_curve_is_the_maps_own() {
        // de_mirage: a straight line up to the white point
        let mirage = Tone { exposure: 0.9, white: 1.5, curve: Tone::STRAIGHT };
        assert!((mirage.apply(1.0) - 0.892).abs() < 0.002, "{}", mirage.apply(1.0));
        assert_eq!(mirage.apply(100.0), 1.0);
        assert!(mirage.apply(0.0).abs() < 0.001);
        // de_ancient: it bends, so the shade is lifted against a straight line to the same white
        let ancient = Tone { exposure: 1.35, white: 4.0, curve: [0.1499, 0.5012, 0.0999, 0.1999, 0.02, 0.2999] };
        assert!((ancient.apply(1.0) - 0.648).abs() < 0.005, "{}", ancient.apply(1.0));
        assert_eq!(ancient.apply(100.0), 1.0);
        assert!(ancient.apply(0.5) < ancient.apply(1.0));
    }

    #[test]
    fn only_colour_is_read_as_srgb() {
        assert!(TextureKind::Color.is_srgb());
        assert!(!TextureKind::Mask.is_srgb());
        assert!(!TextureKind::Lightmap.is_srgb());
    }

    #[test]
    fn tone_numbers_that_would_divide_by_zero_are_replaced() {
        let header = serde_json::json!({ "exposure": 1.0, "white": 2.0, "curve": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0] });
        let tone = Tone::from_header(Some(&header));
        assert_eq!(tone.curve, Tone::STRAIGHT);
        // 2.8 into a straight line whose white point is 2^2
        assert!((tone.apply(1.0) - 0.7).abs() < 0.002, "{}", tone.apply(1.0));
    }
}
