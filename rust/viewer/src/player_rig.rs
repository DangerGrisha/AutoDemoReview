//! Applies `player_anim` and `player_actions` to the Bevy animation players: one small graph
//! per player, and the spine turned by hand after the clips have run.
//!
//! The graph, which every clip is written into as it is needed, because Bevy walks every
//! node for every animated bone every frame:
//!
//! ```text
//! Add ── Blend ── walking and full-body clips (crossfaded by AnimationTransitions)
//!     │        └─ upper-body clips (hold no leg tracks, so the legs keep walking)
//!     ├─ a shot
//!     └─ a flinch or a landing
//! ```
//!
//! Timed clips have speed 0 and get their time written every frame from the replay clock, so
//! pause, slow motion and a scrub all land on the right pose by themselves.

use std::time::Duration;

use bevy::animation::graph::{AnimationGraphHandle, AnimationNodeType};
use bevy::animation::transition::AnimationTransitions;
use bevy::animation::AnimationPlayer;
use bevy::camera::visibility::RenderLayers;
use bevy::prelude::*;

use crate::player_actions::{self, RoundAnim, BODY_MAX_TWIST};
use crate::scene::{self, ActionClips};
use crate::state::{
    ang_lerp, lerp, sample_pos, PlayerAnimSlot, PlayerBody, PlayerClipNodes, PlayerLoco, Viewer,
    CLIP_NODES,
};
use crate::{player_anim, weapons_viz, XRAY_LAYER};

const UPPER_NODES: usize = 2;
/// Crossfade between walking clips, and into and out of a whole-body clip, in real seconds.
const WALK_FADE: Duration = Duration::from_millis(180);
const ACTION_FADE: Duration = Duration::from_millis(100);
/// How long the arms take to go from the legs' pose to a clip's, in real seconds.
const UPPER_FADE_S: f32 = 0.12;
/// Blend weights are shares of the total, so "all of it" over a walking clip of weight 1 is
/// just a large number, and a share `f` is `f / (1 - f)` capped to this.
const UPPER_FULL_WEIGHT: f32 = 1000.0;
/// Bevy wraps a seek time equal to the clip's length back to 0 even at speed 0, which would
/// stand a corpse back up, so a clip that has run out stays just short of its end.
const CLIP_END_MARGIN: f32 = 0.001;

/// Spine bones from the pelvis up, and how much of the aim each takes. They add up to 1, so
/// the chest, the arms and the gun end up pointing where the player looks.
const SPINE_BONES: [&str; 4] = ["spine_0", "spine_1", "spine_2", "spine_3"];
const SPINE_SHARES: [f32; 4] = [0.1, 0.25, 0.3, 0.35];
/// Past these the spine stops bending and the head does the rest, up to its own limit.
const SPINE_MAX_PITCH: f64 = 55.0;
const HEAD_MAX_TURN: f64 = 35.0;

#[derive(Component)]
pub struct PlayerUpperNodes {
    nodes: [AnimationNodeIndex; UPPER_NODES],
    clips: [Option<AssetId<AnimationClip>>; UPPER_NODES],
    /// How much of the arms each node has, 0 to 1.
    shares: [f32; UPPER_NODES],
    current: usize,
}

#[derive(Component)]
pub struct PlayerAddNodes {
    shot: AnimationNodeIndex,
    shot_clip: Option<AssetId<AnimationClip>>,
    reaction: AnimationNodeIndex,
    reaction_clip: Option<AssetId<AnimationClip>>,
}

/// Bones `apply_player_aim` turns by hand.
#[derive(Component)]
pub struct PlayerBones {
    root: Entity,
    pelvis: Entity,
    spine: [Entity; SPINE_BONES.len()],
    neck: Entity,
    head: Entity,
}

/// On a player's mesh, to take a corpse off the x-ray layer.
#[derive(Component)]
pub struct PlayerMeshOf {
    pub slot: usize,
    pub on_xray: bool,
}

pub(crate) fn held_time(time: f32, length: f32) -> f32 {
    time.min((length - CLIP_END_MARGIN).max(0.0))
}

fn write_clip(
    graphs: &mut Assets<AnimationGraph>,
    graph: &AnimationGraphHandle,
    node: AnimationNodeIndex,
    clip: Handle<AnimationClip>,
) {
    if let Some(mut graph) = graphs.get_mut(&graph.0) {
        if let Some(graph_node) = graph.get_mut(node) {
            graph_node.node_type = AnimationNodeType::Clip(clip);
        }
    }
}

/// When a spawned character's `AnimationPlayer` appears, give it its graph, start it on Idle,
/// tag it with the owning player slot, and hand the weapon mount over to the model's weapon
/// bone if it has one.
#[allow(clippy::too_many_arguments)]
pub fn setup_player_anim(
    mut commands: Commands,
    viewer: Res<Viewer>,
    model: Option<Res<scene::PlayerModel>>,
    mut graphs: ResMut<Assets<AnimationGraph>>,
    mut q_new: Query<(Entity, &mut AnimationPlayer), Added<AnimationPlayer>>,
    q_childof: Query<&ChildOf>,
    q_body: Query<&PlayerBody>,
    q_children: Query<&Children>,
    q_names: Query<&Name>,
    mut q_mounts: Query<(Entity, &mut weapons_viz::WeaponMount)>,
) {
    let (Some(model), Some(r)) = (model, viewer.round.as_ref()) else {
        return;
    };
    for (e, mut player) in &mut q_new {
        let Some(slot) = crate::find_body_slot(e, &q_childof, &q_body) else {
            continue;
        };
        let team = r.dec.players.get(slot).map(|p| p.team).unwrap_or(0);
        let rig = model.for_team(team);
        let idle = rig.clips.idle();

        let mut graph = AnimationGraph::new();
        // Children run in node order and the first of an Add is what the rest go on top of,
        // so the Blend has to be made before the added clips.
        let add = graph.add_additive_blend(1.0, graph.root);
        let blend = graph.add_blend(1.0, add);
        let nodes = std::array::from_fn(|_| graph.add_clip(idle.clone(), 1.0, blend));
        let upper = std::array::from_fn(|_| graph.add_clip(idle.clone(), 1.0, blend));
        let shot = graph.add_clip(idle.clone(), 1.0, add);
        let reaction = graph.add_clip(idle.clone(), 1.0, add);

        let mut transitions = AnimationTransitions::new();
        transitions.play(&mut player, nodes[0], Duration::ZERO).repeat();
        commands.entity(e).insert((
            AnimationGraphHandle(graphs.add(graph)),
            transitions,
            PlayerAnimSlot(slot),
            PlayerLoco::default(),
            PlayerClipNodes { nodes, current: 0, clip: idle.id() },
            PlayerUpperNodes { nodes: upper, clips: [None; UPPER_NODES], shares: [0.0; UPPER_NODES], current: 0 },
            PlayerAddNodes { shot, shot_clip: None, reaction, reaction_clip: None },
        ));

        if rig.spec.weapon_bone {
            let bone_named = |name: &str| {
                q_children.iter_descendants(e).find(|b| q_names.get(*b).is_ok_and(|n| n.as_str() == name))
            };
            let weapon = bone_named(weapons_viz::WEAPON_BONE);
            let mount = q_mounts.iter_mut().find(|(_, m)| m.slot == slot);
            if let (Some(bone), Some((mount_e, mut mount))) = (weapon, mount) {
                commands.entity(mount_e).insert(ChildOf(bone));
                mount.on_bone = true;
                // makes update_weapon_mounts respawn the kit at its bone position
                mount.current = None;
            }
            let spine: Vec<Entity> = SPINE_BONES.iter().filter_map(|n| bone_named(n)).collect();
            if let (Some(root), Some(pelvis), Ok(spine), Some(neck), Some(head)) = (
                bone_named("root_motion"),
                bone_named("pelvis"),
                <[Entity; SPINE_BONES.len()]>::try_from(spine),
                bone_named("neck_0"),
                bone_named("head_0"),
            ) {
                commands.entity(e).insert(PlayerBones { root, pelvis, spine, neck, head });
            }
        }
    }
}

/// Build the current round's timelines once both it and the clips are there.
pub fn prepare_round_anim(
    model: Option<Res<scene::PlayerModel>>,
    actions: Option<Res<ActionClips>>,
    mut viewer: ResMut<Viewer>,
) {
    // A model settles which skeleton it is, so nothing is built for one the clips do not fit.
    let (Some(_), Some(actions)) = (model, actions) else {
        return;
    };
    if viewer.round.as_ref().is_none_or(|r| r.anim.is_some()) {
        return;
    }
    let viewer = &mut *viewer;
    let (Some(m), Some(r)) = (viewer.manifest.as_ref(), viewer.round.as_mut()) else {
        return;
    };
    let kills = m.kills.get(&r.dec.round).map_or(&[][..], Vec::as_slice);
    let length = |name: &str| actions.get(name).map(|(_, length)| length);
    r.anim = Some(player_actions::build_round_anim(&r.dec, kills, &length));
}

/// Pick each player's clips and run them on the replay's clock.
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
pub fn drive_player_anim(
    time: Res<Time>,
    viewer: Res<Viewer>,
    model: Option<Res<scene::PlayerModel>>,
    actions: Option<Res<ActionClips>>,
    mut graphs: ResMut<Assets<AnimationGraph>>,
    mut q: Query<(
        &PlayerAnimSlot,
        &mut PlayerLoco,
        &mut PlayerClipNodes,
        &mut PlayerUpperNodes,
        &mut PlayerAddNodes,
        &AnimationGraphHandle,
        &mut AnimationPlayer,
        &mut AnimationTransitions,
    )>,
) {
    let (Some(model), Some(r)) = (model, viewer.round.as_ref()) else {
        return;
    };
    if r.n == 0 {
        return;
    }
    let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
    let now = viewer.clock as f32;
    let playback = if viewer.playing { viewer.speed as f32 } else { 0.0 };
    // Real-time fades would lag behind a fast replay.
    let dt = time.delta_secs() * playback.max(1.0);
    let anim: Option<&RoundAnim> = r.anim.as_ref();
    let actions = actions.as_deref();

    for (slot, mut loco, mut clips, mut upper, mut adds, graph, mut player, mut transitions) in &mut q {
        let Some(tr) = r.dec.tracks.get(slot.0) else {
            continue;
        };
        let timeline = anim.and_then(|a| a.players.get(slot.0));
        let alive = tr.flags[i0] & c2r3::F_ALIVE != 0;
        let (speed, dx, dy) = if alive { player_anim::ground_motion(tr, i0, r.n, r.rate) } else { (0.0, 0.0, 0.0) };
        let body_yaw = timeline.and_then(|tl| tl.body_yaw(i0, i0, 0.0)).unwrap_or(tr.yaw[i0]);
        loco.0.update(speed, player_anim::heading_cw(dx, dy, body_yaw));

        let crouch = tr.flags[i0] & c2r3::F_CROUCH != 0;
        let tag = weapons_viz::current_tag(r, slot.0, i0);
        let class = tag.map_or(player_anim::WeaponClass::Rifle, player_anim::class_for_tag);
        let team = r.dec.players.get(slot.0).map(|p| p.team).unwrap_or(0);

        // --- the whole body
        let action = timeline.and_then(|tl| tl.base(now, crouch)).and_then(|at| {
            let (clip, length) = actions?.get(at.clip)?;
            Some((clip, held_time(at.time, length)))
        });
        let walking = model.for_team(team).clips.pose(class, crouch, loco.0.gait, loco.0.sector, speed);
        let target = action.as_ref().map_or(walking, |(clip, _)| clip);
        if clips.clip != target.id() {
            clips.current = (clips.current + 1) % CLIP_NODES;
            let node = clips.nodes[clips.current];
            write_clip(&mut graphs, graph, node, target.clone());
            clips.clip = target.id();
            let fade = if action.is_some() { ACTION_FADE } else { WALK_FADE };
            transitions.play(&mut player, node, fade).repeat();
        }
        if let Some(active) = player.animation_mut(clips.nodes[clips.current]) {
            match action {
                Some((_, time)) => {
                    active.set_speed(0.0).set_seek_time(time);
                }
                None => {
                    active.set_speed(playback * player_anim::clip_rate(crouch, loco.0.gait, speed));
                }
            }
        }

        let Some(actions) = actions else {
            continue;
        };
        let Some(timeline) = timeline else {
            continue;
        };

        // --- the arms
        let want = timeline.upper(now, crouch, tag).and_then(|at| {
            let (clip, length) = actions.get(at.clip)?;
            Some((clip, held_time(at.time, length)))
        });
        if let Some((clip, _)) = &want {
            if upper.clips[upper.current] != Some(clip.id()) {
                let next = (upper.current + 1) % UPPER_NODES;
                upper.current = next;
                write_clip(&mut graphs, graph, upper.nodes[next], (*clip).clone());
                upper.clips[next] = Some(clip.id());
                player.start(upper.nodes[next]).repeat();
            }
        }
        let current = upper.current;
        for k in 0..UPPER_NODES {
            let goal = if k == current && want.is_some() { 1.0 } else { 0.0 };
            let step = dt / UPPER_FADE_S;
            upper.shares[k] = if goal > upper.shares[k] {
                (upper.shares[k] + step).min(goal)
            } else {
                (upper.shares[k] - step).max(goal)
            };
        }
        let rest = (1.0 - upper.shares.iter().sum::<f32>()).max(0.0);
        for k in 0..UPPER_NODES {
            let node = upper.nodes[k];
            if upper.shares[k] <= 0.0 {
                if player.animation(node).is_some() {
                    player.stop(node);
                    upper.clips[k] = None;
                }
                continue;
            }
            let weight = (upper.shares[k] / rest.max(1.0 / UPPER_FULL_WEIGHT)).min(UPPER_FULL_WEIGHT);
            if let Some(active) = player.animation_mut(node) {
                active.set_weight(weight).set_speed(0.0);
                if k == current {
                    if let Some((_, time)) = want {
                        active.set_seek_time(time);
                    }
                }
            }
        }

        // --- on top of both
        let adds = &mut *adds;
        drive_added(timeline.shot(now), actions, &mut graphs, graph, &mut player, adds.shot, &mut adds.shot_clip);
        drive_added(
            timeline.reaction(now),
            actions,
            &mut graphs,
            graph,
            &mut player,
            adds.reaction,
            &mut adds.reaction_clip,
        );
    }
}

fn drive_added(
    want: Option<player_actions::AddAt>,
    actions: &ActionClips,
    graphs: &mut Assets<AnimationGraph>,
    graph: &AnimationGraphHandle,
    player: &mut AnimationPlayer,
    node: AnimationNodeIndex,
    playing: &mut Option<AssetId<AnimationClip>>,
) {
    let Some((want, clip)) = want.and_then(|w| Some((w, actions.get(w.clip)?.0))) else {
        if playing.take().is_some() {
            player.stop(node);
        }
        return;
    };
    if *playing != Some(clip.id()) {
        write_clip(graphs, graph, node, clip.clone());
        *playing = Some(clip.id());
    }
    player.play(node).repeat().set_speed(0.0).set_seek_time(want.time).set_weight(want.weight);
}

fn wrap_180(deg: f64) -> f64 {
    let t = deg.rem_euclid(360.0);
    if t > 180.0 {
        t - 360.0
    } else {
        t
    }
}

/// Aim with the spine, after the clips have written their pose and before it reaches the
/// mesh. No clip can do this: it has to follow the demo's pitch, and the twist between the
/// legs and the eyes (see `player_actions::body_yaw_track`).
///
/// The model faces +Z with +Y up, so +X is its left: turning about +X leans it forward, which
/// is what a positive Source pitch means, and turning about +Y is counter-clockwise from above
/// like Source's yaw. The whole turn is split by powers of one rotation, so the pieces share
/// an axis and add up exactly. The gun hangs off the chest and follows.
pub fn apply_player_aim(
    viewer: Res<Viewer>,
    q: Query<(&PlayerAnimSlot, &PlayerBones)>,
    mut q_tf: Query<&mut Transform>,
) {
    let Some(r) = viewer.round.as_ref() else {
        return;
    };
    let Some(anim) = r.anim.as_ref() else {
        return;
    };
    if r.n == 0 {
        return;
    }
    let (i0, i1, f, _s) = sample_pos(viewer.clock, r.rate, r.n);
    let now = viewer.clock as f32;
    for (slot, bones) in &q {
        let (Some(tr), Some(timeline)) = (r.dec.tracks.get(slot.0), anim.players.get(slot.0)) else {
            continue;
        };
        let weight = timeline.aim_weight(now);
        if weight <= 0.0 {
            continue;
        }
        let g = if tr.flags[i1] & c2r3::F_ALIVE != 0 { f } else { 0.0 };
        let yaw = ang_lerp(tr.yaw[i0], tr.yaw[i1], g);
        let body = timeline.body_yaw(i0, i1, g).unwrap_or(yaw);
        let twist = wrap_180(yaw - body);
        let pitch = lerp(tr.pitch[i0], tr.pitch[i1], g);

        let spine_pitch = pitch.clamp(-SPINE_MAX_PITCH, SPINE_MAX_PITCH);
        let spine_twist = twist.clamp(-BODY_MAX_TWIST, BODY_MAX_TWIST);
        let aim = aim_rotation(spine_twist, spine_pitch, weight);

        let Ok(root) = q_tf.get(bones.root).map(|t| t.rotation) else { continue };
        let Ok(pelvis) = q_tf.get(bones.pelvis).map(|t| t.rotation) else { continue };
        let mut above = root * pelvis;
        for (bone, share) in bones.spine.iter().zip(SPINE_SHARES) {
            let Ok(mut local) = q_tf.get_mut(*bone) else { continue };
            local.rotation = turned_local(above, local.rotation, Quat::IDENTITY.slerp(aim, share));
            above *= local.rotation;
        }

        let head_pitch = (pitch - spine_pitch).clamp(-HEAD_MAX_TURN, HEAD_MAX_TURN);
        let head_twist = (twist - spine_twist).clamp(-HEAD_MAX_TURN, HEAD_MAX_TURN);
        if head_pitch != 0.0 || head_twist != 0.0 {
            let Ok(neck) = q_tf.get(bones.neck).map(|t| t.rotation) else { continue };
            above *= neck;
            if let Ok(mut local) = q_tf.get_mut(bones.head) {
                local.rotation = turned_local(above, local.rotation, aim_rotation(head_twist, head_pitch, weight));
            }
        }
    }
}

/// The local rotation that turns a bone by `turn`, given in the model's frame, where `above` is
/// the rotation of its parent in that frame.
fn turned_local(above: Quat, local: Quat, turn: Quat) -> Quat {
    above.inverse() * turn * above * local
}

/// Turn the eyes by `twist` degrees to the left, then tilt them down by `pitch` degrees.
fn aim_rotation(twist: f64, pitch: f64, weight: f32) -> Quat {
    let turn = Quat::from_rotation_y(twist.to_radians() as f32) * Quat::from_rotation_x(pitch.to_radians() as f32);
    Quat::IDENTITY.slerp(turn, weight)
}

/// A corpse belongs behind walls: only the living are drawn through them.
pub fn update_corpse_layers(
    viewer: Res<Viewer>,
    mut q: Query<(&mut PlayerMeshOf, &mut RenderLayers)>,
) {
    let Some(r) = viewer.round.as_ref() else {
        return;
    };
    if r.n == 0 {
        return;
    }
    let (i0, _i1, _f, _s) = sample_pos(viewer.clock, r.rate, r.n);
    for (mut mesh, mut layers) in &mut q {
        let alive = r.dec.tracks.get(mesh.slot).is_some_and(|tr| tr.flags[i0] & c2r3::F_ALIVE != 0);
        if alive != mesh.on_xray {
            mesh.on_xray = alive;
            *layers = if alive { RenderLayers::from_layers(&[0, XRAY_LAYER]) } else { RenderLayers::layer(0) };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_clip_that_has_run_out_stays_before_the_point_bevy_wraps_it() {
        assert_eq!(held_time(0.5, 2.0), 0.5);
        assert!(held_time(2.0, 2.0) < 2.0);
        assert!(held_time(50.0, 2.0) < 2.0);
        assert_eq!(held_time(1.0, 0.0), 0.0);
    }

    #[test]
    fn the_spine_takes_the_whole_aim() {
        assert!((SPINE_SHARES.iter().sum::<f32>() - 1.0).abs() < 1e-6);
    }

    #[test]
    fn looking_down_tilts_the_model_forward_and_left_turns_it_left() {
        // the model faces +Z with +Y up, so its left is +X
        let forward = Vec3::Z;
        let down = aim_rotation(0.0, 30.0, 1.0) * forward;
        assert!(down.y < -0.4 && down.z > 0.8, "{down}");
        let left = aim_rotation(30.0, 0.0, 1.0) * forward;
        assert!(left.x > 0.4 && left.z > 0.8, "{left}");
    }

    #[test]
    fn the_chest_ends_up_turned_by_the_whole_aim_whatever_the_pose() {
        let pose = [
            Quat::from_euler(EulerRot::XYZ, 0.2, -0.4, 0.1),
            Quat::from_euler(EulerRot::XYZ, -0.3, 0.1, 0.5),
            Quat::from_euler(EulerRot::XYZ, 0.1, 0.6, -0.2),
            Quat::from_euler(EulerRot::XYZ, 0.4, -0.1, 0.3),
        ];
        let parent = Quat::from_euler(EulerRot::XYZ, 0.3, 1.0, -0.6);
        let aim = aim_rotation(50.0, -25.0, 1.0);

        let mut before = parent;
        let mut after = parent;
        for (local, share) in pose.into_iter().zip(SPINE_SHARES) {
            before *= local;
            after *= turned_local(after, local, Quat::IDENTITY.slerp(aim, share));
        }
        assert!(after.angle_between(aim * before) < 1e-4);
    }

    #[test]
    fn no_weight_means_no_aim() {
        assert!(aim_rotation(40.0, 20.0, 0.0).angle_between(Quat::IDENTITY) < 1e-5);
    }
}
