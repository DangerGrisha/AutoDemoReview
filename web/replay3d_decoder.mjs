// C2R3 v1 tick-level 3D replay decoder (standalone, DataView-based).
//
// Mirrors src/demoreview/binfmt.py decode_round() exactly. Pure: no DOM, no deps.
// decode(arrayBuffer) -> { header, tracks: [{ X,Y,Z,yaw,pitch: Float32Array,
//                                             flags,health,weapon,flash: Uint8Array }] }
//
// "Decode once": each round is expanded a single time into typed arrays; the renderer
// scrubs/interpolates over those, so the delta/varint/change-list encoding costs nothing
// at frame time. See binfmt.py for the authoritative format spec.

const MAGIC = 0x43325233; // "C2R3" big-endian read of the 4 magic bytes
const VERSION = 1;
const CODEC_VARINT_CHANGELIST = 0x01;
const ANG_SCALE = 32768.0 / 180.0;
const YAW_UNITS = 65536;

// geometry channels in fixed wire order; kind: "lin" (int16) or "ang" (uint16 modular)
const GEOM = [
  ["X", "lin"], ["Y", "lin"], ["Z", "lin"], ["yaw", "ang"], ["pitch", "lin"],
];
const DISCRETE = ["flags", "health", "weapon", "flash"];

function zigzagDecode(u) {
  return (u >>> 1) ^ -(u & 1);
}

// LEB128 unsigned varint. Returns [value, newPos]. Values here stay < 2^31.
function readVarint(bytes, pos) {
  let result = 0;
  let shift = 0;
  for (;;) {
    const b = bytes[pos++];
    result = (result | ((b & 0x7f) << shift)) >>> 0;
    if ((b & 0x80) === 0) return [result, pos];
    shift += 7;
  }
}

export function decode(arrayBuffer) {
  const u8 = new Uint8Array(arrayBuffer);
  const dv = new DataView(arrayBuffer);

  // ---- container ----
  const magic = dv.getUint32(0, false); // read as big-endian to compare literal bytes
  if (magic !== MAGIC) throw new Error("bad magic: not a C2R3 blob");
  const version = dv.getUint8(4);
  if (version !== VERSION) throw new Error("unsupported version " + version);
  const codec = dv.getUint8(5);
  if (codec !== CODEC_VARINT_CHANGELIST) {
    throw new Error("unsupported codecFlags 0x" + codec.toString(16));
  }
  const headerLen = dv.getUint32(8, true);
  const headerBytes = u8.subarray(12, 12 + headerLen);
  const header = JSON.parse(new TextDecoder("utf-8").decode(headerBytes));

  const blobStart = 12 + headerLen;
  const n = header.nSamples;
  const [cx, cy, cz] = header.origin;
  const scaleXY = header.scaleXY;
  const scaleZ = header.scaleZ;

  const nPlayers = dv.getUint32(blobStart, true);
  const offsets = new Array(nPlayers);
  for (let i = 0; i < nPlayers; i++) {
    offsets[i] = dv.getUint32(blobStart + 4 + 4 * i, true);
  }

  const tracks = [];
  for (let slot = 0; slot < nPlayers; slot++) {
    let pos = blobStart + offsets[slot];

    // geometry: quantized ints per channel, then dequantized to Float32Array
    const quant = {};
    for (const [name, kind] of GEOM) {
      const q = new Int32Array(n);
      if (n > 0) {
        let kf;
        if (kind === "ang") { kf = dv.getUint16(pos, true); pos += 2; }
        else { kf = dv.getInt16(pos, true); pos += 2; }
        q[0] = kf;
        let prev = kf;
        for (let i = 1; i < n; i++) {
          let zz;
          [zz, pos] = readVarint(u8, pos);
          const d = zigzagDecode(zz);
          const cur = kind === "ang" ? ((prev + d) & 0xffff) : (prev + d);
          q[i] = cur;
          prev = cur;
        }
      }
      quant[name] = q;
    }

    // discrete: change-lists filled forward into Uint8Array
    const disc = {};
    for (const name of DISCRETE) {
      let nChanges;
      [nChanges, pos] = readVarint(u8, pos);
      const arr = new Uint8Array(n);
      let curIdx = 0;
      let curVal = 0;
      let first = true;
      for (let c = 0; c < nChanges; c++) {
        let gap, val;
        [gap, pos] = readVarint(u8, pos);
        val = u8[pos++];
        const idx = first ? gap : curIdx + gap;
        for (let j = curIdx; j < idx; j++) arr[j] = curVal;
        curIdx = idx;
        curVal = val;
        first = false;
      }
      for (let j = curIdx; j < n; j++) arr[j] = curVal;
      disc[name] = arr;
    }

    // dequantize geometry
    const X = new Float32Array(n);
    const Y = new Float32Array(n);
    const Z = new Float32Array(n);
    const yaw = new Float32Array(n);
    const pitch = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      X[i] = cx + quant.X[i] / scaleXY;
      Y[i] = cy + quant.Y[i] / scaleXY;
      Z[i] = cz + quant.Z[i] / scaleZ;
      yaw[i] = (quant.yaw[i] / YAW_UNITS) * 360.0;
      pitch[i] = quant.pitch[i] / ANG_SCALE;
    }
    tracks.push({
      X, Y, Z, yaw, pitch,
      flags: disc.flags, health: disc.health, weapon: disc.weapon, flash: disc.flash,
    });
  }

  return { header, tracks };
}
