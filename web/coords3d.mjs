// Source-engine world <-> Three.js scene coordinate transform.
//
// THIS IS THE ONE PLACE the mapping lives. Everything in the 3D replay (players, geometry,
// camera, and later utility) must go through these functions so the whole scene shares a
// single, verified convention. Do not scatter axis/scale math elsewhere.
//
// Conventions
// -----------
//   Source engine : X = east, Y = north, Z = up.   Right-handed, Z-up.  Units ~= inches.
//   Three.js      : X = right, Y = up,   Z = toward the viewer.  Right-handed, Y-up.
//
// Mapping (verified against the game's own player_death coordinates and the 2D radar):
//   scene.x =  world.x * S
//   scene.y =  world.z * S      // Source up (Z) -> Three.js up (Y)
//   scene.z = -world.y * S      // Source north (Y) -> Three.js -Z
//
// This is a pure axis relabel with a uniform scale: it preserves right-handedness (no
// mirror), so callouts/left-right are never flipped, and the ground projection (scene x,z)
// is proportional to the radar projection. Verified: attacker position round-trips to the
// player_death event XY to 0.0 units, and all 10 players land on the correct radar callouts.
//
// SCENE_SCALE just keeps scene numbers small/manageable; de_mirage spans ~5120 world units
// -> ~102 scene units at S = 0.02. Nothing about correctness depends on its value.

export const SCENE_SCALE = 0.02;

// world position (Source units) -> Three.js scene position (array [x,y,z]).
export function worldToScene(x, y, z, s = SCENE_SCALE) {
  return [x * s, z * s, -y * s];
}

// world direction (dx,dy,dz) -> scene direction, ignoring scale (unit-ish preserved up to S).
export function worldDirToScene(dx, dy, dz) {
  return [dx, dz, -dy];
}

// A player's Source yaw (degrees; 0 = +X east, increasing CCW toward +Y north) -> the
// rotation (radians) to apply about the scene up-axis (Y) for a mesh whose local forward is
// +X. Derivation: facing = (cos y, sin y, 0) -> scene (cos y, 0, -sin y); a Y-rotation by
// theta sends local +X to (cos t, 0, -sin t), so theta = yaw. (See module header.)
export function yawToSceneRotationY(yawDeg) {
  return yawDeg * Math.PI / 180;
}

// Convenience for placing the radar underlay: given the map calibration
// {pos_x, pos_y, scale, size}, return the scene-space rectangle (center + half extents on
// the ground plane) that the 1024px radar image should cover, matching worldToScene.
// Radar covers world X in [pos_x, pos_x + size*scale], world Y in [pos_y - size*scale, pos_y].
export function radarGroundRect(info, s = SCENE_SCALE) {
  const wx0 = info.pos_x, wx1 = info.pos_x + info.size * info.scale;
  const wy1 = info.pos_y, wy0 = info.pos_y - info.size * info.scale;
  const [cx, , cz] = worldToScene((wx0 + wx1) / 2, (wy0 + wy1) / 2, 0, s);
  return {
    cx, cz,
    halfX: (wx1 - wx0) / 2 * s,     // extent along scene +X (world east)
    halfZ: (wy1 - wy0) / 2 * s,     // extent along scene +Z (world -north)
  };
}
