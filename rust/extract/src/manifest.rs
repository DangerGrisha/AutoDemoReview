//! Port of `build3d_app.py`'s manifest generation: map + calibration + match-wide
//! elevation levels + per-round metadata (nSamples, sampleRate, duration, kill markers).

use std::collections::HashMap;

use c2r3::{DecodedRound, F_ALIVE};
use serde_json::{Map, Number, Value};

use crate::mapscal::MapInfo;
use crate::KillEvent;

/// Keep in sync with the viewer's SCENE_SCALE (single-sourced here for the Rust port).
pub const SCENE_SCALE: f64 = 0.02;

fn jf(v: f64) -> Value {
    Number::from_f64(v).map(Value::Number).unwrap_or(Value::Null)
}

/// Python `round(x, 2)` approximation (see codec's naive_json_bytes note — the values
/// this touches are durations, consumed only by the viewer UI).
fn round2(v: f64) -> f64 {
    (v * 100.0).round_ties_even() / 100.0
}

/// `(file_name, decoded_round)` pairs must be in round order (r01, r02, ...).
/// `winners` is index-aligned with `rounds` ("CT"/"T" side that won each round;
/// see Meta3d::winners). All-None (synthetic fixtures) leaves the manifest
/// byte-identical to the pre-winner format.
pub fn build_manifest(
    rounds: &[(String, DecodedRound)],
    kills: &[KillEvent],
    info: &MapInfo,
    winners: &[Option<String>],
    bombs: &[Option<crate::BombRound>],
) -> Value {
    // round_no (1-based) -> kill rows, warmup filtered
    let mut kills_by_round: HashMap<i64, Vec<&KillEvent>> = HashMap::new();
    for k in kills.iter().filter(|k| !k.is_warmup) {
        kills_by_round.entry(k.total_rounds_played + 1).or_default().push(k);
    }

    let mut elev_bins: HashMap<i64, u64> = HashMap::new();
    let mut rounds_meta = Vec::with_capacity(rounds.len());
    let mut map_name = String::new();

    // Running score with correct halftime/overtime side swaps: "team A" = the
    // sids on CT (team 3) in the first round with a roster; each round, A's
    // CURRENT side comes from that round's own player list (majority vote), so
    // the winner side maps to a stable team identity with no MR-format logic.
    let team_a: std::collections::HashSet<&str> = rounds
        .iter()
        .find(|(_, d)| !d.players.is_empty())
        .map(|(_, d)| {
            d.players
                .iter()
                .filter(|p| p.team == 3)
                .map(|p| p.sid.as_str())
                .collect()
        })
        .unwrap_or_default();
    let (mut wins_a, mut wins_b) = (0i64, 0i64);

    for (ri, (fname, dec)) in rounds.iter().enumerate() {
        map_name = dec.map.clone();
        let (start, stride, rate, n) = (
            dec.start_tick,
            dec.sample_stride,
            dec.sample_rate,
            dec.n_samples,
        );
        // match-wide elevation levels from the DECODED (dequantized) alive samples
        for tr in &dec.tracks {
            for (i, fl) in tr.flags.iter().enumerate() {
                if fl & F_ALIVE != 0 {
                    let bin = (tr.z[i] / 40.0).round_ties_even() as i64 * 40;
                    *elev_bins.entry(bin).or_insert(0) += 1;
                }
            }
        }
        let kmarks: Vec<Value> = kills_by_round
            .get(&dec.round)
            .map(|ks| {
                ks.iter()
                    .map(|k| {
                        let s = (k.tick - start)
                            .div_euclid(stride)
                            .clamp(0, n as i64 - 1);
                        let mut m = Map::new();
                        m.insert("sample".into(), Value::from(s));
                        m.insert(
                            "atk".into(),
                            Value::from(non_empty(k.attacker_name.as_deref(), "world")),
                        );
                        m.insert(
                            "vic".into(),
                            Value::from(non_empty(k.victim_name.as_deref(), "?")),
                        );
                        m.insert("atkTeam".into(), Value::from(k.attacker_team.unwrap_or(0)));
                        // kill-feed enrichment; keys present only when meaningful,
                        // so pre-enrichment sources keep the old mark shape
                        if let Some(w) = k.weapon.as_deref() {
                            let tag = crate::weapons::event_weapon_tag(w);
                            if !tag.is_empty() {
                                m.insert("w".into(), Value::from(tag));
                            }
                        }
                        if k.headshot {
                            m.insert("hs".into(), Value::from(true));
                        }
                        if k.penetrated {
                            m.insert("wb".into(), Value::from(true));
                        }
                        if k.thrusmoke {
                            m.insert("ts".into(), Value::from(true));
                        }
                        if k.attackerblind {
                            m.insert("ab".into(), Value::from(true));
                        }
                        if k.noscope {
                            m.insert("ns".into(), Value::from(true));
                        }
                        Value::Object(m)
                    })
                    .collect()
            })
            .unwrap_or_default();

        let mut rm = Map::new();
        rm.insert("n".into(), Value::from(dec.round));
        rm.insert("file".into(), Value::from(fname.as_str()));
        rm.insert("nSamples".into(), Value::from(n as i64));
        rm.insert("sampleRate".into(), jf(rate));
        rm.insert("startTick".into(), Value::from(start));
        rm.insert(
            "durationS".into(),
            jf(if n > 1 { round2((n as f64 - 1.0) / rate) } else { 0.0 }),
        );
        rm.insert("kills".into(), Value::Array(kmarks));
        // winner + score ENTERING the round, expressed for the sides as seen in
        // this round (what a CS2-style top bar shows while it plays). Emitted
        // only when the source had round_end enrichment.
        if let Some(Some(w)) = winners.get(ri) {
            let (mut on_ct, mut on_t) = (0u32, 0u32);
            for p in &dec.players {
                if team_a.contains(p.sid.as_str()) {
                    match p.team {
                        3 => on_ct += 1,
                        2 => on_t += 1,
                        _ => {}
                    }
                }
            }
            let a_is_ct = on_ct >= on_t;
            let (score_ct, score_t) = if a_is_ct { (wins_a, wins_b) } else { (wins_b, wins_a) };
            rm.insert("winner".into(), Value::from(w.as_str()));
            rm.insert("scoreCT".into(), Value::from(score_ct));
            rm.insert("scoreT".into(), Value::from(score_t));
            if (w == "CT") == a_is_ct {
                wins_a += 1;
            } else {
                wins_b += 1;
            }
        }
        // C4 timeline, in the same sample units as the kill marks. Emitted only
        // for rounds that actually had a plant, so a source without bomb events
        // (or a round that never got one) keeps the old manifest shape exactly.
        if let Some(Some(b)) = bombs.get(ri) {
            let to_sample =
                |t: i64| (t - start).div_euclid(stride).clamp(0, n as i64 - 1);
            let mut bm = Map::new();
            bm.insert("plant".into(), Value::from(to_sample(b.plant_tick)));
            if let Some(site) = b.site.as_deref() {
                bm.insert("site".into(), Value::from(site));
            }
            if let Some(pos) = b.pos {
                bm.insert("pos".into(), Value::Array(pos.iter().map(|&v| jf(v)).collect()));
            }
            if let (Some(t), Some(kind)) = (b.end_tick, b.end_kind) {
                let key = match kind {
                    crate::BombKind::Defused => "defused",
                    _ => "exploded",
                };
                bm.insert(key.into(), Value::from(to_sample(t)));
            }
            rm.insert("bomb".into(), Value::Object(bm));
        }
        rounds_meta.push(Value::Object(rm));
    }

    let total: u64 = elev_bins.values().sum::<u64>().max(1);
    let mut elevation: Vec<i64> = elev_bins
        .iter()
        .filter(|(_, &c)| c as f64 / total as f64 >= 0.01)
        .map(|(&b, _)| b)
        .collect();
    elevation.sort_unstable();

    let mut calibration = Map::new();
    calibration.insert("pos_x".into(), jf(info.pos_x));
    calibration.insert("pos_y".into(), jf(info.pos_y));
    calibration.insert("scale".into(), jf(info.scale));
    calibration.insert("size".into(), Value::from(info.size));

    let mut team_colors = Map::new();
    team_colors.insert("2".into(), Value::from("#f59e0b"));
    team_colors.insert("3".into(), Value::from("#3b82f6"));

    let mut manifest = Map::new();
    manifest.insert("map".into(), Value::from(map_name));
    manifest.insert("radarAsset".into(), Value::from(info.asset));
    manifest.insert("calibration".into(), Value::Object(calibration));
    manifest.insert("sceneScale".into(), jf(SCENE_SCALE));
    manifest.insert("teamColors".into(), Value::Object(team_colors));
    manifest.insert(
        "elevationWorldZ".into(),
        Value::Array(elevation.into_iter().map(Value::from).collect()),
    );
    manifest.insert("rounds".into(), Value::Array(rounds_meta));
    manifest.insert("dataVersion".into(), Value::from(crate::DATA_VERSION));
    Value::Object(manifest)
}

fn non_empty(s: Option<&str>, default: &str) -> String {
    match s {
        Some(v) if !v.is_empty() => v.to_string(),
        _ => default.to_string(),
    }
}
