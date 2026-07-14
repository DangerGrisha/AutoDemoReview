//! DemoSource adapter over demoparser2's Rust core (vendored crate `parser`, v0.41.3).
//! Call patterns are copied from the working probe / the Python binding's src/lib.rs so
//! semantics match the Python wheel exactly (same props, same event enrichment).

use ahash::AHashMap;
use anyhow::{anyhow, Context, Result};
use parser::first_pass::parser_settings::{
    rm_user_friendly_names, FirstPassParser, ParserInputs,
};
use parser::first_pass::prop_controller::{
    ENTITY_ID_ID, GRENADE_TYPE_ID, GRENADE_X, GRENADE_Y, GRENADE_Z, NAME_ID, STEAMID_ID, TICK_ID,
};
use parser::parse_demo::{DemoOutput, Parser, ParsingMode};
use parser::second_pass::game_events::GameEvent;
use parser::second_pass::parser_settings::create_huffman_lookup_table;
use parser::second_pass::variants::{PropColumn, VarVec, Variant};

use crate::{BlindEvent, DemoSource, DetEvent, GrenadeRow, KillEvent, TickRow};

/// The per-tick props the extraction samples (mirrors replay3d.TICK_PROPS; note
/// `ducked` forces the parser single-threaded — accepted, it costs ~1 s total).
const TICK_PROPS: [&str; 13] = [
    "X", "Y", "Z", "pitch", "yaw", "health", "is_alive", "active_weapon_name",
    "flash_duration", "ducked", "is_scoped", "is_defusing", "team_num",
];

const EVENTS: [&str; 9] = [
    "round_freeze_end",
    "round_end",
    "player_death",
    "player_blind",
    "smokegrenade_detonate",
    "hegrenade_detonate",
    "flashbang_detonate",
    "inferno_startburn",
    "decoy_started",
];

pub struct Dp2Source {
    bytes: Vec<u8>,
    huf: Vec<(u8, u8)>,
    events: Option<Vec<GameEvent>>,
}

impl Dp2Source {
    pub fn new(demo_path: &str) -> Result<Self> {
        let bytes = std::fs::read(demo_path).with_context(|| format!("reading {demo_path}"))?;
        Ok(Dp2Source {
            bytes,
            huf: create_huffman_lookup_table(),
            events: None,
        })
    }

    fn base_inputs(&self) -> ParserInputs<'_> {
        ParserInputs {
            real_name_to_og_name: AHashMap::default(),
            wanted_players: vec![],
            wanted_player_props: vec![],
            wanted_other_props: vec![],
            wanted_prop_states: AHashMap::default(),
            wanted_ticks: vec![],
            wanted_events: vec![],
            parse_ents: false,
            parse_projectiles: false,
            parse_grenades: false,
            only_header: false,
            only_convars: false,
            huffman_lookup_table: &self.huf,
            order_by_steamid: false,
            list_props: false,
            fallback_bytes: None,
        }
    }

    /// One combined event pass (all 9 types + team/round enrichment), cached.
    fn events(&mut self) -> Result<&[GameEvent]> {
        if self.events.is_none() {
            let (real_player, mut real_to_og) = map_props(&["team_num"])?;
            let (real_other, r2) = map_props(&["total_rounds_played", "is_warmup_period"])?;
            real_to_og.extend(r2);
            let mut settings = self.base_inputs();
            settings.real_name_to_og_name = real_to_og;
            settings.wanted_player_props = real_player;
            settings.wanted_other_props = real_other;
            settings.wanted_events = EVENTS.iter().map(|s| s.to_string()).collect();
            settings.parse_ents = true;
            settings.only_header = true; // Python binding sets this too
            let mut p = Parser::new(settings, ParsingMode::Normal);
            let out = p.parse_demo(&self.bytes).map_err(|e| anyhow!("{e}"))?;
            self.events = Some(out.game_events);
        }
        Ok(self.events.as_deref().unwrap())
    }

    fn tick_parse(&self, friendly_props: &[&str], ticks: &[i64]) -> Result<DemoOutput> {
        let (real_props, real_to_og) = map_props(friendly_props)?;
        let mut settings = self.base_inputs();
        settings.real_name_to_og_name = real_to_og;
        settings.wanted_player_props = real_props;
        settings.wanted_ticks = ticks.iter().map(|&t| t as i32).collect();
        settings.parse_ents = true;
        settings.only_header = true;
        let mut p = Parser::new(settings, ParsingMode::Normal);
        p.parse_demo(&self.bytes).map_err(|e| anyhow!("{e}"))
    }
}

fn map_props(friendly: &[&str]) -> Result<(Vec<String>, AHashMap<String, String>)> {
    let friendly: Vec<String> = friendly.iter().map(|s| s.to_string()).collect();
    let real = rm_user_friendly_names(&friendly).map_err(|e| anyhow!("{e}"))?;
    let mut real_to_og = AHashMap::default();
    for (r, f) in real.iter().zip(&friendly) {
        real_to_og.insert(r.clone(), f.clone());
    }
    Ok((real, real_to_og))
}

// ---- event field accessors --------------------------------------------------

fn field<'a>(e: &'a GameEvent, name: &str) -> Option<&'a Variant> {
    e.fields.iter().find(|f| f.name == name).and_then(|f| f.data.as_ref())
}

fn v_f64(v: Option<&Variant>) -> Option<f64> {
    match v {
        Some(Variant::F32(x)) => Some(*x as f64),
        Some(Variant::I32(x)) => Some(*x as f64),
        Some(Variant::U32(x)) => Some(*x as f64),
        _ => None,
    }
}

fn v_i64(v: Option<&Variant>) -> Option<i64> {
    match v {
        Some(Variant::I32(x)) => Some(*x as i64),
        Some(Variant::U32(x)) => Some(*x as i64),
        Some(Variant::U64(x)) => Some(*x as i64),
        _ => None,
    }
}

fn v_bool(v: Option<&Variant>) -> Option<bool> {
    match v {
        Some(Variant::Bool(b)) => Some(*b),
        _ => None,
    }
}

fn v_string(v: Option<&Variant>) -> Option<String> {
    match v {
        Some(Variant::String(s)) => Some(s.clone()),
        _ => None,
    }
}

// ---- columnar df accessors ---------------------------------------------------

fn col_f64(col: Option<&PropColumn>, i: usize) -> Option<f64> {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::F32(v)) => v.get(i).copied().flatten().map(|x| x as f64),
        Some(VarVec::I32(v)) => v.get(i).copied().flatten().map(|x| x as f64),
        _ => None,
    }
}

fn col_i64(col: Option<&PropColumn>, i: usize) -> Option<i64> {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::I32(v)) => v.get(i).copied().flatten().map(|x| x as i64),
        Some(VarVec::U32(v)) => v.get(i).copied().flatten().map(|x| x as i64),
        Some(VarVec::U64(v)) => v.get(i).copied().flatten().map(|x| x as i64),
        _ => None,
    }
}

fn col_u64(col: Option<&PropColumn>, i: usize) -> Option<u64> {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::U64(v)) => v.get(i).copied().flatten(),
        _ => None,
    }
}

fn col_bool(col: Option<&PropColumn>, i: usize) -> Option<bool> {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::Bool(v)) => v.get(i).copied().flatten(),
        _ => None,
    }
}

fn col_string(col: Option<&PropColumn>, i: usize) -> Option<String> {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::String(v)) => v.get(i).cloned().flatten(),
        _ => None,
    }
}

fn col_len(col: Option<&PropColumn>) -> usize {
    match col.and_then(|c| c.data.as_ref()) {
        Some(VarVec::F32(v)) => v.len(),
        Some(VarVec::I32(v)) => v.len(),
        Some(VarVec::U32(v)) => v.len(),
        Some(VarVec::U64(v)) => v.len(),
        Some(VarVec::Bool(v)) => v.len(),
        Some(VarVec::String(v)) => v.len(),
        _ => 0,
    }
}

impl DemoSource for Dp2Source {
    fn map_name(&mut self) -> Result<String> {
        let mut settings = self.base_inputs();
        settings.only_header = true;
        let mut fp = FirstPassParser::new(&settings);
        let header = fp.parse_header_only(&self.bytes).map_err(|e| anyhow!("{e}"))?;
        header
            .get("map_name")
            .cloned()
            .ok_or_else(|| anyhow!("demo header has no map_name"))
    }

    fn event_ticks(&mut self, event: &str) -> Result<Vec<i64>> {
        Ok(self
            .events()?
            .iter()
            .filter(|e| e.name == event)
            .map(|e| e.tick as i64)
            .collect())
    }

    fn game_time_samples(&mut self, ticks: &[i64]) -> Result<Vec<(i64, f64)>> {
        let out = self.tick_parse(&["game_time"], ticks)?;
        let by_name = |friendly: &str| {
            out.prop_controller
                .prop_infos
                .iter()
                .find(|pi| pi.prop_friendly_name == friendly)
                .and_then(|pi| out.df.get(&pi.id))
        };
        let (c_tick, c_gt) = (out.df.get(&TICK_ID), by_name("game_time"));
        let n = col_len(c_tick);
        let mut rows = Vec::with_capacity(n);
        for i in 0..n {
            if let (Some(t), Some(gt)) = (col_i64(c_tick, i), col_f64(c_gt, i)) {
                rows.push((t, gt));
            }
        }
        Ok(rows)
    }

    fn tick_rows(&mut self, ticks: &[i64]) -> Result<Vec<TickRow>> {
        let out = self.tick_parse(&TICK_PROPS, ticks)?;
        let by_name = |friendly: &str| {
            out.prop_controller
                .prop_infos
                .iter()
                .find(|pi| pi.prop_friendly_name == friendly)
                .and_then(|pi| out.df.get(&pi.id))
        };
        let c_tick = out.df.get(&TICK_ID);
        let c_sid = out.df.get(&STEAMID_ID);
        let c_name = out.df.get(&NAME_ID);
        let (c_x, c_y, c_z) = (by_name("X"), by_name("Y"), by_name("Z"));
        let (c_pitch, c_yaw) = (by_name("pitch"), by_name("yaw"));
        let (c_hp, c_alive) = (by_name("health"), by_name("is_alive"));
        let (c_weap, c_flash) = (by_name("active_weapon_name"), by_name("flash_duration"));
        let (c_duck, c_scope, c_defuse) =
            (by_name("ducked"), by_name("is_scoped"), by_name("is_defusing"));
        let c_team = by_name("team_num");

        let n = col_len(c_tick);
        let mut rows = Vec::with_capacity(n);
        for i in 0..n {
            let tick = match col_i64(c_tick, i) {
                Some(t) => t,
                None => continue,
            };
            let sid = match col_u64(c_sid, i) {
                Some(s) => s.to_string(),
                None => continue,
            };
            rows.push(TickRow {
                tick,
                sid,
                name: col_string(c_name, i),
                x: col_f64(c_x, i),
                y: col_f64(c_y, i),
                z: col_f64(c_z, i),
                yaw: col_f64(c_yaw, i),
                pitch: col_f64(c_pitch, i),
                health: col_i64(c_hp, i),
                is_alive: col_bool(c_alive, i).unwrap_or(false),
                weapon: col_string(c_weap, i),
                flash_duration: col_f64(c_flash, i),
                ducked: col_bool(c_duck, i).unwrap_or(false),
                is_scoped: col_bool(c_scope, i).unwrap_or(false),
                is_defusing: col_bool(c_defuse, i).unwrap_or(false),
                team_num: col_i64(c_team, i),
            });
        }
        Ok(rows)
    }

    fn detonate_events(&mut self, event: &str) -> Result<Vec<DetEvent>> {
        Ok(self
            .events()?
            .iter()
            .filter(|e| e.name == event)
            .map(|e| DetEvent {
                tick: e.tick as i64,
                x: v_f64(field(e, "x")),
                y: v_f64(field(e, "y")),
                z: v_f64(field(e, "z")),
                sid: v_string(field(e, "user_steamid")).unwrap_or_default(),
            })
            .collect())
    }

    fn blind_events(&mut self) -> Result<Vec<BlindEvent>> {
        Ok(self
            .events()?
            .iter()
            .filter(|e| e.name == "player_blind")
            .map(|e| BlindEvent {
                tick: e.tick as i64,
                sid: v_string(field(e, "user_steamid")).unwrap_or_default(),
                blind_duration: v_f64(field(e, "blind_duration")).unwrap_or(0.0),
            })
            .collect())
    }

    fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>> {
        let mut settings = self.base_inputs();
        settings.parse_ents = true;
        settings.parse_projectiles = true; // Python parse_grenades() sets exactly this
        let mut p = Parser::new(settings, ParsingMode::Normal);
        let out = p.parse_demo(&self.bytes).map_err(|e| anyhow!("{e}"))?;
        // NOTE: DemoOutput.projectiles is legacy and empty — data is in out.df under
        // special prop ids, exactly how the Python wheel builds its dataframe.
        let (c_type, c_eid) = (out.df.get(&GRENADE_TYPE_ID), out.df.get(&ENTITY_ID_ID));
        let (c_x, c_y, c_z) = (
            out.df.get(&GRENADE_X),
            out.df.get(&GRENADE_Y),
            out.df.get(&GRENADE_Z),
        );
        let (c_tick, c_sid) = (out.df.get(&TICK_ID), out.df.get(&STEAMID_ID));
        let n = col_len(c_tick);
        let mut rows = Vec::with_capacity(n);
        for i in 0..n {
            let (tick, eid) = match (col_i64(c_tick, i), col_i64(c_eid, i)) {
                (Some(t), Some(e)) => (t, e),
                _ => continue,
            };
            rows.push(GrenadeRow {
                entity_id: eid,
                grenade_type: col_string(c_type, i).unwrap_or_default(),
                sid: col_u64(c_sid, i).map(|s| s.to_string()).unwrap_or_default(),
                tick,
                x: col_f64(c_x, i),
                y: col_f64(c_y, i),
                z: col_f64(c_z, i),
            });
        }
        Ok(rows)
    }

    fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
        Ok(self
            .events()?
            .iter()
            .filter(|e| e.name == "player_death")
            .map(|e| KillEvent {
                tick: e.tick as i64,
                attacker_name: v_string(field(e, "attacker_name")),
                victim_name: v_string(field(e, "user_name")),
                attacker_team: v_i64(field(e, "attacker_team_num")),
                total_rounds_played: v_i64(field(e, "total_rounds_played")).unwrap_or(0),
                is_warmup: v_bool(field(e, "is_warmup_period")).unwrap_or(false),
            })
            .collect())
    }
}
