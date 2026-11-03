// Effect sprites: smoke, fire, explosions, blood, muzzle flashes. Every sprite is a
// quad that turns to face the camera here, so the CPU only writes its centre.
//
// The atlas is premultiplied by its alpha, and so is the vertex colour: its rgb is the
// light the sprite gives and its alpha how much it hides what is behind. Fire and
// flashes come with an alpha of zero and only add light; smoke and blood cover. That
// is one blend, `one, one-minus-source-alpha`, for every kind.

#import bevy_pbr::mesh_functions
#import bevy_pbr::mesh_view_bindings::view

@group(#{MATERIAL_BIND_GROUP}) @binding(0) var atlas: texture_2d<f32>;
@group(#{MATERIAL_BIND_GROUP}) @binding(1) var atlas_smp: sampler;

struct Vertex {
    @builtin(instance_index) instance_index: u32,
    // the sprite's centre, the same on all four corners
    @location(0) position: vec3<f32>,
    // corner x and y (-1 or 1), half the side (negative: no fading near the camera), turn
    @location(1) corner: vec4<f32>,
    @location(2) uv: vec2<f32>,
    @location(3) color: vec4<f32>,
};

struct VertexOut {
    @builtin(position) clip: vec4<f32>,
    @location(0) uv: vec2<f32>,
    @location(1) color: vec4<f32>,
};

@vertex
fn vertex(v: Vertex) -> VertexOut {
    var out: VertexOut;
    let world_from_local = mesh_functions::get_world_from_local(v.instance_index);
    let centre = (world_from_local * vec4<f32>(v.position, 1.0)).xyz;
    let right = view.world_from_view[0].xyz;
    let up = view.world_from_view[1].xyz;
    let size = abs(v.corner.z);
    let c = cos(v.corner.w);
    let s = sin(v.corner.w);
    let x = v.corner.x * c - v.corner.y * s;
    let y = v.corner.x * s + v.corner.y * c;
    let world = centre + (right * x + up * y) * size;
    out.clip = view.clip_from_world * vec4<f32>(world, 1.0);
    // A sprite the camera is inside of would cover the whole screen with its middle:
    // it fades out as the camera comes within its size.
    var fade = 1.0;
    if (v.corner.z > 0.0) {
        let d = distance(centre, view.world_from_view[3].xyz);
        fade = clamp((d - 0.3 * size) / (0.7 * size), 0.0, 1.0);
    }
    out.color = v.color * fade;
    out.uv = v.uv;
    return out;
}

@fragment
fn fragment(in: VertexOut) -> @location(0) vec4<f32> {
    return textureSample(atlas, atlas_smp, in.uv) * in.color;
}
