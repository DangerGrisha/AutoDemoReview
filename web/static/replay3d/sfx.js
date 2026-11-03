/* ============================================================================
 * CS2 sound for the 3D replay (page-side, no wasm work). One file, mirrored to
 * rust/viewer/www/sfx.js for the dev page.
 *
 * Two sources behind the same event logic:
 *   - real CS2 sounds, when the page calls useSamples(). tools/convert_sounds.ps1
 *     builds them from a local CS2 install, never committed. index.json carries
 *     the game's own volume, pitch and volume-by-distance curve for each sound;
 *     a gun plays its near and distant event together, each on its own curve,
 *     which is the crossfade CS2 itself does.
 *   - the Web Audio synth, for anything without a decoded sample: no sound files
 *     on this machine, a browser that cannot decode them, or a sound still loading.
 *
 * Without samples the mix is gunfire only, as it always was. With them the steps,
 * grenades, bomb and hits are worth hearing and start on (see SAMPLE_MIX).
 *
 * Where the events come from (all data the page already has):
 *   round header (the .3dr JSON header returned by load_round)
 *     shots[]    {s: fractional sample, p: shooter slot}      -> gunshots
 *     utility[]  {type, pos, radius, detSample, duration,
 *                 throwPos, throwSample, thrower}             -> HE/flash pops,
 *                                                                decoy pot-shots
 *     players[]  {slot, name, team} / weaponTable[]           -> names, weapon class
 *   manifest round meta
 *     kills[]    {sample, atk, vic, w, hs, ...}               -> kill sounds, C4 blast
 *     bomb       {plant, defused, exploded}                   -> bomb beeps and blast
 *     durationS / winner                                      -> round start/end stings
 *   per-frame payload (PSTRIDE = 10 floats/player, after the page's HDR header floats,
 *   which frame() is told: a header that grew by one shifted every player read before)
 *     worldX/worldY  -> footsteps (travelled distance) + 3D placement
 *     health         -> bullet impacts / own "hurt"
 *     alive          -> death sound
 *     weapon         -> weapon-swap click, where the bomb was planted
 *     flashOpacity   -> flash ringing + ducking of everything else
 *
 * NO CONSTANT BEDS. The first pass also ran a room-tone bed plus molotov/smoke
 * noise loops; those came out as a constant loud fan hum and a painful hiss, so
 * all three are GONE from the code (no makeBrown, no loop beds — a decoy's fake
 * gunfire is the only surviving loop). For the same reason a smoke plays only the
 * start of CS2's 14 s hiss. A zeroed bus builds no audio nodes at all, so muted
 * categories are free.
 *
 * Timing is driven by the PLAYBACK CLOCK, never by wall time, so scrubbing and
 * slow-mo stay correct: a clock jump larger than MAX_STEP_S counts as a scrub
 * (re-anchor silently, fire nothing). Speed changes need no pitch work — events
 * simply arrive closer together.
 *
 * Positioning: the "ear" is the spectated player — real distance attenuation,
 * air-absorption low-pass, speed-of-sound delay, and stereo pan from that
 * player's own yaw. In free camera the wasm never reports the camera pose, so
 * the ear falls back to the centroid of the living players and the pan comes
 * from the event's SCREEN x (which is exact). See NOTE_CAM_POSE.
 *
 * NOTE_CAM_POSE: when the Rust toolchain is back, adding {camX, camY, camYaw}
 * to report_frame's header floats makes free-cam audio exact too — everything
 * here already funnels through updateEar()/at().
 *
 * Not implemented for lack of data: footsteps by floor material and walls muffling a
 * sound. Footsteps come from distance travelled, because a demo's player_footstep event
 * exists for only about 8% of steps. Not implemented yet: reloads and grenade bounces,
 * whose data new replays do have. A replay from before data version 2 has no bomb
 * position: it is taken from whoever held the bomb at the plant, so a scrub past the
 * plant loses it and the bomb then sounds from a fixed middle distance.
 * ========================================================================== */
(function () {
  'use strict';

  // ------------------------------------------------------------------ tuning
  const U2M = 0.0254;                  // 1 Source unit = 1 inch
  const SOUND_U_PER_S = 343 / U2M;     // speed of sound in world units (~13500)
  const REF_D = 640;                   // distance at which a source halves
  const MAX_D = 6500;                  // farther than this: not played at all
  const STEP_MAX_D = 1500;             // CS2-ish footstep audibility radius
  const STEP_LEN = 76;                 // world units travelled per footstep
  const RUN_SPEED = 135;               // u/s above which a step reads as running
  const MAX_STEP_S = 0.40;             // clock jump above this = scrub, not play
  // Hard cap on simultaneous one-shots. One gunshot costs 4 voices (crack,
  // body noise, body tone, room tail) and a 5v5 exchange keeps a dozen shots in
  // flight, so a low cap audibly swallows gunfire.
  const MAX_VOICES = 96;
  const DECOY_PERIOD = 0.62;           // seconds between a decoy's fake shots
  const DECOY_GUN = 'AK';              // the data does not say what a decoy imitates
  const MAX_EV_PER_FRAME = 64;         // runaway guard for the event cursor
  const STEP_BUDGET = 4;               // max footsteps started in one frame
  const MIN_SAMPLE_GAIN = 0.002;       // below this a sample is not worth a voice
  const STEAL_FADE_S = 0.1;            // CS2's fade when a gun's next shot cuts its last
  const SAMPLE_SEND = 0.1;             // CS2's files carry their own tails: barely any reverb
  // CS2's files are mastered hot, and the compressor's makeup gain was set for the
  // quieter synth: at full level close gunfire clips before the compressor reacts.
  const SAMPLE_HEADROOM = 0.7;
  const SMOKE_S = 2.5;                 // how much of the 14 s smoke hiss is played
  const C4_FUSE_S = 40;                // used when the data does not say when it blew
  // Interval between beeps, by the share of the fuse that is left. Remembered from
  // the CS:GO code rather than read from CS2, as are the times of the warnings.
  const c4Interval = left => Math.max(0.15, 0.1 + 0.9 * left);
  const C4_WARN_BEFORE_S = 1.5, C4_TRIP_BEFORE_S = 1;

  // Weapon display tags (weaponTable) -> synth class; same tag vocabulary as
  // extract/src/weapons.rs. An unknown tag on a shot record still fires as a
  // rifle (something clearly went off), unknown non-shooters stay silent.
  const WCLASS = {
    AK: 'rifle', M4: 'rifle', M4S: 'rifle_s', AUG: 'rifle', SG553: 'rifle',
    FAMAS: 'rifle', GALIL: 'rifle',
    AWP: 'sniper', SCOUT: 'scout', SCAR: 'sniper', G3: 'sniper',
    MP9: 'smg', MP7: 'smg', MP5: 'smg_s', MAC10: 'smg', UMP: 'smg',
    P90: 'smg', BIZON: 'smg',
    GLOCK: 'pistol', USP: 'pistol_s', P2000: 'pistol', P250: 'pistol',
    FIVE7: 'pistol', TEC9: 'pistol', CZ: 'pistol', DUALS: 'pistol',
    DEAG: 'deagle', R8: 'deagle',
    NOVA: 'shotgun', XM: 'shotgun', SAWED: 'shotgun', MAG7: 'shotgun',
    M249: 'mg', NEGEV: 'mg',
    ZEUS: 'zeus', '🔪': 'knife',
  };
  // per-class synth parameters: g=peak, crack=muzzle band, b0/b1=body sweep,
  // dur=core length, tail=room tail, send=reverb amount, hi=crack brightness
  const GUN = {
    rifle:    { g: 0.85, crack: 2700, hi: 0.90, b0: 155, b1: 58, dur: 0.100, tail: 0.30, send: 0.55 },
    rifle_s:  { g: 0.55, crack: 2100, hi: 0.60, b0: 140, b1: 62, dur: 0.080, tail: 0.16, send: 0.30 },
    sniper:   { g: 1.00, crack: 1750, hi: 1.00, b0: 112, b1: 40, dur: 0.170, tail: 0.60, send: 0.85 },
    scout:    { g: 0.80, crack: 2300, hi: 0.90, b0: 130, b1: 48, dur: 0.120, tail: 0.40, send: 0.65 },
    smg:      { g: 0.58, crack: 3300, hi: 0.80, b0: 195, b1: 82, dur: 0.065, tail: 0.16, send: 0.38 },
    smg_s:    { g: 0.42, crack: 2600, hi: 0.50, b0: 170, b1: 80, dur: 0.055, tail: 0.10, send: 0.22 },
    pistol:   { g: 0.55, crack: 3050, hi: 0.80, b0: 215, b1: 92, dur: 0.060, tail: 0.15, send: 0.38 },
    pistol_s: { g: 0.38, crack: 2400, hi: 0.45, b0: 190, b1: 88, dur: 0.050, tail: 0.09, send: 0.20 },
    deagle:   { g: 0.95, crack: 2050, hi: 1.00, b0: 125, b1: 45, dur: 0.140, tail: 0.45, send: 0.70 },
    shotgun:  { g: 0.90, crack: 1500, hi: 0.90, b0: 135, b1: 50, dur: 0.190, tail: 0.35, send: 0.60 },
    mg:       { g: 0.70, crack: 2450, hi: 0.85, b0: 165, b1: 68, dur: 0.085, tail: 0.22, send: 0.48 },
  };

  const BUSES = ['shots', 'steps', 'util', 'hits', 'voice', 'ui'];
  // Defaults: GUNFIRE ONLY. Everything else starts at 0 and is opt-in from the
  // mixer — the first pass had a room-tone bed and molotov/smoke loops running
  // constantly, which read as a loud fan hum and hurt to listen to. The room
  // tone is gone from the code entirely; the fire/smoke beds went with it.
  const DEFAULTS = {
    on: true, master: 0.75, reverb: 0.5, propagation: true, tinnitus: true,
    shots: 0.9, steps: 0, util: 0, hits: 0, voice: 0, ui: 0,
  };
  // What changes when the real sounds are there, unless the user set the slider.
  const SAMPLE_MIX = { steps: 0.8, util: 0.9, hits: 0.8 };
  // Bumped whenever DEFAULTS change meaning: a stored older mix would otherwise
  // resurrect the settings the user just asked to get rid of.
  const STORE_KEY = 'replay3d.sfx.v3';

  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const rnd = (a, b) => a + Math.random() * (b - a);
  // "ZEUS X" in rounds extracted before the tag was shortened.
  const tagOf = w => (w || '').split(' ')[0];

  // ------------------------------------------------------------------- state
  const cfg = Object.assign({}, DEFAULTS);
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {};
    for (const k in DEFAULTS) if (k in stored) cfg[k] = stored[k];
  } catch (_) { /* private mode / bad json: defaults */ }
  const save = () => { try {
    localStorage.setItem(STORE_KEY, JSON.stringify(cfg));
  } catch (_) {} };

  let ctx = null, master = null, duckGain = null, duckLp = null, comp = null;
  let revConv = null, revGain = null, noiseBuf = null;
  const bus = {};                      // name -> GainNode
  // Voice cap by SCHEDULED END TIME rather than an onended counter: onended is
  // not guaranteed to fire for every node, and a single missed callback would
  // leak the counter until the whole engine went silent.
  let voiceEnds = [];

  // round data + playback bookkeeping
  let R = null;                        // {rate, dur, names, teams, wtab, winner}
  let HDR = 7;                         // floats before the players in the frame payload
  let events = [];                     // sorted [{t, k, ...}]
  let evCur = 0;
  let loops = [];                      // [{t0,t1,type,x,y,nodes,nextFake}]
  let lastClock = -1, speed = 1;
  let camSlot = -1, camFirst = true;
  // per-slot state: cur* is THIS frame (used for positioning), prev* is the
  // frame before (deltas: step travel, damage, death, weapon swap). Both reset
  // per round, so a new round never inherits the old one's HP/positions.
  let curX = [], curY = [], curAlive = [], curWpn = [], curPresent = [];
  let prevX = [], prevY = [], prevHP = [], prevAlive = [], prevWpn = [];
  let stepAcc = [];
  let prevFlash = 0;
  let earX = 0, earY = 0, earYaw = 0, earOk = false;
  // Where the bomb was last seen in someone's hands, and where it was planted.
  let lastC4 = null, bombAt = null;

  // real sounds: null until useSamples() has its index
  let bank = null;                     // {base, sounds, buffers, last}
  let roundTags = [];                  // guns of this round, to load their sounds
  const live = {};                     // 'key/source' -> voices it still holds

  // ============================================================= audio graph
  function ensureCtx() {
    if (ctx) { if (ctx.state === 'suspended') ctx.resume(); return ctx; }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC({ latencyHint: 'interactive' });

    master = ctx.createGain();
    master.gain.value = cfg.on ? cfg.master : 0;
    // flash duck: everything gets quieter AND muffled while you are blinded
    duckGain = ctx.createGain(); duckGain.gain.value = 1;
    duckLp = ctx.createBiquadFilter(); duckLp.type = 'lowpass'; duckLp.frequency.value = 20000;
    comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -14; comp.knee.value = 22;
    comp.ratio.value = 4; comp.attack.value = 0.004; comp.release.value = 0.16;
    // Last in line: a grenade next to a firefight still overshoots the compressor.
    const limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -3; limiter.knee.value = 0;
    limiter.ratio.value = 20; limiter.attack.value = 0.001; limiter.release.value = 0.08;
    master.connect(duckGain); duckGain.connect(duckLp);
    duckLp.connect(comp); comp.connect(limiter); limiter.connect(ctx.destination);

    for (const b of BUSES) {
      const g = ctx.createGain(); g.gain.value = cfg[b] != null ? cfg[b] : 1;
      g.connect(master); bus[b] = g;
    }
    // map reverb: procedural impulse response (nothing to download)
    revConv = ctx.createConvolver(); revConv.buffer = makeIR(1.9, 2.6);
    revGain = ctx.createGain(); revGain.gain.value = cfg.reverb;
    revConv.connect(revGain); revGain.connect(master);

    noiseBuf = makeNoise(2.0);
    return ctx;
  }

  function makeNoise(sec) {
    const b = ctx.createBuffer(1, Math.floor(ctx.sampleRate * sec), ctx.sampleRate);
    const d = b.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    return b;
  }
  /** Stereo exponential-decay noise IR plus a few early reflections: a big,
   *  hard, mostly-concrete room — which is what a de_* map sounds like. */
  function makeIR(sec, decay) {
    const n = Math.floor(ctx.sampleRate * sec);
    const b = ctx.createBuffer(2, n, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = b.getChannelData(ch);
      for (let i = 0; i < n; i++) {
        const t = i / n;
        d[i] = (Math.random() * 2 - 1) * Math.pow(1 - t, decay);
      }
      // early reflections give the tail walls instead of a formless wash
      for (const ref of [[11, 0.50], [23, 0.38], [37, 0.28], [61, 0.20], [97, 0.13]]) {
        const i = Math.floor(ctx.sampleRate * (ref[0] + (ch ? 3.5 : 0)) / 1000);
        if (i < n) d[i] += ref[1] * (ch ? -1 : 1);
      }
    }
    return b;
  }

  // --------------------------------------------------------- voice plumbing
  const distGain = d => 1 / (1 + Math.pow(Math.max(0, d) / REF_D, 1.35));
  const distCut = d => Math.max(650, 19000 * Math.exp(-Math.max(0, d) / 2100));

  /** Tail of a positioned voice; returns the node to connect INTO.
   *  `p` = {pan, dist, bus, send}. */
  function place(p) {
    const g = ctx.createGain();
    g.gain.value = p.gain != null ? p.gain : distGain(p.dist);
    const lp = ctx.createBiquadFilter();
    lp.type = 'lowpass'; lp.frequency.value = p.noAir ? 20000 : distCut(p.dist);
    const pan = ctx.createStereoPanner();
    pan.pan.value = clamp(p.pan || 0, -1, 1);
    g.connect(lp); lp.connect(pan); pan.connect(bus[p.bus] || bus.shots);
    if (p.send > 0 && revConv) {
      const s = ctx.createGain();
      // distant sources are mostly room — cheap "far away = more reverb"
      s.gain.value = p.send * (0.5 + 0.5 * Math.min(1, p.dist / 2500));
      pan.connect(s); s.connect(revConv);
    }
    return g;
  }

  /** Claim a voice slot; false when the bus is muted or too many are scheduled.
   *  Checking the bus HERE means a zeroed slider costs nothing at all — no
   *  nodes are built for a category you cannot hear. */
  function claim(end, busName) {
    if (!(cfg[busName || 'shots'] > 0)) return false;
    if (voiceEnds.length >= MAX_VOICES) {
      const now = ctx.currentTime;
      voiceEnds = voiceEnds.filter(e => e > now);
      if (voiceEnds.length >= MAX_VOICES) return false;
    }
    voiceEnds.push(end);
    return true;
  }

  /** One shaped noise burst (optionally swept filter). */
  function burst(o) {
    if (!claim(o.t + o.dur, o.bus)) return;
    const t = o.t, dur = o.dur;
    const src = ctx.createBufferSource();
    src.buffer = noiseBuf;
    src.playbackRate.value = o.rate || 1;
    const f = ctx.createBiquadFilter();
    f.type = o.type || 'bandpass';
    f.frequency.setValueAtTime(o.f0, t);
    if (o.f1 && o.f1 !== o.f0) f.frequency.exponentialRampToValueAtTime(Math.max(30, o.f1), t + dur);
    f.Q.value = o.q != null ? o.q : 1;
    const env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, t);
    env.gain.exponentialRampToValueAtTime(Math.max(0.0002, o.g), t + (o.a || 0.002));
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(f); f.connect(env); env.connect(place(o));
    src.start(t, Math.random() * 1.5, dur + 0.02);
  }

  /** One shaped oscillator tone (optionally swept). */
  function tone(o) {
    if (!claim(o.t + o.dur, o.bus)) return;
    const t = o.t, dur = o.dur;
    const osc = ctx.createOscillator();
    osc.type = o.wave || 'sine';
    osc.frequency.setValueAtTime(o.f0, t);
    if (o.f1 && o.f1 !== o.f0) osc.frequency.exponentialRampToValueAtTime(Math.max(20, o.f1), t + dur);
    const env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, t);
    env.gain.exponentialRampToValueAtTime(Math.max(0.0002, o.g), t + (o.a || 0.004));
    env.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(env); env.connect(place(o));
    osc.start(t); osc.stop(t + dur + 0.02);
  }

  // ============================================================== real sounds
  /** Load the real sounds from `base`. Before mountUI, so the sliders show the mix.
   *  If the index then fails to load, the raised sliders simply drive the synth. */
  function useSamples(base) {
    for (const k in SAMPLE_MIX) if (!(k in stored)) cfg[k] = SAMPLE_MIX[k];
    return fetch(base + 'index.json')
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(index => {
        bank = { base, sounds: index.sounds || {}, buffers: {}, last: {} };
        loadRoundSounds();
      })
      .catch(err => console.warn('[sfx] no real sounds, using the synth:', err));
  }

  /** Decoding needs no user gesture on an offline context, unlike a live one, so
   *  the sounds are ready before the first click starts the audio. */
  function loadFile(name) {
    if (name in bank.buffers) return;
    bank.buffers[name] = null;                       // asked for: one fetch per file
    const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!OAC) return;
    fetch(bank.base + name)
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
      .then(data => new OAC(2, 1, 48000).decodeAudioData(data))
      .then(buffer => { bank.buffers[name] = buffer; })
      .catch(err => console.warn('[sfx] ' + name + ':', err));   // stays synthesized
  }

  function loadRoundSounds() {
    if (!bank) return;
    for (const key of Object.keys(bank.sounds)) {
      if (!/^(shot|far):[A-Z0-9]+$/.test(key)) bank.sounds[key].f.forEach(loadFile);
    }
    for (const tag of roundTags) {
      for (const key of ['shot:' + tag, 'far:' + tag]) {
        if (bank.sounds[key]) bank.sounds[key].f.forEach(loadFile);
      }
    }
  }

  /** A decoded file of this sound, not the same one twice running if there is a choice. */
  function pickBuffer(key, entry) {
    const ready = entry.f.filter(name => bank.buffers[name]);
    if (!ready.length) return null;
    let name = ready[Math.floor(Math.random() * ready.length)];
    if (name === bank.last[key] && ready.length > 1) {
      name = ready[(ready.indexOf(name) + 1) % ready.length];
    }
    bank.last[key] = name;
    return bank.buffers[name];
  }

  function curveAt(curve, dist) {
    if (dist <= curve[0][0]) return curve[0][1];
    for (let i = 1; i < curve.length; i++) {
      if (dist <= curve[i][0]) {
        const a = curve[i - 1], b = curve[i];
        return a[1] + (b[1] - a[1]) * (dist - a[0]) / (b[0] - a[0]);
      }
    }
    return curve[curve.length - 1][1];
  }

  /** CS2 limits how many voices of a sound one source holds: a gun's next shot cuts
   *  its last with a short fade. Files here run for seconds, so without this one long
   *  burst would fill every voice. */
  function holdVoice(key, source, limit, when, voice) {
    const id = key + '/' + source;
    const held = (live[id] || []).filter(v => v.end > when);
    while (held.length >= limit) {
      const old = held.shift();
      old.gain.gain.setValueAtTime(old.level, when);
      old.gain.gain.linearRampToValueAtTime(0, when + STEAL_FADE_S);
      old.node.stop(when + STEAL_FADE_S);
      const i = voiceEnds.indexOf(old.end);
      if (i >= 0) voiceEnds[i] = when + STEAL_FADE_S;
    }
    held.push(voice);
    live[id] = held;
  }

  /** Play a real sound. False means there is none ready, so the caller synthesizes.
   *  True includes a sound its own curve makes silent at this distance. */
  function sample(key, when, pos, opts) {
    const entry = bank && bank.sounds[key];
    if (!entry) return false;
    const buffer = pickBuffer(key, entry);
    if (!buffer) return false;
    const o = opts || {};
    const busName = o.bus || 'shots';
    const level = SAMPLE_HEADROOM * entry.v * (entry.c ? curveAt(entry.c, pos.dist) : distGain(pos.dist));
    if (level < MIN_SAMPLE_GAIN) return true;
    const rate = entry.p + (entry.r ? rnd(entry.r[0], entry.r[1]) : 0);
    const length = buffer.duration / rate;
    const end = when + (o.cutAfter ? Math.min(length, o.cutAfter) : length);
    if (!claim(end, busName)) return true;
    const gain = place({ pan: pos.pan, dist: pos.dist, bus: busName, send: o.send || 0,
      gain: level, noAir: o.noAir });
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.playbackRate.value = rate;
    node.connect(gain);
    node.start(when);
    if (o.cutAfter && o.cutAfter < length) {
      gain.gain.setValueAtTime(level, end - 0.8);
      gain.gain.linearRampToValueAtTime(0, end);
      node.stop(end);
    }
    if (o.source != null && entry.n) holdVoice(key, o.source, entry.n, when, { gain, node, level, end });
    return true;
  }

  /** A gun's near sound and its distant one, each fading by its own curve. */
  function gunSample(tag, when, pos, source) {
    if (!sample('shot:' + tag, when, pos, { send: SAMPLE_SEND, source })) return false;
    sample('far:' + tag, when, pos, { noAir: true, source });
    return true;
  }

  // ============================================================ sound design
  function gunshot(when, cls, pos) {
    const P = GUN[cls] || GUN.rifle;
    const v = rnd(0.94, 1.06);                    // per-shot variance
    const g = P.g * v * (pos.own ? 1.15 : 1);
    const base = { t: when, pan: pos.pan, dist: pos.dist, bus: 'shots' };
    // 1) muzzle crack — the part that carries far and localizes the shot
    burst(Object.assign({}, base, {
      f0: P.crack * v, f1: P.crack * 0.55, q: 1.1, dur: P.dur * 0.55,
      g: g * 0.9 * P.hi, a: 0.0012, send: P.send * 0.7,
    }));
    // 2) body — low noise plus a fast downward sweep = the charge's thump
    burst(Object.assign({}, base, {
      type: 'lowpass', f0: P.b0 * 6, f1: P.b0 * 2.4, q: 0.7,
      dur: P.dur, g: g * 0.8, a: 0.002, send: P.send * 0.5,
    }));
    tone(Object.assign({}, base, {
      f0: P.b0 * v, f1: P.b1, dur: P.dur * 1.5, g: g * 0.5, send: P.send * 0.4,
    }));
    // 3) tail into the room (the reverb send does most of the work)
    burst(Object.assign({}, base, {
      type: 'lowpass', f0: 1500, f1: 500, dur: P.tail, g: g * 0.22,
      a: 0.012, send: P.send,
    }));
  }

  function knifeSwing(when, pos) {
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'shots', type: 'bandpass',
      f0: 1800, f1: 4200, q: 0.8, dur: 0.16, g: 0.42, a: 0.03, send: 0.25 });
  }

  function zeusZap(when, pos) {
    for (let i = 0; i < 7; i++) {
      burst({ t: when + i * 0.018, pan: pos.pan, dist: pos.dist, bus: 'shots',
        type: 'highpass', f0: 1200 + Math.random() * 2600, q: 0.6,
        dur: 0.03, g: 0.5 * (1 - i / 9), a: 0.001, send: 0.35 });
    }
  }

  /** Footsteps are by far the most frequent sound (ten players, ~3/s each), so
   *  the extra layers are spent only where they are actually audible. */
  function footstep(when, pos) {
    const g = 0.5 * rnd(0.85, 1.15);
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'steps', type: 'bandpass',
      f0: rnd(620, 1150), q: 1.5, dur: 0.05, g, a: 0.002,
      send: pos.dist < 1200 ? 0.3 : 0 });
    if (pos.dist < 900) {                            // scuff/grit of the sole
      burst({ t: when + 0.008, pan: pos.pan, dist: pos.dist, bus: 'steps',
        type: 'highpass', f0: rnd(3200, 5200), q: 0.7, dur: 0.03,
        g: g * 0.45, a: 0.001, send: 0 });
    }
  }

  function bodyFall(when, pos) {
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'hits', type: 'lowpass',
      f0: 320, f1: 140, dur: 0.28, g: 0.5, a: 0.006, send: 0.45 });
    tone({ t: when, pan: pos.pan, dist: pos.dist, bus: 'hits',
      f0: 74, f1: 44, dur: 0.24, g: 0.34, send: 0.3 });
  }

  function bulletImpact(when, pos, heavy) {
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'hits', type: 'bandpass',
      f0: heavy ? 420 : 700, q: 1.2, dur: heavy ? 0.09 : 0.05,
      g: heavy ? 0.42 : 0.30, a: 0.0015, send: 0.3 });
  }

  /** Taking damage yourself: dull thud plus a low "air out of the lungs". */
  function ownHurt(when, amount) {
    const g = clamp(0.25 + amount / 90, 0.25, 0.8);
    burst({ t: when, pan: 0, dist: 0, bus: 'voice', type: 'lowpass',
      f0: 520, f1: 190, dur: 0.22, g, a: 0.004, send: 0.15 });
    tone({ t: when + 0.01, pan: 0, dist: 0, bus: 'voice', wave: 'triangle',
      f0: rnd(150, 190), f1: 96, dur: 0.30, g: g * 0.45, send: 0.1 });
  }

  function explosion(when, pos, big) {
    const s = big ? 1 : 0.7;
    tone({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util',
      f0: 120 * s, f1: 26, dur: 0.75, g: 0.95 * s, a: 0.004, send: 1 });
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util', type: 'lowpass',
      f0: 3000, f1: 180, q: 0.6, dur: 0.55, g: 0.85 * s, a: 0.003, send: 1 });
    for (let i = 0; i < 6; i++) {                 // debris ticks
      burst({ t: when + rnd(0.06, 0.42), pan: pos.pan + rnd(-0.2, 0.2), dist: pos.dist,
        bus: 'util', type: 'bandpass', f0: rnd(1400, 4200), q: 1.4, dur: 0.04,
        g: 0.14 * s, a: 0.001, send: 0.5 });
    }
  }

  function flashPop(when, pos) {
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util', type: 'highpass',
      f0: 900, q: 0.7, dur: 0.30, g: 0.8, a: 0.001, send: 0.9 });
    tone({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util',
      f0: 220, f1: 70, dur: 0.20, g: 0.4, send: 0.5 });
  }

  /** The ringing when YOU get flashed (not positioned — it is inside your head). */
  function tinnitus(when, strength) {
    if (!cfg.tinnitus || !(cfg.voice > 0)) return;
    const dur = 2.2 + 2.4 * strength;
    for (const p of [[4300, 0.13], [6450, 0.075], [2870, 0.05]]) {
      const osc = ctx.createOscillator();
      osc.frequency.value = p[0] * rnd(0.98, 1.02);
      const env = ctx.createGain();
      env.gain.setValueAtTime(0.0001, when);
      env.gain.exponentialRampToValueAtTime(Math.max(0.0002, p[1] * strength), when + 0.05);
      env.gain.exponentialRampToValueAtTime(0.0001, when + dur);
      osc.connect(env); env.connect(bus.voice);
      osc.start(when); osc.stop(when + dur + 0.05);
    }
  }

  function throwWhoosh(when, pos) {
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util', type: 'bandpass',
      f0: 700, f1: 2100, q: 0.9, dur: 0.22, g: 0.3, a: 0.05, send: 0.3 });
    burst({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util', type: 'highpass',
      f0: 5200, q: 0.8, dur: 0.03, g: 0.16, a: 0.001, send: 0.15 });   // the pin
  }

  function c4Beep(when, pos) {
    tone({ t: when, pan: pos.pan, dist: pos.dist, bus: 'util', f0: 2100, dur: 0.1, g: 0.25, a: 0.002 });
  }

  /** Kill confirmation for the spectated player (CS2's kill ding). */
  function killDing(when, hs) {
    const f = hs ? 1560 : 1080;
    tone({ t: when, pan: 0, dist: 0, bus: 'voice', f0: f, dur: 0.13, g: 0.30, a: 0.002, send: 0.1 });
    tone({ t: when + 0.005, pan: 0, dist: 0, bus: 'voice', f0: f * 2, dur: 0.09, g: 0.13, send: 0.08 });
    if (hs) burst({ t: when, pan: 0, dist: 0, bus: 'voice', type: 'highpass',
      f0: 5200, dur: 0.05, g: 0.2, a: 0.001, send: 0.1 });
  }

  function roundStart(when) {
    tone({ t: when, pan: 0, dist: 0, bus: 'ui', f0: 700, dur: 0.10, g: 0.22, send: 0.2 });
    tone({ t: when + 0.13, pan: 0, dist: 0, bus: 'ui', f0: 1050, dur: 0.14, g: 0.22, send: 0.2 });
  }

  function roundEndSting(when, winnerCT) {
    const ch = winnerCT ? [523.25, 659.25, 783.99] : [493.88, 587.33, 698.46];
    ch.forEach((f, i) => tone({
      t: when + i * 0.05, pan: 0, dist: 0, bus: 'ui', wave: 'triangle',
      f0: f, dur: 1.1, g: 0.17, a: 0.02, send: 0.5,
    }));
  }

  function uiSound(kind) {
    if (!cfg.on || !ctx || ctx.state !== 'running') return;
    const t = ctx.currentTime + 0.001;
    if (kind === 'seek') {
      tone({ t, pan: 0, dist: 0, bus: 'ui', f0: 520, dur: 0.05, g: 0.12 });
    } else if (kind === 'toggle') {
      tone({ t, pan: 0, dist: 0, bus: 'ui', f0: 880, dur: 0.05, g: 0.13 });
      tone({ t: t + 0.055, pan: 0, dist: 0, bus: 'ui', f0: 1320, dur: 0.06, g: 0.11 });
    } else {
      tone({ t, pan: 0, dist: 0, bus: 'ui', f0: 1180, dur: 0.035, g: 0.10 });
      burst({ t, pan: 0, dist: 0, bus: 'ui', type: 'highpass', f0: 4200, dur: 0.02, g: 0.06 });
    }
  }

  // ============================================================== positioning
  /** Ear pose for this frame: the spectated player, else the living centroid. */
  function updateEar(d, nP) {
    if (camSlot >= 0 && camSlot < nP && curPresent[camSlot]) {
      earX = curX[camSlot]; earY = curY[camSlot];
      earYaw = d[HDR + camSlot * 10 + 8] * Math.PI / 180;
      earOk = true; return;
    }
    let sx = 0, sy = 0, n = 0;
    for (let i = 0; i < nP; i++) if (curPresent[i] && curAlive[i]) { sx += curX[i]; sy += curY[i]; n++; }
    if (n) { earX = sx / n; earY = sy / n; earOk = true; } else earOk = false;
    earYaw = NaN;                                    // unknown -> pan from screen x
  }

  /** Placement of a world point. `sx` (viewport 0..1) supplies the pan when the
   *  ear has no orientation (free camera). */
  function at(x, y, sx) {
    if (!earOk) return { pan: 0, dist: 900, delay: 0, own: false };
    const dx = x - earX, dy = y - earY;
    let dist = Math.sqrt(dx * dx + dy * dy);
    let pan;
    if (isFinite(earYaw)) {
      // Source yaw grows counter-clockwise, so a target to the RIGHT of the
      // player sits at a negative relative angle -> positive (right) pan.
      pan = -Math.sin(Math.atan2(dy, dx) - earYaw) * 0.92;
    } else {
      pan = (sx != null && sx > 0 && sx < 1) ? (sx * 2 - 1) * 0.85 : 0;
      dist *= 0.65;                                  // free cam: the ear floats nearer
    }
    return { pan, dist, delay: cfg.propagation ? dist / SOUND_U_PER_S : 0, own: false };
  }
  const atSlot = (i, d) => at(curX[i], curY[i], d ? d[HDR + i * 10] : null);
  const AT_EAR = () => ({ pan: 0, dist: 0, delay: 0, own: true });

  // =============================================================== scheduling
  /** Called from loadRound: rebuild the whole event list for the new round. */
  function setRound(header, meta) {
    R = null; events = []; loops = [];
    evCur = 0; lastClock = -1;
    curX = []; curY = []; curAlive = []; curWpn = []; curPresent = [];
    prevX = []; prevY = []; prevHP = []; prevAlive = []; prevWpn = [];
    stepAcc = []; prevFlash = 0;
    lastC4 = null; bombAt = null;
    for (const id of Object.keys(live)) delete live[id];
    if (!header || !meta) return;

    const rate = meta.sampleRate || header.sampleRate || 16;
    const bomb = meta.bomb && meta.bomb.plant != null ? meta.bomb : null;
    R = {
      rate, dur: meta.durationS || 0,
      names: (header.players || []).map(p => p.name || ''),
      teams: (header.players || []).map(p => p.team),
      wtab: header.weaponTable || [], winner: meta.winner || null,
      bombBlast: !!(bomb && bomb.exploded != null),
      bombPos: bomb && bomb.pos ? { x: bomb.pos[0], y: bomb.pos[1] } : null,
    };
    const tags = new Set(R.wtab.map(tagOf));

    for (const s of (header.shots || [])) {
      if (s && s.s != null) events.push({ t: s.s / rate, k: 'shot', slot: s.p | 0 });
    }
    for (const u of (header.utility || [])) {
      if (!u || !u.pos) continue;
      const x = u.pos[0], y = u.pos[1];
      if (u.detSample != null) {
        const t0 = u.detSample / rate, dur = +u.duration || 0;
        events.push({ t: t0, k: 'det', type: u.type, x, y });
        // a decoy keeps firing fake shots for its whole lifetime (still gunfire,
        // so it survived the "gunshots only" cut; smoke/fire beds did not)
        if (dur > 0 && u.type === 'decoy') {
          loops.push({ t0, t1: t0 + dur, x, y, nextFake: 0 });
          tags.add(DECOY_GUN);
        }
        if (dur > 0 && u.type === 'molotov') {
          events.push({ t: t0 + dur, k: 'fire_out', x, y });
        }
      }
      if (u.throwSample != null && u.throwPos) {
        events.push({ t: u.throwSample / rate, k: 'throw', type: u.type,
          x: u.throwPos[0], y: u.throwPos[1] });
      }
    }
    for (const kk of (meta.kills || [])) {
      events.push({ t: kk.sample / rate, k: 'kill', atk: kk.atk, vic: kk.vic,
        hs: !!kk.hs, w: kk.w || '' });
    }
    if (bomb) events.push(...bombEvents(bomb, rate, R.dur));
    events.push({ t: 0.35, k: 'start' });
    if (R.dur > 0 && R.winner) events.push({ t: R.dur, k: 'end', ct: R.winner === 'CT' });
    events.sort((a, b) => a.t - b.t);

    roundTags = Array.from(tags);
    loadRoundSounds();
  }

  /** The bomb's sounds, from the plant to whatever ended it. */
  function bombEvents(bomb, rate, roundDur) {
    const plant = bomb.plant / rate;
    const blown = bomb.exploded != null;
    const blow = blown ? bomb.exploded / rate : plant + C4_FUSE_S;
    const fuse = Math.max(1, blow - plant);
    let stop = blow;
    if (bomb.defused != null) stop = bomb.defused / rate;
    else if (!blown && roundDur > 0) stop = Math.min(blow, roundDur);

    const out = [{ t: plant, k: 'c4', s: 'plant' }];
    for (let t = plant; t < stop; t += c4Interval((blow - t) / fuse)) {
      out.push({ t, k: 'c4', s: 'beep' });
    }
    if (bomb.defused != null) out.push({ t: stop, k: 'c4', s: 'defused' });
    if (blown) {
      out.push({ t: blow - C4_WARN_BEFORE_S, k: 'c4', s: 'warn' });
      out.push({ t: blow - C4_TRIP_BEFORE_S, k: 'c4', s: 'trip' });
      out.push({ t: blow, k: 'c4', s: 'explode' });
    }
    return out;
  }

  /** Scrub / round change: forget where we were. */
  function resync() {
    lastClock = -1;
    loops.forEach(l => { l.nextFake = 0; });
    for (let i = 0; i < stepAcc.length; i++) stepAcc[i] = 0;
  }

  const slotOfName = name => R ? R.names.indexOf(name) : -1;

  /** Play one timeline event at absolute AudioContext time `when`. */
  function fire(ev, when, d) {
    switch (ev.k) {
      case 'shot': {
        const i = ev.slot;
        if (i < 0 || i >= curX.length || !curPresent[i]) return;
        const tag = tagOf(R.wtab[curWpn[i]]);
        const cls = WCLASS[tag] || 'rifle';
        const own = i === camSlot;
        const pos = own ? AT_EAR() : atSlot(i, d);
        if (pos.dist > MAX_D) return;
        const t = when + pos.delay;
        if (cls === 'knife') { if (!sample('knife', t, pos)) knifeSwing(t, pos); }
        else if (cls === 'zeus') { if (!sample('shot:ZEUS', t, pos)) zeusZap(t, pos); }
        else if (!gunSample(tag, t, pos, i)) gunshot(t, cls, pos);
        return;
      }
      case 'det': {
        const pos = at(ev.x, ev.y, null);
        if (pos.dist > MAX_D) return;
        const t = when + pos.delay;
        const util = { bus: 'util', send: SAMPLE_SEND }, far = { bus: 'util', noAir: true };
        // The synth stays silent on smoke and molotov: its loops were the "fan hum"
        // and the ear-splitting hiss of the first pass. The real sounds are one-shots.
        if (ev.type === 'he') {
          if (!sample('he', t, pos, util)) explosion(t, pos, true);
          sample('far:he', t, pos, far);
        } else if (ev.type === 'flash') {
          if (!sample('flash', t, pos, util)) flashPop(t, pos);
          sample('far:flash', t, pos, far);
        } else if (ev.type === 'smoke') {
          sample('smoke', t, pos, { bus: 'util', cutAfter: SMOKE_S });
          sample('far:smoke', t, pos, far);
        } else if (ev.type === 'molotov') {
          sample('molotov', t, pos, util);
        } else if (ev.type === 'decoy') {
          burst({ t, pan: pos.pan, dist: pos.dist, bus: 'util', type: 'bandpass',
            f0: 1600, q: 0.8, dur: 0.25, g: 0.3, a: 0.01, send: 0.3 });
        }
        return;
      }
      case 'fire_out': {
        const pos = at(ev.x, ev.y, null);
        if (pos.dist <= MAX_D) sample('molotov:end', when + pos.delay, pos, { bus: 'util' });
        return;
      }
      case 'throw': {
        const pos = at(ev.x, ev.y, null);
        if (pos.dist < 2200 && !sample('throw:' + ev.type, when + pos.delay, pos, { bus: 'util' })) {
          throwWhoosh(when + pos.delay, pos);
        }
        return;
      }
      case 'c4': fireBomb(ev.s, when); return;
      case 'kill': {
        const vs = slotOfName(ev.vic);
        // A round without bomb data still says when the bomb killed someone: a kill
        // tagged "💣". Put the blast on the victim, who was standing next to the bomb.
        if (ev.w === '💣' && !R.bombBlast) {
          const bp = vs >= 0 && curPresent[vs] ? atSlot(vs, d) : { pan: 0, dist: 700, delay: 0 };
          if (!sample('c4:explode', when + bp.delay, bp, { bus: 'util' })) explosion(when + bp.delay, bp, true);
        }
        if (vs >= 0 && curPresent[vs] && ev.w !== '💣') {
          const pos = vs === camSlot ? AT_EAR() : atSlot(vs, d);
          const hit = { bus: 'hits' };
          if (ev.hs) sample('kill:hs', when + pos.delay, pos, hit);
          else sample('kill:body', when + pos.delay, pos, hit);
        }
        if (camSlot >= 0 && R.names[camSlot] === ev.atk) killDing(when, ev.hs);
        else if (vs >= 0 && vs === camSlot) {           // you died: dull low hit
          burst({ t: when, pan: 0, dist: 0, bus: 'voice', type: 'lowpass',
            f0: 300, f1: 90, dur: 0.5, g: 0.5, a: 0.005, send: 0.3 });
        }
        return;
      }
      case 'start': roundStart(when); return;
      case 'end': roundEndSting(when, ev.ct); return;
    }
  }

  function fireBomb(what, when) {
    if (what === 'plant') bombAt = lastC4;
    const src = R.bombPos || bombAt;
    const pos = src ? at(src.x, src.y, null) : { pan: 0, dist: 900, delay: 0 };
    const t = when + pos.delay, util = { bus: 'util' };
    if (what === 'plant') sample('c4:plant', t, pos, util);
    else if (what === 'beep') { if (!sample('c4:beep', t, pos, util)) c4Beep(t, pos); }
    else if (what === 'warn') sample('c4:warn', t, pos, util);
    else if (what === 'trip') sample('c4:trip', t, pos, util);
    else if (what === 'defused') sample('c4:defused', t, pos, util);
    else if (what === 'explode') { if (!sample('c4:explode', t, pos, util)) explosion(t, pos, true); }
  }

  // ==================================================================== frame
  function readPlayers(d, nP) {
    for (let i = 0; i < nP; i++) {
      const o = HDR + i * 10;
      curPresent[i] = d[o + 9] > 0.5;
      curAlive[i] = d[o + 3] > 0.5;
      curWpn[i] = d[o + 5] | 0;
      curX[i] = d[o + 6]; curY[i] = d[o + 7];
      if (curPresent[i] && curAlive[i] && R.wtab[curWpn[i]] === '💣') lastC4 = { x: curX[i], y: curY[i] };
    }
  }
  function snapshot(d, nP) {
    for (let i = 0; i < nP; i++) {
      const o = HDR + i * 10;
      prevX[i] = d[o + 6]; prevY[i] = d[o + 7]; prevHP[i] = d[o + 4] | 0;
      prevAlive[i] = d[o + 3] > 0.5; prevWpn[i] = d[o + 5] | 0;
    }
  }
  /** Move the cursor to `clock` without playing anything (scrub / (re)load). */
  function reanchor(clock, nP) {
    evCur = 0;
    while (evCur < events.length && events[evCur].t <= clock) evCur++;
    for (let i = 0; i < nP; i++) stepAcc[i] = 0;
    loops.forEach(l => { l.nextFake = 0; });
  }
  function fireEvents(clock, now, d) {
    let guard = 0;
    while (evCur < events.length && events[evCur].t <= clock && guard++ < MAX_EV_PER_FRAME) {
      const ev = events[evCur++];
      // spread the frame's events over their real spacing, compressed by speed
      const lead = Math.max(0, ev.t - lastClock) / Math.max(0.25, speed);
      fire(ev, now + lead, d);
    }
  }
  /** Footsteps, impacts, deaths and weapon swaps — all from frame deltas. */
  function playerSounds(d, nP, dc, now) {
    let stepBudget = STEP_BUDGET;                    // never flood one frame
    for (let i = 0; i < nP; i++) {
      if (!curPresent[i] || prevX[i] == null) continue;
      const own = i === camSlot;
      if (curAlive[i] && prevAlive[i]) {
        const dx = curX[i] - prevX[i], dy = curY[i] - prevY[i];
        const moved = Math.sqrt(dx * dx + dy * dy), sp = moved / dc;
        if (sp > RUN_SPEED && sp < 700) {            // walking is silent, as in the game
          stepAcc[i] = (stepAcc[i] || 0) + moved;
          if (stepAcc[i] >= STEP_LEN) {
            stepAcc[i] -= STEP_LEN;
            const pos = own ? { pan: rnd(-0.22, 0.22), dist: 0, delay: 0 } : atSlot(i, d);
            if ((own || pos.dist < STEP_MAX_D) && stepBudget-- > 0) {
              const key = R.teams[i] === 3 ? 'step:ct' : 'step:t';
              if (!sample(key, now + pos.delay, pos, { bus: 'steps' })) footstep(now + pos.delay, pos);
            }
          }
        } else stepAcc[i] = Math.min(stepAcc[i] || 0, STEP_LEN);
      }
      const hp = d[HDR + i * 10 + 4] | 0;
      const lost = prevHP[i] - hp;
      if (lost > 0 && curAlive[i]) {
        if (own) {
          if (!sample('hit:own', now, AT_EAR(), { bus: 'hits' })) ownHurt(now, lost);
        } else {
          const pos = atSlot(i, d);
          if (pos.dist < 3000 && !sample('hit:body', now + pos.delay, pos, { bus: 'hits' })) {
            bulletImpact(now + pos.delay, pos, lost >= 40);
          }
        }
      }
      if (prevAlive[i] && !curAlive[i]) {
        const pos = own ? AT_EAR() : atSlot(i, d);
        if ((own || pos.dist < 3500) && !sample('death', now + pos.delay, pos, { bus: 'hits' })) {
          bodyFall(now + pos.delay, pos);
        }
      }
      if (curWpn[i] !== prevWpn[i] && curAlive[i]) {  // weapon swap click
        const pos = own ? AT_EAR() : atSlot(i, d);
        if (own || pos.dist < 900) {
          burst({ t: now + pos.delay, pan: pos.pan, dist: pos.dist, bus: 'ui',
            type: 'bandpass', f0: 2600, q: 1.6, dur: 0.05, g: 0.16, a: 0.001, send: 0.15 });
        }
      }
    }
  }
  /** Decoy pot-shots: a state window rather than a single event. */
  function updateLoops(clock, playing, stepping, now) {
    if (!stepping) return;
    for (const l of loops) {
      if (clock < l.t0 || clock > l.t1 || clock < l.nextFake) continue;
      l.nextFake = clock + DECOY_PERIOD * rnd(0.75, 1.3);
      const pos = at(l.x, l.y, null);
      if (pos.dist < MAX_D && !gunSample(DECOY_GUN, now + pos.delay, pos)) {
        gunshot(now + pos.delay, 'rifle', pos);
      }
    }
  }
  function applyDuck(flashOp, now) {
    if (!duckGain) return;
    const f = Math.min(1, flashOp);
    duckGain.gain.setTargetAtTime(1 - 0.55 * f, now, 0.06);
    duckLp.frequency.setTargetAtTime(20000 - 17500 * f, now, 0.06);
  }

  /** Called once per rendered frame with the raw wasm payload. */
  function frame(d, nP, hdr) {
    if (hdr > 0) HDR = hdr;
    if (!cfg.on || !ctx || !R || ctx.state !== 'running') return;
    const clock = d[0], playing = d[2] > 0.5, flashOp = d[3];
    const now = ctx.currentTime + 0.02;              // small scheduling lead

    readPlayers(d, nP);
    updateEar(d, nP);

    const dc = clock - lastClock;
    const stepping = playing && lastClock >= 0 && dc > 0 && dc <= MAX_STEP_S * Math.max(1, speed);
    if (stepping) {
      fireEvents(clock, now, d);
      playerSounds(d, nP, dc, now);
      if (flashOp - prevFlash > 0.22 && flashOp > 0.35) tinnitus(now, Math.min(1, flashOp));
    } else if (lastClock < 0 || dc !== 0) {
      reanchor(clock, nP);                           // (re)load, scrub or rewind
    }
    updateLoops(clock, playing, stepping, now);
    applyDuck(flashOp, now);

    snapshot(d, nP);
    prevFlash = flashOp;
    lastClock = clock;
  }

  // ====================================================================== UI
  const CSS = `
  #sfxwrap { position:relative; display:inline-flex; gap:6px; }
  #sfxgear { padding:6px 9px; }
  #sfxpanel { position:absolute; bottom:38px; right:0; z-index:12; width:238px;
    padding:11px 13px 13px; background:var(--panel,#0d111a);
    border:1px solid var(--line,#232a36); border-radius:12px;
    box-shadow:0 10px 34px rgba(0,0,0,.5); backdrop-filter:blur(10px); display:none; }
  #sfxpanel.open { display:block; }
  #sfxpanel h4 { margin:0 0 8px; font-size:11px; font-weight:700; letter-spacing:.07em;
    text-transform:uppercase; color:var(--muted,#8b93a5); }
  #sfxpanel .r { display:grid; grid-template-columns:66px 1fr 32px; align-items:center;
    gap:8px; margin-top:6px; }
  #sfxpanel .r label { font-size:11px; color:var(--muted,#8b93a5); }
  #sfxpanel .r output { font-size:10px; color:var(--dim,#5f6875); text-align:right;
    font-variant-numeric:tabular-nums; }
  #sfxpanel input[type=range] { width:100%; height:14px; margin:0; accent-color:var(--accent,#2b6cff); }
  #sfxpanel .cb { display:flex; align-items:center; gap:7px; margin-top:9px; font-size:11px;
    color:var(--muted,#8b93a5); cursor:pointer; }
  #sfxpanel .sep { height:1px; background:var(--line,#232a36); margin:10px 0 2px; }`;

  function mountUI(host) {
    if (!host || document.getElementById('sfxwrap')) return;
    document.head.appendChild(Object.assign(document.createElement('style'), { textContent: CSS }));

    const wrap = document.createElement('span'); wrap.id = 'sfxwrap';
    const btn = document.createElement('button'); btn.id = 'sfxbtn';
    btn.title = 'Match audio: gunfire, footsteps, grenades, the bomb, hits (M)';
    const gear = document.createElement('button'); gear.id = 'sfxgear';
    gear.textContent = '⚙'; gear.title = 'Sound mixer';
    const panel = document.createElement('div'); panel.id = 'sfxpanel';
    wrap.append(btn, gear, panel);
    host.appendChild(wrap);

    const label = () => {
      btn.textContent = cfg.on ? '🔊 Sound' : '🔇 Sound';
      btn.classList.toggle('on', cfg.on);
    };
    label();
    btn.onclick = () => { setEnabled(!cfg.on); label(); };
    gear.onclick = () => panel.classList.toggle('open');

    panel.appendChild(Object.assign(document.createElement('h4'), { textContent: 'sound mixer' }));
    const slider = (key, text) => {
      const r = document.createElement('div'); r.className = 'r';
      const l = document.createElement('label'); l.textContent = text;
      const i = document.createElement('input');
      i.type = 'range'; i.min = 0; i.max = 1; i.step = 0.05; i.value = cfg[key];
      const o = document.createElement('output'); o.textContent = Math.round(cfg[key] * 100) + '%';
      i.oninput = () => {
        cfg[key] = +i.value; o.textContent = Math.round(cfg[key] * 100) + '%';
        applyMix(key); save();
      };
      r.append(l, i, o); panel.appendChild(r);
    };
    const sep = () => panel.appendChild(Object.assign(document.createElement('div'), { className: 'sep' }));
    slider('master', 'master'); sep();
    slider('shots', 'gunfire');
    slider('steps', 'footsteps');
    slider('util', 'grenades, C4');
    slider('hits', 'impacts');
    slider('voice', 'own / POV');
    slider('ui', 'interface');
    slider('reverb', 'reverb'); sep();
    const check = (key, text) => {
      const l = document.createElement('label'); l.className = 'cb';
      const i = document.createElement('input'); i.type = 'checkbox'; i.checked = !!cfg[key];
      i.onchange = () => { cfg[key] = i.checked; save(); };
      l.append(i, document.createTextNode(text)); panel.appendChild(l);
    };
    check('propagation', 'speed-of-sound delay');
    check('tinnitus', 'flash ringing');

    // A browser only allows audio after a gesture: any click/key starts (or
    // resumes) the context. The same handler gives every button a UI tick.
    document.addEventListener('pointerdown', e => {
      if (cfg.on) ensureCtx();
      if (panel.classList.contains('open') && !wrap.contains(e.target)) panel.classList.remove('open');
      if (e.target && e.target.closest && e.target.closest('button')) uiSound('click');
    }, { passive: true });
    addEventListener('keydown', e => {
      if (cfg.on) ensureCtx();
      if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
      // by where the key sits, so a Russian layout mutes too
      if (e.code === 'KeyM' && !e.ctrlKey && !e.metaKey && !e.altKey) { setEnabled(!cfg.on); label(); }
    });
  }

  function applyMix(key) {
    if (!ctx) return;
    const t = ctx.currentTime;
    if (key === 'master') master.gain.setTargetAtTime(cfg.on ? cfg.master : 0, t, 0.03);
    else if (key === 'reverb') revGain.gain.setTargetAtTime(cfg.reverb, t, 0.05);
    else if (bus[key]) bus[key].gain.setTargetAtTime(cfg[key], t, 0.03);
  }

  function setEnabled(on) {
    cfg.on = !!on; save();
    if (cfg.on) {
      ensureCtx();
      if (master) master.gain.setTargetAtTime(cfg.master, ctx.currentTime, 0.05);
      uiSound('toggle');
    } else if (ctx) {
      master.gain.setTargetAtTime(0, ctx.currentTime, 0.05);
    }
  }

  window.ReplaySfx = {
    mountUI, useSamples, setRound, frame, resync, ui: uiSound, setEnabled,
    setListener(slot, first) { camSlot = slot; camFirst = first; },
    setSpeed(x) { speed = x || 1; },
    isOn: () => cfg.on,
  };
})();
