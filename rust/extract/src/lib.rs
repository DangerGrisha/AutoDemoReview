//! Tick-level 3D replay extraction — Rust port of `src/demoreview/replay3d.py`
//! (branch feature/3d-replay). Consumes a demo through the `DemoSource` trait
//! (implemented by the demoparser2 adapter) and produces `c2r3::RoundModel`s whose
//! encoded bytes should match the Python pipeline's `.3dr` sidecars.
//!
//! Faithfulness notes (all mirror Python semantics exactly unless flagged):
//! - tick rate is DETECTED from `game_time` deltas (median), defaulting to 64 on
//!   any failure — never hardcoded;
//! - sampling: 64 Hz target (the Python original used 16), per-round range
//!   freeze_end..=round_end+3 s, capped before the next round's freeze;
//! - freeze-on-death: geometry always carries the most recent alive coords, and
//!   samples before a late joiner's first alive sample back-fill its FIRST alive
//!   coords (never-alive players carry zeros);
//! - slot order sorts (team, steamid-as-STRING) — string order is part of the format;
//! - utility list is grouped by detonate-event type (smoke, he, flash, molotov,
//!   decoy), NOT sorted by tick, and detonations after the un-padded round_end are
//!   dropped;
//! - grenade entity ids are reused across the match: flights are segmented by
//!   tick gaps and throws matched within the detonation's own round only.
//!
//! Deliberate deviation: where Python would crash on NaN (e.g. `int(round(nan))`),
//! we treat non-finite coordinates as not-alive / 0.0. Real demos never hit this
//! (the Python selfcheck passes), so outputs stay byte-identical.

pub mod dp2;
pub mod manifest;
pub mod mapscal;
pub mod weapons;

use std::collections::HashMap;

use anyhow::Result;
use c2r3::{PlayerMeta, RoundModel, Track};
use serde_json::{Map, Number, Value};

/// One sample per tick on a 64-tick demo.
pub const TARGET_HZ: f64 = 64.0;
/// What the replay files hold. Raise it, together with DATA_VERSION in web/replay3d.py,
/// when extraction starts giving files something older ones lack: a re-upload and
/// tools/rebuild_replays.py then replace older replays. A manifest without the key is 1.
pub const DATA_VERSION: i64 = 2;
pub const PAD_SECONDS: i64 = 3;
pub const FLASH_FULL_S: f64 = 5.1;
/// A >1 s gap in an entity's trajectory (raw ticks, NOT tick-rate scaled) = reused id.
pub const FLIGHT_GAP_TICKS: i64 = 64;
pub const MAX_FLIGHT_SECONDS: f64 = 5.0;
/// A smoke, fire or decoy's end event further away than this belongs to a later grenade
/// that got the same entity id.
pub const MAX_EFFECT_SECONDS: i64 = 30;
/// How far a thinned grenade path may stray from the real one, in world units.
pub const PATH_TOLERANCE: f64 = 1.0;

// flags bitfield (mirrors c2r3 constants; re-exported for convenience)
pub const F_PRESENT: u8 = 0x01;
pub const F_ALIVE: u8 = 0x02;
pub const F_CROUCH: u8 = 0x04;
pub const F_SCOPED: u8 = 0x08;
pub const F_DEFUSING: u8 = 0x10;
pub const F_WALKING: u8 = 0x20;
pub const F_AIRBORNE: u8 = 0x40;

/// Utility gameplay constants — APPROXIMATE, world units / seconds (radius, duration).
pub const UTIL_CONSTANTS: &[(&str, i64, f64)] = &[
    ("smoke", 144, 18.0),
    ("molotov", 150, 7.0),
    ("he", 350, 0.5),
    ("flash", 120, 0.0),
    ("decoy", 0, 15.0),
];

/// Detonate event -> normalized type, in the FIXED iteration order that determines
/// per-round utility list grouping (part of the header bytes).
pub const DET_EVENTS: &[(&str, &str)] = &[
    ("smokegrenade_detonate", "smoke"),
    ("hegrenade_detonate", "he"),
    ("flashbang_detonate", "flash"),
    ("inferno_startburn", "molotov"),
    ("decoy_started", "decoy"),
];

// --------------------------------------------------------------------------- //
// neutral input rows (filled by the demoparser2 adapter)
// --------------------------------------------------------------------------- //

#[derive(Clone, Debug, Default)]
pub struct TickRow {
    pub tick: i64,
    /// steamid64 as a string (string form is part of the format's sort order).
    pub sid: String,
    pub name: Option<String>,
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub z: Option<f64>,
    pub yaw: Option<f64>,
    pub pitch: Option<f64>,
    pub health: Option<i64>,
    pub is_alive: bool,
    pub weapon: Option<String>,
    pub flash_duration: Option<f64>,
    pub ducked: bool,
    pub is_scoped: bool,
    pub is_defusing: bool,
    pub team_num: Option<i64>,
    pub is_walking: bool,
    pub is_airborne: bool,
    pub armor: Option<i64>,
    pub has_helmet: Option<bool>,
    pub has_defuser: Option<bool>,
    pub money: Option<i64>,
    /// Rounds in the held weapon's clip. None for what has no clip, like a knife.
    pub ammo: Option<i64>,
}

#[derive(Clone, Debug)]
pub struct DetEvent {
    pub tick: i64,
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub z: Option<f64>,
    pub sid: String,
    /// The grenade's entity id, which ties a smoke, fire or decoy to its end event.
    pub entity: Option<i64>,
}

/// The real end of a smoke, fire or decoy.
#[derive(Clone, Debug)]
pub struct ExpireEvent {
    pub tick: i64,
    /// Normalized type, as in DET_EVENTS.
    pub gtype: &'static str,
    pub entity: i64,
}

/// What a `PlayerEvent` says happened.
#[derive(Clone, Debug, PartialEq)]
pub enum PlayerEventKind {
    Hurt {
        attacker: Option<String>,
        damage: i64,
        armor_damage: i64,
        hitgroup: Option<String>,
        weapon: Option<String>,
    },
    Reload,
    Zoom,
    PlantStart,
    PlantAbort,
    DefuseStart { kit: bool },
    DefuseAbort,
    BombDrop,
    BombPickup,
    Pickup { item: String },
}

/// One moment of one player, for a round's `events` header key.
#[derive(Clone, Debug)]
pub struct PlayerEvent {
    pub tick: i64,
    pub sid: String,
    pub kind: PlayerEventKind,
}

#[derive(Clone, Debug)]
pub struct BlindEvent {
    pub tick: i64,
    pub sid: String,
    pub blind_duration: f64,
}

#[derive(Clone, Debug)]
pub struct GrenadeRow {
    pub entity_id: i64,
    pub grenade_type: String,
    pub sid: String,
    pub tick: i64,
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub z: Option<f64>,
}

#[derive(Clone, Debug, Default)]
pub struct KillEvent {
    pub tick: i64,
    pub attacker_name: Option<String>,
    pub victim_name: Option<String>,
    pub attacker_team: Option<i64>,
    pub total_rounds_played: i64,
    pub is_warmup: bool,
    // player_death enrichment for the kill feed; defaults = "plain kill"
    /// Event weapon name (internal, e.g. "ak47"); mapped to a display tag by
    /// weapons::event_weapon_tag when building the manifest.
    pub weapon: Option<String>,
    pub headshot: bool,
    /// Wallbang (the event carries a penetration count; true when > 0).
    pub penetrated: bool,
    pub thrusmoke: bool,
    pub attackerblind: bool,
    pub noscope: bool,
}

/// A `weapon_fire` event: tick + shooter steamid. The weapon isn't carried here —
/// it's read from the shooter's own track at build time, so bullet/grenade
/// classification depends only on the (reliable) shooter id, not the event field.
#[derive(Clone, Debug)]
pub struct FireEvent {
    pub tick: i64,
    pub sid: String,
}

/// `round_freeze_end` with the total_rounds_played enrichment (None when the
/// source cannot provide it).
#[derive(Clone, Debug)]
pub struct FreezeEvent {
    pub tick: i64,
    pub total_rounds_played: Option<i64>,
}

/// `round_end` with the winner enrichment ("CT"/"T"; None = artifact/unknown).
#[derive(Clone, Debug)]
pub struct EndEvent {
    pub tick: i64,
    pub winner: Option<String>,
}

/// What happened to the C4. The plant starts the 40 s fuse; the round's bomb
/// story ends with exactly one of Defused / Exploded (or neither, when the CTs
/// were wiped and the round ended on the timer with the bomb still ticking).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BombKind {
    Planted,
    Defused,
    Exploded,
}

/// One C4 lifecycle event. `site` is the bombsite letter when the demo carries
/// it in a form we can read ("A"/"B"), else None — the viewer just omits it.
#[derive(Clone, Debug)]
pub struct BombEvent {
    pub tick: i64,
    pub kind: BombKind,
    pub site: Option<String>,
    /// Where the player stood when it happened. A planted bomb lies at the planter's feet.
    pub pos: Option<[f64; 3]>,
}

/// One round's C4 story in TICKS (the tick -> sample conversion belongs to
/// manifest.rs, which owns each round's start/stride). `end_*` is None when the
/// round ended with the bomb still ticking (CTs wiped, time ran out).
#[derive(Clone, Debug)]
pub struct BombRound {
    pub plant_tick: i64,
    pub site: Option<String>,
    pub pos: Option<[f64; 3]>,
    pub end_tick: Option<i64>,
    pub end_kind: Option<BombKind>,
}

/// The demo access surface (mirrors the demoparser2 calls replay3d.py makes).
pub trait DemoSource {
    fn map_name(&mut self) -> Result<String>;
    /// Ticks of a named event (`round_freeze_end` / `round_end`), in event order.
    fn event_ticks(&mut self, event: &str) -> Result<Vec<i64>>;
    /// `(tick, game_time)` rows for the given ticks (one per player per tick is fine).
    fn game_time_samples(&mut self, ticks: &[i64]) -> Result<Vec<(i64, f64)>>;
    /// Full per-tick prop rows for the given ticks.
    fn tick_rows(&mut self, ticks: &[i64]) -> Result<Vec<TickRow>>;
    fn detonate_events(&mut self, event: &str) -> Result<Vec<DetEvent>>;
    fn blind_events(&mut self) -> Result<Vec<BlindEvent>>;
    fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>>;
    fn kill_events(&mut self) -> Result<Vec<KillEvent>>;
    /// `weapon_fire` events (tick + shooter steamid). Default empty so simple /
    /// test sources need not implement it.
    fn fire_events(&mut self) -> Result<Vec<FireEvent>> {
        Ok(Vec::new())
    }
    /// C4 plant/defuse/explode events. Default empty so simple / test sources
    /// need not implement it (their manifests then carry no `bomb` key).
    fn bomb_events(&mut self) -> Result<Vec<BombEvent>> {
        Ok(Vec::new())
    }
    /// The real ends of smokes, fires and decoys. Default empty: those then keep their
    /// usual duration from UTIL_CONSTANTS.
    fn expire_events(&mut self) -> Result<Vec<ExpireEvent>> {
        Ok(Vec::new())
    }
    /// Damage, reloads, zooms, bomb handling and pickups, in any order. Default empty:
    /// those rounds carry no `events` key.
    fn player_events(&mut self) -> Result<Vec<PlayerEvent>> {
        Ok(Vec::new())
    }
    /// Round events with the fields `segment_rounds` pairs on. The default
    /// falls back to bare ticks (no enrichment) for simple/test sources.
    fn round_events(&mut self) -> Result<(Vec<FreezeEvent>, Vec<EndEvent>)> {
        let freezes = self
            .event_ticks("round_freeze_end")?
            .into_iter()
            .map(|tick| FreezeEvent { tick, total_rounds_played: None })
            .collect();
        let ends = self
            .event_ticks("round_end")?
            .into_iter()
            .map(|tick| EndEvent { tick, winner: None })
            .collect();
        Ok((freezes, ends))
    }
}

#[derive(Clone, Debug)]
pub struct Meta3d {
    pub map: String,
    pub tick_rate: i64,
    pub sample_stride: i64,
    pub sample_rate: f64,
    pub n_rounds: usize,
    pub map_supported: bool,
    /// Per-round winner ("CT"/"T"), index-aligned with the returned rounds.
    /// None for sources without round_end enrichment (synthetic fixtures).
    pub winners: Vec<Option<String>>,
    /// Per-round C4 story, index-aligned with the rounds. None = never planted
    /// (or a source that doesn't report bomb events).
    pub bombs: Vec<Option<BombRound>>,
}

// --------------------------------------------------------------------------- //
// small pure helpers
// --------------------------------------------------------------------------- //

/// Readable name with surrounding whitespace stripped; empty -> "(unnamed)".
/// (Python also strips lone UTF-16 surrogates, which cannot exist in a Rust String —
/// the demoparser FFI boundary already had to resolve them.)
pub fn sanitize_name(name: Option<&str>) -> String {
    let text = name.unwrap_or("").trim();
    if text.is_empty() {
        "(unnamed)".to_string()
    } else {
        text.to_string()
    }
}

/// Normalize a raw grenade/projectile type name. Substring test order matters.
pub fn norm_type(raw: &str) -> Option<&'static str> {
    let t = raw.to_lowercase();
    if t.contains("flash") {
        Some("flash")
    } else if t.contains("smoke") {
        Some("smoke")
    } else if t.contains("molotov") || t.contains("incendiary") || t.contains("inferno") {
        Some("molotov")
    } else if t.contains("decoy") {
        Some("decoy")
    } else if t.contains("he") {
        Some("he")
    } else {
        None
    }
}

/// True for a weapon-table tag that fires a bullet (so it gets a shot tracer).
/// `weapon_fire` also fires for grenades / knife / zeus / c4-plant; those tags
/// (from `weapons::weapon_tag`) are excluded, as is the empty "none" tag.
pub fn is_bullet_weapon_tag(tag: &str) -> bool {
    !matches!(
        tag,
        "" | "HE" | "FLASH" | "SMOKE" | "MOLLY" | "INC" | "DECOY" | "🔪" | "💣"
    )
}

/// flash_duration seconds -> 0..255 byte (255 ~= 5.1 s). Python banker's rounding.
pub fn flash_byte(fd: Option<f64>) -> u8 {
    let fd = fd.unwrap_or(0.0);
    if !(fd > 0.0) {
        return 0;
    }
    let v = (fd.min(FLASH_FULL_S) / FLASH_FULL_S * 255.0).round_ties_even() as i64;
    v.min(255) as u8
}

/// `statistics.median` — sort, middle element (odd) or mean of the two middles (even).
fn median(mut vals: Vec<f64>) -> Option<f64> {
    if vals.is_empty() {
        return None;
    }
    vals.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = vals.len();
    Some(if n % 2 == 1 {
        vals[n / 2]
    } else {
        (vals[n / 2 - 1] + vals[n / 2]) / 2.0
    })
}

/// Native tick rate = round(1 / median per-tick Δgame_time). Defaults to 64 on ANY failure.
pub fn detect_tickrate(source: &mut dyn DemoSource) -> i64 {
    let mut inner = || -> Result<Option<i64>> {
        let fe = source.event_ticks("round_freeze_end")?;
        let t0 = match fe.first() {
            Some(&t) => t,
            None => return Ok(None),
        };
        let ticks: Vec<i64> = (t0..t0 + 96).collect();
        let mut rows = source.game_time_samples(&ticks)?;
        // dedupe by tick (keep first), then sort by tick
        let mut seen = std::collections::HashSet::new();
        rows.retain(|(t, _)| seen.insert(*t));
        rows.sort_by_key(|(t, _)| *t);
        let mut deltas = Vec::new();
        for w in rows.windows(2) {
            let dt = w[1].0 - w[0].0;
            if dt > 0 {
                deltas.push((w[1].1 - w[0].1) / dt as f64);
            }
        }
        if let Some(per_tick) = median(deltas) {
            if per_tick > 0.0 {
                return Ok(Some((1.0 / per_tick).round_ties_even() as i64));
            }
        }
        Ok(None)
    };
    match inner() {
        Ok(Some(rate)) => rate,
        _ => 64,
    }
}

/// Pair freeze-times with round-ends into the real match rounds (port of
/// parsing.py::_segment_rounds). Positional zipping breaks on real demos:
/// knife rounds, match restarts, and winner-less round_end artifacts add
/// unpaired events, so the two streams differ in length and alignment.
///
///   * The first real round begins at the LAST round_freeze_end whose
///     total_rounds_played == 0 (warm-up / knife / pre-restart are dropped).
///   * Each round ends at the first round_end with a real winner (CT/T)
///     after its freeze; winner-less artifacts are skipped.
///
/// Sources without enrichment (all winners None) keep every end — the naive
/// behavior the synthetic test fixtures rely on.
pub fn segment_rounds(freezes: &[FreezeEvent], ends: &[EndEvent]) -> (Vec<i64>, Vec<i64>) {
    let mut fr: Vec<&FreezeEvent> = freezes.iter().collect();
    fr.sort_by_key(|f| f.tick);
    let any_winner = ends.iter().any(|e| e.winner.is_some());
    let mut ve: Vec<&EndEvent> = ends
        .iter()
        .filter(|e| !any_winner || matches!(e.winner.as_deref(), Some("CT") | Some("T")))
        .collect();
    ve.sort_by_key(|e| e.tick);

    let mut start_i = 0;
    for (i, f) in fr.iter().enumerate() {
        if f.total_rounds_played == Some(0) {
            start_i = i;
        }
    }

    let (mut freeze_ticks, mut end_ticks) = (Vec::new(), Vec::new());
    let mut ei = 0;
    for f in &fr[start_i..] {
        while ei < ve.len() && ve[ei].tick <= f.tick {
            ei += 1; // skip ends at/before this freeze
        }
        if ei >= ve.len() {
            break; // last freeze had no matching end
        }
        freeze_ticks.push(f.tick);
        end_ticks.push(ve[ei].tick);
        ei += 1;
    }
    (freeze_ticks, end_ticks)
}

/// Per-round sample tick lists + the sorted union across all rounds.
/// `freeze_ticks`/`end_ticks` must be index-aligned (see segment_rounds).
pub fn round_sample_ticks(
    freeze_ticks: &[i64],
    end_ticks: &[i64],
    stride: i64,
    tickrate: i64,
) -> (Vec<Vec<i64>>, Vec<i64>) {
    let n = end_ticks.len().min(freeze_ticks.len());
    let mut per_round = Vec::with_capacity(n);
    let mut all: std::collections::BTreeSet<i64> = std::collections::BTreeSet::new();
    for i in 0..n {
        let mut pad_end = end_ticks[i] + PAD_SECONDS * tickrate;
        if i + 1 < n {
            pad_end = pad_end.min(freeze_ticks[i + 1] - stride);
        }
        pad_end = pad_end.max(end_ticks[i]);
        let ts: Vec<i64> = (freeze_ticks[i]..=pad_end).step_by(stride as usize).collect();
        all.extend(ts.iter().copied());
        per_round.push(ts);
    }
    (per_round, all.into_iter().collect())
}

// --------------------------------------------------------------------------- //
// throw-trajectory flights
// --------------------------------------------------------------------------- //

#[derive(Clone, Debug, PartialEq)]
pub struct Flight {
    pub entity: i64,
    pub gtype: &'static str,
    pub sid: String,
    pub start: i64,
    pub pos: [f64; 3],
    /// Every recorded point as `[tick, x, y, z]`, from the throw on.
    pub path: Vec<[f64; 4]>,
}

/// Segment projectile trajectories into flights. Entity ids are REUSED across the
/// match, so a new flight starts on id change OR a > FLIGHT_GAP_TICKS gap. Rows whose
/// type cannot be normalized are skipped WITHOUT advancing the id/tick cursor.
pub fn build_flights(mut rows: Vec<GrenadeRow>) -> Vec<Flight> {
    let mut flights = Vec::new();
    rows.retain(|r| {
        r.x.map(f64::is_finite).unwrap_or(false) && r.y.map(f64::is_finite).unwrap_or(false)
    });
    rows.sort_by(|a, b| (a.entity_id, a.tick).cmp(&(b.entity_id, b.tick)));
    let mut last: Option<(i64, i64)> = None; // (eid, tick)
    for row in rows {
        let gtype = match norm_type(&row.grenade_type) {
            Some(t) => t,
            None => continue,
        };
        let new_flight = match last {
            None => true,
            Some((eid, tick)) => row.entity_id != eid || row.tick - tick > FLIGHT_GAP_TICKS,
        };
        let pos = [
            row.x.unwrap(),
            row.y.unwrap(),
            row.z.filter(|v| v.is_finite()).unwrap_or(0.0),
        ];
        if new_flight {
            flights.push(Flight {
                entity: row.entity_id,
                gtype,
                sid: row.sid.clone(),
                start: row.tick,
                pos,
                path: Vec::new(),
            });
        }
        if let Some(flight) = flights.last_mut() {
            flight.path.push([row.tick as f64, pos[0], pos[1], pos[2]]);
        }
        last = Some((row.entity_id, row.tick));
    }
    flights
}

/// Latest same-round flight of this type+thrower that plausibly produced det_tick.
pub fn match_throw<'a>(
    flights: &'a [Flight],
    gtype: &str,
    sid: &str,
    det_tick: i64,
    round_start: i64,
    max_flight: i64,
) -> Option<&'a Flight> {
    let mut best: Option<&Flight> = None;
    for rec in flights {
        if rec.gtype != gtype || rec.sid != sid {
            continue;
        }
        if rec.start < round_start || rec.start > det_tick {
            continue;
        }
        if det_tick - rec.start > max_flight {
            continue;
        }
        if best.map(|b| rec.start > b.start).unwrap_or(true) {
            best = Some(rec);
        }
    }
    best
}

/// The flight that ended in this detonation, by the grenade's own entity id. `match_throw`
/// is the fallback: it guesses from the thrower and the time, which picks the wrong
/// grenade when one player has two in the air.
pub fn flight_of_entity<'a>(
    flights: &'a [Flight],
    entity: i64,
    gtype: &str,
    det_tick: i64,
    round_start: i64,
) -> Option<&'a Flight> {
    flights
        .iter()
        .filter(|f| {
            f.entity == entity && f.gtype == gtype && f.start >= round_start && f.start <= det_tick
        })
        .max_by_key(|f| f.start)
}

/// Keep only the `[tick, x, y, z]` points a path needs: each dropped one lies within
/// `tolerance` of where a straight line between the kept ones puts it at that moment, so
/// a bounce keeps its corner and a resting grenade collapses to two points.
pub fn thin_path(points: &[[f64; 4]], tolerance: f64) -> Vec<[f64; 4]> {
    if points.len() <= 2 {
        return points.to_vec();
    }
    let mut keep = vec![false; points.len()];
    keep[0] = true;
    keep[points.len() - 1] = true;
    let mut spans = vec![(0, points.len() - 1)];
    while let Some((a, b)) = spans.pop() {
        let (pa, pb) = (points[a], points[b]);
        let duration = pb[0] - pa[0];
        let mut worst = None;
        let mut worst_d2 = tolerance * tolerance;
        for (i, p) in points.iter().enumerate().take(b).skip(a + 1) {
            let t = if duration > 0.0 { (p[0] - pa[0]) / duration } else { 0.0 };
            let d2: f64 = (1..4).map(|c| (p[c] - (pa[c] + (pb[c] - pa[c]) * t)).powi(2)).sum();
            if d2 > worst_d2 {
                worst = Some(i);
                worst_d2 = d2;
            }
        }
        if let Some(i) = worst {
            keep[i] = true;
            spans.push((a, i));
            spans.push((i, b));
        }
    }
    points.iter().zip(&keep).filter(|(_, &k)| k).map(|(p, _)| *p).collect()
}

/// A flight up to its detonation for the header: flat `[sample, x, y, z, ...]`, thinned,
/// at a tenth of a unit. None when under two points are left, which says nothing.
fn path_json(flight: &Flight, det_tick: i64, freeze: i64, stride: i64) -> Option<Value> {
    let flown: Vec<[f64; 4]> =
        flight.path.iter().filter(|p| p[0] <= det_tick as f64).copied().collect();
    if flown.len() < 2 {
        return None;
    }
    let tenth = |v: f64| jnum_f((v * 10.0).round() / 10.0);
    let mut flat = Vec::new();
    for p in thin_path(&flown, PATH_TOLERANCE) {
        flat.push(jnum_f((p[0] - freeze as f64) / stride as f64));
        flat.extend([tenth(p[1]), tenth(p[2]), tenth(p[3])]);
    }
    Some(Value::Array(flat))
}

/// How long a smoke, fire or decoy really lasted in seconds, if its end is in the demo.
/// A round can end first, and then there is no end event inside MAX_EFFECT_SECONDS.
pub fn real_duration(
    expires: &[ExpireEvent],
    gtype: &str,
    entity: Option<i64>,
    det_tick: i64,
    tickrate: i64,
) -> Option<f64> {
    let entity = entity?;
    let latest = det_tick + MAX_EFFECT_SECONDS * tickrate;
    expires
        .iter()
        .filter(|e| e.gtype == gtype && e.entity == entity && e.tick > det_tick && e.tick <= latest)
        .map(|e| e.tick)
        .min()
        .map(|end| (end - det_tick) as f64 / tickrate as f64)
}

// --------------------------------------------------------------------------- //
// utility extraction
// --------------------------------------------------------------------------- //

fn jnum_f(v: f64) -> Value {
    Number::from_f64(v).map(Value::Number).unwrap_or(Value::Null)
}

/// Per-round utility event dicts (detonations + throws + flash victims). Key insertion
/// order inside each item is serialized verbatim into the C2R3 header.
#[allow(clippy::too_many_arguments)]
pub fn extract_utility(
    source: &mut dyn DemoSource,
    freeze_ticks: &[i64],
    end_ticks: &[i64],
    stride: i64,
    tickrate: i64,
) -> Vec<Vec<Value>> {
    let n = end_ticks.len();
    let mut per_round: Vec<Vec<Value>> = vec![Vec::new(); n];
    let flights = build_flights(source.grenade_rows().unwrap_or_default());
    let max_flight = (MAX_FLIGHT_SECONDS * tickrate as f64) as i64;
    let expires = source.expire_events().unwrap_or_default();

    // flash victims grouped by detonation tick
    let mut blind_by_tick: HashMap<i64, Vec<Value>> = HashMap::new();
    for b in source.blind_events().unwrap_or_default() {
        let mut m = Map::new();
        m.insert("sid".into(), Value::from(b.sid));
        m.insert("blindDuration".into(), jnum_f(b.blind_duration));
        blind_by_tick.entry(b.tick).or_default().push(Value::Object(m));
    }

    // NOTE: uses the UN-padded round_end, so aftermath-window detonations are dropped.
    let round_of = |tick: i64| -> Option<usize> {
        (0..n).find(|&i| freeze_ticks[i] <= tick && tick <= end_ticks[i])
    };

    for (ev, gtype) in DET_EVENTS {
        let dets = match source.detonate_events(ev) {
            Ok(d) => d,
            Err(_) => continue,
        };
        for row in dets {
            let tick = row.tick;
            let ri = match round_of(tick) {
                Some(i) => i,
                None => continue,
            };
            let (x, y) = match (row.x, row.y) {
                (Some(x), Some(y)) => (x, y),
                _ => continue,
            };
            let z = row.z.filter(|v| v.is_finite()).unwrap_or(0.0);
            let sid = row.sid;
            let entity = row.entity;
            let (_name, radius, duration) = UTIL_CONSTANTS
                .iter()
                .find(|(name, _, _)| name == gtype)
                .copied()
                .unwrap_or(("", 0, 0.0));
            let duration =
                real_duration(&expires, gtype, entity, tick, tickrate).unwrap_or(duration);
            let det_sample = ((tick - freeze_ticks[ri]).div_euclid(stride)).max(0);

            let mut item = Map::new();
            item.insert("type".into(), Value::from(*gtype));
            item.insert("thrower".into(), Value::from(sid.clone()));
            item.insert("detTick".into(), Value::from(tick));
            item.insert("detSample".into(), Value::from(det_sample));
            item.insert("pos".into(), Value::Array(vec![jnum_f(x), jnum_f(y), jnum_f(z)]));
            item.insert("radius".into(), Value::from(radius));
            item.insert("duration".into(), jnum_f(duration));
            // A fire is a new entity, so only the thrower and the time lead back to its
            // molotov. Its id can even match an earlier grenade's.
            let by_entity = entity.filter(|_| *gtype != "molotov").and_then(|id| {
                flight_of_entity(&flights, id, gtype, tick, freeze_ticks[ri])
            });
            let flight = by_entity
                .or_else(|| match_throw(&flights, gtype, &sid, tick, freeze_ticks[ri], max_flight));
            if let Some(flight) = flight {
                item.insert("throwTick".into(), Value::from(flight.start));
                item.insert(
                    "throwSample".into(),
                    Value::from(((flight.start - freeze_ticks[ri]).div_euclid(stride)).max(0)),
                );
                item.insert(
                    "throwPos".into(),
                    Value::Array(flight.pos.iter().map(|&v| jnum_f(v)).collect()),
                );
                if let Some(path) = path_json(flight, tick, freeze_ticks[ri], stride) {
                    item.insert("path".into(), path);
                }
            }
            if *gtype == "flash" {
                if let Some(victims) = blind_by_tick.get(&tick) {
                    if !victims.is_empty() {
                        item.insert("affected".into(), Value::Array(victims.clone()));
                    }
                }
            }
            per_round[ri].push(Value::Object(item));
        }
    }
    per_round
}

// --------------------------------------------------------------------------- //
// per-player events and state
// --------------------------------------------------------------------------- //

/// One record of a round's `events` header key. Key order is part of the format.
fn event_json(ev: &PlayerEvent, s: f64, slot: usize, slot_of: &HashMap<&str, usize>) -> Value {
    let kind = match &ev.kind {
        PlayerEventKind::Hurt { .. } => "hurt",
        PlayerEventKind::Reload => "reload",
        PlayerEventKind::Zoom => "zoom",
        PlayerEventKind::PlantStart => "plant",
        PlayerEventKind::PlantAbort => "plantAbort",
        PlayerEventKind::DefuseStart { .. } => "defuse",
        PlayerEventKind::DefuseAbort => "defuseAbort",
        PlayerEventKind::BombDrop => "bombDrop",
        PlayerEventKind::BombPickup => "bombPickup",
        PlayerEventKind::Pickup { .. } => "pickup",
    };
    let mut m = Map::new();
    m.insert("k".into(), Value::from(kind));
    m.insert("s".into(), jnum_f(s));
    m.insert("p".into(), Value::from(slot as i64));
    match &ev.kind {
        PlayerEventKind::Hurt { attacker, damage, armor_damage, hitgroup, weapon } => {
            // no attacker in the roster: a fall, the bomb or the world
            if let Some(&a) = attacker.as_deref().and_then(|sid| slot_of.get(sid)) {
                m.insert("a".into(), Value::from(a as i64));
            }
            m.insert("d".into(), Value::from(*damage));
            if *armor_damage > 0 {
                m.insert("da".into(), Value::from(*armor_damage));
            }
            if let Some(hg) = hitgroup {
                m.insert("hg".into(), Value::from(hg.as_str()));
            }
            let tag = weapon.as_deref().map(weapons::event_weapon_tag).unwrap_or_default();
            if !tag.is_empty() {
                m.insert("w".into(), Value::from(tag));
            }
        }
        PlayerEventKind::DefuseStart { kit } => {
            m.insert("kit".into(), Value::from(*kit));
        }
        PlayerEventKind::Pickup { item } => {
            m.insert("item".into(), Value::from(item.as_str()));
        }
        _ => {}
    }
    Value::Object(m)
}

/// A flat `[sample, value, sample, value, ...]` list of the moments a value changed.
#[derive(Default)]
struct ChangeList {
    flat: Vec<i64>,
    last: Option<i64>,
}

impl ChangeList {
    fn note(&mut self, sample: usize, value: i64) {
        if self.last != Some(value) {
            self.flat.push(sample as i64);
            self.flat.push(value);
            self.last = Some(value);
        }
    }

    /// False when the demo never gave a value: nothing, or only the "no value" -1.
    fn says_anything(&self) -> bool {
        self.flat.iter().skip(1).step_by(2).any(|&v| v >= 0)
    }

    fn insert_into(self, key: &str, out: &mut Map<String, Value>) {
        if self.says_anything() {
            out.insert(key.into(), Value::Array(self.flat.into_iter().map(Value::from).collect()));
        }
    }
}

// --------------------------------------------------------------------------- //
// main extraction
// --------------------------------------------------------------------------- //

struct WeaponIndexer {
    table: Vec<String>,
    idx: HashMap<String, usize>,
}

impl WeaponIndexer {
    fn new() -> Self {
        // index 0 = none/empty tag, reserved and never assigned
        WeaponIndexer {
            table: vec![String::new()],
            idx: HashMap::from([(String::new(), 0)]),
        }
    }

    fn windex(&mut self, name: Option<&str>) -> u8 {
        let tag = match weapons::weapon_tag(name) {
            Some(t) if !t.is_empty() => t,
            _ => return 255,
        };
        let idx = match self.idx.get(&tag) {
            Some(&i) => i,
            None => {
                let i = self.table.len();
                self.idx.insert(tag.clone(), i);
                self.table.push(tag);
                i
            }
        };
        if idx < 255 {
            idx as u8
        } else {
            255
        }
    }
}

/// Extract all rounds. Mirrors `replay3d.build_replay3d`.
pub fn build_replay3d(source: &mut dyn DemoSource) -> Result<(Vec<RoundModel>, Meta3d)> {
    build_replay3d_hz(source, TARGET_HZ)
}

pub fn build_replay3d_hz(
    source: &mut dyn DemoSource,
    target_hz: f64,
) -> Result<(Vec<RoundModel>, Meta3d)> {
    let map_name = source.map_name()?;
    let tickrate = detect_tickrate(source);
    let stride = ((tickrate as f64 / target_hz).round_ties_even() as i64).max(1);
    let sample_rate = tickrate as f64 / stride as f64;

    let (freezes, ends) = source.round_events()?;
    let (freeze_ticks, end_ticks) = segment_rounds(&freezes, &ends);
    let n_rounds = end_ticks.len();

    let (per_round_ticks, all_ticks) =
        round_sample_ticks(&freeze_ticks, &end_ticks, stride, tickrate);

    let rows = source.tick_rows(&all_ticks)?;

    // freeze tick -> round index, to capture the per-round roster in one pass
    let freeze_to_round: HashMap<i64, usize> = freeze_ticks
        .iter()
        .take(n_rounds)
        .enumerate()
        .map(|(i, &t)| (t, i))
        .collect();

    let mut cell: HashMap<(i64, String), TickRow> = HashMap::new();
    let mut name_by_sid: HashMap<String, String> = HashMap::new();
    let mut roster_by_round: Vec<HashMap<String, i64>> = vec![HashMap::new(); n_rounds];
    for row in rows {
        if !name_by_sid.contains_key(&row.sid) {
            name_by_sid.insert(row.sid.clone(), sanitize_name(row.name.as_deref()));
        }
        if let Some(&ri) = freeze_to_round.get(&row.tick) {
            roster_by_round[ri].insert(row.sid.clone(), row.team_num.unwrap_or(0));
        }
        cell.insert((row.tick, row.sid.clone()), row);
    }

    let utility_by_round =
        extract_utility(source, &freeze_ticks, &end_ticks, stride, tickrate);
    // all weapon_fire events (tick + shooter sid); binned into rounds below.
    let fire_events = source.fire_events().unwrap_or_default();
    let mut player_events = source.player_events().unwrap_or_default();
    player_events.sort_by_key(|e| e.tick);

    let mut models = Vec::with_capacity(n_rounds);
    for i in 0..n_rounds {
        let ts = &per_round_ticks[i];
        let n = ts.len();
        let freeze = freeze_ticks[i];

        // slot order = (team, steamid-as-STRING) — string comparison is part of the format
        let roster = &roster_by_round[i];
        let mut slots: Vec<&String> = roster.keys().collect();
        slots.sort_by(|a, b| (roster[*a], a.as_str()).cmp(&(roster[*b], b.as_str())));

        let mut wi = WeaponIndexer::new();
        let mut tracks = Vec::with_capacity(slots.len());
        let mut players = Vec::with_capacity(slots.len());
        let mut state = Vec::with_capacity(slots.len());

        for (slot, sid) in slots.iter().enumerate() {
            let mut tr = Track {
                x: vec![0.0; n],
                y: vec![0.0; n],
                z: vec![0.0; n],
                yaw: vec![0.0; n],
                pitch: vec![0.0; n],
                flags: vec![0; n],
                health: vec![0; n],
                weapon: vec![255; n],
                flash: vec![0; n],
            };

            // first pass: find the first alive coords (for pre-appearance back-fill)
            let mut first_coords: Option<(f64, f64, f64, f64, f64)> = None;
            for &tk in ts {
                if let Some(c) = cell.get(&(tk, (*sid).clone())) {
                    if row_alive(c) {
                        first_coords = Some(row_coords(c));
                        break;
                    }
                }
            }

            let mut last = first_coords.unwrap_or((0.0, 0.0, 0.0, 0.0, 0.0));
            let (mut armor, mut helmet, mut kit, mut money, mut ammo) = (
                ChangeList::default(),
                ChangeList::default(),
                ChangeList::default(),
                ChangeList::default(),
                ChangeList::default(),
            );
            for (j, &tk) in ts.iter().enumerate() {
                let row = cell.get(&(tk, (*sid).clone()));
                // money keeps changing after death: the round's reward arrives then
                if let Some(v) = row.and_then(|c| c.money) {
                    money.note(j, v);
                }
                match row {
                    Some(c) if row_alive(c) => {
                        last = row_coords(c);
                        let mut fl = F_PRESENT | F_ALIVE;
                        if c.ducked {
                            fl |= F_CROUCH;
                        }
                        if c.is_scoped {
                            fl |= F_SCOPED;
                        }
                        if c.is_defusing {
                            fl |= F_DEFUSING;
                        }
                        if c.is_walking {
                            fl |= F_WALKING;
                        }
                        if c.is_airborne {
                            fl |= F_AIRBORNE;
                        }
                        tr.flags[j] = fl;
                        tr.health[j] = c.health.unwrap_or(0).clamp(0, 255) as u8;
                        tr.weapon[j] = wi.windex(c.weapon.as_deref());
                        tr.flash[j] = flash_byte(c.flash_duration);
                        if let Some(v) = c.armor {
                            armor.note(j, v);
                        }
                        if let Some(v) = c.has_helmet {
                            helmet.note(j, v as i64);
                        }
                        if let Some(v) = c.has_defuser {
                            kit.note(j, v as i64);
                        }
                        ammo.note(j, c.ammo.unwrap_or(-1));
                    }
                    Some(_) => {
                        tr.flags[j] = F_PRESENT; // present but dead
                    }
                    None => {} // absent: flags 0
                }
                tr.x[j] = last.0;
                tr.y[j] = last.1;
                tr.z[j] = last.2;
                tr.yaw[j] = last.3;
                tr.pitch[j] = last.4;
            }

            let mut values = Map::new();
            armor.insert_into("armor", &mut values);
            helmet.insert_into("helmet", &mut values);
            kit.insert_into("kit", &mut values);
            money.insert_into("money", &mut values);
            ammo.insert_into("ammo", &mut values);
            state.push(values);

            tracks.push(tr);
            players.push(PlayerMeta {
                slot: slot as i64,
                sid: (*sid).clone(),
                name: name_by_sid
                    .get(*sid)
                    .cloned()
                    .unwrap_or_else(|| "(unnamed)".to_string()),
                team: roster[*sid],
            });
        }

        // shot tracers: weapon_fire events in this round -> (fractional sample,
        // slot), keeping only bullet weapons (the shooter's held weapon at that
        // sample, read from the track — so we never rely on the event's weapon
        // field). The viewer reconstructs the muzzle ray from the shooter's pose.
        let sid_to_slot: HashMap<&str, usize> =
            slots.iter().enumerate().map(|(k, s)| (s.as_str(), k)).collect();
        let mut shots: Vec<Value> = Vec::new();
        for fe in &fire_events {
            if fe.tick < freeze || fe.tick > end_ticks[i] {
                continue;
            }
            let slot = match sid_to_slot.get(fe.sid.as_str()) {
                Some(&s) => s,
                None => continue,
            };
            let s = (fe.tick - freeze) as f64 / stride as f64;
            let samp = (s.floor().max(0.0) as usize).min(n.saturating_sub(1));
            let widx = tracks[slot].weapon.get(samp).copied().unwrap_or(255) as usize;
            let tag = wi.table.get(widx).map(String::as_str).unwrap_or("");
            if !is_bullet_weapon_tag(tag) {
                continue;
            }
            let mut m = Map::new();
            m.insert("s".into(), jnum_f(s));
            m.insert("p".into(), Value::from(slot as i64));
            shots.push(Value::Object(m));
        }

        // Up to the last sample, not to round_end: players still walk, reload and hurt
        // each other during the aftermath the replay shows.
        let last_tick = ts.last().copied().unwrap_or(freeze);
        let events: Vec<Value> = player_events
            .iter()
            .filter(|e| e.tick >= freeze && e.tick <= last_tick)
            .filter_map(|e| {
                let slot = *sid_to_slot.get(e.sid.as_str())?;
                let s = (e.tick - freeze) as f64 / stride as f64;
                Some(event_json(e, s, slot, &sid_to_slot))
            })
            .collect();
        let state: Vec<Value> = if state.iter().any(|values| !values.is_empty()) {
            state.into_iter().map(Value::Object).collect()
        } else {
            Vec::new()
        };

        models.push(RoundModel {
            map: map_name.clone(),
            round: (i + 1) as i64,
            tick_rate: tickrate,
            sample_stride: stride,
            sample_rate,
            n_samples: n,
            start_tick: freeze,
            end_tick: end_ticks[i],
            players,
            weapon_table: wi.table,
            utility: utility_by_round[i].clone(),
            tracks,
            shots,
            events,
            state,
        });
    }

    // Per-round winner, looked up by the round's end tick (segment_rounds kept
    // only real-winner ends when the source is enriched, so the tick is unique).
    let winner_by_tick: HashMap<i64, &str> = ends
        .iter()
        .filter_map(|e| e.winner.as_deref().map(|w| (e.tick, w)))
        .collect();
    let winners: Vec<Option<String>> = end_ticks
        .iter()
        .map(|t| winner_by_tick.get(t).map(|w| w.to_string()))
        .collect();

    // Per-round C4 story: the first plant inside the round, then the defuse or
    // explosion that closes it. The closing event is allowed slightly past the
    // round_end tick — an explosion IS what ends the round, and the two land on
    // neighbouring ticks in some demos.
    let bomb_events = source.bomb_events().unwrap_or_default();
    let bombs: Vec<Option<BombRound>> = (0..n_rounds)
        .map(|i| {
            let (start, end) = (freeze_ticks[i], end_ticks[i]);
            let plant = bomb_events
                .iter()
                .find(|b| b.kind == BombKind::Planted && b.tick >= start && b.tick <= end)?;
            let closing = bomb_events.iter().find(|b| {
                matches!(b.kind, BombKind::Defused | BombKind::Exploded)
                    && b.tick >= plant.tick
                    && b.tick <= end + 2 * tickrate
            });
            Some(BombRound {
                plant_tick: plant.tick,
                site: plant.site.clone(),
                pos: plant.pos,
                end_tick: closing.map(|b| b.tick),
                end_kind: closing.map(|b| b.kind),
            })
        })
        .collect();

    let meta = Meta3d {
        map: map_name.clone(),
        tick_rate: tickrate,
        sample_stride: stride,
        sample_rate,
        n_rounds,
        map_supported: mapscal::map_info(&map_name).is_some(),
        winners,
        bombs,
    };
    Ok((models, meta))
}

/// Python: `alive = bool(c.is_alive) and c.X is not None and c.Y is not None`
/// (+ the deliberate NaN-is-not-alive strengthening).
fn row_alive(c: &TickRow) -> bool {
    c.is_alive
        && c.x.map(f64::is_finite).unwrap_or(false)
        && c.y.map(f64::is_finite).unwrap_or(false)
}

/// Python: `(float(c.X), float(c.Y), float(c.Z or 0.0), float(c.yaw or 0.0), float(c.pitch or 0.0))`
fn row_coords(c: &TickRow) -> (f64, f64, f64, f64, f64) {
    (
        c.x.unwrap_or(0.0),
        c.y.unwrap_or(0.0),
        c.z.filter(|v| v.is_finite()).unwrap_or(0.0),
        c.yaw.filter(|v| v.is_finite()).unwrap_or(0.0),
        c.pitch.filter(|v| v.is_finite()).unwrap_or(0.0),
    )
}
