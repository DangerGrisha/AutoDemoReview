//! Unit tests for the parser-independent extraction logic, using a fake DemoSource.
//! These mirror the semantics documented in replay3d.py (the Python reference).

use anyhow::Result;
use extract3d::{
    build_flights, build_replay3d, detect_tickrate, flash_byte, match_throw, norm_type,
    round_sample_ticks, weapons::weapon_tag, BlindEvent, DemoSource, DetEvent, GrenadeRow,
    KillEvent, TickRow, F_ALIVE, F_PRESENT,
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

    fn kill_events(&mut self) -> Result<Vec<KillEvent>> {
        Ok(vec![
            KillEvent {
                tick: 1100,
                attacker_name: Some("Player9".into()),
                victim_name: Some("Player10".into()),
                attacker_team: Some(2),
                total_rounds_played: 0,
                is_warmup: false,
            },
            KillEvent {
                tick: 500,
                attacker_name: None,
                victim_name: None,
                attacker_team: None,
                total_rounds_played: 0,
                is_warmup: true, // filtered out
            },
        ])
    }
}

#[test]
fn full_extraction_pipeline() {
    let (models, meta) = build_replay3d(&mut FakeDemo).unwrap();
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
         "throwTick", "throwSample", "throwPos"]
    );

    // models encode + round-trip through the codec cleanly
    let (bytes, clamps) = c2r3::encode_round(m).unwrap();
    assert_eq!(clamps, 0);
    let dec = c2r3::decode_round(&bytes).unwrap();
    assert_eq!(dec.players, m.players);
    assert_eq!(&dec.utility[..], &m.utility[..]);
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

#[test]
fn manifest_shape() {
    let (models, _) = build_replay3d(&mut FakeDemo).unwrap();
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
    let man = extract3d::manifest::build_manifest(&decoded, &kills, &info);

    assert_eq!(man["map"], "de_mirage");
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
    assert_eq!(man["rounds"][1]["kills"].as_array().unwrap().len(), 0);
    // top-level key order
    let keys: Vec<&str> = man.as_object().unwrap().keys().map(|s| s.as_str()).collect();
    assert_eq!(
        keys,
        ["map", "calibration", "sceneScale", "teamColors", "elevationWorldZ", "rounds"]
    );
}
