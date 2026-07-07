// Cross-validates the JS decoder (web/replay3d_decoder.mjs) against the exact bytes and
// dequantized values produced by the Python encoder (via tests/gen_fixture.py).
//
// Run:  node --test tests/replay3d_decoder.test.mjs
// (Regenerate the fixture first if binfmt changed:  python tests/gen_fixture.py)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { decode } from "../web/replay3d_decoder.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, "fixtures", "synthetic_round.json"), "utf-8")
);

function toArrayBuffer(b64) {
  const buf = Buffer.from(b64, "base64");
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

function circDegDiff(a, b) {
  return Math.abs(((a - b + 180) % 360 + 360) % 360 - 180);
}

test("container length matches Python", () => {
  const buf = Buffer.from(fixture.b64, "base64");
  assert.equal(buf.byteLength, fixture.byteLength);
});

test("header metadata parity", () => {
  const { header } = decode(toArrayBuffer(fixture.b64));
  assert.equal(header.map, fixture.header.map);
  assert.equal(header.round, fixture.header.round);
  assert.equal(header.nSamples, fixture.header.nSamples);
  assert.deepEqual(header.players, fixture.header.players);
  assert.deepEqual(header.weaponTable, fixture.header.weaponTable);
  assert.deepEqual(header.utility, fixture.header.utility);
});

test("tracks decode within tolerance of Python decode", () => {
  const { tracks } = decode(toArrayBuffer(fixture.b64));
  const exp = fixture.expected.tracks;
  const tol = fixture.tolerances;
  const n = fixture.expected.nSamples;

  assert.equal(tracks.length, exp.length, "player count");
  for (let p = 0; p < exp.length; p++) {
    const a = exp[p];
    const b = tracks[p];
    for (let i = 0; i < n; i++) {
      assert.ok(Math.abs(a.X[i] - b.X[i]) <= tol.pos, `p${p} s${i} X`);
      assert.ok(Math.abs(a.Y[i] - b.Y[i]) <= tol.pos, `p${p} s${i} Y`);
      assert.ok(Math.abs(a.Z[i] - b.Z[i]) <= tol.z, `p${p} s${i} Z`);
      assert.ok(Math.abs(a.pitch[i] - b.pitch[i]) <= tol.pitch, `p${p} s${i} pitch`);
      assert.ok(circDegDiff(a.yaw[i], b.yaw[i]) <= tol.ang, `p${p} s${i} yaw`);
      assert.equal(b.flags[i], a.flags[i], `p${p} s${i} flags`);
      assert.equal(b.health[i], a.health[i], `p${p} s${i} health`);
      assert.equal(b.weapon[i], a.weapon[i], `p${p} s${i} weapon`);
      assert.equal(b.flash[i], a.flash[i], `p${p} s${i} flash`);
    }
  }
});

test("typed arrays are the expected kinds", () => {
  const { tracks } = decode(toArrayBuffer(fixture.b64));
  const t = tracks[0];
  assert.ok(t.X instanceof Float32Array);
  assert.ok(t.flags instanceof Uint8Array);
});
