//! What a player is doing besides walking, worked out once per round from what the replay
//! says happened. Everything here answers "what is on at time t" from the round's data
//! alone, with no memory of earlier frames, which is why a scrub or a pause lands on the
//! right pose. `player_rig.rs` applies the answers to Bevy.
//!
//! Three layers, as `player_actions.txt` says for each clip: `base` replaces the whole body,
//! `upper` plays over whatever the legs do, and `shot` and `reaction` go on top of both.

use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use c2r3::{DecodedRound, Track};
use serde_json::Value;

use crate::player_anim::{class_for_tag, ground_motion, heading_cw, WeaponClass};
use crate::state::KillFact;

const ACTION_LIST: &str = include_str!("player_actions.txt");

/// A thrown grenade leaves the hand this far into the throw clips, and `throwSample` is the
/// moment it leaves, so the clip starts this long before it. Read off the hand's path.
const THROW_RELEASE_S: f32 = 0.30;
/// A knife connects this far into a swing. Read off the hand's path.
pub const KNIFE_HIT_S: f32 = 0.15;
/// A draw or reload the player cuts short by firing ends here after it started at the soonest.
const CUT_BY_SHOT_AFTER_S: f32 = 0.05;
/// Moving this far from where a plant started means it was given up. `bomb_abortplant` does
/// not reach the demos, so this is how a stopped plant shows.
pub const PLANT_ABORT_UNITS: f64 = 16.0;
/// Shorter runs of the airborne flag are steps and stairs.
const MIN_AIR_S: f32 = 0.1;
/// Airborne this long before landing deserves a landing.
const MIN_LANDING_AIR_S: f32 = 0.25;
/// Rising this much in the first 0.1 s of a run makes it a jump and not a walk off a ledge.
const JUMP_RISE_UNITS: f64 = 10.0;
const JUMP_RISE_WINDOW_S: f64 = 0.1;
/// Slower than this at take-off is a jump on the spot.
const JUMP_STAND_SPEED: f64 = 60.0;
/// The flash byte holds seconds * 50 (`flash_byte` in rust/extract), and how long a player is
/// truly blind is less than it says. Shorter flashes do not raise the arm.
pub const FLASH_BYTE_PER_S: f32 = 50.0;
const FLASH_MIN_BYTE: u8 = 50;
const FLASH_POSE_SHARE: f32 = 0.8;
/// How fast the aim comes out of the spine, for a death, a plant and a defuse.
const AIM_FADE_S: f32 = 0.15;
/// An added clip eases in over this, since some start away from their zero.
const ADD_EASE_IN_S: f32 = 0.03;
/// A kill in the manifest and the alive flag falling this close (samples at 64 Hz or fewer)
/// are the same death.
const KILL_MATCH_SAMPLES: usize = 8;
/// A corpse takes this long to reach the floor from where the jump left it in the air.
const CORPSE_DROP_S: f32 = 0.4;
/// The alive flag drops out for a single sample now and then, health back and all, which is
/// not a death: anyone dead for less than this and then alive again never died.
const MIN_DEAD_S: f32 = 0.1;

// The legs follow the body with a lag, in degrees and seconds.
/// Above this ground speed the legs turn with the player (Source units/s).
const BODY_MOVING_SPEED: f64 = 30.0;
const BODY_MOVING_TIME: f64 = 0.12;
/// A standing player keeps their feet until twisted this far, then turns them to within
/// `BODY_TURN_STOP` at `BODY_TURN_RATE`.
const BODY_TURN_START: f64 = 50.0;
const BODY_TURN_STOP: f64 = 8.0;
const BODY_TURN_RATE: f64 = 360.0;
/// The spine does not twist further than this however fast the player flicks.
pub const BODY_MAX_TWIST: f64 = 80.0;

fn wrap_180(deg: f64) -> f64 {
    let t = deg.rem_euclid(360.0);
    if t > 180.0 {
        t - 360.0
    } else {
        t
    }
}

/// `(name, layer)` of each line of `player_actions.txt`.
pub fn action_list() -> impl Iterator<Item = (&'static str, &'static str)> {
    ACTION_LIST.lines().filter(|l| !l.is_empty() && !l.starts_with('#')).filter_map(|l| {
        let mut words = l.split_whitespace();
        Some((words.next()?, words.next()?))
    })
}

/// `name` as the list's own string, so the timelines can hold it cheaply. None for a clip the
/// list does not have, such as the reload of a Zeus.
fn action(name: &str) -> Option<&'static str> {
    static NAMES: OnceLock<HashSet<&'static str>> = OnceLock::new();
    NAMES.get_or_init(|| action_list().map(|(n, _)| n).collect()).get(name).copied()
}

/// What a weaponTable tag goes by in `player_actions.txt`: grenades share their clips, and the
/// emoji and truncated tags spell out.
pub fn anim_tag(tag: &str) -> &str {
    match tag {
        "🔪" => "KNIFE",
        "💣" => "C4",
        "ZEUS X" => "ZEUS",
        "HE" | "FLASH" | "SMOKE" | "DECOY" => "GRENADE",
        "MOLLY" | "INC" => "MOLOTOV",
        other => other,
    }
}

fn class_suffix(class: WeaponClass) -> &'static str {
    match class {
        WeaponClass::Rifle => "rifle",
        WeaponClass::Pistol => "pistol",
        WeaponClass::Knife => "knife",
    }
}

/// A clip and where in it to be.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ClipAt {
    pub clip: &'static str,
    pub time: f32,
}

/// An added clip, how far along, and how much of it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct AddAt {
    pub clip: &'static str,
    pub time: f32,
    pub weight: f32,
}

/// A clip that plays once from a moment on.
#[derive(Clone, Copy, Debug)]
struct Once {
    t: f32,
    clip: &'static str,
    length: f32,
}

#[derive(Clone, Copy, Debug)]
pub struct Death {
    pub t: f32,
    /// The player is alive again from here on, which can happen at the very end of a round.
    until: f32,
    clip: &'static str,
    /// How far above the floor it happened, for a kill in mid-air (Source units).
    drop: f64,
}

/// Standing and crouched versions of one clip, of the same length.
#[derive(Clone, Copy, Debug)]
struct Part {
    stand: &'static str,
    crouch: &'static str,
    length: f32,
    looped: bool,
}

impl Part {
    fn clip(&self, crouch: bool) -> &'static str {
        if crouch {
            self.crouch
        } else {
            self.stand
        }
    }

    /// `time` from the part's start.
    fn at(&self, time: f32, crouch: bool) -> ClipAt {
        let time = if self.looped && self.length > 0.0 { time % self.length } else { time.min(self.length) };
        ClipAt { clip: self.clip(crouch), time }
    }
}

/// A stretch of time with one or two clips in a row (a jump and the fall after it).
#[derive(Clone, Copy, Debug)]
struct Span {
    t0: f32,
    t1: f32,
    first: Part,
    then: Option<Part>,
    /// The player fired, which ends a draw or a reload early.
    cut_by_shot: bool,
    /// The whole body is busy: nothing plays over it and it stops aiming.
    exclusive: bool,
}

impl Span {
    fn at(&self, t: f32, crouch: bool) -> ClipAt {
        let time = t - self.t0;
        match self.then {
            Some(then) if time >= self.first.length => then.at(time - self.first.length, crouch),
            _ => self.first.at(time, crouch),
        }
    }
}

fn span_at(spans: &[Span], t: f32) -> Option<&Span> {
    let after = spans.partition_point(|s| s.t0 <= t);
    spans[..after].last().filter(|s| t < s.t1)
}

/// Sorts `spans`, and a span ends where the next one starts.
fn settle(spans: &mut Vec<Span>) {
    spans.sort_by(|a, b| a.t0.total_cmp(&b.t0));
    for i in 1..spans.len() {
        spans[i - 1].t1 = spans[i - 1].t1.min(spans[i].t0);
    }
    spans.retain(|s| s.t1 > s.t0);
}

/// One player's round.
#[derive(Debug, Default)]
pub struct PlayerTimeline {
    deaths: Vec<Death>,
    base: Vec<Span>,
    upper: Vec<Span>,
    shots: Vec<Once>,
    reactions: Vec<Once>,
    /// Which way the legs face, per sample, in degrees like `Track::yaw`.
    body_yaw: Vec<f32>,
}

impl PlayerTimeline {
    /// The clip that replaces the walking clips, if any.
    pub fn base(&self, t: f32, crouch: bool) -> Option<ClipAt> {
        if let Some(d) = self.dead_at(t) {
            return Some(ClipAt { clip: d.clip, time: t - d.t });
        }
        span_at(&self.base, t).map(|s| s.at(t, crouch))
    }

    /// The clip for the upper body, given what the player holds. Nothing plays over a body
    /// that is busy as a whole.
    pub fn upper(&self, t: f32, crouch: bool, tag: Option<&str>) -> Option<ClipAt> {
        if self.dead_at(t).is_some() || span_at(&self.base, t).is_some_and(|s| s.exclusive) {
            return None;
        }
        if let Some(span) = span_at(&self.upper, t) {
            return Some(span.at(t, crouch));
        }
        // CS2 walks with the knife's legs for these, and holds them with a pose over the top.
        let held = match anim_tag(tag?) {
            "C4" => ("hold_C4", "hold_crouch_C4"),
            "GRENADE" => ("hold_GRENADE", "hold_crouch_GRENADE"),
            "MOLOTOV" => ("hold_MOLOTOV", "hold_crouch_MOLOTOV"),
            _ => return None,
        };
        Some(ClipAt { clip: if crouch { held.1 } else { held.0 }, time: 0.0 })
    }

    pub fn shot(&self, t: f32) -> Option<AddAt> {
        once_at(&self.shots, t)
    }

    /// A flinch or a landing.
    pub fn reaction(&self, t: f32) -> Option<AddAt> {
        once_at(&self.reactions, t)
    }

    /// How much the spine follows the aim: 0 for a corpse or a player bent over the bomb.
    pub fn aim_weight(&self, t: f32) -> f32 {
        if let Some(d) = self.dead_at(t) {
            return (1.0 - (t - d.t) / AIM_FADE_S).max(0.0);
        }
        let after = self.base.partition_point(|s| s.t0 <= t);
        let Some(span) = self.base[..after].iter().rev().find(|s| s.exclusive) else {
            return 1.0;
        };
        if t < span.t1 {
            (1.0 - (t - span.t0) / AIM_FADE_S).max(0.0)
        } else {
            ((t - span.t1) / AIM_FADE_S).min(1.0)
        }
    }

    /// Which way the legs face between two samples, in degrees.
    pub fn body_yaw(&self, i0: usize, i1: usize, f: f64) -> Option<f64> {
        let (a, b) = (*self.body_yaw.get(i0)? as f64, *self.body_yaw.get(i1)? as f64);
        Some(a + wrap_180(b - a) * f)
    }

    pub fn dead_at(&self, t: f32) -> Option<&Death> {
        let after = self.deaths.partition_point(|d| d.t <= t);
        self.deaths[..after].last().filter(|d| t < d.until)
    }

    /// How far a corpse has come down from where it died (Source units).
    pub fn corpse_drop(&self, t: f32) -> f64 {
        self.dead_at(t).map_or(0.0, |d| d.drop * ((t - d.t) / CORPSE_DROP_S).min(1.0) as f64)
    }
}

fn once_at(list: &[Once], t: f32) -> Option<AddAt> {
    let after = list.partition_point(|o| o.t <= t);
    let last = list[..after].last()?;
    let time = t - last.t;
    (time < last.length).then(|| AddAt {
        clip: last.clip,
        time,
        weight: (time / ADD_EASE_IN_S).min(1.0),
    })
}

#[derive(Debug, Default)]
pub struct RoundAnim {
    pub players: Vec<PlayerTimeline>,
}

/// What building a timeline needs from a track, by sample.
struct Source<'a> {
    dec: &'a DecodedRound,
    tr: &'a Track,
    rate: f64,
    n: usize,
    length: &'a dyn Fn(&str) -> Option<f32>,
}

impl<'a> Source<'a> {
    fn seconds(&self, sample: f64) -> f32 {
        (sample / self.rate) as f32
    }

    fn sample(&self, s: f64) -> usize {
        (s.max(0.0) as usize).min(self.n.saturating_sub(1))
    }

    fn tag(&self, i: usize) -> Option<&'a str> {
        let w = *self.tr.weapon.get(i)? as usize;
        self.dec.weapon_table.get(w).map(String::as_str)
    }

    fn alive(&self, i: usize) -> bool {
        self.tr.flags[i] & c2r3::F_ALIVE != 0
    }

    fn flag(&self, i: usize, flag: u8) -> bool {
        self.tr.flags[i] & flag != 0
    }

    /// A clip pair with the length of the standing one, if the list and the file have both.
    fn part(&self, stand: &str, crouch: &str, looped: bool) -> Option<Part> {
        let (stand, crouch) = (action(stand)?, action(crouch)?);
        let length = (self.length)(stand)?;
        (self.length)(crouch)?;
        Some(Part { stand, crouch, length, looped })
    }

    fn once(&self, t: f32, clip: &str) -> Option<Once> {
        let clip = action(clip)?;
        Some(Once { t, clip, length: (self.length)(clip)? })
    }
}

/// The legs lag behind where the player looks: a standing player keeps their feet until
/// twisted too far, and a moving one turns them along quickly. Worked out from the whole
/// round at once, so that a scrub lands on the same answer as playing there.
fn body_yaw_track(tr: &Track, n: usize, rate: f64) -> Vec<f32> {
    let dt = 1.0 / rate;
    let mut out = Vec::with_capacity(n);
    let Some(&first) = tr.yaw.first() else {
        return out;
    };
    let (mut body, mut turning) = (first, false);
    for i in 0..n {
        if tr.flags[i] & c2r3::F_ALIVE != 0 {
            let speed = ground_motion(tr, i, n, rate).0;
            let off = wrap_180(tr.yaw[i] - body);
            if speed > BODY_MOVING_SPEED {
                body += off * (dt / BODY_MOVING_TIME).min(1.0);
                turning = false;
            } else {
                if off.abs() > BODY_TURN_START {
                    turning = true;
                }
                if turning {
                    let step = BODY_TURN_RATE * dt;
                    body += off.clamp(-step, step);
                    turning = wrap_180(tr.yaw[i] - body).abs() > BODY_TURN_STOP;
                }
            }
            let off = wrap_180(tr.yaw[i] - body);
            if off.abs() > BODY_MAX_TWIST {
                body = tr.yaw[i] - off.signum() * BODY_MAX_TWIST;
            }
        }
        out.push(body as f32);
    }
    out
}

struct Hit {
    sample: f64,
    attacker: Option<usize>,
    group: String,
    weapon: String,
}

/// `hurt` events for one player, in order.
fn hits_of(dec: &DecodedRound, slot: usize) -> Vec<Hit> {
    let Some(events) = dec.header.get("events").and_then(Value::as_array) else {
        return Vec::new();
    };
    events
        .iter()
        .filter(|e| e.get("k").and_then(Value::as_str) == Some("hurt"))
        .filter(|e| e.get("p").and_then(Value::as_u64) == Some(slot as u64))
        .filter_map(|e| {
            Some(Hit {
                sample: e.get("s")?.as_f64()?,
                attacker: e.get("a").and_then(Value::as_u64).map(|a| a as usize),
                group: e.get("hg").and_then(Value::as_str).unwrap_or("generic").to_string(),
                weapon: e.get("w").and_then(Value::as_str).unwrap_or("").to_string(),
            })
        })
        .collect()
}

/// Whether someone at `(x, y)` is in front of a player at `(px, py)` facing `yaw` degrees.
fn is_in_front(x: f64, y: f64, px: f64, py: f64, yaw: f64) -> bool {
    wrap_180((y - py).atan2(x - px).to_degrees() - yaw).abs() < 90.0
}

/// Which fall fits how the shot arrived. From behind a body goes forward, the others go
/// backward. `seed` varies it per player and round without a random number, so a scrub lands
/// on the same fall each time.
pub fn death_clip(head: bool, leg: bool, from_front: bool, seed: usize) -> &'static str {
    let set: &[&'static str] = if !from_front {
        &["death_gut_a", "death_chest_b"]
    } else if head {
        &["death_rshoulder", "death_rknee_b", "death_chest_b"]
    } else if leg {
        &["death_rknee_a", "death_rknee_b"]
    } else {
        &["death_chest_a", "death_rshoulder", "death_rknee_a"]
    };
    set[seed % set.len()]
}

fn is_head(group: &str) -> bool {
    matches!(group, "head" | "neck")
}

fn is_leg(group: &str) -> bool {
    group.contains("leg")
}

fn flinch_clip(group: &str, from_front: bool) -> &'static str {
    match group {
        "head" | "neck" if from_front => "flinch_head",
        "head" | "neck" => "flinch_head_rear",
        "stomach" => "flinch_stomach",
        "left_arm" => "flinch_arm_left",
        "right_arm" => "flinch_arm_right",
        "left_leg" => "flinch_leg_left",
        "right_leg" => "flinch_leg_right",
        _ if from_front => "flinch_chest",
        _ => "flinch_chest_rear",
    }
}

/// Standing in fire hurts twice a second, which would be a twitch and not a flinch.
fn is_burn(weapon: &str) -> bool {
    weapon == "🔥"
}

/// How `slot` falls at sample `i`, the first one it is dead on.
fn death_at(
    src: &Source,
    slot: usize,
    i: usize,
    until: f32,
    hits: &[Hit],
    body_yaw: &[f32],
    kills: &[KillFact],
) -> Option<Death> {
    let (dec, tr) = (src.dec, src.tr);
    let hit = hits.iter().rev().find(|h| h.sample <= i as f64 + 2.0);
    let name = dec.players.get(slot).map(|p| p.name.as_str());
    let kill = kills.iter().find(|k| Some(k.victim.as_str()) == name && k.sample.abs_diff(i) <= KILL_MATCH_SAMPLES);
    let attacker = hit.and_then(|h| h.attacker).or_else(|| {
        let atk = kill?.attacker.as_str();
        dec.players.iter().position(|p| p.name == atk)
    });
    let yaw = body_yaw[i - 1] as f64;
    let from_front = attacker
        .and_then(|a| dec.tracks.get(a))
        .map_or(true, |at| is_in_front(at.x[i - 1], at.y[i - 1], tr.x[i - 1], tr.y[i - 1], yaw));
    let head = hit.is_some_and(|h| is_head(&h.group)) || kill.is_some_and(|k| k.headshot);
    let leg = hit.is_some_and(|h| is_leg(&h.group));
    let clip = action(death_clip(head, leg, from_front, slot + dec.round.max(0) as usize))?;
    (src.length)(clip)?;
    // A kill in mid-air: the body comes down to where the player last stood.
    let drop = if src.flag(i - 1, c2r3::F_AIRBORNE) {
        let ground = (0..i).rev().find(|&j| !src.flag(j, c2r3::F_AIRBORNE));
        ground.map_or(0.0, |g| (tr.z[i - 1] - tr.z[g]).max(0.0))
    } else {
        0.0
    };
    Some(Death { t: src.seconds(i as f64), until, clip, drop })
}

/// When `slot` fired, in samples.
pub fn shot_samples(dec: &DecodedRound, slot: usize) -> Vec<f64> {
    dec.header
        .get("shots")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|s| s.get("p").and_then(Value::as_u64) == Some(slot as u64))
        .filter_map(|s| s.get("s")?.as_f64())
        .collect()
}

/// When a knife swing of `slot`'s connected, in samples. The replay keeps gun shots only, so all
/// that shows of a knife is the swings that hit: the `hurt` events on new replays, and the kills
/// on old ones too.
pub fn knife_hit_samples(dec: &DecodedRound, slot: usize, kills: &[KillFact]) -> Vec<f64> {
    let hits_dealt = dec.header.get("events").and_then(Value::as_array).into_iter().flatten().filter_map(|e| {
        let dealt = e.get("k").and_then(Value::as_str) == Some("hurt")
            && e.get("a").and_then(Value::as_u64) == Some(slot as u64)
            && e.get("w").and_then(Value::as_str) == Some("🔪");
        if dealt { e.get("s")?.as_f64() } else { None }
    });
    let name = dec.players.get(slot).map(|p| p.name.as_str());
    let kills_dealt = kills.iter().filter(|k| Some(k.attacker.as_str()) == name && k.weapon == "🔪").map(|k| k.sample as f64);
    hits_dealt.chain(kills_dealt).collect()
}

/// When a grenade of `slot`'s left the hand, in samples. Only the utility list says.
pub fn throw_samples(dec: &DecodedRound, slot: usize) -> Vec<f64> {
    let Some(sid) = dec.players.get(slot).map(|p| p.sid.as_str()) else {
        return Vec::new();
    };
    dec.utility
        .iter()
        .filter(|u| u.get("thrower").and_then(Value::as_str) == Some(sid))
        .filter_map(|u| u.get("throwSample").and_then(Value::as_f64))
        .collect()
}

fn build_player(
    dec: &DecodedRound,
    slot: usize,
    kills: &[KillFact],
    length: &dyn Fn(&str) -> Option<f32>,
) -> PlayerTimeline {
    let Some(tr) = dec.tracks.get(slot) else {
        return PlayerTimeline::default();
    };
    let (n, rate) = (dec.n_samples, dec.sample_rate);
    let src = Source { dec, tr, rate, n, length };
    let body_yaw = body_yaw_track(tr, n, rate);
    let hits = hits_of(dec, slot);

    let mut upper = Vec::new();
    let mut shots = Vec::new();
    for s in shot_samples(dec, slot) {
        let i = src.sample(s);
        let Some(tag) = src.tag(i) else { continue };
        let t = src.seconds(s);
        match anim_tag(tag) {
            // the throw has its own record, the bomb its events, and a knife see below
            "KNIFE" | "GRENADE" | "MOLOTOV" | "C4" => {}
            name => shots.extend(src.once(t, &format!("shoot_{name}"))),
        }
    }
    shots.sort_by(|a: &Once, b: &Once| a.t.total_cmp(&b.t));

    if let Some(first) = src.part("swing_knife", "swing_crouch_knife", false) {
        for s in knife_hit_samples(dec, slot, kills) {
            let t0 = (src.seconds(s) - KNIFE_HIT_S).max(0.0);
            upper.push(Span { t0, t1: t0 + first.length, first, then: None, cut_by_shot: false, exclusive: false });
        }
    }

    // A weapon in the hand changing is a draw.
    for i in 1..n {
        if tr.weapon[i] == tr.weapon[i - 1] || !src.alive(i) || !src.alive(i - 1) {
            continue;
        }
        let Some(name) = src.tag(i).map(anim_tag) else { continue };
        if let Some(first) = src.part(&format!("draw_{name}"), &format!("draw_crouch_{name}"), false) {
            let t0 = src.seconds(i as f64);
            upper.push(Span { t0, t1: t0 + first.length, first, then: None, cut_by_shot: true, exclusive: false });
        }
    }

    // Throws.
    for s in throw_samples(dec, slot) {
        if let Some(first) = src.part("throw", "throw_crouch", false) {
            let t0 = (src.seconds(s) - THROW_RELEASE_S).max(0.0);
            upper.push(Span { t0, t1: t0 + first.length, first, then: None, cut_by_shot: false, exclusive: false });
        }
    }

    // A flash raises the arm while it lasts.
    let mut was_flashed = false;
    for i in 0..n {
        let byte = tr.flash.get(i).copied().unwrap_or(0);
        if byte >= FLASH_MIN_BYTE && !was_flashed && src.alive(i) {
            if let Some(first) = src.part("flashed", "flashed_crouch", false) {
                let t0 = src.seconds(i as f64);
                let t1 = t0 + byte as f32 / FLASH_BYTE_PER_S * FLASH_POSE_SHARE;
                upper.push(Span { t0, t1, first, then: None, cut_by_shot: true, exclusive: false });
            }
        }
        was_flashed = byte > 0;
    }

    let mut base = Vec::new();
    let mut reactions = Vec::new();

    // Events only new replays have.
    for e in dec.header.get("events").and_then(Value::as_array).into_iter().flatten() {
        if e.get("p").and_then(Value::as_u64) != Some(slot as u64) {
            continue;
        }
        let Some(s) = e.get("s").and_then(Value::as_f64) else { continue };
        let i = src.sample(s);
        let t0 = src.seconds(s);
        match e.get("k").and_then(Value::as_str) {
            Some("reload") => {
                let Some(name) = src.tag(i).map(anim_tag) else { continue };
                if let Some(first) = src.part(&format!("reload_{name}"), &format!("reload_crouch_{name}"), false) {
                    upper.push(Span { t0, t1: t0 + first.length, first, then: None, cut_by_shot: true, exclusive: false });
                }
            }
            Some("plant") => {
                let Some(first) = src.part("planting", "planting_crouch", false) else { continue };
                let (px, py) = (tr.x[i], tr.y[i]);
                let stops = (i + 1..n).find(|&j| {
                    !src.alive(j)
                        || src.tag(j) != Some("💣")
                        || (tr.x[j] - px).hypot(tr.y[j] - py) > PLANT_ABORT_UNITS
                });
                let t1 = stops.map_or(f32::MAX, |j| src.seconds(j as f64)).min(t0 + first.length);
                base.push(Span { t0, t1, first, then: None, cut_by_shot: false, exclusive: true });
            }
            _ => {}
        }
    }

    // Defusing is a flag, so old replays have it too.
    let mut i = 0;
    while i < n {
        if !src.flag(i, c2r3::F_DEFUSING) || !src.alive(i) {
            i += 1;
            continue;
        }
        let start = i;
        while i < n && src.flag(i, c2r3::F_DEFUSING) && src.alive(i) {
            i += 1;
        }
        let class = src.tag(start).map_or(WeaponClass::Knife, class_for_tag);
        let c = class_suffix(class);
        let enter = src.part(&format!("defuse_enter_{c}"), &format!("defuse_crouch_enter_{c}"), false);
        let hold = src.part(&format!("defuse_loop_{c}"), &format!("defuse_crouch_loop_{c}"), true);
        if let (Some(first), Some(then)) = (enter, hold) {
            base.push(Span {
                t0: src.seconds(start as f64),
                t1: src.seconds(i as f64),
                first,
                then: Some(then),
                cut_by_shot: false,
                exclusive: true,
            });
        }
    }

    // Jumps and falls: only new replays have the flag.
    let mut i = 0;
    while i < n {
        if !src.flag(i, c2r3::F_AIRBORNE) || !src.alive(i) {
            i += 1;
            continue;
        }
        let start = i;
        while i < n && src.flag(i, c2r3::F_AIRBORNE) && src.alive(i) {
            i += 1;
        }
        let (t0, t1) = (src.seconds(start as f64), src.seconds(i as f64));
        if t1 - t0 < MIN_AIR_S {
            continue;
        }
        let class = src.tag(start).map_or(WeaponClass::Rifle, class_for_tag);
        let c = class_suffix(class);
        let (speed, dx, dy) = ground_motion(tr, start, n, rate);
        let dir = if speed < JUMP_STAND_SPEED {
            "stand"
        } else {
            let turn = heading_cw(dx, dy, body_yaw[start] as f64).rem_euclid(std::f64::consts::TAU);
            ["n", "e", "s", "w"][((turn / std::f64::consts::FRAC_PI_2).round() as usize) % 4]
        };
        let inair = src.part(&format!("inair_{dir}_{c}"), &format!("inair_crouch_{dir}_{c}"), true);
        let jump = src.part(&format!("jump_{dir}_{c}"), &format!("jump_crouch_{dir}_{c}"), false);
        let window = ((JUMP_RISE_WINDOW_S * rate).round() as usize).max(1);
        let rises = tr.z[(start + window).min(i - 1)] - tr.z[start] > JUMP_RISE_UNITS;
        match (rises, jump, inair) {
            (true, Some(first), Some(then)) => {
                base.push(Span { t0, t1, first, then: Some(then), cut_by_shot: false, exclusive: false })
            }
            (_, _, Some(first)) => base.push(Span { t0, t1, first, then: None, cut_by_shot: false, exclusive: false }),
            _ => {}
        }
        if t1 - t0 >= MIN_LANDING_AIR_S && i < n && src.alive(i) {
            let clip = if src.flag(i, c2r3::F_CROUCH) { "land_crouch" } else { "land" };
            reactions.extend(src.once(t1, clip));
        }
    }

    // Deaths, from the falling edge of the alive flag.
    let mut deaths = Vec::new();
    let mut i = 1;
    while i < n {
        if src.alive(i) || !src.alive(i - 1) {
            i += 1;
            continue;
        }
        let died = i;
        while i < n && !src.alive(i) {
            i += 1;
        }
        let (t0, until) = (src.seconds(died as f64), if i < n { src.seconds(i as f64) } else { f32::MAX });
        if until - t0 >= MIN_DEAD_S {
            deaths.extend(death_at(&src, slot, died, until, &hits, &body_yaw, kills));
        }
    }

    // Flinches: the event where there is one, the health channel on old replays.
    if hits.is_empty() {
        for i in 1..n {
            if src.alive(i) && src.alive(i - 1) && tr.health[i] < tr.health[i - 1] {
                reactions.extend(src.once(src.seconds(i as f64), "flinch_chest"));
            }
        }
    } else {
        for h in &hits {
            let i = src.sample(h.sample);
            if !src.alive(i) || is_burn(&h.weapon) {
                continue;
            }
            let from_front = h.attacker.and_then(|a| dec.tracks.get(a)).map_or(true, |at| {
                is_in_front(at.x[i], at.y[i], tr.x[i], tr.y[i], body_yaw[i] as f64)
            });
            reactions.extend(src.once(src.seconds(h.sample), flinch_clip(&h.group, from_front)));
        }
    }
    reactions.sort_by(|a, b| a.t.total_cmp(&b.t));

    settle(&mut base);
    settle(&mut upper);
    // Firing ends a draw or a reload, or the arms would keep doing it through the burst.
    for span in upper.iter_mut().filter(|s| s.cut_by_shot) {
        let soonest = span.t0 + CUT_BY_SHOT_AFTER_S;
        let at = shots.partition_point(|o| o.t < soonest);
        if let Some(next) = shots.get(at) {
            span.t1 = span.t1.min(next.t);
        }
    }

    PlayerTimeline { deaths, base, upper, shots, reactions, body_yaw }
}

/// All players' timelines. `length` gives a clip's length in seconds; a clip it does not know
/// is left out, which is all that happens on an older set of clip files.
pub fn build_round_anim(
    dec: &DecodedRound,
    kills: &[KillFact],
    length: &dyn Fn(&str) -> Option<f32>,
) -> RoundAnim {
    if dec.n_samples == 0 || dec.sample_rate <= 0.0 {
        return RoundAnim::default();
    }
    RoundAnim { players: (0..dec.players.len()).map(|slot| build_player(dec, slot, kills, length)).collect() }
}

/// Kills in the manifest, by the round's number.
pub fn parse_kills(manifest: &Value) -> HashMap<i64, Vec<KillFact>> {
    let mut out = HashMap::new();
    for round in manifest.get("rounds").and_then(Value::as_array).into_iter().flatten() {
        let Some(number) = round.get("n").and_then(Value::as_i64) else { continue };
        let kills = round
            .get("kills")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|k| {
                Some(KillFact {
                    sample: k.get("sample")?.as_u64()? as usize,
                    attacker: k.get("atk")?.as_str()?.to_string(),
                    victim: k.get("vic")?.as_str()?.to_string(),
                    weapon: k.get("w").and_then(Value::as_str).unwrap_or("").to_string(),
                    headshot: k.get("hs").and_then(Value::as_bool).unwrap_or(false),
                })
            })
            .collect();
        out.insert(number, kills);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use c2r3::{PlayerMeta, F_AIRBORNE, F_ALIVE, F_CROUCH, F_DEFUSING, F_PRESENT};
    use serde_json::json;

    const RATE: f64 = 64.0;
    const SECONDS: usize = 20;
    const AK: u8 = 0;
    const KNIFE: u8 = 1;
    const C4: u8 = 2;
    const HE: u8 = 3;
    const ZEUS: u8 = 4;
    const LIVE: u8 = F_PRESENT | F_ALIVE;

    fn weapon_table() -> Vec<String> {
        ["AK", "🔪", "💣", "HE", "ZEUS X"].iter().map(|s| s.to_string()).collect()
    }

    /// Anyone standing still, alive, facing +X, holding an AK.
    fn still_track(n: usize) -> Track {
        Track {
            x: vec![0.0; n],
            y: vec![0.0; n],
            z: vec![0.0; n],
            yaw: vec![0.0; n],
            pitch: vec![0.0; n],
            flags: vec![LIVE; n],
            health: vec![100; n],
            weapon: vec![AK; n],
            flash: vec![0; n],
        }
    }

    fn round(tracks: Vec<Track>, header: Value) -> DecodedRound {
        let n = tracks[0].x.len();
        DecodedRound {
            map: "de_test".into(),
            round: 3,
            tick_rate: 64,
            sample_stride: 1,
            sample_rate: RATE,
            n_samples: n,
            start_tick: 0,
            end_tick: n as i64,
            origin: [0.0; 3],
            scale_xy: 1.0,
            scale_z: 1.0,
            players: (0..tracks.len())
                .map(|slot| PlayerMeta { slot: slot as i64, sid: format!("sid{slot}"), name: format!("p{slot}"), team: 2 })
                .collect(),
            weapon_table: weapon_table(),
            utility: header.get("utility").and_then(Value::as_array).cloned().unwrap_or_default(),
            tracks,
            header,
        }
    }

    /// Every clip is two seconds long unless the name says otherwise.
    fn length(name: &str) -> Option<f32> {
        action(name)?;
        Some(if name.starts_with("reload") { 2.4 } else if name.starts_with("draw") { 1.0 } else { 2.0 })
    }

    fn timeline(tracks: Vec<Track>, header: Value) -> PlayerTimeline {
        let dec = round(tracks, header);
        build_round_anim(&dec, &[], &length).players.into_iter().next().unwrap()
    }

    fn samples() -> usize {
        SECONDS * RATE as usize
    }

    /// `(time, sample)` at a whole number of seconds.
    fn at(seconds: f32) -> usize {
        (seconds as f64 * RATE) as usize
    }

    #[test]
    fn nothing_going_on_means_nothing_plays() {
        let tl = timeline(vec![still_track(samples())], json!({}));
        assert_eq!(tl.base(5.0, false), None);
        assert_eq!(tl.upper(5.0, false, Some("AK")), None);
        assert_eq!(tl.shot(5.0), None);
        assert_eq!(tl.aim_weight(5.0), 1.0);
    }

    #[test]
    fn a_shot_plays_the_weapons_clip_from_its_moment_at_any_time_asked() {
        let tl = timeline(vec![still_track(samples())], json!({"shots": [{"s": 320.0, "p": 0}]}));
        assert_eq!(tl.shot(4.9), None);
        let later = tl.shot(5.5).unwrap();
        assert_eq!(later.clip, "shoot_AK");
        assert!((later.time - 0.5).abs() < 1e-4);
        // asking out of order is the point: a scrub back gets the earlier answer
        assert_eq!(tl.shot(4.9), None);
        assert_eq!(tl.shot(9.0), None, "the clip is over");
    }

    #[test]
    fn a_later_shot_restarts_the_clip() {
        let tl = timeline(vec![still_track(samples())], json!({"shots": [{"s": 320.0, "p": 0}, {"s": 352.0, "p": 0}]}));
        assert!((tl.shot(5.6).unwrap().time - 0.1).abs() < 1e-4);
    }

    #[test]
    fn neither_a_knife_nor_a_grenade_plays_a_gun_shot() {
        let mut tr = still_track(samples());
        tr.weapon[300..360].fill(KNIFE);
        tr.weapon[400..].fill(HE);
        let tl = timeline(vec![tr], json!({"shots": [{"s": 320.0, "p": 0}, {"s": 420.0, "p": 0}]}));
        assert_eq!(tl.shot(5.1), None);
        assert_eq!(tl.shot(6.7), None);
    }

    #[test]
    fn a_knife_hit_was_a_swing_that_started_just_before_it() {
        let mut tr = still_track(samples());
        tr.weapon.fill(KNIFE);
        let header = json!({"events": [{"k": "hurt", "s": at(5.0) as f64, "p": 1, "a": 0, "d": 40, "hg": "chest", "w": "🔪"}]});
        let tl = timeline(vec![tr, still_track(samples())], header);
        let swing = tl.upper(5.0, false, Some("🔪")).unwrap();
        assert_eq!(swing.clip, "swing_knife");
        assert!((swing.time - KNIFE_HIT_S).abs() < 1e-3);
        assert_eq!(tl.upper(5.0, true, Some("🔪")).unwrap().clip, "swing_crouch_knife");
    }

    #[test]
    fn a_gun_hit_is_not_a_swing() {
        let header = json!({"events": [{"k": "hurt", "s": at(5.0) as f64, "p": 1, "a": 0, "d": 40, "hg": "chest", "w": "AK"}]});
        let tl = timeline(vec![still_track(samples()), still_track(samples())], header);
        assert_eq!(tl.upper(5.0, false, Some("AK")), None);
    }

    #[test]
    fn old_replays_get_knife_swings_from_the_kill_list() {
        let dec = round(vec![still_track(samples()), still_track(samples())], json!({}));
        let kills = [KillFact { sample: at(5.0), attacker: "p0".into(), victim: "p1".into(), weapon: "🔪".into(), headshot: false }];
        let tl = build_round_anim(&dec, &kills, &length).players.into_iter().next().unwrap();
        assert_eq!(tl.upper(5.0, false, Some("🔪")).unwrap().clip, "swing_knife");
    }

    #[test]
    fn changing_weapon_is_a_draw_and_firing_cuts_it_short() {
        let mut tr = still_track(samples());
        tr.weapon[at(5.0)..].fill(KNIFE);
        tr.weapon[at(8.0)..].fill(AK);
        let tl = timeline(vec![tr], json!({"shots": [{"s": at(8.5) as f64, "p": 0}]}));
        assert_eq!(tl.upper(5.2, false, Some("🔪")).unwrap().clip, "draw_KNIFE");
        assert_eq!(tl.upper(5.2, true, Some("🔪")).unwrap().clip, "draw_crouch_KNIFE");
        assert_eq!(tl.upper(6.5, false, Some("🔪")), None, "over after its length");
        assert_eq!(tl.upper(8.2, false, Some("AK")).unwrap().clip, "draw_AK");
        assert_eq!(tl.upper(8.6, false, Some("AK")), None, "the shot ended it");
    }

    #[test]
    fn no_draw_at_the_round_start_or_from_a_death() {
        let mut tr = still_track(samples());
        tr.flags[at(5.0)..].fill(F_PRESENT);
        tr.weapon[at(5.0)..].fill(c2r3::WEAPON_NONE);
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.upper(0.1, false, Some("AK")), None);
        assert_eq!(tl.upper(5.1, false, None), None);
    }

    #[test]
    fn a_reload_needs_a_clip_for_that_weapon() {
        let mut tr = still_track(samples());
        tr.weapon[at(6.0)..].fill(ZEUS);
        let events = json!({"events": [
            {"k": "reload", "s": at(2.0) as f64, "p": 0},
            {"k": "reload", "s": at(8.0) as f64, "p": 0},
        ]});
        let tl = timeline(vec![tr], events);
        assert_eq!(tl.upper(3.0, false, Some("AK")).unwrap().clip, "reload_AK");
        assert_eq!(tl.upper(8.2, false, Some("ZEUS X")), None);
    }

    #[test]
    fn a_throw_starts_early_enough_to_let_go_at_the_recorded_moment() {
        let mut tr = still_track(samples());
        tr.weapon[at(4.0)..at(6.0)].fill(HE);
        let header = json!({"utility": [{"thrower": "sid0", "throwSample": at(5.0) as f64}]});
        let tl = timeline(vec![tr], header);
        let on = tl.upper(5.0, false, Some("HE")).unwrap();
        assert_eq!(on.clip, "throw");
        assert!((on.time - THROW_RELEASE_S).abs() < 1e-3);
    }

    #[test]
    fn someone_elses_throw_is_not_mine() {
        let header = json!({"utility": [{"thrower": "sid9", "throwSample": at(5.0) as f64}]});
        let tl = timeline(vec![still_track(samples())], header);
        assert_eq!(tl.upper(5.0, false, Some("AK")), None);
    }

    #[test]
    fn grenades_and_the_bomb_are_held_with_a_pose_and_guns_are_not() {
        let tl = timeline(vec![still_track(samples())], json!({}));
        assert_eq!(tl.upper(1.0, false, Some("💣")).unwrap().clip, "hold_C4");
        assert_eq!(tl.upper(1.0, true, Some("SMOKE")).unwrap().clip, "hold_crouch_GRENADE");
        assert_eq!(tl.upper(1.0, false, Some("MOLLY")).unwrap().clip, "hold_MOLOTOV");
        assert_eq!(tl.upper(1.0, false, Some("AK")), None);
    }

    #[test]
    fn a_plant_lasts_until_it_ends_or_the_player_walks_off() {
        let mut tr = still_track(samples());
        tr.weapon[at(4.0)..at(5.5)].fill(C4);
        let tl = timeline(vec![tr], json!({"events": [{"k": "plant", "s": at(4.0) as f64, "p": 0}]}));
        assert_eq!(tl.base(5.0, false).unwrap().clip, "planting");
        assert_eq!(tl.base(5.0, true).unwrap().clip, "planting_crouch");
        assert_eq!(tl.base(5.6, false), None, "the weapon left the hand");

        let mut tr = still_track(samples());
        tr.weapon[at(4.0)..at(9.0)].fill(C4);
        tr.x[at(5.0)..].fill(100.0);
        let tl = timeline(vec![tr], json!({"events": [{"k": "plant", "s": at(4.0) as f64, "p": 0}]}));
        assert!(tl.base(4.9, false).is_some());
        assert_eq!(tl.base(5.1, false), None, "walked off");
    }

    #[test]
    fn a_defuse_enters_then_loops_and_stops_aiming() {
        let mut tr = still_track(samples());
        for f in &mut tr.flags[at(5.0)..at(12.0)] {
            *f |= F_DEFUSING;
        }
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(5.5, false).unwrap().clip, "defuse_enter_rifle");
        let looped = tl.base(8.5, false).unwrap();
        assert_eq!(looped.clip, "defuse_loop_rifle");
        assert!(looped.time < 2.0, "time wraps inside the loop: {}", looped.time);
        assert_eq!(tl.base(12.1, false), None);
        assert_eq!(tl.upper(8.5, false, Some("AK")), None, "the arms are busy");
        assert_eq!(tl.aim_weight(8.5), 0.0);
        assert_eq!(tl.aim_weight(12.5), 1.0, "and aims again afterwards");
    }

    #[test]
    fn a_jump_takes_off_then_hangs_then_lands() {
        let mut tr = still_track(samples());
        let (a, b) = (at(5.0), at(6.0));
        for (k, f) in tr.flags[a..b].iter_mut().enumerate() {
            *f |= F_AIRBORNE;
            tr.z[a + k] = (k as f64 / RATE * 250.0).min(40.0);
        }
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(5.1, false).unwrap().clip, "jump_stand_rifle");
        // length() gives 2 s, longer than the run here, so inair shows only on a shorter clip
        assert_eq!(tl.reaction(6.1).unwrap().clip, "land");
        assert_eq!(tl.base(6.1, false), None);
    }

    #[test]
    fn walking_off_a_ledge_only_hangs() {
        let mut tr = still_track(samples());
        let (a, b) = (at(5.0), at(6.0));
        for (k, f) in tr.flags[a..b].iter_mut().enumerate() {
            *f |= F_AIRBORNE;
            tr.z[a + k] = -(k as f64);
        }
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(5.5, false).unwrap().clip, "inair_stand_rifle");
    }

    #[test]
    fn a_stair_step_is_not_a_jump() {
        let mut tr = still_track(samples());
        for f in &mut tr.flags[at(5.0)..at(5.0) + 3] {
            *f |= F_AIRBORNE;
        }
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(5.01, false), None);
    }

    #[test]
    fn a_body_falls_and_stays_down_to_the_end() {
        let mut tr = still_track(samples());
        tr.flags[at(5.0)..].fill(F_PRESENT);
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(4.9, false), None);
        let falling = tl.base(5.5, false).unwrap();
        assert!(falling.clip.starts_with("death_"));
        assert!((falling.time - 0.5).abs() < 1e-3);
        let long_after = tl.base(19.0, false).unwrap();
        assert_eq!(long_after.clip, falling.clip, "same fall at any time asked");
        assert!(long_after.time > 2.0, "clamped to the last frame by the player, not here");
        assert_eq!(tl.upper(6.0, false, Some("AK")), None);
        assert_eq!(tl.aim_weight(6.0), 0.0);
    }

    #[test]
    fn a_one_sample_dropout_of_the_alive_flag_is_not_a_death() {
        let mut tr = still_track(samples());
        tr.flags[at(5.0)] = F_PRESENT;
        let tl = timeline(vec![tr], json!({}));
        assert!(tl.dead_at(5.0).is_none());
        assert_eq!(tl.base(6.0, false), None);
    }

    #[test]
    fn a_second_life_ends_the_first_fall() {
        let mut tr = still_track(samples());
        tr.flags[at(5.0)..at(7.0)].fill(F_PRESENT);
        tr.flags[at(10.0)..].fill(F_PRESENT);
        let tl = timeline(vec![tr], json!({}));
        assert!(tl.dead_at(6.0).is_some());
        assert!(tl.dead_at(8.0).is_none());
        assert_eq!(tl.base(8.0, false), None);
        assert_eq!(tl.aim_weight(8.0), 1.0);
        assert!(tl.dead_at(11.0).is_some());
        assert!((tl.base(11.0, false).unwrap().time - 1.0).abs() < 1e-3);
    }

    #[test]
    fn someone_dead_from_the_first_sample_has_no_fall() {
        let mut tr = still_track(samples());
        tr.flags.fill(F_PRESENT);
        assert!(timeline(vec![tr], json!({})).dead_at(5.0).is_none());
    }

    /// slot 0 dies facing +X with an attacker at `attacker_x`.
    fn death_with_attacker(attacker_x: f64, hit_group: &str) -> &'static str {
        let mut victim = still_track(samples());
        victim.flags[at(5.0)..].fill(F_PRESENT);
        let mut attacker = still_track(samples());
        attacker.x.fill(attacker_x);
        let header = json!({"events": [{"k": "hurt", "s": at(5.0) as f64, "p": 0, "a": 1, "d": 100, "da": 0, "hg": hit_group, "w": "AK"}]});
        timeline(vec![victim, attacker], header).base(6.0, false).unwrap().clip
    }

    #[test]
    fn shot_from_behind_falls_forward() {
        assert!(["death_gut_a", "death_chest_b"].contains(&death_with_attacker(-500.0, "chest")));
    }

    #[test]
    fn the_hit_decides_the_fall_from_the_front() {
        let head = death_with_attacker(500.0, "head");
        assert!(["death_rshoulder", "death_rknee_b", "death_chest_b"].contains(&head), "{head}");
        let leg = death_with_attacker(500.0, "left_leg");
        assert!(["death_rknee_a", "death_rknee_b"].contains(&leg), "{leg}");
    }

    #[test]
    fn old_replays_get_the_attacker_from_the_kill_list() {
        let mut victim = still_track(samples());
        victim.flags[at(5.0)..].fill(F_PRESENT);
        let mut attacker = still_track(samples());
        attacker.x.fill(-500.0);
        let dec = round(vec![victim, attacker], json!({}));
        let kills = [KillFact { sample: at(5.0), attacker: "p1".into(), victim: "p0".into(), weapon: "AK".into(), headshot: false }];
        let tl = build_round_anim(&dec, &kills, &length).players.into_iter().next().unwrap();
        assert!(["death_gut_a", "death_chest_b"].contains(&tl.base(6.0, false).unwrap().clip));
    }

    #[test]
    fn a_kill_in_mid_air_comes_down_to_the_last_ground() {
        let mut tr = still_track(samples());
        tr.z.fill(100.0);
        let (a, b) = (at(5.0), at(5.5));
        for k in a..b {
            tr.flags[k] |= F_AIRBORNE;
            tr.z[k] = 140.0;
        }
        tr.flags[b..].fill(F_PRESENT);
        tr.z[b..].fill(140.0);
        let tl = timeline(vec![tr], json!({}));
        assert!(tl.corpse_drop(5.5) < 1.0);
        assert!((tl.corpse_drop(10.0) - 40.0).abs() < 1e-6);
    }

    #[test]
    fn hits_flinch_by_where_they_land_and_burns_do_not() {
        let mut attacker = still_track(samples());
        attacker.x.fill(500.0);
        let header = json!({"events": [
            {"k": "hurt", "s": at(3.0) as f64, "p": 0, "a": 1, "d": 90, "da": 0, "hg": "head", "w": "AK"},
            {"k": "hurt", "s": at(9.0) as f64, "p": 0, "d": 85, "hg": "generic", "w": "🔥"},
        ]});
        let tl = timeline(vec![still_track(samples()), attacker], header);
        assert_eq!(tl.reaction(3.1).unwrap().clip, "flinch_head");
        assert_eq!(tl.reaction(9.1), None);
    }

    #[test]
    fn old_replays_flinch_from_the_health_channel() {
        let mut tr = still_track(samples());
        tr.health[at(3.0)..].fill(70);
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.reaction(3.1).unwrap().clip, "flinch_chest");
    }

    #[test]
    fn a_long_flash_raises_the_arm_and_a_short_one_does_not() {
        let mut tr = still_track(samples());
        tr.flash[at(3.0)..at(5.0)].fill(100); // 2 seconds' worth
        tr.flash[at(9.0)..at(9.5)].fill(20);
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.upper(3.5, false, Some("AK")).unwrap().clip, "flashed");
        assert_eq!(tl.upper(5.5, false, Some("AK")), None);
        assert_eq!(tl.upper(9.2, false, Some("AK")), None);
    }

    /// Standing on the spot, looking to `yaw` from `at(2.0)` on.
    fn body_after_a_turn(yaw: f64) -> PlayerTimeline {
        let mut tr = still_track(samples());
        tr.yaw[at(2.0)..].fill(yaw);
        timeline(vec![tr], json!({}))
    }

    #[test]
    fn a_standing_player_keeps_their_feet_for_a_small_turn() {
        let tl = body_after_a_turn(30.0);
        assert_eq!(tl.body_yaw(at(10.0), at(10.0), 0.0), Some(0.0));
    }

    #[test]
    fn a_big_turn_brings_the_feet_along_but_not_at_once() {
        let tl = body_after_a_turn(70.0);
        let soon = tl.body_yaw(at(2.0), at(2.0), 0.0).unwrap();
        assert!(soon < 10.0, "feet lag behind: {soon}");
        let later = tl.body_yaw(at(5.0), at(5.0), 0.0).unwrap();
        assert!((later - 70.0).abs() <= BODY_TURN_STOP + 1.0, "{later}");
    }

    #[test]
    fn a_flick_never_twists_the_spine_past_the_limit() {
        let tl = body_after_a_turn(179.0);
        let b = tl.body_yaw(at(2.0), at(2.0), 0.0).unwrap();
        assert!(wrap_180(179.0 - b).abs() <= BODY_MAX_TWIST + 1e-3, "{b}");
    }

    #[test]
    fn a_moving_player_turns_their_feet_along() {
        let mut tr = still_track(samples());
        tr.x = (0..samples()).map(|i| i as f64 / RATE * 200.0).collect();
        tr.yaw[at(2.0)..].fill(30.0);
        let tl = timeline(vec![tr], json!({}));
        let b = tl.body_yaw(at(3.0), at(3.0), 0.0).unwrap();
        assert!((b - 30.0).abs() < 1.0, "{b}");
    }

    #[test]
    fn body_yaw_interpolates_the_short_way_round() {
        let tl = PlayerTimeline { body_yaw: vec![170.0, -170.0], ..Default::default() };
        let mid = tl.body_yaw(0, 1, 0.5).unwrap();
        assert!((wrap_180(mid) - 180.0).abs() < 1e-3 || (wrap_180(mid) + 180.0).abs() < 1e-3, "{mid}");
    }

    #[test]
    fn crouch_picks_the_clip_at_the_time_asked() {
        let mut tr = still_track(samples());
        for f in &mut tr.flags[at(5.0)..at(12.0)] {
            *f |= F_DEFUSING | F_CROUCH;
        }
        let tl = timeline(vec![tr], json!({}));
        assert_eq!(tl.base(5.5, true).unwrap().clip, "defuse_crouch_enter_rifle");
    }

    #[test]
    fn missing_clip_files_leave_things_out_without_failing() {
        let mut tr = still_track(samples());
        tr.flags[at(5.0)..].fill(F_PRESENT);
        let dec = round(vec![tr], json!({"shots": [{"s": 100.0, "p": 0}]}));
        let tl = build_round_anim(&dec, &[], &|_| None).players.into_iter().next().unwrap();
        assert_eq!(tl.shot(1.6), None);
        assert_eq!(tl.base(6.0, false), None, "no clip, no fall: the body stays hidden as before");
        assert!(tl.dead_at(6.0).is_none());
    }

    #[test]
    fn an_empty_round_builds_nothing() {
        let mut dec = round(vec![still_track(1)], json!({}));
        dec.n_samples = 0;
        assert!(build_round_anim(&dec, &[], &length).players.is_empty());
    }

    #[test]
    fn kills_come_from_the_manifest_by_round_number() {
        let m = json!({"rounds": [
            {"n": 7, "kills": [{"sample": 5, "atk": "a", "vic": "b", "w": "AK", "hs": true}, {"sample": 9, "atk": "c", "vic": "d"}]},
            {"n": 8},
        ]});
        let kills = parse_kills(&m);
        assert_eq!(kills[&7].len(), 2);
        assert!(kills[&7][0].headshot);
        assert!(!kills[&7][1].headshot);
        assert_eq!(kills[&7][0].weapon, "AK");
        assert!(kills[&8].is_empty());
    }

    #[test]
    fn every_weapon_has_its_clips() {
        let list: HashSet<&str> = action_list().map(|(n, _)| n).collect();
        for line in include_str!("weapon_models.txt").lines().filter(|l| !l.is_empty() && !l.starts_with('#')) {
            let tag = line.split_whitespace().next().unwrap();
            let name = match tag {
                "KNIFE_T" | "KNIFE_CT" => "KNIFE",
                other => anim_tag(other),
            };
            assert!(list.contains(format!("draw_{name}").as_str()), "draw_{name}");
            assert!(list.contains(format!("draw_crouch_{name}").as_str()), "draw_crouch_{name}");
            if matches!(name, "KNIFE" | "C4" | "GRENADE" | "MOLOTOV") {
                continue;
            }
            assert!(list.contains(format!("shoot_{name}").as_str()), "shoot_{name}");
            if name != "ZEUS" {
                assert!(list.contains(format!("reload_{name}").as_str()), "reload_{name}");
                assert!(list.contains(format!("reload_crouch_{name}").as_str()), "reload_crouch_{name}");
            }
        }
    }

    #[test]
    fn every_clip_named_here_is_in_the_list() {
        let list: HashSet<&str> = action_list().map(|(n, _)| n).collect();
        for head in [true, false] {
            for leg in [true, false] {
                for front in [true, false] {
                    for seed in 0..6 {
                        let c = death_clip(head, leg, front, seed);
                        assert!(list.contains(c), "{c}");
                    }
                }
            }
        }
        for group in ["head", "neck", "chest", "stomach", "left_arm", "right_arm", "left_leg", "right_leg", "generic"] {
            for front in [true, false] {
                let c = flinch_clip(group, front);
                assert!(list.contains(c), "{c}");
            }
        }
        for name in ["land", "land_crouch", "throw", "throw_crouch", "planting", "planting_crouch", "flashed",
            "flashed_crouch", "swing_knife", "swing_crouch_knife", "hold_C4", "hold_crouch_C4", "hold_GRENADE",
            "hold_crouch_GRENADE", "hold_MOLOTOV", "hold_crouch_MOLOTOV"] {
            assert!(list.contains(name), "{name}");
        }
        for class in ["rifle", "pistol", "knife"] {
            for part in ["enter", "loop"] {
                assert!(list.contains(format!("defuse_{part}_{class}").as_str()));
                assert!(list.contains(format!("defuse_crouch_{part}_{class}").as_str()));
            }
            for dir in ["stand", "n", "e", "s", "w"] {
                for kind in ["jump", "inair"] {
                    assert!(list.contains(format!("{kind}_{dir}_{class}").as_str()));
                    assert!(list.contains(format!("{kind}_crouch_{dir}_{class}").as_str()));
                }
            }
        }
    }

    #[test]
    fn every_line_says_a_known_layer() {
        for (name, layer) in action_list() {
            assert!(["full", "upper", "add"].contains(&layer), "{name}: {layer}");
        }
    }
}
