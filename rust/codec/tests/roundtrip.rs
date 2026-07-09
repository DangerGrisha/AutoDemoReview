//! Port of Python `tests/test_binfmt.py`: round-trip, edge-case, and size tests.

use serde_json::json;

use c2r3::{
    decode_round, encode_round, naive_json_bytes, read_varint, write_varint, zigzag_decode,
    zigzag_encode, PlayerMeta, RoundModel, Track, ANG_SCALE,
};

const PRESENT: u8 = 0x01;
const ALIVE: u8 = 0x02;
const CROUCH: u8 = 0x04;

fn circ_deg_diff(a: f64, b: f64) -> f64 {
    ((a - b + 180.0).rem_euclid(360.0) - 180.0).abs()
}

// ---------------------------------------------------------------------------
// synthetic builders (port of make_track / make_model)
// ---------------------------------------------------------------------------

struct TrackParams {
    alive_from: usize,
    alive_to: Option<usize>,
    x0: f64,
    y0: f64,
    z0: f64,
    vx: f64,
    vy: f64,
    yaw0: f64,
    dyaw: f64,
    pitch0: f64,
    weapon: u8,
    hp0: i64,
}

impl Default for TrackParams {
    fn default() -> Self {
        TrackParams {
            alive_from: 0,
            alive_to: None,
            x0: 0.0,
            y0: 0.0,
            z0: -200.0,
            vx: 15.0,
            vy: -9.0,
            yaw0: -55.0,
            dyaw: 3.0,
            pitch0: 2.0,
            weapon: 1,
            hp0: 100,
        }
    }
}

fn make_track(n: usize, p: TrackParams) -> Track {
    let alive_to = p.alive_to.unwrap_or(n);
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
    let mut last: Option<(f64, f64, f64, f64, f64)> = None;
    for i in 0..n {
        let alive = p.alive_from <= i && i < alive_to;
        if alive {
            let k = (i - p.alive_from) as f64;
            tr.x[i] = p.x0 + p.vx * k;
            tr.y[i] = p.y0 + p.vy * k;
            tr.z[i] = p.z0 + (i as f64 * 0.3).sin() * 40.0;
            tr.yaw[i] = (p.yaw0 + p.dyaw * i as f64 + 180.0).rem_euclid(360.0) - 180.0;
            tr.pitch[i] = p.pitch0 + (i as f64 * 0.2).sin() * 5.0;
            tr.flags[i] = PRESENT | ALIVE | if i % 7 == 0 { CROUCH } else { 0 };
            tr.health[i] = std::cmp::max(1, p.hp0 - (i - p.alive_from) as i64) as u8;
            tr.weapon[i] = p.weapon;
            tr.flash[i] = if i - p.alive_from == 2 || i - p.alive_from == 3 { 200 } else { 0 };
            last = Some((tr.x[i], tr.y[i], tr.z[i], tr.yaw[i], tr.pitch[i]));
        } else {
            let fx = last.unwrap_or((p.x0, p.y0, p.z0, p.yaw0, p.pitch0));
            tr.x[i] = fx.0;
            tr.y[i] = fx.1;
            tr.z[i] = fx.2;
            tr.yaw[i] = fx.3;
            tr.pitch[i] = fx.4;
        }
    }
    tr
}

fn make_model(n: usize, tracks: Vec<Track>) -> RoundModel {
    let n_players = tracks.len();
    let players: Vec<PlayerMeta> = (0..n_players)
        .map(|i| PlayerMeta {
            slot: i as i64,
            sid: format!("{}", 76561190000000000u64 + i as u64),
            name: format!("P{}", i),
            team: if i < n_players / 2 { 2 } else { 3 },
        })
        .collect();
    let thrower = players.first().map(|p| p.sid.clone()).unwrap_or_default();
    RoundModel {
        map: "de_mirage".into(),
        round: 5,
        tick_rate: 64,
        sample_stride: 4,
        sample_rate: 16.0,
        n_samples: n,
        start_tick: 1000,
        end_tick: 1000 + n as i64 * 4,
        players,
        weapon_table: vec!["".into(), "AK".into(), "AWP".into(), "USP".into(), "🔪".into(), "💣".into()],
        utility: vec![json!({"type": "smoke", "thrower": thrower, "throwTick": 1100,
                             "detTick": 1150, "throwSample": 25, "detSample": 37,
                             "pos": [100.0, 200.0, -150.0], "radius": 144, "duration": 18.0})],
        tracks,
    }
}

fn assert_roundtrip(model: &RoundModel, label: &str) -> (Vec<u8>, u32) {
    let (data, clamps) = encode_round(model).expect(label);
    let dec = decode_round(&data).expect(label);
    let n = model.n_samples;
    let tol_xy = 0.5 / dec.scale_xy + 1e-6;
    let tol_z = 0.5 / dec.scale_z + 1e-6;
    let tol_ang = 0.5 * 360.0 / 65536.0 + 1e-6;
    let tol_pitch = 0.5 / ANG_SCALE + 1e-6;

    assert_eq!(dec.n_samples, n, "{label}: nSamples preserved");
    assert_eq!(dec.tracks.len(), model.tracks.len(), "{label}: player count preserved");
    assert_eq!(dec.players, model.players, "{label}: player metadata preserved");
    assert_eq!(dec.weapon_table, model.weapon_table, "{label}: weaponTable preserved");
    assert_eq!(dec.utility, model.utility, "{label}: utility preserved");

    for (si, (a, b)) in model.tracks.iter().zip(&dec.tracks).enumerate() {
        for i in 0..n {
            let ex = (a.x[i] - b.x[i]).abs();
            let ey = (a.y[i] - b.y[i]).abs();
            let ez = (a.z[i] - b.z[i]).abs();
            let ep = (a.pitch[i] - b.pitch[i]).abs();
            let ea = circ_deg_diff(a.yaw[i], b.yaw[i]);
            assert!(ex <= tol_xy, "{label} p{si} s{i} X err {ex:.4} > {tol_xy:.4}");
            assert!(ey <= tol_xy, "{label} p{si} s{i} Y err {ey:.4} > {tol_xy:.4}");
            assert!(ez <= tol_z, "{label} p{si} s{i} Z err {ez:.4} > {tol_z:.4}");
            assert!(ep <= tol_pitch, "{label} p{si} s{i} pitch err {ep:.5}");
            assert!(ea <= tol_ang, "{label} p{si} s{i} yaw err {ea:.5}");
        }
        assert_eq!(a.flags, b.flags, "{label} p{si} flags");
        assert_eq!(a.health, b.health, "{label} p{si} health");
        assert_eq!(a.weapon, b.weapon, "{label} p{si} weapon");
        assert_eq!(a.flash, b.flash, "{label} p{si} flash");
    }
    (data, clamps)
}

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

#[test]
fn varint_roundtrip() {
    for v in [0u64, 1, 127, 128, 255, 16383, 16384, 65535, 131071, 2097151] {
        let mut buf = Vec::new();
        write_varint(&mut buf, v);
        let (got, pos) = read_varint(&buf, 0).unwrap();
        assert_eq!((got, pos), (v, buf.len()), "varint {v} roundtrip");
    }
}

#[test]
fn zigzag_roundtrip() {
    for n in [0i64, 1, -1, 2, -2, 32767, -32767, -32768, 32768, 100000, -100000] {
        assert_eq!(zigzag_decode(zigzag_encode(n)), n, "zigzag {n}");
    }
}

#[test]
fn basic_roundtrip() {
    let tracks: Vec<Track> = (0..10)
        .map(|i| {
            make_track(
                120,
                TrackParams {
                    x0: 100.0 + 200.0 * i as f64,
                    y0: -300.0 + 150.0 * i as f64,
                    weapon: 1 + (i % 4) as u8,
                    ..Default::default()
                },
            )
        })
        .collect();
    let (data, clamps) = assert_roundtrip(&make_model(120, tracks), "basic10p");
    assert_eq!(clamps, 0, "basic10p: no clamps");
    println!("  basic10p: 120 samples x10p -> {} bytes, {} clamps", data.len(), clamps);
}

#[test]
fn dead_and_absent() {
    let tracks = vec![
        make_track(80, TrackParams { alive_from: 0, alive_to: Some(40), ..Default::default() }),
        make_track(80, TrackParams { alive_from: 20, alive_to: Some(80), ..Default::default() }),
        make_track(80, TrackParams { alive_from: 0, alive_to: Some(0), ..Default::default() }),
        make_track(80, TrackParams { alive_from: 0, alive_to: Some(80), ..Default::default() }),
    ];
    assert_roundtrip(&make_model(80, tracks), "dead_absent");
}

#[test]
fn single_sample() {
    let tracks = vec![
        make_track(1, TrackParams::default()),
        make_track(1, TrackParams::default()),
    ];
    assert_roundtrip(&make_model(1, tracks), "single_sample");
}

#[test]
fn zero_samples() {
    let tracks = vec![Track::default(), Track::default(), Track::default()];
    assert_roundtrip(&make_model(0, tracks), "zero_samples");
}

#[test]
fn angle_wrap() {
    let n = 12;
    let yaws = [170.0, 175.0, 179.0, 180.0, -179.0, -175.0, -170.0, -180.0, 178.0, -178.0, 0.0, 90.0];
    let mut tr = make_track(n, TrackParams::default());
    tr.yaw = yaws.to_vec();
    assert_roundtrip(&make_model(n, vec![tr.clone()]), "angle_wrap");

    // The +/-180 crossing must cost the same as small non-crossing steps.
    let wrap_size = encode_round(&make_model(n, vec![tr])).unwrap().0.len();
    let mut ctrl = make_track(n, TrackParams::default());
    ctrl.yaw = (0..n).map(|i| ((i * 3) % 60) as f64).collect();
    let ctrl_size = encode_round(&make_model(n, vec![ctrl])).unwrap().0.len();
    assert!(
        wrap_size <= ctrl_size + 4,
        "angle_wrap: wrap size {wrap_size} not ~= control {ctrl_size} (modular delta failed)"
    );
}

#[test]
fn clamp_counting() {
    // a DEAD sample carrying a wild coordinate is outside the alive-only bbox and
    // must clamp safely (and be counted) rather than wrap.
    let mut tr = make_track(30, TrackParams { alive_from: 0, alive_to: Some(20), ..Default::default() });
    tr.x[25] = 1e9;
    let (_data, clamps) = encode_round(&make_model(30, vec![tr])).unwrap();
    assert!(clamps >= 1, "clamp_counting: wild dead coord was clamped (got {clamps})");
}

#[test]
fn size_vs_naive() {
    let tracks: Vec<Track> = (0..10)
        .map(|i| {
            make_track(
                200,
                TrackParams {
                    x0: 100.0 + 180.0 * i as f64,
                    y0: -400.0 + 120.0 * i as f64,
                    weapon: 1 + (i % 4) as u8,
                    ..Default::default()
                },
            )
        })
        .collect();
    let model = make_model(200, tracks);
    let (data, _) = encode_round(&model).unwrap();
    let naive = naive_json_bytes(&model);
    let ratio = naive.len() as f64 / data.len() as f64;
    println!("  size_vs_naive: codec={} B  naive_json={} B  ratio={:.1}x", data.len(), naive.len(), ratio);
    assert!(data.len() < naive.len(), "codec smaller than naive JSON");
    assert!(ratio > 3.0, "ratio >3x (got {ratio:.1}x)");
}
