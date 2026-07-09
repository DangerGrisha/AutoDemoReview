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
pub fn build_manifest(
    rounds: &[(String, DecodedRound)],
    kills: &[KillEvent],
    info: &MapInfo,
) -> Value {
    // round_no (1-based) -> kill rows, warmup filtered
    let mut kills_by_round: HashMap<i64, Vec<&KillEvent>> = HashMap::new();
    for k in kills.iter().filter(|k| !k.is_warmup) {
        kills_by_round.entry(k.total_rounds_played + 1).or_default().push(k);
    }

    let mut elev_bins: HashMap<i64, u64> = HashMap::new();
    let mut rounds_meta = Vec::with_capacity(rounds.len());
    let mut map_name = String::new();

    for (fname, dec) in rounds {
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
    manifest.insert("calibration".into(), Value::Object(calibration));
    manifest.insert("sceneScale".into(), jf(SCENE_SCALE));
    manifest.insert("teamColors".into(), Value::Object(team_colors));
    manifest.insert(
        "elevationWorldZ".into(),
        Value::Array(elevation.into_iter().map(Value::from).collect()),
    );
    manifest.insert("rounds".into(), Value::Array(rounds_meta));
    Value::Object(manifest)
}

fn non_empty(s: Option<&str>, default: &str) -> String {
    match s {
        Some(v) if !v.is_empty() => v.to_string(),
        _ => default.to_string(),
    }
}
