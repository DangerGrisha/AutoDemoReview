//! CLI: extract + encode the tick-level 3D replay (Rust port of build3d.py, with
//! build3d_app.py's manifest folded in).
//!
//!     extract3d <demo.dem> [--selfcheck] [--no-write] [--out-dir DIR]
//!
//! Writes per-round C2R3 sidecars to <out-dir>/rNN.3dr (default output/<stem>/),
//! plus manifest.json, and prints the size table comparing the codec against a
//! naive-JSON equivalent. --selfcheck decodes every round back and verifies within
//! quantization tolerance.

use std::io::Write as _;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use c2r3::{decode_round, encode_round, naive_json_bytes, RoundModel, ANG_SCALE};
use extract3d::dp2::Dp2Source;
use extract3d::{build_replay3d, manifest, mapscal, DemoSource};
use flate2::{write::GzEncoder, Compression};

fn fmt_thousands(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn gzip6(data: &[u8]) -> usize {
    let mut enc = GzEncoder::new(Vec::new(), Compression::new(6));
    enc.write_all(data).unwrap();
    enc.finish().unwrap().len()
}

fn circ(a: f64, b: f64) -> f64 {
    ((a - b + 180.0).rem_euclid(360.0) - 180.0).abs()
}

/// Decode and verify within tolerance (port of build3d._selfcheck_round).
fn selfcheck_round(model: &RoundModel, data: &[u8]) -> bool {
    let dec = match decode_round(data) {
        Ok(d) => d,
        Err(_) => return false,
    };
    let tol_xy = 0.5 / dec.scale_xy + 1e-6;
    let tol_z = 0.5 / dec.scale_z + 1e-6;
    let tol_ang = 0.5 * 360.0 / 65536.0 + 1e-6;
    let tol_pitch = 0.5 / ANG_SCALE + 1e-6;
    for (a, b) in model.tracks.iter().zip(&dec.tracks) {
        for i in 0..model.n_samples {
            if (a.x[i] - b.x[i]).abs() > tol_xy
                || (a.y[i] - b.y[i]).abs() > tol_xy
                || (a.z[i] - b.z[i]).abs() > tol_z
                || (a.pitch[i] - b.pitch[i]).abs() > tol_pitch
                || circ(a.yaw[i], b.yaw[i]) > tol_ang
                || a.flags[i] != b.flags[i]
                || a.health[i] != b.health[i]
                || a.weapon[i] != b.weapon[i]
                || a.flash[i] != b.flash[i]
            {
                return false;
            }
        }
    }
    true
}

fn main() -> Result<()> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut args: Vec<&str> = Vec::new();
    let mut do_selfcheck = false;
    let mut do_write = true;
    let mut out_dir_arg: Option<PathBuf> = None;
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "--selfcheck" => do_selfcheck = true,
            "--no-write" => do_write = false,
            "--out-dir" => {
                i += 1;
                out_dir_arg = Some(PathBuf::from(argv.get(i).context("--out-dir needs a value")?));
            }
            a if a.starts_with("--") => bail!("unknown flag {a}"),
            a => args.push(a),
        }
        i += 1;
    }
    let demo_path = *args.first().context("usage: extract3d <demo.dem> [--selfcheck] [--no-write] [--out-dir DIR]")?;
    let stem = Path::new(demo_path)
        .file_stem()
        .and_then(|s| s.to_str())
        .context("bad demo path")?;
    let out_dir = out_dir_arg.unwrap_or_else(|| PathBuf::from("output").join(stem));

    println!("Parsing {demo_path} ...");
    let mut source = Dp2Source::new(demo_path)?;
    let (models, meta) = build_replay3d(&mut source)?;

    println!(
        "map={}  tickRate={} (detected)  stride={}  sampleRate={:.2} Hz  rounds={}  mapSupported={}",
        meta.map, meta.tick_rate, meta.sample_stride, meta.sample_rate, meta.n_rounds,
        if meta.map_supported { "True" } else { "False" }
    );
    if do_write {
        std::fs::create_dir_all(&out_dir)?;
    }

    println!();
    let hdr = format!(
        "{:>4} {:>6} {:>5} | {:>10} {:>10} {:>10} | {:>11} {:>11} | {:>6} {:>6}",
        "rnd", "nSamp", "plyrs", "codec", "gzip", "base64", "naiveJSON", "naive_gz", "ratio", "clamp"
    );
    println!("{hdr}");
    println!("{}", "-".repeat(hdr.len()));

    let (mut t_codec, mut t_gz, mut t_b64, mut t_naive, mut t_naive_gz, mut t_clamps) =
        (0usize, 0usize, 0usize, 0usize, 0usize, 0u64);
    let mut tot_ok = true;
    let mut encoded: Vec<Vec<u8>> = Vec::with_capacity(models.len());

    for model in &models {
        let (data, clamps) = encode_round(model)?;
        let naive = naive_json_bytes(model);
        let c_gz = gzip6(&data);
        let n_gz = gzip6(&naive);
        let b64 = data.len().div_ceil(3) * 4;
        let ratio = if !data.is_empty() {
            naive.len() as f64 / data.len() as f64
        } else {
            0.0
        };

        t_codec += data.len();
        t_gz += c_gz;
        t_b64 += b64;
        t_naive += naive.len();
        t_naive_gz += n_gz;
        t_clamps += clamps as u64;

        let mut note = "";
        if do_selfcheck {
            let ok = selfcheck_round(model, &data);
            tot_ok = tot_ok && ok;
            note = if ok { "ok" } else { "FAIL" };
        }

        println!(
            "{:>4} {:>6} {:>5} | {:>10} {:>10} {:>10} | {:>11} {:>11} | {:>5.1}x {:>6} {}",
            model.round,
            model.n_samples,
            model.players.len(),
            fmt_thousands(data.len()),
            fmt_thousands(c_gz),
            fmt_thousands(b64),
            fmt_thousands(naive.len()),
            fmt_thousands(n_gz),
            ratio,
            clamps,
            note
        );

        if do_write {
            std::fs::write(out_dir.join(format!("r{:02}.3dr", model.round)), &data)?;
        }
        encoded.push(data);
    }

    println!("{}", "-".repeat(hdr.len()));
    let ratio_tot = if t_codec > 0 { t_naive as f64 / t_codec as f64 } else { 0.0 };
    println!(
        "{:>4} {:>6} {:>5} | {:>10} {:>10} {:>10} | {:>11} {:>11} | {:>5.1}x {:>6}",
        "TOT", "", "",
        fmt_thousands(t_codec),
        fmt_thousands(t_gz),
        fmt_thousands(t_b64),
        fmt_thousands(t_naive),
        fmt_thousands(t_naive_gz),
        ratio_tot,
        t_clamps
    );

    println!();
    println!("Match totals:");
    println!("  codec raw      : {} bytes", fmt_thousands(t_codec));
    println!("  naive JSON     : {} bytes", fmt_thousands(t_naive));
    println!("  raw(codec)    vs naive JSON : {ratio_tot:.1}x smaller");
    println!(
        "  total clamps   : {} {}",
        t_clamps,
        if t_clamps == 0 { "(expected 0)" } else { "<-- INVESTIGATE" }
    );

    // manifest.json (build3d_app.py equivalent) from the encoded rounds + kill events
    if do_write {
        let decoded: Vec<(String, c2r3::DecodedRound)> = models
            .iter()
            .zip(&encoded)
            .map(|(m, data)| {
                Ok((format!("r{:02}.3dr", m.round), decode_round(data)?))
            })
            .collect::<Result<_>>()?;
        let kills = source.kill_events()?;
        let info = mapscal::map_info(&meta.map)
            .with_context(|| format!("no radar calibration for map {}", meta.map))?;
        let man = manifest::build_manifest(&decoded, &kills, &info);
        std::fs::write(out_dir.join("manifest.json"), serde_json::to_vec(&man)?)?;
        println!("  wrote {} .3dr sidecars + manifest.json to {}/", models.len(), out_dir.display());

        // radar image for the viewer's textured ground plane (best-effort copy)
        let radar_src = Path::new("src/demoreview/assets").join(info.asset);
        if radar_src.is_file() {
            let assets = out_dir.join("assets");
            std::fs::create_dir_all(&assets)?;
            std::fs::copy(&radar_src, assets.join(info.asset))?;
            println!("  copied radar asset to {}/assets/{}", out_dir.display(), info.asset);
        } else {
            println!("  NOTE: radar asset {} not found (run from the repo root to copy it)", radar_src.display());
        }
    }
    if do_selfcheck {
        println!("  self-check     : {}", if tot_ok { "PASS" } else { "FAIL" });
        if !tot_ok {
            std::process::exit(1);
        }
    }
    Ok(())
}
