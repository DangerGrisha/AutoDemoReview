//! Compact binary codec for tick-level 3D replay rounds (format `C2R3` v1).
//!
//! Rust port of `src/demoreview/binfmt.py` (branch `feature/3d-replay`). That Python
//! module is the format's reference implementation; the byte-level behavior here must
//! match it exactly — including Python semantics that differ from Rust defaults:
//!
//! - rounding is round-half-to-EVEN (`round_ties_even`, Python's `round()`), never
//!   half-away-from-zero;
//! - `deg % 360.0` is Python's floored modulo → `rem_euclid`;
//! - the JSON header is minified with a FIXED key insertion order, raw UTF-8
//!   (no \uXXXX escaping), ints as bare integers and floats always with a fractional
//!   part (`16.0`) — `serde_json` with `preserve_order` matches all of this;
//! - `-32768` is a reserved sentinel: linear channels clamp to [-32767, 32767];
//! - yaw is an UNclamped modular u16 ("brad") channel with minimal modular deltas.
//!
//! All fixed multi-byte integers are little-endian; varints are unsigned LEB128.

use serde_json::{Map, Number, Value};

pub const MAGIC: &[u8; 4] = b"C2R3";
pub const VERSION: u8 = 1;
pub const CODEC_VARINT_CHANGELIST: u8 = 0x01;
/// Quant units per degree for pitch (fixed). Prints as 182.04444444444445 in headers.
pub const ANG_SCALE: f64 = 32768.0 / 180.0;
/// Yaw brad resolution (u16 wraps at this).
pub const YAW_UNITS: f64 = 65536.0;
pub const I16_MAX_Q: f64 = 32767.0;
pub const POS_MARGIN: f64 = 1.02;
pub const SCALE_TARGET: f64 = 32000.0;

// FLAGS bitfield (uint8)
pub const F_PRESENT: u8 = 0x01;
pub const F_ALIVE: u8 = 0x02;
pub const F_CROUCH: u8 = 0x04;
pub const F_SCOPED: u8 = 0x08;
pub const F_DEFUSING: u8 = 0x10;

/// Weapon index meaning "none / empty hands".
pub const WEAPON_NONE: u8 = 255;

#[derive(Debug)]
pub enum CodecError {
    BadMagic,
    UnsupportedVersion(u8),
    UnsupportedCodec(u8),
    Truncated,
    Malformed(&'static str),
    /// Python's `int(round(v))` raises on NaN/inf; we refuse the same inputs instead of
    /// silently emitting 0 like a bare `as` cast would.
    NonFinite { channel: &'static str },
    Header(String),
}

impl std::fmt::Display for CodecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CodecError::BadMagic => write!(f, "bad magic: not a C2R3 blob"),
            CodecError::UnsupportedVersion(v) => write!(f, "unsupported version {}", v),
            CodecError::UnsupportedCodec(c) => write!(f, "unsupported codecFlags 0x{:02x}", c),
            CodecError::Truncated => write!(f, "truncated C2R3 blob"),
            CodecError::Malformed(m) => write!(f, "malformed C2R3 blob: {}", m),
            CodecError::NonFinite { channel } => {
                write!(f, "non-finite value in channel {}", channel)
            }
            CodecError::Header(m) => write!(f, "bad C2R3 header: {}", m),
        }
    }
}

impl std::error::Error for CodecError {}

#[derive(Clone, Debug, PartialEq)]
pub struct PlayerMeta {
    pub slot: i64,
    /// steamid64 as a STRING everywhere (it exceeds JS 2^53).
    pub sid: String,
    pub name: String,
    pub team: i64,
}

/// One player's per-sample channels; every Vec has length `nSamples`.
/// Extraction has already applied freeze-on-death to the geometry channels.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Track {
    pub x: Vec<f64>,
    pub y: Vec<f64>,
    pub z: Vec<f64>,
    pub yaw: Vec<f64>,
    pub pitch: Vec<f64>,
    pub flags: Vec<u8>,
    pub health: Vec<u8>,
    pub weapon: Vec<u8>,
    pub flash: Vec<u8>,
}

/// Input to `encode_round` — mirrors the Python `round_model` dict contract.
#[derive(Clone, Debug, Default)]
pub struct RoundModel {
    pub map: String,
    pub round: i64,
    pub tick_rate: i64,
    pub sample_stride: i64,
    pub sample_rate: f64,
    pub n_samples: usize,
    pub start_tick: i64,
    pub end_tick: i64,
    pub players: Vec<PlayerMeta>,
    pub weapon_table: Vec<String>,
    /// Opaque metadata carried VERBATIM into the header (object key order preserved).
    pub utility: Vec<Value>,
    pub tracks: Vec<Track>,
}

/// Output of `decode_round` — geometry dequantized back to floats.
#[derive(Clone, Debug)]
pub struct DecodedRound {
    pub map: String,
    pub round: i64,
    pub tick_rate: i64,
    pub sample_stride: i64,
    pub sample_rate: f64,
    pub n_samples: usize,
    pub start_tick: i64,
    pub end_tick: i64,
    pub origin: [f64; 3],
    pub scale_xy: f64,
    pub scale_z: f64,
    pub players: Vec<PlayerMeta>,
    pub weapon_table: Vec<String>,
    pub utility: Vec<Value>,
    pub tracks: Vec<Track>,
    /// The raw parsed JSON header (for verbatim access / parity checks).
    pub header: Value,
}

// --------------------------------------------------------------------------- //
// primitive helpers (mirrored exactly by binfmt.py and the JS decoder)
// --------------------------------------------------------------------------- //

pub fn zigzag_encode(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

pub fn zigzag_decode(u: u64) -> i64 {
    ((u >> 1) as i64) ^ -((u & 1) as i64)
}

pub fn write_varint(buf: &mut Vec<u8>, mut value: u64) {
    while value >= 0x80 {
        buf.push((value & 0x7F) as u8 | 0x80);
        value >>= 7;
    }
    buf.push(value as u8);
}

pub fn read_varint(buf: &[u8], pos: usize) -> Result<(u64, usize), CodecError> {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    let mut p = pos;
    loop {
        let b = *buf.get(p).ok_or(CodecError::Truncated)?;
        p += 1;
        result |= ((b & 0x7F) as u64) << shift;
        if b & 0x80 == 0 {
            return Ok((result, p));
        }
        shift += 7;
        if shift >= 64 {
            return Err(CodecError::Malformed("varint too long"));
        }
    }
}

/// Python 3 `round()` — round-half-to-even. The value must be finite.
#[inline]
fn py_round(v: f64) -> i64 {
    v.round_ties_even() as i64
}

/// Round to int, clamp to [-32767, 32767] (−32768 reserved); bump `clamps` on clamp.
fn clamp_i16(v: f64, channel: &'static str, clamps: &mut u32) -> Result<i16, CodecError> {
    if !v.is_finite() {
        return Err(CodecError::NonFinite { channel });
    }
    let q = v.round_ties_even();
    if q > I16_MAX_Q {
        *clamps += 1;
        return Ok(32767);
    }
    if q < -I16_MAX_Q {
        *clamps += 1;
        return Ok(-32767);
    }
    Ok(q as i16)
}

/// Quantize a yaw in degrees to a u16 brad. Wraps; never clamps.
fn yaw_brad(deg: f64) -> Result<u16, CodecError> {
    if !deg.is_finite() {
        return Err(CodecError::NonFinite { channel: "yaw" });
    }
    // Python: int(round((deg % 360.0) / 360.0 * 65536)) & 0xFFFF
    Ok((py_round(deg.rem_euclid(360.0) / 360.0 * YAW_UNITS) & 0xFFFF) as u16)
}

/// Minimal signed delta in [-32768, 32767] between two u16 angle brads.
fn mod_delta(cur: u16, prev: u16) -> i64 {
    ((cur as i64 - prev as i64 + 32768) & 0xFFFF) - 32768
}

// --------------------------------------------------------------------------- //
// scale derivation
// --------------------------------------------------------------------------- //

/// Per-round (origin, scaleXY, scaleZ) over present-and-alive samples only.
/// Exact operation order matters: the results are serialized into the header floats.
pub fn derive_transform(tracks: &[Track]) -> (f64, f64, f64, f64, f64) {
    let mut lo = [f64::INFINITY; 3];
    let mut hi = [f64::NEG_INFINITY; 3];
    let mut seen = false;
    for tr in tracks {
        for (i, &fl) in tr.flags.iter().enumerate() {
            if fl & F_ALIVE == 0 {
                continue;
            }
            seen = true;
            for (axis, arr) in [(0usize, &tr.x), (1, &tr.y), (2, &tr.z)] {
                let v = arr[i];
                if v < lo[axis] {
                    lo[axis] = v;
                }
                if v > hi[axis] {
                    hi[axis] = v;
                }
            }
        }
    }
    if !seen {
        return (0.0, 0.0, 0.0, 1.0, 1.0);
    }
    let cx = (lo[0] + hi[0]) / 2.0;
    let cy = (lo[1] + hi[1]) / 2.0;
    let cz = (lo[2] + hi[2]) / 2.0;
    let half_xy = (hi[0] - cx).max(hi[1] - cy).max(1e-6) * POS_MARGIN;
    let half_z = (hi[2] - cz).max(1e-6) * POS_MARGIN;
    (cx, cy, cz, SCALE_TARGET / half_xy, SCALE_TARGET / half_z)
}

// --------------------------------------------------------------------------- //
// per-player block encode / decode
// --------------------------------------------------------------------------- //

fn encode_player_block(
    tr: &Track,
    n: usize,
    cx: f64,
    cy: f64,
    cz: f64,
    scale_xy: f64,
    scale_z: f64,
    clamps: &mut u32,
) -> Result<Vec<u8>, CodecError> {
    let mut buf: Vec<u8> = Vec::new();

    // Quantize whole channels first, then keyframe + deltas over the quantized ints
    // (deltas are exact integer differences — no error accumulation).
    let mut qx = Vec::with_capacity(n);
    for &v in &tr.x {
        qx.push(clamp_i16((v - cx) * scale_xy, "X", clamps)? as i64);
    }
    let mut qy = Vec::with_capacity(n);
    for &v in &tr.y {
        qy.push(clamp_i16((v - cy) * scale_xy, "Y", clamps)? as i64);
    }
    let mut qz = Vec::with_capacity(n);
    for &v in &tr.z {
        qz.push(clamp_i16((v - cz) * scale_z, "Z", clamps)? as i64);
    }
    let mut qpitch = Vec::with_capacity(n);
    for &v in &tr.pitch {
        qpitch.push(clamp_i16(v * ANG_SCALE, "pitch", clamps)? as i64);
    }
    let mut qyaw = Vec::with_capacity(n);
    for &v in &tr.yaw {
        qyaw.push(yaw_brad(v)? as i64);
    }

    // geometry channels in fixed wire order: X, Y, Z, yaw, pitch
    let channels: [(&Vec<i64>, bool); 5] = [
        (&qx, false),
        (&qy, false),
        (&qz, false),
        (&qyaw, true), // "ang": u16 keyframe + minimal modular deltas
        (&qpitch, false),
    ];
    for (q, is_ang) in channels {
        if n == 0 {
            continue;
        }
        if is_ang {
            buf.extend_from_slice(&(q[0] as u16).to_le_bytes());
        } else {
            buf.extend_from_slice(&(q[0] as i16).to_le_bytes());
        }
        let mut prev = q[0];
        for &cur in &q[1..] {
            let d = if is_ang {
                mod_delta(cur as u16, prev as u16)
            } else {
                cur - prev
            };
            let zz = zigzag_encode(d);
            let start = buf.len();
            write_varint(&mut buf, zz);
            debug_assert!(buf.len() - start <= 3, "geometry varint exceeded 3 bytes");
            prev = cur;
        }
    }

    // discrete channels: sparse change-lists. Sample 0 is ALWAYS emitted when n > 0.
    for arr in [&tr.flags, &tr.health, &tr.weapon, &tr.flash] {
        let mut changes: Vec<(usize, u8)> = Vec::new();
        if n > 0 {
            changes.push((0, arr[0]));
            for (i, &v) in arr.iter().enumerate().skip(1) {
                if v != changes.last().unwrap().1 {
                    changes.push((i, v));
                }
            }
        }
        write_varint(&mut buf, changes.len() as u64);
        let mut last = 0usize;
        for (idx, val) in changes {
            write_varint(&mut buf, (idx - last) as u64);
            buf.push(val);
            last = idx;
        }
    }
    Ok(buf)
}

struct QuantBlock {
    // lin channels as i64 (keyframe + accumulated deltas), yaw as raw u16 values in i64
    x: Vec<i64>,
    y: Vec<i64>,
    z: Vec<i64>,
    yaw: Vec<i64>,
    pitch: Vec<i64>,
}

fn decode_player_block(
    blob: &[u8],
    mut pos: usize,
    n: usize,
) -> Result<(Track, QuantBlock), CodecError> {
    let mut geom: [Vec<i64>; 5] = Default::default();
    for (ci, is_ang) in [false, false, false, true, false].into_iter().enumerate() {
        let vals = &mut geom[ci];
        if n > 0 {
            let raw: [u8; 2] = blob
                .get(pos..pos + 2)
                .ok_or(CodecError::Truncated)?
                .try_into()
                .unwrap();
            pos += 2;
            let kf: i64 = if is_ang {
                u16::from_le_bytes(raw) as i64
            } else {
                i16::from_le_bytes(raw) as i64
            };
            vals.push(kf);
            let mut prev = kf;
            for _ in 1..n {
                let (zz, np) = read_varint(blob, pos)?;
                pos = np;
                let d = zigzag_decode(zz);
                let cur = if is_ang { (prev + d) & 0xFFFF } else { prev + d };
                vals.push(cur);
                prev = cur;
            }
        }
    }
    let [qx, qy, qz, qyaw, qpitch] = geom;

    let mut discrete: [Vec<u8>; 4] = Default::default();
    for arr in discrete.iter_mut() {
        let (n_changes, np) = read_varint(blob, pos)?;
        pos = np;
        *arr = vec![0u8; n];
        let mut cur_idx = 0usize;
        let mut cur_val = 0u8;
        let mut first = true;
        for _ in 0..n_changes {
            let (gap, np) = read_varint(blob, pos)?;
            pos = np;
            let val = *blob.get(pos).ok_or(CodecError::Truncated)?;
            pos += 1;
            // the FIRST gap is an absolute sample index; later gaps are deltas
            let idx = if first { gap as usize } else { cur_idx + gap as usize };
            if idx > n {
                return Err(CodecError::Malformed("change-list index out of range"));
            }
            for slot in &mut arr[cur_idx..idx] {
                *slot = cur_val;
            }
            cur_idx = idx;
            cur_val = val;
            first = false;
        }
        for slot in &mut arr[cur_idx..n] {
            *slot = cur_val;
        }
    }
    let [flags, health, weapon, flash] = discrete;

    Ok((
        Track {
            flags,
            health,
            weapon,
            flash,
            ..Default::default()
        },
        QuantBlock {
            x: qx,
            y: qy,
            z: qz,
            yaw: qyaw,
            pitch: qpitch,
        },
    ))
}

fn dequantize(tr: &mut Track, q: &QuantBlock, cx: f64, cy: f64, cz: f64, sxy: f64, sz: f64) {
    tr.x = q.x.iter().map(|&v| cx + v as f64 / sxy).collect();
    tr.y = q.y.iter().map(|&v| cy + v as f64 / sxy).collect();
    tr.z = q.z.iter().map(|&v| cz + v as f64 / sz).collect();
    tr.pitch = q.pitch.iter().map(|&v| v as f64 / ANG_SCALE).collect();
    tr.yaw = q.yaw.iter().map(|&v| (v as f64 / YAW_UNITS) * 360.0).collect();
}

// --------------------------------------------------------------------------- //
// public API
// --------------------------------------------------------------------------- //

fn jf(v: f64) -> Result<Value, CodecError> {
    Number::from_f64(v)
        .map(Value::Number)
        .ok_or(CodecError::NonFinite { channel: "header" })
}

/// Encode a `RoundModel` into C2R3 v1 bytes. Returns `(data, clamp_count)`.
pub fn encode_round(model: &RoundModel) -> Result<(Vec<u8>, u32), CodecError> {
    let n = model.n_samples;
    let (cx, cy, cz, scale_xy, scale_z) = derive_transform(&model.tracks);
    let mut clamps: u32 = 0;

    let mut blocks: Vec<Vec<u8>> = Vec::with_capacity(model.tracks.len());
    for tr in &model.tracks {
        blocks.push(encode_player_block(
            tr, n, cx, cy, cz, scale_xy, scale_z, &mut clamps,
        )?);
    }

    // blob = nPlayers + offset table + concatenated blocks
    let n_players = blocks.len();
    let mut blob: Vec<u8> = Vec::new();
    blob.extend_from_slice(&(n_players as u32).to_le_bytes());
    let mut running: u32 = (4 + 4 * n_players) as u32;
    for b in &blocks {
        blob.extend_from_slice(&running.to_le_bytes());
        running += b.len() as u32;
    }
    for b in &blocks {
        blob.extend_from_slice(b);
    }

    // Header: key INSERTION ORDER is part of the format (Python dict order).
    let mut header = Map::new();
    header.insert("v".into(), Value::from(VERSION as i64));
    header.insert("map".into(), Value::from(model.map.as_str()));
    header.insert("round".into(), Value::from(model.round));
    header.insert("tickRate".into(), Value::from(model.tick_rate));
    header.insert("sampleStride".into(), Value::from(model.sample_stride));
    header.insert("sampleRate".into(), jf(model.sample_rate)?); // ALWAYS a float (16.0)
    header.insert("nSamples".into(), Value::from(n as i64));
    header.insert("startTick".into(), Value::from(model.start_tick));
    header.insert("endTick".into(), Value::from(model.end_tick));
    header.insert("origin".into(), Value::Array(vec![jf(cx)?, jf(cy)?, jf(cz)?]));
    header.insert("scaleXY".into(), jf(scale_xy)?);
    header.insert("scaleZ".into(), jf(scale_z)?);
    header.insert("angScale".into(), jf(ANG_SCALE)?);
    let players: Vec<Value> = model
        .players
        .iter()
        .map(|p| {
            let mut m = Map::new();
            m.insert("slot".into(), Value::from(p.slot));
            m.insert("sid".into(), Value::from(p.sid.as_str()));
            m.insert("name".into(), Value::from(p.name.as_str()));
            m.insert("team".into(), Value::from(p.team));
            Value::Object(m)
        })
        .collect();
    header.insert("players".into(), Value::Array(players));
    header.insert(
        "weaponTable".into(),
        Value::Array(model.weapon_table.iter().map(|s| Value::from(s.as_str())).collect()),
    );
    header.insert("utility".into(), Value::Array(model.utility.clone()));

    let header_bytes = serde_json::to_vec(&Value::Object(header))
        .map_err(|e| CodecError::Header(e.to_string()))?;

    let mut out = Vec::with_capacity(12 + header_bytes.len() + blob.len());
    out.extend_from_slice(MAGIC);
    out.push(VERSION);
    out.push(CODEC_VARINT_CHANGELIST);
    out.extend_from_slice(&0u16.to_le_bytes()); // reserved
    out.extend_from_slice(&(header_bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(&header_bytes);
    out.extend_from_slice(&blob);
    Ok((out, clamps))
}

/// Parse just the JSON header from a C2R3 blob (no tick-stream decode).
pub fn read_header(data: &[u8]) -> Result<Value, CodecError> {
    if data.len() < 12 || &data[..4] != MAGIC {
        return Err(CodecError::BadMagic);
    }
    let header_len = u32::from_le_bytes(data[8..12].try_into().unwrap()) as usize;
    let raw = data.get(12..12 + header_len).ok_or(CodecError::Truncated)?;
    serde_json::from_slice(raw).map_err(|e| CodecError::Header(e.to_string()))
}

fn h_i64(h: &Value, key: &str) -> Result<i64, CodecError> {
    h.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| CodecError::Header(format!("missing/invalid {}", key)))
}

fn h_f64(h: &Value, key: &str) -> Result<f64, CodecError> {
    h.get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| CodecError::Header(format!("missing/invalid {}", key)))
}

fn h_str(h: &Value, key: &str) -> Result<String, CodecError> {
    h.get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| CodecError::Header(format!("missing/invalid {}", key)))
}

/// Decode C2R3 v1 bytes; geometry is dequantized back to floats.
pub fn decode_round(data: &[u8]) -> Result<DecodedRound, CodecError> {
    if data.len() < 12 || &data[..4] != MAGIC {
        return Err(CodecError::BadMagic);
    }
    if data[4] != VERSION {
        return Err(CodecError::UnsupportedVersion(data[4]));
    }
    if data[5] != CODEC_VARINT_CHANGELIST {
        return Err(CodecError::UnsupportedCodec(data[5]));
    }
    let header_len = u32::from_le_bytes(data[8..12].try_into().unwrap()) as usize;
    let raw = data.get(12..12 + header_len).ok_or(CodecError::Truncated)?;
    let header: Value =
        serde_json::from_slice(raw).map_err(|e| CodecError::Header(e.to_string()))?;
    let blob = data.get(12 + header_len..).ok_or(CodecError::Truncated)?;

    let n = h_i64(&header, "nSamples")? as usize;
    let origin = header
        .get("origin")
        .and_then(Value::as_array)
        .filter(|a| a.len() == 3)
        .ok_or_else(|| CodecError::Header("missing/invalid origin".into()))?;
    let (cx, cy, cz) = (
        origin[0].as_f64().ok_or_else(|| CodecError::Header("origin[0]".into()))?,
        origin[1].as_f64().ok_or_else(|| CodecError::Header("origin[1]".into()))?,
        origin[2].as_f64().ok_or_else(|| CodecError::Header("origin[2]".into()))?,
    );
    let scale_xy = h_f64(&header, "scaleXY")?;
    let scale_z = h_f64(&header, "scaleZ")?;

    let players: Vec<PlayerMeta> = header
        .get("players")
        .and_then(Value::as_array)
        .ok_or_else(|| CodecError::Header("missing players".into()))?
        .iter()
        .map(|p| {
            Ok(PlayerMeta {
                slot: h_i64(p, "slot")?,
                sid: h_str(p, "sid")?,
                name: h_str(p, "name")?,
                team: h_i64(p, "team")?,
            })
        })
        .collect::<Result<_, CodecError>>()?;
    let weapon_table: Vec<String> = header
        .get("weaponTable")
        .and_then(Value::as_array)
        .ok_or_else(|| CodecError::Header("missing weaponTable".into()))?
        .iter()
        .map(|v| {
            v.as_str()
                .map(str::to_owned)
                .ok_or_else(|| CodecError::Header("weaponTable entry".into()))
        })
        .collect::<Result<_, CodecError>>()?;
    let utility: Vec<Value> = header
        .get("utility")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    if blob.len() < 4 {
        return Err(CodecError::Truncated);
    }
    let n_players = u32::from_le_bytes(blob[..4].try_into().unwrap()) as usize;
    let mut tracks = Vec::with_capacity(n_players);
    for slot in 0..n_players {
        let off_pos = 4 + 4 * slot;
        let off = u32::from_le_bytes(
            blob.get(off_pos..off_pos + 4)
                .ok_or(CodecError::Truncated)?
                .try_into()
                .unwrap(),
        ) as usize;
        let (mut tr, quant) = decode_player_block(blob, off, n)?;
        dequantize(&mut tr, &quant, cx, cy, cz, scale_xy, scale_z);
        tracks.push(tr);
    }

    Ok(DecodedRound {
        map: h_str(&header, "map")?,
        round: h_i64(&header, "round")?,
        tick_rate: h_i64(&header, "tickRate")?,
        sample_stride: h_i64(&header, "sampleStride")?,
        sample_rate: h_f64(&header, "sampleRate")?,
        n_samples: n,
        start_tick: h_i64(&header, "startTick")?,
        end_tick: h_i64(&header, "endTick")?,
        origin: [cx, cy, cz],
        scale_xy,
        scale_z,
        players,
        weapon_table,
        utility,
        tracks,
        header,
    })
}

// --------------------------------------------------------------------------- //
// naive JSON baseline (size comparison only — NOT byte-exact vs Python's round(x, 1),
// which uses decimal string rounding; close enough for the >3x ratio test)
// --------------------------------------------------------------------------- //

pub fn naive_json_bytes(model: &RoundModel) -> Vec<u8> {
    fn r1(v: f64) -> Value {
        Value::from((v * 10.0).round_ties_even() / 10.0)
    }
    let n = model.n_samples;
    let players: Vec<Value> = model
        .players
        .iter()
        .zip(&model.tracks)
        .map(|(p, tr)| {
            let samples: Vec<Value> = (0..n)
                .map(|i| {
                    Value::Array(vec![
                        r1(tr.x[i]),
                        r1(tr.y[i]),
                        r1(tr.z[i]),
                        r1(tr.yaw[i]),
                        r1(tr.pitch[i]),
                        Value::from(tr.flags[i]),
                        Value::from(tr.health[i]),
                        Value::from(tr.weapon[i]),
                        Value::from(tr.flash[i]),
                    ])
                })
                .collect();
            let mut m = Map::new();
            m.insert("slot".into(), Value::from(p.slot));
            m.insert("sid".into(), Value::from(p.sid.as_str()));
            m.insert("name".into(), Value::from(p.name.as_str()));
            m.insert("team".into(), Value::from(p.team));
            m.insert("samples".into(), Value::Array(samples));
            Value::Object(m)
        })
        .collect();
    let mut obj = Map::new();
    obj.insert("map".into(), Value::from(model.map.as_str()));
    obj.insert("round".into(), Value::from(model.round));
    obj.insert("tickRate".into(), Value::from(model.tick_rate));
    obj.insert("sampleStride".into(), Value::from(model.sample_stride));
    obj.insert("sampleRate".into(), Value::from(model.sample_rate));
    obj.insert("nSamples".into(), Value::from(n as i64));
    obj.insert("startTick".into(), Value::from(model.start_tick));
    obj.insert("endTick".into(), Value::from(model.end_tick));
    obj.insert("players".into(), Value::Array(players));
    obj.insert(
        "weaponTable".into(),
        Value::Array(model.weapon_table.iter().map(|s| Value::from(s.as_str())).collect()),
    );
    obj.insert("utility".into(), Value::Array(model.utility.clone()));
    serde_json::to_vec(&Value::Object(obj)).expect("naive json")
}
