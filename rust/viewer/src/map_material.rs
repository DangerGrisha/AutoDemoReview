//! Materials of the textured CS2 map, drawn by `map_material.wgsl`.
//!
//! Two types instead of one with optional textures: every texture in a bind group costs
//! GL calls on every draw (a texture and a sampler bind through the wasm-to-JS bridge).
//! Only 11 of de_mirage's 374 materials blend two layers, so everything else binds two
//! textures instead of four.

use bevy::asset::embedded_asset;
use bevy::mesh::{MeshVertexAttribute, MeshVertexBufferLayoutRef};
use bevy::pbr::{MaterialPipeline, MaterialPipelineKey};
use bevy::prelude::*;
use bevy::render::render_resource::{
    AsBindGroup, Face, RenderPipelineDescriptor, ShaderType, SpecializedMeshPipelineError,
    VertexFormat,
};
use bevy::shader::ShaderRef;

const SHADER: &str = "embedded://viewer/map_material.wgsl";

/// Smaller than Bevy's own attributes, which are 12 and 16 bytes.
pub const ATTRIBUTE_MAP_NORMAL: MeshVertexAttribute =
    MeshVertexAttribute::new("MapNormal", 0x4D41_5031, VertexFormat::Snorm8x4);
pub const ATTRIBUTE_MAP_LIGHT_UV: MeshVertexAttribute =
    MeshVertexAttribute::new("MapLightUv", 0x4D41_5032, VertexFormat::Unorm16x2);
pub const ATTRIBUTE_MAP_COLOR: MeshVertexAttribute =
    MeshVertexAttribute::new("MapColor", 0x4D41_5033, VertexFormat::Unorm8x4);

pub struct MapMaterialPlugin;

impl Plugin for MapMaterialPlugin {
    fn build(&self, app: &mut App) {
        embedded_asset!(app, "map_material.wgsl");
        app.add_plugins((
            MaterialPlugin::<MapMaterial>::default(),
            MaterialPlugin::<MapLayeredMaterial>::default(),
        ));
    }
}

/// Mirrors `MapParams` in the shader.
#[derive(Clone, Copy, Default, ShaderType)]
pub struct MapParams {
    pub tint: Vec4,
    pub sun_dir: Vec4,
    pub sun_rgb: Vec4,
    pub misc: Vec4,
    pub curve: Vec4,
    pub curve2: Vec4,
}

/// What changes the pipeline, as opposed to what changes a uniform.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct MapKey {
    pub two_sided: bool,
    pub mask: bool,
}

#[derive(Asset, TypePath, AsBindGroup, Clone)]
#[bind_group_data(MapKey)]
pub struct MapMaterial {
    #[uniform(0)]
    pub params: MapParams,
    #[texture(1)]
    #[sampler(2)]
    pub color: Option<Handle<Image>>,
    #[texture(3)]
    #[sampler(4)]
    pub lightmap: Option<Handle<Image>>,
    pub alpha: AlphaMode,
    pub bias: f32,
    pub two_sided: bool,
}

impl From<&MapMaterial> for MapKey {
    fn from(m: &MapMaterial) -> Self {
        MapKey {
            two_sided: m.two_sided,
            mask: matches!(m.alpha, AlphaMode::Mask(_)),
        }
    }
}

#[derive(Asset, TypePath, AsBindGroup, Clone)]
#[bind_group_data(MapKey)]
pub struct MapLayeredMaterial {
    #[uniform(0)]
    pub params: MapParams,
    #[texture(1)]
    #[sampler(2)]
    pub color: Option<Handle<Image>>,
    #[texture(3)]
    #[sampler(4)]
    pub lightmap: Option<Handle<Image>>,
    #[texture(5)]
    #[sampler(6)]
    pub color2: Option<Handle<Image>>,
    #[texture(7)]
    #[sampler(8)]
    pub blend: Option<Handle<Image>>,
    pub alpha: AlphaMode,
    pub bias: f32,
    pub two_sided: bool,
}

impl From<&MapLayeredMaterial> for MapKey {
    fn from(m: &MapLayeredMaterial) -> Self {
        MapKey {
            two_sided: m.two_sided,
            mask: matches!(m.alpha, AlphaMode::Mask(_)),
        }
    }
}

fn specialize_map(
    descriptor: &mut RenderPipelineDescriptor,
    layout: &MeshVertexBufferLayoutRef,
    key: MapKey,
    layered: bool,
) -> Result<(), SpecializedMeshPipelineError> {
    let vertex = layout.0.get_layout(&[
        Mesh::ATTRIBUTE_POSITION.at_shader_location(0),
        ATTRIBUTE_MAP_NORMAL.at_shader_location(1),
        Mesh::ATTRIBUTE_UV_0.at_shader_location(2),
        ATTRIBUTE_MAP_LIGHT_UV.at_shader_location(3),
        ATTRIBUTE_MAP_COLOR.at_shader_location(4),
    ])?;
    descriptor.vertex.buffers = vec![vertex];
    descriptor.primitive.cull_mode = if key.two_sided { None } else { Some(Face::Back) };
    if let Some(fragment) = descriptor.fragment.as_mut() {
        if layered {
            fragment.shader_defs.push("MAP_LAYERS".into());
        }
        if key.mask {
            fragment.shader_defs.push("MAP_MASK".into());
        }
    }
    Ok(())
}

impl Material for MapMaterial {
    fn vertex_shader() -> ShaderRef {
        SHADER.into()
    }
    fn fragment_shader() -> ShaderRef {
        SHADER.into()
    }
    fn alpha_mode(&self) -> AlphaMode {
        self.alpha
    }
    fn depth_bias(&self) -> f32 {
        self.bias
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
        key: MaterialPipelineKey<Self>,
    ) -> Result<(), SpecializedMeshPipelineError> {
        specialize_map(descriptor, layout, key.bind_group_data, false)
    }
}

impl Material for MapLayeredMaterial {
    fn vertex_shader() -> ShaderRef {
        SHADER.into()
    }
    fn fragment_shader() -> ShaderRef {
        SHADER.into()
    }
    fn alpha_mode(&self) -> AlphaMode {
        self.alpha
    }
    fn depth_bias(&self) -> f32 {
        self.bias
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
        key: MaterialPipelineKey<Self>,
    ) -> Result<(), SpecializedMeshPipelineError> {
        specialize_map(descriptor, layout, key.bind_group_data, true)
    }
}
