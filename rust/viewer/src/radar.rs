//! What the page's radar shows besides the players: grenades while they fly and while they
//! work, and the bomb, wherever it is. Sent at the end of every frame's report, in the raw
//! demo coordinates the radar already projects players with.

use bevy::prelude::*;
use serde_json::Value;

use crate::effects::{round_bomb, Bomb};
use crate::state::{sample_pos, RoundState, Viewer};
use crate::util::{along_flight, util_frame};

/// The bomb's tag in the weapon table.
const C4: &str = "\u{1f4a3}";

/// Floats per mark in the frame report: kind, what, x, y, value.
pub const MARK_STRIDE: usize = 5;

/// The marks of this frame, `[kind, what, x, y, value]`, see `Mark`.
#[derive(Resource, Default)]
pub struct RadarOut(pub Vec<[f32; MARK_STRIDE]>);

/// What a mark is. A grenade's `what` is its index in the round's utility, so the page
/// knows its type and radius; the bomb's is its `BombSeen` and its value the carrier.
#[derive(Clone, Copy, PartialEq, Debug)]
#[repr(u8)]
pub enum Mark {
    /// value: nothing
    Flying = 1,
    /// value: how far through its time it is, 0 to 1
    Working = 2,
    /// what: `BombSeen`, value: who carries it, or -1
    Bomb = 3,
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum BombSeen {
    Carried(usize),
    Dropped,
    Planted,
    Defused,
}

impl BombSeen {
    fn code(self) -> f32 {
        match self {
            BombSeen::Carried(_) => 1.0,
            BombSeen::Dropped => 2.0,
            BombSeen::Planted => 3.0,
            BombSeen::Defused => 4.0,
        }
    }
}

/// Who has the bomb from a sample on.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Holder {
    Has(usize),
    /// on the floor, dropped by this player
    DroppedBy(usize),
}

/// Who has the bomb when, in sample order: from the pickups and drops a 64 Hz replay
/// records, and from whoever is seen with it in their hands, which is all an old replay has.
/// The bomb is given out before the replay starts, so the first carrier is only known once
/// they hold it or drop it.
pub fn bomb_timeline(r: &RoundState) -> Vec<(f64, Holder)> {
    let mut out: Vec<(f64, Holder)> = Vec::new();
    let events = r.dec.header.get("events").and_then(Value::as_array).map_or(&[][..], Vec::as_slice);
    for e in events {
        let (Some(s), Some(p)) = (e.get("s").and_then(Value::as_f64), e.get("p").and_then(Value::as_u64)) else { continue };
        match e.get("k").and_then(Value::as_str) {
            Some("bombPickup") => out.push((s, Holder::Has(p as usize))),
            Some("bombDrop") => out.push((s, Holder::DroppedBy(p as usize))),
            _ => {}
        }
    }
    let Some(c4) = r.dec.weapon_table.iter().position(|w| w == C4) else {
        out.sort_by(|a, b| a.0.total_cmp(&b.0));
        return out;
    };
    let mut seen: Option<usize> = None;
    for i in 0..r.n {
        let holder = r.dec.tracks.iter().position(|tr| {
            tr.flags.get(i).is_some_and(|f| f & c2r3::F_ALIVE != 0) && tr.weapon.get(i).is_some_and(|&w| w as usize == c4)
        });
        if let Some(slot) = holder {
            if seen != Some(slot) {
                out.push((i as f64, Holder::Has(slot)));
                seen = Some(slot);
            }
        }
    }
    // stable, so an event and a sighting at the same moment keep the event first
    out.sort_by(|a, b| a.0.total_cmp(&b.0));
    out
}

/// Where the bomb is at `clock` seconds, in raw demo x and y, and how.
pub fn bomb_at(r: &RoundState, timeline: &[(f64, Holder)], planted: Option<&Bomb>, clock: f64) -> Option<([f32; 2], BombSeen)> {
    if let Some(b) = planted.filter(|b| clock >= b.plant) {
        if b.exploded.is_some_and(|t| clock >= t) {
            return None;
        }
        let seen = if b.defused.is_some_and(|t| clock >= t) { BombSeen::Defused } else { BombSeen::Planted };
        return Some(([b.at.x, -b.at.z], seen));
    }
    if r.n == 0 {
        return None;
    }
    let sample = clock * r.rate;
    let (since, holder) = match timeline.iter().rev().find(|(s, _)| *s <= sample) {
        Some(&entry) => entry,
        // before anything is known, whoever turns out to have it had it from the start
        None => match timeline.first()? {
            &(_, Holder::DroppedBy(slot)) | &(_, Holder::Has(slot)) => (0.0, Holder::Has(slot)),
        },
    };
    let (i0, _, _, _) = sample_pos(clock, r.rate, r.n);
    let raw = |slot: usize, i: usize| r.dec.tracks.get(slot).map(|tr| [tr.x[i] as f32, tr.y[i] as f32]);
    match holder {
        Holder::DroppedBy(slot) => {
            let i = (since.round() as usize).min(r.n - 1);
            Some((raw(slot, i)?, BombSeen::Dropped))
        }
        Holder::Has(slot) => {
            let alive = r.dec.tracks.get(slot)?.flags[i0] & c2r3::F_ALIVE != 0;
            // an old replay has no drop when the carrier dies: it falls where they lie
            Some((raw(slot, i0)?, if alive { BombSeen::Carried(slot) } else { BombSeen::Dropped }))
        }
    }
}

/// The bomb's timeline of the round it was made for.
#[derive(Default)]
pub struct BombCache {
    round: Option<(i64, usize, i64)>,
    timeline: Vec<(f64, Holder)>,
}

pub fn radar_marks(viewer: Res<Viewer>, mut out: ResMut<RadarOut>, mut cache: Local<BombCache>) {
    out.0.clear();
    let (Some(r), Some(m)) = (viewer.round.as_ref(), viewer.manifest.as_ref()) else { return };
    let scale = m.scene_scale as f32;
    let sample = viewer.clock * r.rate;
    let raw = |scene: Vec3| [scene.x / scale, -scene.z / scale];
    for (idx, item) in r.utils.iter().enumerate() {
        if let Some(tr) = item.tracer.as_ref().filter(|tr| sample >= tr.tf && sample <= tr.df && tr.df > tr.tf) {
            let [x, y] = raw(along_flight(tr, sample));
            out.0.push([Mark::Flying as u8 as f32, idx as f32, x, y, 0.0]);
            continue;
        }
        let frame = util_frame(item, sample);
        if frame.in_window {
            let [x, y] = raw(item.pos);
            out.0.push([Mark::Working as u8 as f32, idx as f32, x, y, frame.p.clamp(0.0, 1.0) as f32]);
        }
    }
    let key = (r.dec.round, r.n, r.dec.start_tick);
    if cache.round != Some(key) {
        cache.timeline = bomb_timeline(r);
        cache.round = Some(key);
    }
    if let Some(([x, y], seen)) = bomb_at(r, &cache.timeline, round_bomb(r, m).as_ref(), viewer.clock) {
        let carrier = if let BombSeen::Carried(slot) = seen { slot as f32 } else { -1.0 };
        out.0.push([Mark::Bomb as u8 as f32, seen.code(), x, y, carrier]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use c2r3::{DecodedRound, PlayerMeta, Track};
    use serde_json::json;

    const RATE: f64 = 16.0;
    const LIVE: u8 = c2r3::F_PRESENT | c2r3::F_ALIVE;
    const RIFLE: u8 = 0;
    const BOMB: u8 = 1;

    /// A player standing at (x, 0) with a rifle in hand for `n` samples.
    fn track(n: usize, x: f64) -> Track {
        Track {
            x: vec![x; n],
            y: vec![0.0; n],
            z: vec![0.0; n],
            yaw: vec![0.0; n],
            pitch: vec![0.0; n],
            flags: vec![LIVE; n],
            health: vec![100; n],
            weapon: vec![RIFLE; n],
            flash: vec![0; n],
        }
    }

    fn round(tracks: Vec<Track>, header: serde_json::Value) -> RoundState {
        let n = tracks[0].x.len();
        let dec = DecodedRound {
            map: "de_test".into(),
            round: 1,
            tick_rate: 64,
            sample_stride: 4,
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
            weapon_table: vec!["AK".into(), C4.into()],
            utility: Vec::new(),
            tracks,
            header,
        };
        RoundState { dec, n, rate: RATE, duration: (n - 1) as f64 / RATE, utils: Vec::new(), shots: Vec::new(), anim: None }
    }

    #[test]
    fn whoever_is_seen_with_the_bomb_had_it_from_the_start() {
        let (mut a, b) = (track(100, 100.0), track(100, 200.0));
        a.weapon[40..50].fill(BOMB);
        let r = round(vec![b, a], json!({}));
        let tl = bomb_timeline(&r);
        assert_eq!(tl, vec![(40.0, Holder::Has(1))]);
        // before and after holding it, they still carry it
        assert_eq!(bomb_at(&r, &tl, None, 0.0), Some(([100.0, 0.0], BombSeen::Carried(1))));
        assert_eq!(bomb_at(&r, &tl, None, 80.0 / RATE), Some(([100.0, 0.0], BombSeen::Carried(1))));
    }

    #[test]
    fn a_dropped_bomb_lies_where_it_fell_until_picked_up() {
        let (mut a, mut b) = (track(100, 100.0), track(100, 200.0));
        // a walks on after dropping it at x=100
        for (i, x) in a.x.iter_mut().enumerate().skip(30) {
            *x = 100.0 + i as f64;
        }
        b.weapon[70..].fill(BOMB);
        let events = json!({"events": [
            {"k": "bombDrop", "s": 30.0, "p": 0},
            {"k": "bombPickup", "s": 60.0, "p": 1},
        ]});
        let r = round(vec![a, b], events);
        let tl = bomb_timeline(&r);
        assert_eq!(bomb_at(&r, &tl, None, 10.0 / RATE).map(|m| m.1), Some(BombSeen::Carried(0)), "a carried it before the drop");
        let lying = bomb_at(&r, &tl, None, 45.0 / RATE).unwrap();
        assert_eq!(lying, ([130.0, 0.0], BombSeen::Dropped), "where a was when it fell, not where a is now");
        assert_eq!(bomb_at(&r, &tl, None, 65.0 / RATE), Some(([200.0, 0.0], BombSeen::Carried(1))));
    }

    #[test]
    fn an_old_replay_leaves_the_bomb_where_its_carrier_died() {
        let mut a = track(100, 100.0);
        a.weapon[10..20].fill(BOMB);
        a.flags[50..].fill(c2r3::F_PRESENT);
        let r = round(vec![a], json!({}));
        let tl = bomb_timeline(&r);
        assert_eq!(bomb_at(&r, &tl, None, 70.0 / RATE), Some(([100.0, 0.0], BombSeen::Dropped)));
    }

    #[test]
    fn the_planted_bomb_stays_on_the_site_until_it_blows() {
        let mut a = track(200, 100.0);
        a.weapon.fill(BOMB);
        let r = round(vec![a], json!({}));
        let tl = bomb_timeline(&r);
        let bomb = Bomb { plant: 5.0, at: Vec3::new(-560.0, -180.0, 2140.0), defused: None, exploded: Some(9.0) };
        assert_eq!(bomb_at(&r, &tl, Some(&bomb), 4.0).map(|m| m.1), Some(BombSeen::Carried(0)));
        assert_eq!(bomb_at(&r, &tl, Some(&bomb), 6.0), Some(([-560.0, -2140.0], BombSeen::Planted)), "map frame back to demo x, y");
        assert_eq!(bomb_at(&r, &tl, Some(&bomb), 9.5), None, "gone once it exploded");
        let defused = Bomb { exploded: None, defused: Some(8.0), ..bomb };
        assert_eq!(bomb_at(&r, &tl, Some(&defused), 8.5).map(|m| m.1), Some(BombSeen::Defused));
    }

    #[test]
    fn a_round_without_the_bomb_shows_none() {
        let r = round(vec![track(50, 0.0)], json!({}));
        assert_eq!(bomb_at(&r, &bomb_timeline(&r), None, 1.0), None);
    }
}
