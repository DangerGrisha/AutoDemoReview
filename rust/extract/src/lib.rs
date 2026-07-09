//! Tick-level 3D replay extraction — Rust port of `src/demoreview/replay3d.py`
//! (branch feature/3d-replay). Consumes a demo through the `DemoSource` trait
//! (implemented by the demoparser2 adapter) and produces `c2r3::RoundModel`s whose
//! encoded bytes should match the Python pipeline's `.3dr` sidecars.
//!
//! Faithfulness notes (all mirror Python semantics exactly unless flagged):
//! - tick rate is DETECTED from `game_time` deltas (median), defaulting to 64 on
//!   any failure — never hardcoded;
//! - sampling: 16 Hz target, per-round range freeze_end..=round_end+3 s, capped
//!   before the next round's freeze;
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

pub const TARGET_HZ: f64 = 16.0;
pub const PAD_SECONDS: i64 = 3;
pub const FLASH_FULL_S: f64 = 5.1;
/// A >1 s gap in an entity's trajectory (raw ticks, NOT tick-rate scaled) = reused id.
pub const FLIGHT_GAP_TICKS: i64 = 64;
pub const MAX_FLIGHT_SECONDS: f64 = 5.0;

// flags bitfield (mirrors c2r3 constants; re-exported for convenience)
pub const F_PRESENT: u8 = 0x01;
pub const F_ALIVE: u8 = 0x02;
pub const F_CROUCH: u8 = 0x04;
pub const F_SCOPED: u8 = 0x08;
pub const F_DEFUSING: u8 = 0x10;

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
}

#[derive(Clone, Debug)]
pub struct DetEvent {
    pub tick: i64,
    pub x: Option<f64>,
    pub y: Option<f64>,
    pub z: Option<f64>,
    pub sid: String,
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

#[derive(Clone, Debug)]
pub struct KillEvent {
    pub tick: i64,
    pub attacker_name: Option<String>,
    pub victim_name: Option<String>,
    pub attacker_team: Option<i64>,
    pub total_rounds_played: i64,
    pub is_warmup: bool,
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
}

#[derive(Clone, Debug)]
pub struct Meta3d {
    pub map: String,
    pub tick_rate: i64,
    pub sample_stride: i64,
    pub sample_rate: f64,
    pub n_rounds: usize,
    pub map_supported: bool,
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

/// Per-round sample tick lists + the sorted union across all rounds.
pub fn round_sample_ticks(
    freeze_ticks: &[i64],
    end_ticks: &[i64],
    stride: i64,
    tickrate: i64,
) -> (Vec<Vec<i64>>, Vec<i64>) {
    let n = end_ticks.len();
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
    pub gtype: &'static str,
    pub sid: String,
    pub start: i64,
    pub pos: [f64; 3],
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
        if new_flight {
            flights.push(Flight {
                gtype,
                sid: row.sid.clone(),
                start: row.tick,
                pos: [
                    row.x.unwrap(),
                    row.y.unwrap(),
                    row.z.filter(|v| v.is_finite()).unwrap_or(0.0),
                ],
            });
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
            let (_name, radius, duration) = UTIL_CONSTANTS
                .iter()
                .find(|(name, _, _)| name == gtype)
                .copied()
                .unwrap_or(("", 0, 0.0));
            let det_sample = ((tick - freeze_ticks[ri]).div_euclid(stride)).max(0);

            let mut item = Map::new();
            item.insert("type".into(), Value::from(*gtype));
            item.insert("thrower".into(), Value::from(sid.clone()));
            item.insert("detTick".into(), Value::from(tick));
            item.insert("detSample".into(), Value::from(det_sample));
            item.insert("pos".into(), Value::Array(vec![jnum_f(x), jnum_f(y), jnum_f(z)]));
            item.insert("radius".into(), Value::from(radius));
            item.insert("duration".into(), jnum_f(duration));
            if let Some(flight) =
                match_throw(&flights, gtype, &sid, tick, freeze_ticks[ri], max_flight)
            {
                item.insert("throwTick".into(), Value::from(flight.start));
                item.insert(
                    "throwSample".into(),
                    Value::from(((flight.start - freeze_ticks[ri]).div_euclid(stride)).max(0)),
                );
                item.insert(
                    "throwPos".into(),
                    Value::Array(flight.pos.iter().map(|&v| jnum_f(v)).collect()),
                );
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

    let freeze_ticks = source.event_ticks("round_freeze_end")?;
    let end_ticks = source.event_ticks("round_end")?;
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
            for (j, &tk) in ts.iter().enumerate() {
                match cell.get(&(tk, (*sid).clone())) {
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
                        tr.flags[j] = fl;
                        tr.health[j] = c.health.unwrap_or(0).clamp(0, 255) as u8;
                        tr.weapon[j] = wi.windex(c.weapon.as_deref());
                        tr.flash[j] = flash_byte(c.flash_duration);
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
        });
    }

    let meta = Meta3d {
        map: map_name.clone(),
        tick_rate: tickrate,
        sample_stride: stride,
        sample_rate,
        n_rounds,
        map_supported: mapscal::map_info(&map_name).is_some(),
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
