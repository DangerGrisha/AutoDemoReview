/* Crosshairs for the 3D replay's first person: CS2's share codes and a canvas to draw them.
 *
 * A code is a 128-bit (old `CSGO-xxxxx-…`) or 256-bit (`CS…`, CS2 since 30 September 2026)
 * number in base 57. Four layouts exist: legacy-v1 (CS:GO's own units), legacy-v3 and
 * legacy-v4 (pixels, September 2026), cs2-v1 (pixels, signed gap, outline colour). Their bytes
 * were checked against akiver/csgo-sharecode (MIT), tests/test_replay3d_crosshair.py.
 *
 * Everything is turned into one shape, sizes in pixels on a screen `ref` pixels tall, and
 * drawn scaled to the window's height, as CS2 does since the pixel crosshair.
 */

const DICT = 'ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789';

/** CS:GO's colour presets, `cl_crosshaircolor` 0-4 (5 is the code's own RGB). */
export const COLOR_PRESETS = [[250, 50, 50], [50, 250, 50], [250, 250, 50], [50, 50, 250], [50, 250, 250]];

/** The height the page's own crosshair settings are given for. */
export const REF_HEIGHT = 1080;

function bytesOf(chars, n) {
  let big = 0n;
  for (const ch of [...chars].reverse()) {
    const d = DICT.indexOf(ch);
    if (d < 0) return null;
    big = big * 57n + BigInt(d);
  }
  const hex = big.toString(16);
  if (hex.length > n * 2) return null;
  const pad = hex.padStart(n * 2, '0'), out = [];
  for (let i = 0; i < n * 2; i += 2) out.push(parseInt(pad.slice(i, i + 2), 16));
  return out;
}

const sum = (a) => a.reduce((x, y) => x + y, 0);
const int8 = (b) => (b << 24) >> 24;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** A code's fields as the game stores them, with `format`; null for anything else. */
export function decodeShareCode(code) {
  const s = String(code || '').trim();
  let b;
  if (/^CSGO(-?[A-Za-z0-9]{5}){5}$/.test(s)) b = bytesOf(s.replace(/CSGO|-/g, ''), 18);
  else if (/^CS[A-Za-z0-9]{44}$/.test(s)) b = bytesOf(s.slice(2), 32);
  if (!b || b[0] !== sum(b.slice(1)) % 256) return null;
  const cs2 = b.length === 32;
  if (cs2) {
    if (b[1] !== 1) return null;
    const h = b[2] | (b[3] << 8);
    if (!h) return null;
    const bits = b[18] | (b[19] << 8) | (b[20] << 16) | (b[21] << 24);
    return {
      format: 'cs2-v1', style: Math.min(b[4] & 0x1f, 9), followRecoil: !!(b[4] & 0x20),
      centerDotEnabled: !!(b[4] & 0x40), tStyleEnabled: !!(b[4] & 0x80),
      outlineMode: Math.min(b[13] >>> 6, 2), red: b[5], green: b[6], blue: b[7], alpha: b[8],
      outlineRed: b[9], outlineGreen: b[10], outlineBlue: b[11], outlineAlpha: b[12],
      gap: clamp(((b[14] | (b[15] << 8)) << 16) >> 16, -3840, 3840), length: b[16],
      thickness: Math.min(b[13] & 0x3f, 32), dynamicSpreadLimit: b[17], splitDistance: bits & 0x7f,
      screenHeight: Math.max(h, 240),
    };
  }
  if (b[1] === 1) {
    return {
      format: 'legacy-v1', gap: int8(b[2]) / 10, outline: b[3] / 2, red: b[4], green: b[5],
      blue: b[6], alpha: b[7], color: b[10] & 7, outlineEnabled: !!(b[10] & 8),
      thickness: b[12] / 10, centerDotEnabled: !!((b[13] >> 4) & 1),
      alphaEnabled: !!((b[13] >> 4) & 4), tStyleEnabled: !!((b[13] >> 4) & 8),
      style: (b[13] & 0xf) >> 1, length: b[14] / 10,
    };
  }
  if (b[1] === 3 || b[1] === 4) {
    const bits = b[10] | (b[11] << 8) | (b[12] << 16) | (b[13] << 24);
    const out = {
      format: b[1] === 3 ? 'legacy-v3' : 'legacy-v4', style: b[2] & 0xf,
      followRecoil: !!(b[2] & 0x10), centerDotEnabled: !!(b[2] & 0x40), tStyleEnabled: !!(b[2] & 0x80),
      red: b[3], green: b[4], blue: b[5], alpha: b[6], gap: b[7], length: b[8],
      thickness: (bits >> 23) & 0x1f, screenHeight: b[14] | (b[15] << 8),
    };
    if (b[1] === 3) out.outlineEnabled = !!(b[2] & 0x20);
    else out.outlineMode = (b[13] >> 4) & 3;
    return out;
  }
  return null;
}

/** What the styles look like here. Dynamic ones stand still: the replay has no spread. */
function shapeOf(format, style) {
  if (format === 'legacy-v1') return 'cross';
  if (style === 1 || style === 3) return 'circle';
  if (style === 6) return 'dot';
  if (style === 8) return 'square';
  return 'cross';
}

/** One shape for every layout: sizes in pixels on a screen `ref` tall. */
export function fromDecoded(c) {
  if (!c) return null;
  if (c.format === 'legacy-v1') {
    // CS:GO's units: Source's YRES, a 480-line screen, for length and thickness; the gap
    // in pixels after a fixed 4 on a 1200-line screen. Not confirmed against the game.
    const ref = REF_HEIGHT, k = ref / 480;
    const rgb = c.color < 5 ? COLOR_PRESETS[c.color] : [c.red, c.green, c.blue];
    return {
      ref, shape: 'cross', length: c.length * k, thickness: Math.max(1, c.thickness * k),
      gap: 4 * ref / 1200 + c.gap, dot: c.centerDotEnabled, t: c.tStyleEnabled,
      outline: c.outlineEnabled ? Math.max(0.5, c.outline) : 0, outlineRgba: [0, 0, 0, 255],
      rgba: [...rgb, c.alphaEnabled ? c.alpha : 200],
    };
  }
  const outlined = c.format === 'legacy-v3' ? c.outlineEnabled : c.outlineMode > 0;
  return {
    ref: c.screenHeight || REF_HEIGHT, shape: shapeOf(c.format, c.style), length: c.length,
    thickness: Math.max(1, c.thickness), gap: c.gap, dot: c.centerDotEnabled || c.style === 6,
    t: c.tStyleEnabled, outline: outlined ? 1 : 0,
    outlineRgba: c.format === 'cs2-v1' ? [c.outlineRed, c.outlineGreen, c.outlineBlue, c.outlineAlpha] : [0, 0, 0, 255],
    rgba: [c.red, c.green, c.blue, c.alpha],
  };
}

/** The page's own crosshair settings (sizes on a 1080-line screen) in the same shape. */
export function fromSettings(s) {
  const rgb = s.color < 5 ? COLOR_PRESETS[s.color] : [s.r, s.g, s.b];
  return {
    ref: REF_HEIGHT, shape: s.shape, length: s.length, thickness: s.thickness, gap: s.gap,
    dot: s.dot || s.shape === 'dot', t: s.t, outline: s.outline, outlineRgba: [0, 0, 0, s.outlineAlpha],
    rgba: [...rgb, s.alpha],
  };
}

/** A code as the page's own settings, to fill the sliders; null for a code that is not one. */
export function settingsFromCode(code) {
  const x = fromDecoded(decodeShareCode(code));
  if (!x) return null;
  const k = REF_HEIGHT / x.ref, r = (v) => Math.round(v * 2) / 2;
  const preset = COLOR_PRESETS.findIndex(p => p.every((v, i) => v === x.rgba[i]));
  return {
    shape: x.shape, length: r(x.length * k), thickness: Math.max(1, r(x.thickness * k)), gap: r(x.gap * k),
    dot: x.dot, t: x.t, outline: x.outline ? Math.max(1, Math.round(x.outline * k)) : 0,
    outlineAlpha: x.outlineRgba[3], color: preset >= 0 ? preset : 5,
    r: x.rgba[0], g: x.rgba[1], b: x.rgba[2], alpha: x.rgba[3],
  };
}

const css = ([r, g, b, a]) => `rgba(${r},${g},${b},${(a / 255).toFixed(3)})`;

/** Draw `x` in the middle of `canvas`, which covers the window, for a window this tall. */
export function drawCrosshair(canvas, x, viewHeight) {
  const dpr = Math.min(3, window.devicePixelRatio || 1);
  const w = canvas.clientWidth || 1, h = canvas.clientHeight || 1;
  const W = Math.round(w * dpr), H = Math.round(h * dpr);
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  if (!x) return;
  // pixels of this screen, rounded so lines stay crisp
  const k = (viewHeight || h) * dpr / x.ref, px = (v) => Math.round(v * k);
  const len = Math.max(0, px(x.length)), th = Math.max(1, px(x.thickness)), gap = px(x.gap);
  const ol = x.outline > 0 ? Math.max(1, px(x.outline)) : 0;
  const cx = Math.floor(W / 2), cy = Math.floor(H / 2), lo = Math.floor(th / 2);
  const rects = [];
  if (x.shape === 'cross' && len > 0) {
    rects.push([cx + gap, cy - lo, len, th], [cx - gap - len, cy - lo, len, th], [cx - lo, cy + gap, th, len]);
    if (!x.t) rects.push([cx - lo, cy - gap - len, th, len]);
  }
  if (x.dot) rects.push([cx - lo, cy - lo, th, th]);
  if (ol) {
    ctx.fillStyle = css(x.outlineRgba);
    for (const [rx, ry, rw, rh] of rects) ctx.fillRect(rx - ol, ry - ol, rw + 2 * ol, rh + 2 * ol);
  }
  ctx.fillStyle = css(x.rgba);
  for (const [rx, ry, rw, rh] of rects) ctx.fillRect(rx, ry, rw, rh);
  if (x.shape === 'circle' || x.shape === 'square') {
    const rad = Math.max(2, gap + len / 2);
    const path = () => {
      ctx.beginPath();
      if (x.shape === 'circle') ctx.arc(cx + 0.5, cy + 0.5, rad, 0, Math.PI * 2);
      else ctx.rect(cx - rad + 0.5, cy - rad + 0.5, rad * 2, rad * 2);
    };
    if (ol) { ctx.strokeStyle = css(x.outlineRgba); ctx.lineWidth = th + 2 * ol; path(); ctx.stroke(); }
    ctx.strokeStyle = css(x.rgba); ctx.lineWidth = th; path(); ctx.stroke();
  }
}
