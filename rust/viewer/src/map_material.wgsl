// The textured CS2 map: colour texture times baked light, plus the sun where the
// bake says it reaches. No normal maps and no specular: they would double the
// texture download, and the look comes mostly from the baked light.
//
// The sum is Source 2's own: albedo * (irradiance + sun * NdotL * seen), with no 1/pi
// anywhere, because its lights and lightmaps already carry it. Dividing the sun by pi
// looks plausible and leaves the whole map a third too dim against its shade.

#import bevy_pbr::mesh_functions
#import bevy_pbr::{mesh_view_bindings as view_bindings, mesh_view_types, shadows}

struct MapParams {
    // rgb tint, a opacity
    tint: vec4<f32>,
    // xyz towards the sun, w the range the light is stored in
    sun_dir: vec4<f32>,
    // rgb sun light on a surface facing it, w what light is multiplied by before the curve
    sun_rgb: vec4<f32>,
    // x unlit, y lightmap is there, z alpha cutoff
    misc: vec4<f32>,
    // Valve's filmic curve: shoulder, linear strength, linear angle, toe strength
    curve: vec4<f32>,
    // toe numerator, toe denominator, white point (linear), 1 / the curve at the white point
    curve2: vec4<f32>,
};

@group(#{MATERIAL_BIND_GROUP}) @binding(0) var<uniform> params: MapParams;
@group(#{MATERIAL_BIND_GROUP}) @binding(1) var color_tex: texture_2d<f32>;
@group(#{MATERIAL_BIND_GROUP}) @binding(2) var color_smp: sampler;
@group(#{MATERIAL_BIND_GROUP}) @binding(3) var light_tex: texture_2d<f32>;
@group(#{MATERIAL_BIND_GROUP}) @binding(4) var light_smp: sampler;
#ifdef MAP_LAYERS
@group(#{MATERIAL_BIND_GROUP}) @binding(5) var color2_tex: texture_2d<f32>;
@group(#{MATERIAL_BIND_GROUP}) @binding(6) var color2_smp: sampler;
@group(#{MATERIAL_BIND_GROUP}) @binding(7) var blend_tex: texture_2d<f32>;
@group(#{MATERIAL_BIND_GROUP}) @binding(8) var blend_smp: sampler;
#endif

struct Vertex {
    @builtin(instance_index) instance_index: u32,
    @location(0) position: vec3<f32>,
    @location(1) normal: vec4<f32>,
    @location(2) uv: vec2<f32>,
    @location(3) lm: vec2<f32>,
    @location(4) color: vec4<f32>,
};

struct VertexOut {
    @builtin(position) clip: vec4<f32>,
    @location(0) uv: vec2<f32>,
    @location(1) lm: vec2<f32>,
    @location(2) normal: vec3<f32>,
    @location(3) color: vec4<f32>,
    @location(4) world: vec3<f32>,
};

@vertex
fn vertex(v: Vertex) -> VertexOut {
    var out: VertexOut;
    let world_from_local = mesh_functions::get_world_from_local(v.instance_index);
    let world = mesh_functions::mesh_position_local_to_world(world_from_local, vec4<f32>(v.position, 1.0));
    out.clip = view_bindings::view.clip_from_world * world;
    out.world = world.xyz;
    out.uv = v.uv;
    out.lm = v.lm;
    // the map is only ever scaled uniformly, so the normal needs no inverse transpose
    out.normal = v.normal.xyz;
    out.color = v.color;
    return out;
}

// The map's tone curve, from its post-processing file. On de_mirage these numbers make it a
// straight line up to the white point, so a surface in full sun outshines its texture; on
// de_ancient it bends.
fn tone(c: vec3<f32>) -> vec3<f32> {
    let a = params.curve;
    let b = params.curve2;
    let x = min(c * params.sun_rgb.w, vec3<f32>(b.z));
    let top = x * (a.x * x + a.z * a.y) + a.w * b.x;
    let bottom = x * (a.x * x + a.y) + a.w * b.y;
    return (top / bottom - b.x / b.y) * b.w;
}

// Only players cast into the shadow map: the map's own shadows are in the bake. So this
// takes away the sun where a player blocks it, and nothing where the bake already did.
fn player_shadow(world: vec3<f32>, n: vec3<f32>, frag: vec2<f32>) -> f32 {
    if (view_bindings::lights.n_directional_lights == 0u) {
        return 1.0;
    }
    let flags = view_bindings::lights.directional_lights[0].flags;
    if ((flags & mesh_view_types::DIRECTIONAL_LIGHT_FLAGS_SHADOWS_ENABLED_BIT) == 0u) {
        return 1.0;
    }
    let p = vec4<f32>(world, 1.0);
    let v = view_bindings::view.view_from_world;
    let view_z = dot(vec4<f32>(v[0].z, v[1].z, v[2].z, v[3].z), p);
    return shadows::fetch_directional_shadow(0u, p, n, view_z, frag);
}

@fragment
fn fragment(in: VertexOut, @builtin(front_facing) is_front: bool) -> @location(0) vec4<f32> {
    var base = textureSample(color_tex, color_smp, in.uv);

#ifdef MAP_LAYERS
    // vertex paint decides between two layers, and a mask texture makes the border
    // follow the bricks instead of the triangles
    let mask = textureSample(blend_tex, blend_smp, in.uv);
    let edge0 = max(mask.r - mask.g, 0.0);
    let edge1 = min(mask.r + mask.g, 1.0);
    let w = smoothstep(edge0, max(edge1, edge0 + 0.001), in.color.r);
    base = mix(base, textureSample(color2_tex, color2_smp, in.uv), w);
#endif

#ifdef MAP_MASK
    if (base.a < params.misc.z) {
        discard;
    }
#endif

    let albedo = base.rgb * params.tint.rgb;
    let alpha = base.a * params.tint.a;
    if (params.misc.x > 0.5) {
        return vec4<f32>(tone(albedo), alpha);
    }

    var light: vec3<f32>;
    if (in.color.a > 0.5) {
        var irradiance = vec3<f32>(0.35);
        var sun_seen = 0.5;
        if (params.misc.y > 0.5) {
            let baked = textureSample(light_tex, light_smp, in.lm);
            irradiance = baked.rgb * baked.rgb * params.sun_dir.w;
            sun_seen = baked.a;
        }
        var n = normalize(in.normal);
        if (!is_front) {
            n = -n;
        }
        let facing = max(dot(n, params.sun_dir.xyz), 0.0);
        var sun = facing * sun_seen;
        if (sun > 0.001) {
            sun = sun * player_shadow(in.world, n, in.clip.xy);
        }
        light = irradiance + params.sun_rgb.rgb * sun;
    } else {
        // no lightmap on this vertex: the bake put its whole light into the colour
        light = in.color.rgb * in.color.rgb * params.sun_dir.w;
    }

    return vec4<f32>(tone(albedo * light), alpha);
}
