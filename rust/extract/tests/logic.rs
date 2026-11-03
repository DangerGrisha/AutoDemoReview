//! Unit tests for the parser-independent extraction logic, using a fake DemoSource.
//! These mirror the semantics documented in replay3d.py (the Python reference).

use anyhow::Result;
use extract3d::{
    build_flights, build_replay3d, build_replay3d_hz, detect_tickrate, flash_byte,
    flight_of_entity, match_throw, norm_type, real_duration, round_sample_ticks, segment_rounds,
    thin_path, weapons::weapon_tag, BlindEvent, BombEvent, BombKind, DemoSource, DetEvent,
    EndEvent, ExpireEvent, FreezeEvent, GrenadeRow, KillEvent, PlayerEvent, PlayerEventKind,
    TickRow, F_AIRBORNE, F_ALIVE, F_PRESENT, F_WALKING,
};

// ---------------------------------------------------------------------------
// pure helpers
// ---------------------------------------------------------------------------

#[test]
fn weapon_tags() {
    assert_eq!(weapon_tag(Some("AK-47")).as_deref(), Some("AK"));
    assert_eq!(weapon_tag(Some("Desert Eagle")).as_deref(), Some("DEAG"));
    assert_eq!(weapon_tag(None), None);
    assert_eq!(weapon_tag(Some("")), None);
    assert_eq!(weapon_tag(Some("C4 Explosive")).as_deref(), Some("💣"));
    assert_eq!(weapon_tag(Some("knife_karambit")).as_deref(), Some("🔪"));
    assert_eq!(weapon_tag(Some("Butterfly Knife")).as_deref(), Some("🔪"));
    // unknown: uppercased, truncated to 6 CHARS
    assert_eq!(weapon_tag(Some("mystery blaster")).as_deref(), Some("MYSTER"));
}

#[test]
fn flash_bytes() {
    assert_eq!(flash_byte(None), 0);
    assert_eq!(flash_byte(Some(0.0)), 0);
    assert_eq!(flash_byte(Some(-1.0)), 0);
    assert_eq!(flash_byte(Some(5.1)), 255);
    assert_eq!(flash_byte(Some(99.0)), 255); // capped
    assert_eq!(flash_byte(Some(2.55)), 128); // 127.5 rounds half-to-even -> 128
}

#[test]
fn norm_type_order() {
    assert_eq!(norm_type("flashbang_projectile"), Some("flash"));
    assert_eq!(norm_type("smokegrenade"), Some("smoke"));
    assert_eq!(norm_type("molotov"), Some("molotov"));
    assert_eq!(norm_type("incendiary grenade"), Some("molotov"));
    assert_eq!(norm_type("inferno"), Some("molotov"));
    assert_eq!(norm_type("decoy"), Some("decoy"));
    assert_eq!(norm_type("hegrenade"), Some("he"));
    assert_eq!(norm_type("shell"), Some("he")); // substring "he" — mirrors Python
    assert_eq!(norm_type("unknown_zz"), None);
}

#[test]
fn sample_tick_windows() {
    // round 0 pad capped before round 1's freeze; round 1 pads the full 3 s
    let (per_round, all) = round_sample_ticks(&[1000, 1300], &[1160, 2000], 4, 64);
    assert_eq!(per_round[0].first(), Some(&1000));
    // pad_end = min(1160 + 192, 1300 - 4) = 1296, inclusive
    assert_eq!(per_round[0].last(), Some(&1296));
    assert_eq!(per_round[1].first(), Some(&1300));
    assert_eq!(per_round[1].last(), Some(&2192)); // 2000 + 192
    assert!(all.windows(2).all(|w| w[0] < w[1]), "union sorted+deduped");
}

fn grow(entity_id: i64, gtype: &str, sid: &str, tick: i64, x: f64) -> GrenadeRow {
    GrenadeRow {
        entity_id,
        grenade_type: gtype.into(),
        sid: sid.into(),
        tick,
        x: Some(x),
        y: Some(0.0),
        z: Some(10.0),
    }
}

#[test]
fn flight_segmentation() {
    let rows = vec![
        // flight 1: entity 5, ticks 100..132
        grow(5, "smoke", "9", 100, 1.0),
        grow(5, "smoke", "9", 116, 2.0),
        grow(5, "smoke", "9", 132, 3.0),
        // unknown-type row must NOT advance the cursor (same eid, later tick)
        grow(5, "chicken_wat", "9", 140, 9.0),
        // gap 164 - 132 = 32 <= 64: SAME flight even though the unknown row intervened
        grow(5, "smoke", "9", 164, 4.0),
        // entity 5 REUSED after a big gap -> new flight
        grow(5, "smoke", "9", 400, 5.0),
        // different entity -> new flight
        grow(7, "flashbang", "10", 120, 6.0),
    ];
    let flights = build_flights(rows);
    assert_eq!(flights.len(), 3, "{flights:?}");
    assert_eq!((flights[0].start, flights[0].pos[0]), (100, 1.0));
    let path_ticks: Vec<f64> = flights[0].path.iter().map(|p| p[0]).collect();
    assert_eq!(path_ticks, [100.0, 116.0, 132.0, 164.0], "the whole flight, without the unknown row");
    assert_eq!((flights[1].start, flights[1].pos[0]), (400, 5.0));
    assert_eq!(flights[2].gtype, "flash");
    // NaN coordinates are dropped before segmentation
    let mut nan_row = grow(9, "smoke", "9", 10, 1.0);
    nan_row.x = Some(f64::NAN);
    assert_eq!(build_flights(vec![nan_row]).len(), 0);
}

#[test]
fn throw_matching() {
    let flights = build_flights(vec![
        grow(1, "smoke", "9", 500, 1.0),  // previous round (before round_start)
        grow(2, "smoke", "9", 1100, 2.0), // plausible
        grow(3, "smoke", "9", 1130, 3.0), // plausible, LATER start -> wins
        grow(4, "smoke", "10", 1140, 4.0), // other thrower
        grow(5, "smoke", "9", 1155, 5.0), // starts after det tick
    ]);
    let m = match_throw(&flights, "smoke", "9", 1150, 1000, 320).unwrap();
    assert_eq!(m.start, 1130);
    // airtime cap: detonation 400 ticks after the only candidate start
    assert!(match_throw(&flights, "smoke", "9", 1500, 1000, 320).is_none());
    // same-round constraint
    assert!(match_throw(&flights, "smoke", "9", 990, 980, 320).is_none());
}

fn gpoint(tick: f64, x: f64, z: f64) -> [f64; 4] {
    [tick, x, 0.0, z]
}

#[test]
fn a_thinned_path_keeps_its_bounce_and_drops_the_rest() {
    // falling on a straight line to tick 50, bouncing, then rising on another
    let mut path: Vec<[f64; 4]> = (0..=50).map(|t| gpoint(t as f64, t as f64, 100.0 - t as f64 * 2.0)).collect();
    path.extend((51..=100).map(|t| gpoint(t as f64, t as f64, (t - 50) as f64 * 2.0)));
    let thin = thin_path(&path, 1.0);
    let ticks: Vec<f64> = thin.iter().map(|p| p[0]).collect();
    assert_eq!(ticks, [0.0, 50.0, 100.0]);

    // a grenade resting on the ground needs its two ends and nothing between
    let rest: Vec<[f64; 4]> = (0..200).map(|t| gpoint(t as f64, 5.0, 7.0)).collect();
    assert_eq!(thin_path(&rest, 1.0).len(), 2);

    // standing still is a real deviation from a line through a later move: time counts
    let mut stop: Vec<[f64; 4]> = (0..50).map(|t| gpoint(t as f64, 0.0, 0.0)).collect();
    stop.push(gpoint(50.0, 100.0, 0.0));
    let ticks: Vec<f64> = thin_path(&stop, 1.0).iter().map(|p| p[0]).collect();
    assert_eq!(ticks, [0.0, 49.0, 50.0]);

    // short paths pass through untouched
    assert_eq!(thin_path(&[], 1.0).len(), 0);
    assert_eq!(thin_path(&stop[..2], 1.0).len(), 2);
}

#[test]
fn the_entity_id_finds_the_right_grenade_of_two_in_the_air() {
    let flights = build_flights(vec![
        grow(5, "flashbang", "9", 1000, 1.0),
        grow(6, "flashbang", "9", 1060, 2.0), // thrown later, still flying
    ]);
    // the older one goes off first: the thrower and the time alone pick the wrong one
    let guess = match_throw(&flights, "flash", "9", 1090, 900, 320).unwrap();
    assert_eq!(guess.entity, 6);
    assert_eq!(flight_of_entity(&flights, 5, "flash", 1090, 900).unwrap().entity, 5);

    // no airtime cap: a smoke can roll and rest for longer than any flight
    let slow = build_flights(vec![grow(7, "smoke", "9", 1000, 1.0)]);
    assert!(match_throw(&slow, "smoke", "9", 1500, 900, 320).is_none());
    assert_eq!(flight_of_entity(&slow, 7, "smoke", 1500, 900).unwrap().start, 1000);

    // an id from an earlier round, or of another type, is another grenade
    assert!(flight_of_entity(&slow, 7, "smoke", 1500, 1200).is_none());
    assert!(flight_of_entity(&slow, 7, "flash", 1500, 900).is_none());
}

#[test]
fn a_real_end_gives_a_real_duration() {
    let end = |tick, gtype, entity| ExpireEvent { tick, gtype, entity };
    let ends = [end(2408, "smoke", 5), end(1000, "smoke", 5), end(5000, "smoke", 6), end(1500, "molotov", 5)];
    assert_eq!(real_duration(&ends, "smoke", Some(5), 1000, 64), Some(22.0));
    assert_eq!(real_duration(&ends, "molotov", Some(5), 1000, 64), Some(7.8125));
    // the same id on a much later grenade is not this one's end
    assert_eq!(real_duration(&ends, "smoke", Some(6), 1000, 64), None);
    assert_eq!(real_duration(&ends, "decoy", Some(5), 1000, 64), None, "another type");
    assert_eq!(real_duration(&ends, "smoke", None, 1000, 64), None);
}

// ---------------------------------------------------------------------------
// fake demo source for the full pipeline
// ---------------------------------------------------------------------------

/// 2 rounds at 64-tick. Players: sids "9"/"10" on team 2 (string sort puts "10"
/// first!) and "42" on team 3. In round 1: "9" alive throughout, "10" dies at tick
/// 1100, "42" is in the freeze-tick roster but dead there, absent (no rows) until it
/// spawns alive at tick 1052 — exercising the first-alive back-fill.
struct FakeDemo;

const FREEZE: [i64; 2] = [1000, 2000];
const END: [i64; 2] = [1160, 2160];

impl FakeDemo {
    fn row(&self, tick: i64, sid: &str) -> Option<TickRow> {
        let (team, alive) = match sid {
            "9" => (2, true),
            "10" => (2, tick < 1100 || tick >= FREEZE[1]),
            "42" => {
                let at_freeze = tick == FREEZE[0] || tick == FREEZE[1];
                if tick < 1052 && !at_freeze {
                    return None; // absent entirely (no row)
                }
                (3, !at_freeze || tick >= FREEZE[1])
            }
            _ => return None,
        };
        let base = match sid {
            "9" => 0.0,
            "10" => 300.0,
            _ => 600.0,
        };
        Some(TickRow {
            tick,
            sid: sid.into(),
            name: Some(format!("Player{sid} ")), // trailing space: sanitize must trim
            x: Some(base + (tick - 1000) as f64 * 0.5),
            y: Some(-base - (tick - 1000) as f64 * 0.25),
            z: Some(-100.0),
            yaw: Some(((tick % 360) - 180) as f64),
            pitch: Some(1.5),
            health: Some(if alive { 77 } else { 0 }),
            is_alive: alive,
            weapon: Some(if sid == "10" { "AWP".into() } else { "AK-47".into() }),
            flash_duration: Some(if sid == "42" && tick == 1100 { 2.55 } else { 0.0 }),
            ducked: tick % 8 == 0,
            is_scoped: sid == "10",
            is_defusing: false,
            team_num: Some(team),
            is_walking: sid == "9" && tick < 1040,
            is_airborne: sid == "9" && (1040..1048).contains(&tick),
            armor: Some(if tick < 1100 { 100 } else { 73 }),
            has_helmet: Some(true),
            has_defuser: Some(sid == "42"),
            money: Some(if tick < 1150 { 3400 } else { 3700 }),
            ammo: if sid == "9" && tick >= 1120 { None } else { Some(30 - (tick - 1000) / 40) },
        })
    }
}

impl DemoSource for FakeDemo {
    fn map_name(&mut self) -> Result<String> {
        Ok("de_mirage".into())
    }

    fn event_ticks(&mut self, event: &str) -> Result<Vec<i64>> {
        Ok(match event {
            "round_freeze_end" => FREEZE.to_vec(),
            "round_end" => END.to_vec(),
            _ => vec![],
        })
    }

    fn game_time_samples(&mut self, ticks: &[i64]) -> Result<Vec<(i64, f64)>> {
        // 64-tick: game_time advances 1/64 s per tick; two rows per tick (two players)
        Ok(ticks
            .iter()
            .flat_map(|&t| {
                let gt = t as f64 / 64.0;
                [(t, gt), (t, gt)]
            })
            .collect())
    }

    fn tick_rows(&mut self, ticks: &[i64]) -> Result<Vec<TickRow>> {
        let mut rows = Vec::new();
        for &t in ticks {
            for s in ["9", "10", "42"] {
                if let Some(r) = self.row(t, s) {
                    rows.push(r);
                }
            }
        }
        Ok(rows)
    }

    fn detonate_events(&mut self, event: &str) -> Result<Vec<DetEvent>> {
        let det = |tick, sid: &str| DetEvent {
            tick,
            x: Some(50.0),
            y: Some(60.0),
            z: Some(-90.0),
            sid: sid.into(),
            entity: None,
        };
        Ok(match event {
            // one in-round smoke (matched to a flight) + one during the aftermath pad
            // window (tick > round_end: must be DROPPED)
            "smokegrenade_detonate" => vec![det(1150, "9"), det(1200, "9")],
            "hegrenade_detonate" => vec![det(1120, "10")],
            "flashbang_detonate" => vec![det(1140, "42")],
            _ => vec![],
        })
    }

    fn blind_events(&mut self) -> Result<Vec<BlindEvent>> {
        Ok(vec![
            BlindEvent { tick: 1140, sid: "9".into(), blind_duration: 2.35 },
            BlindEvent { tick: 999, sid: "9".into(), blind_duration: 1.0 }, // wrong tick
        ])
    }

    fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>> {
        Ok(vec![
            grow(11, "smokegrenade_projectile", "9", 1100, 500.0),
            grow(11, "smokegrenade_projectile", "9", 1130, 520.0),
        ])
    }

    fn player_events(&mut self) -> Result<Vec<PlayerEvent>> {
        let ev = |tick, sid: &str, kind| PlayerEvent { tick, sid: sid.into(), kind };
        Ok(vec![
            // out of order on purpose, as the demo's event pass may give them
            ev(1120, "42", PlayerEventKind::Reload),
            ev(1100, "10", PlayerEventKind::Hurt {
                attacker: Some("9".into()),
                damage: 27,
                armor_damage: 4,
                hitgroup: Some("chest".into()),
                weapon: Some("ak47".into()),
            }),
            ev(1104, "9", PlayerEventKind::Hurt {
                attacker: None,
                damage: 3,
                armor_damage: 0,
                hitgroup: Some("generic".into()),
                weapon: None,
            }),
            ev(1160, "42", PlayerEventKind::DefuseStart { kit: true }),
            ev(1200, "9", PlayerEventKind::Pickup { item: "ak47".into() }), // in the aftermath
            ev(999, "9", PlayerEventKind::Zoom),                            // before the round
            ev(1110, "7777", PlayerEventKind::Zoom),                        // not in the roster
        ])
    }

    fn bomb_events(&mut self) -> Result<Vec<BombEvent>> {
        Ok(vec![BombEvent {
            tick: 1130,
            kind: BombKind::Planted,
            site: Some("A".into()),
            pos: Some([1.5, 2.5, 3.5]),
        }])
    }

    fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
        Ok(vec![
            KillEvent {
                tick: 1100,
                attacker_name: Some("Player9".into()),
                victim_name: Some("Player10".into()),
                assister_name: Some("Player8".into()),
                attacker_team: Some(2),
                total_rounds_played: 0,
                is_warmup: false,
                ..Default::default()
            },
            KillEvent {
                tick: 500,
                attacker_name: None,
                victim_name: None,
                attacker_team: None,
                total_rounds_played: 0,
                is_warmup: true, // filtered out
                ..Default::default()
            },
        ])
    }
}

#[test]
fn full_extraction_pipeline() {
    let (models, meta) = build_replay3d_hz(&mut FakeDemo, 16.0).unwrap();
    assert_eq!(meta.tick_rate, 64, "tick rate detected from game_time");
    assert_eq!(meta.sample_stride, 4);
    assert_eq!(meta.sample_rate, 16.0);
    assert_eq!(meta.n_rounds, 2);
    assert!(meta.map_supported);

    let m = &models[0];
    assert_eq!(m.round, 1);
    assert_eq!(m.start_tick, 1000);
    assert_eq!(m.end_tick, 1160);
    // pad: 1160 + 192 capped at freeze[1] - stride = 1996 -> 1352; inclusive range
    assert_eq!(m.n_samples, (1352 - 1000) / 4 + 1);

    // slot order: team 2 before 3, sid as STRING: "10" < "9"
    let sids: Vec<&str> = m.players.iter().map(|p| p.sid.as_str()).collect();
    assert_eq!(sids, ["10", "9", "42"]);
    assert_eq!(m.players[0].team, 2);
    assert_eq!(m.players[2].team, 3);
    // names sanitized (trailing space trimmed)
    assert_eq!(m.players[1].name, "Player9");

    // weaponTable built lazily in slot order: slot 0 ("10") holds AWP first
    assert_eq!(m.weapon_table[0], "");
    assert_eq!(m.weapon_table[1], "AWP");
    assert_eq!(m.weapon_table[2], "AK");

    // "10" (slot 0) dies at tick 1100 = sample 25: alive before, frozen after
    let tr10 = &m.tracks[0];
    let s_death = ((1100 - 1000) / 4) as usize;
    assert_ne!(tr10.flags[s_death - 1] & F_ALIVE, 0);
    assert_eq!(tr10.flags[s_death] & F_ALIVE, 0);
    assert_ne!(tr10.flags[s_death] & F_PRESENT, 0, "present but dead");
    let last_alive_x = tr10.x[s_death - 1];
    assert_eq!(tr10.x[s_death], last_alive_x, "geometry frozen at death");
    assert_eq!(tr10.x[m.n_samples - 1], last_alive_x);
    assert_eq!(tr10.health[s_death], 0);
    assert_eq!(tr10.weapon[s_death], 255);

    // "42" spawns at tick 1052 = sample 13: earlier samples back-fill FIRST alive coords
    let tr42 = &m.tracks[2];
    let s_join = ((1052 - 1000) / 4) as usize;
    assert_eq!(tr42.flags[0], F_PRESENT, "present-but-dead at the freeze snapshot");
    assert_eq!(tr42.flags[1], 0, "absent (no row) before spawn");
    assert_ne!(tr42.flags[s_join] & F_ALIVE, 0);
    assert_eq!(tr42.x[0], tr42.x[s_join], "back-filled with first alive coords");
    // flash byte at tick 1100 (sample 25): 2.55 s -> 128
    assert_eq!(tr42.flash[((1100 - 1000) / 4) as usize], 128);

    // utility: grouped smoke, he, flash (DET_EVENTS order), aftermath smoke dropped
    let types: Vec<&str> = m.utility.iter().map(|u| u["type"].as_str().unwrap()).collect();
    assert_eq!(types, ["smoke", "he", "flash"]);
    let smoke = &m.utility[0];
    assert_eq!(smoke["detTick"], 1150);
    assert_eq!(smoke["detSample"], (1150 - 1000) / 4);
    assert_eq!(smoke["throwTick"], 1100, "matched to the flight's first point");
    assert_eq!(smoke["throwSample"], (1100 - 1000) / 4);
    assert_eq!(smoke["throwPos"][0], 500.0);
    assert_eq!(smoke["radius"], 144);
    let flash = &m.utility[2];
    assert_eq!(flash["affected"][0]["sid"], "9");
    assert_eq!(flash["affected"][0]["blindDuration"], 2.35);
    assert!(flash.get("throwTick").is_none(), "no flight for the flash");
    // item key order is part of the format
    let keys: Vec<&str> = smoke.as_object().unwrap().keys().map(|s| s.as_str()).collect();
    assert_eq!(
        keys,
        ["type", "thrower", "detTick", "detSample", "pos", "radius", "duration",
         "throwTick", "throwSample", "throwPos", "path"]
    );
    // the path in samples: ticks 1100 and 1130 at four ticks a sample
    assert_eq!(smoke["path"], serde_json::json!([25.0, 500.0, 0.0, 10.0, 32.5, 520.0, 0.0, 10.0]));

    // models encode + round-trip through the codec cleanly
    let (bytes, clamps) = c2r3::encode_round(m).unwrap();
    assert_eq!(clamps, 0);
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert_eq!(dec.players, m.players);
    assert_eq!(&dec.utility[..], &m.utility[..]);
}

#[test]
fn a_64_tick_demo_gives_a_sample_for_every_tick() {
    let (models, meta) = build_replay3d(&mut FakeDemo).unwrap();
    assert_eq!((meta.sample_stride, meta.sample_rate), (1, 64.0));
    let m = &models[0];
    assert_eq!(m.n_samples, 1352 - 1000 + 1);
    // marks stay in that round's own samples
    let smoke = &m.utility[0];
    assert_eq!(smoke["detSample"], 150);
    assert_eq!(smoke["path"], serde_json::json!([100.0, 500.0, 0.0, 10.0, 130.0, 520.0, 0.0, 10.0]));
}

#[test]
fn walking_and_jumping_land_in_the_free_flag_bits() {
    let (models, _) = build_replay3d(&mut FakeDemo).unwrap();
    let m = &models[0];
    let slot = m.players.iter().position(|p| p.sid == "9").unwrap();
    let flags = &m.tracks[slot].flags;
    assert_ne!(flags[39] & F_WALKING, 0);
    assert_eq!(flags[40] & F_WALKING, 0);
    assert_ne!(flags[47] & F_AIRBORNE, 0);
    assert_eq!(flags[48] & F_AIRBORNE, 0);
    // and they survive the file
    let (bytes, _) = c2r3::encode_round(m).unwrap();
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert_eq!(&dec.tracks[slot].flags, flags);
}

#[test]
fn rarely_changing_values_become_change_lists() {
    let (models, _) = build_replay3d(&mut FakeDemo).unwrap();
    let m = &models[0];
    assert_eq!(m.state.len(), m.players.len());
    let slot_of = |sid: &str| m.players.iter().position(|p| p.sid == sid).unwrap();

    let nine = &m.state[slot_of("9")];
    assert_eq!(nine["armor"], serde_json::json!([0, 100, 100, 73]));
    assert_eq!(nine["helmet"], serde_json::json!([0, 1]));
    assert_eq!(nine["kit"], serde_json::json!([0, 0]));
    // -1 marks what has no clip, from the moment the knife comes out
    let ammo = nine["ammo"].as_array().unwrap();
    assert_eq!(serde_json::Value::from(ammo[..4].to_vec()), serde_json::json!([0, 30, 40, 29]));
    assert_eq!(serde_json::Value::from(ammo[ammo.len() - 2..].to_vec()), serde_json::json!([120, -1]));

    // money keeps being told after death, armour does not
    let ten = &m.state[slot_of("10")];
    assert_eq!(ten["money"], serde_json::json!([0, 3400, 150, 3700]));
    assert_eq!(ten["armor"], serde_json::json!([0, 100]), "dead at tick 1100");
    assert_eq!(m.state[slot_of("42")]["kit"], serde_json::json!([52, 1]), "first alive at tick 1052");

    let (bytes, _) = c2r3::encode_round(m).unwrap();
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert_eq!(dec.header["state"][slot_of("10")]["money"], ten["money"]);
}

#[test]
fn events_land_in_their_round_in_order() {
    let (models, _) = build_replay3d(&mut FakeDemo).unwrap();
    let m = &models[0];
    let slot_of = |sid: &str| m.players.iter().position(|p| p.sid == sid).unwrap() as i64;
    let kinds: Vec<(&str, f64)> =
        m.events.iter().map(|e| (e["k"].as_str().unwrap(), e["s"].as_f64().unwrap())).collect();
    assert_eq!(
        kinds,
        [("hurt", 100.0), ("hurt", 104.0), ("reload", 120.0), ("defuse", 160.0), ("pickup", 200.0)],
        "sorted, with the aftermath, without what is outside the round or the roster"
    );
    let hurt = &m.events[0];
    assert_eq!(hurt["p"], slot_of("10"));
    assert_eq!(hurt["a"], slot_of("9"));
    assert_eq!(hurt["d"], 27);
    assert_eq!(hurt["da"], 4);
    assert_eq!(hurt["hg"], "chest");
    assert_eq!(hurt["w"], "AK");
    let keys: Vec<&str> = hurt.as_object().unwrap().keys().map(|s| s.as_str()).collect();
    assert_eq!(keys, ["k", "s", "p", "a", "d", "da", "hg", "w"], "key order is part of the format");

    // a fall or the bomb: no attacker, no weapon
    assert!(m.events[1].get("a").is_none());
    assert!(m.events[1].get("da").is_none());
    assert!(m.events[1].get("w").is_none());
    assert_eq!(m.events[3]["kit"], true);
    assert_eq!(m.events[4]["item"], "ak47");

    // the other round had none of it
    assert!(models[1].events.is_empty());
    let (bytes, _) = c2r3::encode_round(m).unwrap();
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert_eq!(dec.header["events"][2]["k"], "reload");
}

/// FakeDemo with what older sources lacked taken away.
struct BareDemo;

impl DemoSource for BareDemo {
    fn map_name(&mut self) -> Result<String> {
        FakeDemo.map_name()
    }
    fn event_ticks(&mut self, event: &str) -> Result<Vec<i64>> {
        FakeDemo.event_ticks(event)
    }
    fn game_time_samples(&mut self, ticks: &[i64]) -> Result<Vec<(i64, f64)>> {
        FakeDemo.game_time_samples(ticks)
    }
    fn tick_rows(&mut self, ticks: &[i64]) -> Result<Vec<TickRow>> {
        let mut rows = FakeDemo.tick_rows(ticks)?;
        for r in &mut rows {
            (r.armor, r.has_helmet, r.has_defuser, r.money, r.ammo) = (None, None, None, None, None);
        }
        Ok(rows)
    }
    fn detonate_events(&mut self, event: &str) -> Result<Vec<DetEvent>> {
        FakeDemo.detonate_events(event)
    }
    fn blind_events(&mut self) -> Result<Vec<BlindEvent>> {
        FakeDemo.blind_events()
    }
    fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>> {
        FakeDemo.grenade_rows()
    }
    fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
        FakeDemo.kill_events()
    }
}

#[test]
fn a_source_without_the_new_data_leaves_no_keys_behind() {
    let (models, meta) = build_replay3d(&mut BareDemo).unwrap();
    assert!(models[0].state.is_empty(), "not even the -1 of an unknown clip");
    assert!(models[0].events.is_empty());
    assert!(meta.bombs[0].is_none());
    let (bytes, _) = c2r3::encode_round(&models[0]).unwrap();
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert!(dec.header.get("state").is_none());
    assert!(dec.header.get("events").is_none());
}

#[test]
fn a_planted_bomb_keeps_where_the_planter_stood() {
    let (_, meta) = build_replay3d(&mut FakeDemo).unwrap();
    assert_eq!(meta.bombs[0].as_ref().unwrap().pos, Some([1.5, 2.5, 3.5]));
    assert!(meta.bombs[1].is_none());
}

#[test]
fn a_smoke_with_a_real_end_lasts_that_long() {
    struct WithEnd;
    impl DemoSource for WithEnd {
        fn map_name(&mut self) -> Result<String> {
            FakeDemo.map_name()
        }
        fn event_ticks(&mut self, event: &str) -> Result<Vec<i64>> {
            FakeDemo.event_ticks(event)
        }
        fn game_time_samples(&mut self, ticks: &[i64]) -> Result<Vec<(i64, f64)>> {
            FakeDemo.game_time_samples(ticks)
        }
        fn tick_rows(&mut self, ticks: &[i64]) -> Result<Vec<TickRow>> {
            FakeDemo.tick_rows(ticks)
        }
        fn detonate_events(&mut self, event: &str) -> Result<Vec<DetEvent>> {
            let mut dets = FakeDemo.detonate_events(event)?;
            for d in &mut dets {
                d.entity = Some(11);
            }
            Ok(dets)
        }
        fn expire_events(&mut self) -> Result<Vec<ExpireEvent>> {
            Ok(vec![ExpireEvent { tick: 1150 + 22 * 64, gtype: "smoke", entity: 11 }])
        }
        fn blind_events(&mut self) -> Result<Vec<BlindEvent>> {
            FakeDemo.blind_events()
        }
        fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>> {
            FakeDemo.grenade_rows()
        }
        fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
            FakeDemo.kill_events()
        }
    }
    let (models, _) = build_replay3d(&mut WithEnd).unwrap();
    let utility = &models[0].utility;
    assert_eq!(utility[0]["duration"], 22.0, "the smoke's real end");
    assert_eq!(utility[1]["duration"], 0.5, "an HE keeps its constant");
}

#[test]
fn tickrate_default_on_failure() {
    struct Broken;
    impl DemoSource for Broken {
        fn map_name(&mut self) -> Result<String> {
            Ok("x".into())
        }
        fn event_ticks(&mut self, _: &str) -> Result<Vec<i64>> {
            anyhow::bail!("no events")
        }
        fn game_time_samples(&mut self, _: &[i64]) -> Result<Vec<(i64, f64)>> {
            anyhow::bail!("nope")
        }
        fn tick_rows(&mut self, _: &[i64]) -> Result<Vec<TickRow>> {
            Ok(vec![])
        }
        fn detonate_events(&mut self, _: &str) -> Result<Vec<DetEvent>> {
            Ok(vec![])
        }
        fn blind_events(&mut self) -> Result<Vec<BlindEvent>> {
            Ok(vec![])
        }
        fn grenade_rows(&mut self) -> Result<Vec<GrenadeRow>> {
            Ok(vec![])
        }
        fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
            Ok(vec![])
        }
    }
    assert_eq!(detect_tickrate(&mut Broken), 64);
}

// ---------------------------------------------------------------------------
// round segmentation (port of parsing.py::_segment_rounds)
// ---------------------------------------------------------------------------

fn fz(tick: i64, trp: Option<i64>) -> FreezeEvent {
    FreezeEvent { tick, total_rounds_played: trp }
}
fn re(tick: i64, winner: Option<&str>) -> EndEvent {
    EndEvent { tick, winner: winner.map(str::to_string) }
}

/// Real-demo shape: a winner-less round_end artifact at match start plus a
/// knife round / restart mean the streams differ in length and alignment.
#[test]
fn segment_rounds_skips_artifacts_and_restarts() {
    let freezes = [
        fz(100, Some(0)),  // knife round (pre-restart)
        fz(1000, Some(0)), // restart -> the real round 1 (LAST trp==0 wins)
        fz(3000, Some(1)),
        fz(5000, Some(2)), // trailing freeze with no matching end
    ];
    let ends = [
        re(50, None),           // winner-less artifact before any real round
        re(900, Some("CT")),    // knife-round end (before the restart freeze)
        re(2500, Some("T")),
        re(2600, None),         // artifact between rounds
        re(4500, Some("CT")),
    ];
    let (ft, et) = segment_rounds(&freezes, &ends);
    assert_eq!(ft, vec![1000, 3000]);
    assert_eq!(et, vec![2500, 4500]);
}

/// Sources without enrichment (default trait impl / synthetic fixtures) keep
/// the old naive pairing when the streams are already well-formed.
#[test]
fn segment_rounds_unenriched_passthrough() {
    let freezes = [fz(1000, None), fz(3000, None)];
    let ends = [re(2500, None), re(4500, None)];
    let (ft, et) = segment_rounds(&freezes, &ends);
    assert_eq!(ft, vec![1000, 3000]);
    assert_eq!(et, vec![2500, 4500]);
}

/// The panic case from demos/match.dem: more round_ends than freezes must not
/// index out of bounds (extra end pairs with no freeze and is dropped).
#[test]
fn segment_rounds_more_ends_than_freezes() {
    let freezes = [fz(1000, Some(0))];
    let ends = [re(500, Some("CT")), re(2500, Some("T"))];
    let (ft, et) = segment_rounds(&freezes, &ends);
    assert_eq!(ft, vec![1000]);
    assert_eq!(et, vec![2500]);
    // downstream must also stay in bounds with the aligned lists
    let (per_round, all) = round_sample_ticks(&ft, &et, 16, 64);
    assert_eq!(per_round.len(), 1);
    assert!(!all.is_empty());
}

#[test]
fn manifest_shape() {
    let (models, _) = build_replay3d_hz(&mut FakeDemo, 16.0).unwrap();
    let decoded: Vec<(String, c2r3::DecodedRound)> = models
        .iter()
        .map(|m| {
            let (bytes, _) = c2r3::encode_round(m).unwrap();
            (format!("r{:02}.3dr", m.round), c2r3::decode_round(&bytes).unwrap())
        })
        .collect();
    let kills = {
        let mut fd = FakeDemo;
        fd.kill_events().unwrap()
    };
    let info = extract3d::mapscal::map_info("de_mirage").unwrap();
    let man = extract3d::manifest::build_manifest(&decoded, &kills, &info, &[], &[]);

    assert_eq!(man["map"], "de_mirage");
    assert_eq!(man["radarAsset"], "de_mirage.png");
    assert_eq!(man["calibration"]["pos_x"], -3230.0);
    assert_eq!(man["sceneScale"], 0.02);
    assert_eq!(man["teamColors"]["2"], "#f59e0b");
    assert_eq!(man["rounds"][0]["file"], "r01.3dr");
    assert_eq!(man["rounds"][0]["n"], 1);
    // kill at tick 1100 round 1: sample (1100-1000)/4 = 25, warmup kill filtered
    let kills0 = man["rounds"][0]["kills"].as_array().unwrap();
    assert_eq!(kills0.len(), 1);
    assert_eq!(kills0[0]["sample"], 25);
    assert_eq!(kills0[0]["atk"], "Player9");
    assert_eq!(kills0[0]["atkTeam"], 2);
    assert_eq!(kills0[0]["as"], "Player8", "who helped, for the scoreboard");
    assert_eq!(man["rounds"][1]["kills"].as_array().unwrap().len(), 0);
    // no winner enrichment -> the pre-winner manifest shape, byte-compatible
    assert!(man["rounds"][0].get("winner").is_none());
    assert!(man["rounds"][0].get("scoreCT").is_none());
    // top-level key order
    let keys: Vec<&str> = man.as_object().unwrap().keys().map(|s| s.as_str()).collect();
    assert_eq!(
        keys,
        ["map", "radarAsset", "calibration", "sceneScale", "teamColors", "elevationWorldZ", "rounds",
         "dataVersion"]
    );
}

/// The C4 timeline: plant/defuse ticks land on the right SAMPLE indices, the
/// key only appears for rounds that had a plant, and an empty `bombs` slice
/// leaves the manifest exactly as it was before the feature existed.
#[test]
fn manifest_bomb_timeline() {
    let (models, _) = build_replay3d_hz(&mut FakeDemo, 16.0).unwrap();
    let decoded: Vec<(String, c2r3::DecodedRound)> = models
        .iter()
        .map(|m| {
            let (bytes, _) = c2r3::encode_round(m).unwrap();
            (format!("r{:02}.3dr", m.round), c2r3::decode_round(&bytes).unwrap())
        })
        .collect();
    let info = extract3d::mapscal::map_info("de_mirage").unwrap();
    let (start, stride) = (decoded[0].1.start_tick, decoded[0].1.sample_stride);

    // round 0: planted then defused; round 1: no plant at all
    let bombs = [
        Some(extract3d::BombRound {
            plant_tick: start + 3 * stride,
            site: Some("B".to_string()),
            pos: Some([-580.5, -2143.5, -179.5]),
            end_tick: Some(start + 7 * stride),
            end_kind: Some(extract3d::BombKind::Defused),
        }),
        None,
    ];
    let man = extract3d::manifest::build_manifest(&decoded, &[], &info, &[], &bombs);

    assert_eq!(man["rounds"][0]["bomb"]["plant"], 3);
    assert_eq!(man["rounds"][0]["bomb"]["site"], "B");
    assert_eq!(man["rounds"][0]["bomb"]["pos"][1], -2143.5);
    assert_eq!(man["rounds"][0]["bomb"]["defused"], 7);
    assert!(man["rounds"][0]["bomb"].get("exploded").is_none());
    assert!(man["rounds"][1].get("bomb").is_none(), "no plant -> no key");

    // no bomb data at all -> byte-identical to the pre-feature manifest
    let without = extract3d::manifest::build_manifest(&decoded, &[], &info, &[], &[]);
    let with_empty = extract3d::manifest::build_manifest(&decoded, &[], &info, &[], &[None, None]);
    assert_eq!(serde_json::to_vec(&without).unwrap(),
               serde_json::to_vec(&with_empty).unwrap());
}

/// Winner + running score entering each round, including a halftime side swap:
/// the score follows the TEAM (sid identity), displayed for the sides the teams
/// hold in that specific round.
#[test]
fn manifest_winner_score() {
    let (models, _) = build_replay3d_hz(&mut FakeDemo, 16.0).unwrap();
    let mut decoded: Vec<(String, c2r3::DecodedRound)> = models
        .iter()
        .map(|m| {
            let (bytes, _) = c2r3::encode_round(m).unwrap();
            (format!("r{:02}.3dr", m.round), c2r3::decode_round(&bytes).unwrap())
        })
        .collect();
    // synthesize a post-halftime round 3: same players, sides flipped
    let mut r3 = decoded[1].1.clone();
    for p in &mut r3.players {
        p.team = if p.team == 3 { 2 } else { 3 };
    }
    decoded.push(("r03.3dr".into(), r3));

    let winners = [Some("T".to_string()), Some("CT".to_string()), Some("CT".to_string())];
    let info = extract3d::mapscal::map_info("de_mirage").unwrap();
    let man = extract3d::manifest::build_manifest(&decoded, &[], &info, &winners, &[]);

    // FakeDemo: "42" is CT -> team A = {"42"}. Score shown is ENTERING the round.
    assert_eq!(man["rounds"][0]["winner"], "T");
    assert_eq!(man["rounds"][0]["scoreCT"], 0);
    assert_eq!(man["rounds"][0]["scoreT"], 0);
    // T (team B) won round 1
    assert_eq!(man["rounds"][1]["scoreCT"], 0);
    assert_eq!(man["rounds"][1]["scoreT"], 1);
    // CT (team A) won round 2 -> 1:1; sides flipped in round 3, so team A's win
    // now sits on the T side of the display and team B's on the CT side
    assert_eq!(man["rounds"][2]["scoreCT"], 1);
    assert_eq!(man["rounds"][2]["scoreT"], 1);
}
