"""Phase C: generate the interpolated 3D playback app (replay3d.html) + a round manifest.

    python src/demoreview/build3d_app.py demos/match.dem

Reuses the Phase-A per-round binaries (output/<stem>/rNN.3dr) and the Phase-B coordinate
transform. Emits:
  - output/<stem>/manifest.json : map + calibration + match-wide elevation levels +
    per-round metadata (nSamples, sampleRate, duration, kill markers).
  - output/<stem>/replay3d.html : a self-contained playback app. It lazily fetch()es one
    round's .3dr at a time (the point of the per-round format), decodes it once into typed
    arrays, and animates all 10 players via requestAnimationFrame with linear position
    interpolation and shortest-path yaw interpolation between the 16 Hz samples. UI: draggable
    scrubber, play/pause, 0.5x/1x/2x/4x speed, round clock, round selector, kill markers.
    OrbitControls is the only camera. No utility rendering, no camera-follow (Phase D/E).

Serve locally (fetch needs http):  (cd output/<stem> && python -m http.server) then open
http://localhost:8000/replay3d.html
"""

import base64
import json
import os
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from demoreview import binfmt, maps  # noqa: E402

try:
    from demoparser2 import DemoParser
except ImportError:
    sys.exit("demoparser2 not installed. Activate the venv and: pip install -r requirements.txt")

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "web")
F_ALIVE = 0x02


def _strip_exports(src):
    return src.replace("export function", "function").replace("export const", "const")


def _kills_by_round(parser):
    """round_no (1-based) -> [{sid, atk, vic, atkTeam, tick}] from player_death."""
    out = defaultdict(list)
    d = parser.parse_event("player_death", player=["team_num"],
                           other=["total_rounds_played", "is_warmup_period"])
    d = d[~d["is_warmup_period"].astype(bool)]
    for row in d.itertuples(index=False):
        rn = int(getattr(row, "total_rounds_played")) + 1
        out[rn].append({
            "tick": int(row.tick),
            "atk": str(getattr(row, "attacker_name", "") or "world"),
            "vic": str(getattr(row, "user_name", "") or "?"),
            "atkTeam": int(getattr(row, "attacker_team_num", 0) or 0),
        })
    return out


def build(demo_path):
    stem = os.path.splitext(os.path.basename(demo_path))[0]
    out_dir = os.path.join("output", stem)
    if not os.path.isdir(out_dir):
        sys.exit("missing %s -- run build3d.py first to generate the .3dr sidecars" % out_dir)

    dr_files = sorted(f for f in os.listdir(out_dir) if f.endswith(".3dr"))
    if not dr_files:
        sys.exit("no .3dr files in %s -- run build3d.py first" % out_dir)

    print("Parsing %s for kill markers ..." % demo_path)
    parser = DemoParser(demo_path)
    kills = _kills_by_round(parser)

    map_name = None
    calibration = None
    elev_bins = defaultdict(int)
    rounds_meta = []

    for fname in dr_files:
        data = open(os.path.join(out_dir, fname), "rb").read()
        head = binfmt.read_header(data)
        map_name = head["map"]
        rn = head["round"]
        start, stride, rate, n = (head["startTick"], head["sampleStride"],
                                  head["sampleRate"], head["nSamples"])
        # accumulate match-wide elevation levels (decode needed for Z distribution)
        model = binfmt.decode_round(data)
        for tr in model["tracks"]:
            for i, fl in enumerate(tr["flags"]):
                if fl & F_ALIVE:
                    elev_bins[round(tr["Z"][i] / 40.0) * 40] += 1
        # per-round kill markers -> sample index
        kmarks = []
        for k in kills.get(rn, []):
            s = max(0, min(n - 1, (k["tick"] - start) // stride))
            kmarks.append({"sample": s, "atk": k["atk"], "vic": k["vic"],
                           "atkTeam": k["atkTeam"]})
        rounds_meta.append({
            "n": rn, "file": fname, "nSamples": n, "sampleRate": rate,
            "startTick": start, "durationS": round((n - 1) / rate, 2) if n > 1 else 0.0,
            "kills": kmarks,
        })

    info = maps.map_info(map_name)
    if not info:
        sys.exit("no radar calibration for map %s" % map_name)
    calibration = {"pos_x": info["pos_x"], "pos_y": info["pos_y"],
                   "scale": info["scale"], "size": info["size"]}

    total = sum(elev_bins.values()) or 1
    elevation = sorted(b for b, c in elev_bins.items() if c / total >= 0.01)

    manifest = {
        "map": map_name, "calibration": calibration, "sceneScale": binfmt_scene_scale(),
        "teamColors": {"2": "#f59e0b", "3": "#3b82f6"}, "elevationWorldZ": elevation,
        "rounds": rounds_meta,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)

    coords_src = _strip_exports(open(os.path.join(WEB_DIR, "coords3d.mjs")).read())
    decoder_src = _strip_exports(open(os.path.join(WEB_DIR, "replay3d_decoder.mjs")).read())
    radar_uri = maps.radar_data_uri(info) or ""

    html = _TEMPLATE
    html = html.replace("__TITLE__", "%s 3D replay" % map_name)
    html = html.replace("__RADAR_URI__", radar_uri)
    html = html.replace("/*__COORDS__*/", coords_src)
    html = html.replace("/*__DECODER__*/", decoder_src)

    out_path = os.path.join(out_dir, "replay3d.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("map=%s rounds=%d elevation levels=%s"
          % (map_name, len(rounds_meta), ["%.0f" % z for z in elevation]))
    print("wrote %s and manifest.json" % out_path)
    print("open it:  (cd %s && python -m http.server 8000) then http://localhost:8000/replay3d.html"
          % out_dir)
    return out_path


def binfmt_scene_scale():
    # keep in sync with web/coords3d.mjs SCENE_SCALE
    return 0.02


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --ct:#3b82f6; --t:#f59e0b; }
  html,body { margin:0; height:100%; background:#0b0e14; color:#e6e9ef;
    font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; overflow:hidden; }
  #hud { position:fixed; top:12px; left:12px; z-index:10; background:rgba(12,16,24,.82);
    border:1px solid #232a36; border-radius:10px; padding:10px 13px; backdrop-filter:blur(6px); }
  #hud h1 { margin:0 0 8px; font-size:14px; }
  #hud .row { display:flex; align-items:center; gap:8px; margin-top:6px; }
  #hud select { background:#141a24; color:#e6e9ef; border:1px solid #2a3342; border-radius:6px;
    padding:3px 6px; font:inherit; }
  #hud .k { color:#8b93a5; }
  #hud .stat { font-variant-numeric:tabular-nums; }
  .legend span { display:inline-flex; align-items:center; gap:5px; margin-right:10px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  #bar { position:fixed; left:0; right:0; bottom:0; z-index:10; padding:10px 16px 14px;
    background:linear-gradient(transparent, rgba(8,11,17,.9) 30%); }
  #bar .ctl { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
  button { background:#1a2230; color:#e6e9ef; border:1px solid #2a3342; border-radius:7px;
    padding:5px 11px; font:inherit; cursor:pointer; }
  button:hover { background:#222c3d; } button.on { background:#2b6cff; border-color:#2b6cff; }
  #time { font-variant-numeric:tabular-nums; color:#c7cdd8; margin-left:4px; }
  #track { position:relative; height:26px; }
  #scrub { position:absolute; inset:0; width:100%; margin:0; -webkit-appearance:none;
    background:transparent; cursor:pointer; z-index:3; }
  #scrub::-webkit-slider-runnable-track { height:6px; border-radius:3px;
    background:linear-gradient(#2a3342,#2a3342); margin-top:10px; }
  #scrub::-webkit-slider-thumb { -webkit-appearance:none; width:14px; height:14px; margin-top:6px;
    border-radius:50%; background:#e6e9ef; border:2px solid #2b6cff; }
  #fill { position:absolute; left:0; top:10px; height:6px; border-radius:3px; background:#2b6cff;
    z-index:1; pointer-events:none; }
  #marks { position:absolute; left:0; right:0; top:6px; height:14px; z-index:2; pointer-events:none; }
  #marks .m { position:absolute; top:0; width:3px; height:14px; border-radius:2px; transform:translateX(-1px);
    pointer-events:auto; cursor:pointer; opacity:.85; }
  #marks .u { position:absolute; bottom:-3px; width:8px; height:8px; border-radius:50%;
    transform:translateX(-4px); border:1px solid rgba(0,0,0,.55); pointer-events:auto; cursor:pointer; }
  #flash { position:fixed; inset:0; background:#fff; opacity:0; pointer-events:none; z-index:8; }
  #err { position:fixed; inset:0; display:none; place-items:center; padding:40px;
    background:#0b0e14; color:#ff8080; z-index:20; text-align:center; }
</style>
<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.161.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.161.0/examples/jsm/"
}}
</script>
</head>
<body>
<div id="hud">
  <h1 id="title">3D replay</h1>
  <div class="row"><span class="k">Round</span><select id="roundSel"></select></div>
  <div class="row stat"><span class="k">FPS</span><span id="fps">–</span>
    <span class="k">upd</span><span id="upd">–</span><span class="k">ms</span></div>
  <div class="row legend" style="margin-top:8px">
    <span><i class="dot" style="background:var(--ct)"></i>CT</span>
    <span><i class="dot" style="background:var(--t)"></i>T</span></div>
</div>

<div id="bar">
  <div class="ctl">
    <button id="play">⏸ Pause</button>
    <span class="k">speed</span>
    <button class="spd" data-s="0.5">0.5×</button>
    <button class="spd on" data-s="1">1×</button>
    <button class="spd" data-s="2">2×</button>
    <button class="spd" data-s="4">4×</button>
    <span id="time">0:00 / 0:00</span>
  </div>
  <div id="track">
    <div id="fill"></div>
    <div id="marks"></div>
    <input id="scrub" type="range" min="0" max="100" step="0.01" value="0">
  </div>
</div>
<div id="flash"></div>
<div id="err"></div>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/*__COORDS__*/
/*__DECODER__*/

const RADAR_URI = "__RADAR_URI__";
const $ = s => document.querySelector(s);
function fail(m){ const e=$('#err'); e.style.display='grid'; e.textContent='Error: '+m; console.error(m); }

const S = SCENE_SCALE, ALIVE = 0x02, MAX = 12, CAP_H = 0.35*2 + 1.0;
const lerp = (a,b,f) => a + (b-a)*f;
function angLerp(a,b,f){ let d = ((b - a + 540) % 360) - 180; return a + d*f; }  // shortest path
const fmt = s => { s=Math.max(0,s|0); return (s/60|0)+':'+String(s%60).padStart(2,'0'); };

let MANIFEST=null, state=null, count=0;
let clock=0, playing=true, speed=1, seeking=false;

// ---- three basics ----
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
renderer.setSize(innerWidth, innerHeight); renderer.setClearColor(0x0b0e14);
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x20242c, 1.1));
const sun = new THREE.DirectionalLight(0xffffff, 1.35); sun.position.set(40,80,20); scene.add(sun);
const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 0.1, 4000);
const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;

// ---- players: fixed instanced meshes (created once; per-frame = setMatrixAt only) ----
const bodyGeo = new THREE.CapsuleGeometry(0.35, 1.0, 4, 10);
const noseGeo = new THREE.ConeGeometry(0.18, 0.5, 10);
const bodies = new THREE.InstancedMesh(bodyGeo, new THREE.MeshStandardMaterial({roughness:.7}), MAX);
const noses  = new THREE.InstancedMesh(noseGeo, new THREE.MeshStandardMaterial({roughness:.7}), MAX);
bodies.frustumCulled = false; noses.frustumCulled = false;
scene.add(bodies); scene.add(noses);
const labels = [];
for (let i=0;i<MAX;i++){
  const c=document.createElement('canvas'); c.width=256; c.height=64;
  const t=new THREE.CanvasTexture(c); t.colorSpace=THREE.SRGBColorSpace;
  const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:t, depthTest:false})); sp.scale.set(4,1,1);
  sp.visible=false; scene.add(sp); labels.push({sprite:sp, canvas:c, tex:t});
}
function setLabel(i, text, color){
  const {canvas,tex}=labels[i], g=canvas.getContext('2d');
  g.clearRect(0,0,256,64); g.font='bold 30px sans-serif'; g.textAlign='center'; g.textBaseline='middle';
  g.lineWidth=5; g.strokeStyle='rgba(0,0,0,.85)'; g.strokeText(text,128,32);
  g.fillStyle=color; g.fillText(text,128,32); tex.needsUpdate=true;
}

// reusable temporaries (no per-frame allocation)
const _m=new THREE.Matrix4(), _q=new THREE.Quaternion(), _up=new THREE.Vector3(0,1,0);
const _p=new THREE.Vector3(), _s=new THREE.Vector3(1,1,1), _zero=new THREE.Vector3(0,0,0);
const _noseTilt=new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0,0,1), -Math.PI/2);
const _col=new THREE.Color();

// ---- utility rendering (smoke / molotov / HE / flash) ----
// Each utility's on-screen state is recomputed from the playback clock every frame, so it
// stays correct under arbitrary scrubbing. Meshes are built once per round load, not per frame.
const utilGroup = new THREE.Group(); scene.add(utilGroup);
let utils = [];
const _sphereGeo = new THREE.SphereGeometry(1, 18, 14);
const _discGeo = new THREE.CircleGeometry(1, 30);
const _flameGeo = new THREE.ConeGeometry(0.22, 1, 7);
let flashFocusName = null;   // set by ?flash=<name>; Phase E will bind the follow player
let flashFocus = null;       // resolved player index whose flash drives the overlay

const UTIL_COLOR = { smoke:'#d6dae1', molotov:'#ff6a1a', flash:'#ffffff', he:'#ff5040', decoy:'#9aa4b2' };

function makeTracerLabel(text, color){
  const c=document.createElement('canvas'); c.width=256; c.height=64;
  const g=c.getContext('2d'); g.font='bold 26px sans-serif'; g.textAlign='center'; g.textBaseline='middle';
  g.lineWidth=5; g.strokeStyle='rgba(0,0,0,.85)'; g.strokeText(text,128,32);
  g.fillStyle=color; g.fillText(text,128,32);
  const t=new THREE.CanvasTexture(c); t.colorSpace=THREE.SRGBColorSpace;
  const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:t, depthTest:false, transparent:true}));
  sp.scale.set(3.6,0.9,1); return sp;
}

// A parabolic throw tracer: thrower -> detonation, drawn over the real flight time, faded
// out shortly after landing. Attaches desc.tracer (skipped if we have no throw origin).
function buildTracer(desc, u, nameBySid, rate){
  if (!u.throwPos || u.throwSample == null || u.detSample == null || u.detSample <= u.throwSample) return;
  const a = worldToScene(u.throwPos[0], u.throwPos[1], u.throwPos[2], S);
  const b = worldToScene(u.pos[0], u.pos[1], u.pos[2], S);
  const arcH = Math.min(7, Math.max(0.8, Math.hypot(b[0]-a[0], b[2]-a[2]) * 0.18));  // lob height
  const N = 40, pts = [];
  for (let k=0;k<=N;k++){ const t=k/N;
    pts.push(new THREE.Vector3(a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t + arcH*4*t*(1-t), a[2]+(b[2]-a[2])*t)); }
  const geo = new THREE.BufferGeometry().setFromPoints(pts);
  const col = UTIL_COLOR[u.type] || '#cccccc';
  const mat = new THREE.LineBasicMaterial({ color:col, transparent:true, opacity:0 });
  const line = new THREE.Line(geo, mat); line.visible=false; utilGroup.add(line);
  const label = makeTracerLabel(nameBySid[u.thrower] || '?', col); label.visible=false; utilGroup.add(label);
  desc.tracer = { line, mat, geo, pts, N, label, tf:u.throwSample, df:u.detSample,
                  fade:u.detSample + Math.max(1, 1.5*rate) };
}

function updateTracer(tr, s){
  if (s < tr.tf || s > tr.fade){ tr.line.visible=false; tr.label.visible=false; return; }
  tr.line.visible = true;
  const prog = tr.df > tr.tf ? Math.min(1, (s - tr.tf)/(tr.df - tr.tf)) : 1;   // flight progress
  const count = Math.max(2, Math.floor(prog*tr.N) + 1);
  tr.geo.setDrawRange(0, count);                       // draw the arc up to the nade head
  const op = s <= tr.df ? 0.95 : Math.max(0, 0.95*(1 - (s - tr.df)/(tr.fade - tr.df)));
  tr.mat.opacity = op;
  const tip = tr.pts[Math.min(tr.N, count-1)];
  tr.label.visible = op > 0.06; tr.label.material.opacity = op;
  tr.label.position.set(tip.x, tip.y + 0.6, tip.z); tr.label.quaternion.copy(camera.quaternion);
}

function clearUtils(){
  for (const u of utils){
    for (const o of u.objs){ utilGroup.remove(o); o.material && o.material.dispose(); }
    if (u.tracer){ utilGroup.remove(u.tracer.line); utilGroup.remove(u.tracer.label);
      u.tracer.geo.dispose(); u.tracer.mat.dispose();
      u.tracer.label.material.map.dispose(); u.tracer.label.material.dispose(); }
  }
  utils = [];
}

function buildUtility(header, rate){
  clearUtils();
  const nameBySid = {}; header.players.forEach(pl => nameBySid[pl.sid] = pl.name);
  for (const u of (header.utility || [])){
    const before = utils.length;
    const p = worldToScene(u.pos[0], u.pos[1], u.pos[2], S);
    const rS = (u.radius || 0) * S;
    const s0 = (u.detSample != null) ? u.detSample : 0;
    const durS = u.duration || 0;
    if (u.type === 'smoke'){
      const m = new THREE.Mesh(_sphereGeo, new THREE.MeshStandardMaterial({
        color:0xd6dae1, transparent:true, opacity:0, depthWrite:false, roughness:1 }));
      m.position.set(p[0], p[1] + rS*0.55, p[2]); m.visible=false; utilGroup.add(m);
      utils.push({ type:'smoke', s0, s1:s0 + Math.max(1, durS*rate), grow:Math.max(1, 1.2*rate),
                   rS, mesh:m, mat:m.material, objs:[m] });
    } else if (u.type === 'molotov'){
      const d = new THREE.Mesh(_discGeo, new THREE.MeshBasicMaterial({
        color:0xff6a1a, transparent:true, opacity:0, side:THREE.DoubleSide, depthWrite:false }));
      d.rotation.x=-Math.PI/2; d.position.set(p[0], p[1]+0.03, p[2]); d.scale.setScalar(Math.max(rS,0.1));
      d.visible=false; utilGroup.add(d);
      const flames=[]; for (let k=0;k<7;k++){ const a=k/7*6.2832, rr=rS*0.5;
        const fm=new THREE.Mesh(_flameGeo, new THREE.MeshBasicMaterial({
          color:0xff9a2a, transparent:true, opacity:0, depthWrite:false }));
        fm.position.set(p[0]+Math.cos(a)*rr, p[1]+0.4, p[2]+Math.sin(a)*rr); fm.visible=false;
        utilGroup.add(fm); flames.push(fm); }
      utils.push({ type:'molotov', s0, s1:s0 + Math.max(1, durS*rate), rS, disc:d, flames, objs:[d,...flames] });
    } else if (u.type === 'he'){
      const m = new THREE.Mesh(_sphereGeo, new THREE.MeshBasicMaterial({
        color:0xffb060, transparent:true, opacity:0, depthWrite:false }));
      m.position.set(p[0], p[1]+rS*0.3, p[2]); m.visible=false; utilGroup.add(m);
      utils.push({ type:'burst', s0, s1:s0 + Math.max(1, 0.45*rate), rS:rS*0.5, base:0.7,
                   mesh:m, mat:m.material, objs:[m] });
    } else if (u.type === 'flash'){
      const m = new THREE.Mesh(_sphereGeo, new THREE.MeshBasicMaterial({
        color:0xffffff, transparent:true, opacity:0, depthWrite:false }));
      m.position.set(p[0], p[1]+0.6, p[2]); m.scale.setScalar(1.1); m.visible=false; utilGroup.add(m);
      utils.push({ type:'burst', s0, s1:s0 + Math.max(1, 0.25*rate), rS:1.1, base:0.95,
                   mesh:m, mat:m.material, objs:[m] });
    } else if (u.type === 'decoy'){
      const m = new THREE.Mesh(_sphereGeo, new THREE.MeshBasicMaterial({
        color:0x9aa4b2, transparent:true, opacity:0, depthWrite:false }));
      m.position.set(p[0], p[1]+0.5, p[2]); m.scale.setScalar(0.4); m.visible=false; utilGroup.add(m);
      utils.push({ type:'decoy', s0, s1:s0 + Math.max(1, durS*rate), objs:[m], mat:m.material });
    }
    if (utils.length > before) buildTracer(utils[utils.length-1], u, nameBySid, rate);
  }
}

function updateUtility(s, tsec){
  for (const u of utils){
    if (u.tracer) updateTracer(u.tracer, s);
    if (s < u.s0 || s > u.s1){ for (const o of u.objs) o.visible=false; continue; }
    const p = (u.s1 > u.s0) ? (s - u.s0)/(u.s1 - u.s0) : 1;
    if (u.type === 'smoke'){
      const grow = Math.min(1, (s - u.s0)/u.grow), fade = p > 0.9 ? (1-p)/0.1 : 1;
      u.mesh.visible=true; u.mesh.scale.setScalar(u.rS*grow); u.mat.opacity = 0.34*Math.min(grow, fade);
    } else if (u.type === 'molotov'){
      const grow = Math.min(1, (s - u.s0)/Math.max(1,(u.s1-u.s0)*0.06)), fade = p > 0.85 ? (1-p)/0.15 : 1;
      u.disc.visible=true; u.disc.material.opacity = 0.42*fade*grow;
      for (let k=0;k<u.flames.length;k++){ const fm=u.flames[k]; fm.visible=true;
        const fl = 0.55 + 0.45*Math.sin(tsec*10 + k*1.7);
        fm.scale.set(1, fl*1.7, 1); fm.material.opacity = 0.5*fade*grow*fl; }
    } else if (u.type === 'burst'){         // HE / flash detonation pop: expand + fade
      u.mesh.visible=true; u.mesh.scale.setScalar(u.rS*(0.3 + p)); u.mat.opacity = (1-p)*u.base;
    } else if (u.type === 'decoy'){
      u.objs[0].visible=true; u.mat.opacity = 0.22 + 0.14*Math.sin(tsec*6);
    }
  }
}

// Full-screen white flash overlay. Only makes sense with a POV/follow player (Phase E); here
// the data-to-trigger logic is fully wired and driven by the focus player's per-tick flash
// channel (opacity = remaining blindness, fading out over the real flash duration).
function updateFlash(i0, i1, f){
  const ov = $('#flash');
  if (flashFocus == null || !state){ ov.style.opacity = 0; return; }
  const tr = state.tracks[flashFocus];
  const v = lerp(tr.flash[i0], tr.flash[i1], f) / 255;   // 0..1
  ov.style.opacity = Math.min(1, v).toFixed(3);
}

// ---- greybox (built once from calibration + match-wide levels) ----
function buildGreybox(){
  const cal = MANIFEST.calibration, rect = radarGroundRect(cal, S);
  const groundY = (MANIFEST.elevationWorldZ[0] || 0) * S;
  if (RADAR_URI){
    const tex = new THREE.TextureLoader().load(RADAR_URI); tex.colorSpace=THREE.SRGBColorSpace;
    const g = new THREE.Mesh(new THREE.PlaneGeometry(rect.halfX*2, rect.halfZ*2),
      new THREE.MeshBasicMaterial({map:tex}));
    g.rotation.x=-Math.PI/2; g.position.set(rect.cx, groundY-0.02, rect.cz); scene.add(g);
  }
  const grid = new THREE.GridHelper(rect.halfX*2, 40, 0x2a3342, 0x1b2230);
  grid.position.set(rect.cx, groundY-0.015, rect.cz); scene.add(grid);
  MANIFEST.elevationWorldZ.forEach(wz => {
    const p=new THREE.Mesh(new THREE.PlaneGeometry(rect.halfX*1.6, rect.halfZ*1.6),
      new THREE.MeshBasicMaterial({color:0x5b6472, transparent:true, opacity:.09,
        side:THREE.DoubleSide, depthWrite:false}));
    p.rotation.x=-Math.PI/2; p.position.set(rect.cx, wz*S, rect.cz); scene.add(p);
  });
  const wallMat=new THREE.MeshStandardMaterial({color:0x3a4152, roughness:1});
  const b={minX:cal.pos_x, maxX:cal.pos_x+cal.size*cal.scale,
           minY:cal.pos_y-cal.size*cal.scale, maxY:cal.pos_y}, H=200*S;
  const c=[worldToScene(b.minX,b.minY,0,S), worldToScene(b.maxX,b.minY,0,S),
           worldToScene(b.maxX,b.maxY,0,S), worldToScene(b.minX,b.maxY,0,S)];
  for(let i=0;i<4;i++){ const a=c[i], d=c[(i+1)%4], dx=d[0]-a[0], dz=d[2]-a[2], len=Math.hypot(dx,dz);
    const w=new THREE.Mesh(new THREE.BoxGeometry(len,H,0.3), wallMat);
    w.position.set((a[0]+d[0])/2, groundY+H/2, (a[2]+d[2])/2); w.rotation.y=-Math.atan2(dz,dx); scene.add(w); }
  const center = worldToScene((b.minX+b.maxX)/2, (b.minY+b.maxY)/2, groundY/S, S);
  controls.target.set(center[0], center[1], center[2]);
  camera.position.set(center[0], center[1]+58, center[2]+58);
}

// ---- load a round: fetch .3dr, decode once, recolor, reset ----
async function loadRound(meta){
  const buf = await (await fetch(meta.file)).arrayBuffer();
  const { header, tracks } = decode(buf);
  state = { header, tracks, n:meta.nSamples, rate:meta.sampleRate,
            duration:meta.durationS };
  count = header.players.length;
  for (let i=0;i<MAX;i++){
    if (i<count){
      const hex = MANIFEST.teamColors[String(header.players[i].team)] || '#cccccc';
      _col.set(hex); bodies.setColorAt(i,_col); noses.setColorAt(i,_col);
      setLabel(i, header.players[i].name || '?', '#ffffff');
    } else { _m.compose(_zero,_q,_zero); bodies.setMatrixAt(i,_m); noses.setMatrixAt(i,_m);
      labels[i].sprite.visible=false; }
  }
  bodies.instanceColor.needsUpdate=true; noses.instanceColor.needsUpdate=true;
  buildUtility(header, state.rate);
  flashFocus = (flashFocusName != null)
    ? header.players.findIndex(pl => (pl.name||'').toLowerCase() === flashFocusName.toLowerCase())
    : -1;
  if (flashFocus < 0) flashFocus = null;
  buildMarks(meta, header);
  clock = 0; $('#scrub').max = state.duration; seek(0);
}

function buildMarks(meta, header){
  const marks=$('#marks'); marks.innerHTML='';
  const dur = meta.durationS || 1;
  (meta.kills||[]).forEach(k=>{
    const t=k.sample/meta.sampleRate, pct=t/dur*100;
    const el=document.createElement('div'); el.className='m'; el.style.left=pct+'%';
    el.style.background = k.atkTeam===3 ? 'var(--ct)' : 'var(--t)';
    el.title = k.atk+' → '+k.vic; el.onclick = ()=>{ playing=false; syncPlayBtn(); seek(t); };
    marks.appendChild(el);
  });
  ((header && header.utility) || []).forEach(u=>{
    if (u.detSample==null) return;
    const t=u.detSample/meta.sampleRate, pct=t/dur*100;
    const el=document.createElement('div'); el.className='u'; el.style.left=pct+'%';
    el.style.background = UTIL_COLOR[u.type] || '#888';
    el.title = u.type + (u.type==='smoke'?' deployed':u.type==='molotov'?' burning':' @ '+t.toFixed(1)+'s');
    el.onclick = ()=>{ playing=false; syncPlayBtn(); seek(t); };
    marks.appendChild(el);
  });
}

// ---- per-frame player update (the hot path: interpolate + setMatrixAt only) ----
function update(){
  if (!state) return;
  const n=state.n, s=Math.max(0, Math.min(n-1, clock*state.rate));
  const i0=Math.floor(s), i1=Math.min(i0+1, n-1), f=s-i0;
  for (let i=0;i<count;i++){
    const tr=state.tracks[i];
    if (!(tr.flags[i0] & ALIVE)){ _m.compose(_zero,_q,_zero); bodies.setMatrixAt(i,_m);
      noses.setMatrixAt(i,_m); labels[i].sprite.visible=false; continue; }
    const g = (tr.flags[i1] & ALIVE) ? f : 0;      // never interpolate into a dead/frozen frame
    const x=lerp(tr.X[i0],tr.X[i1],g), y=lerp(tr.Y[i0],tr.Y[i1],g), z=lerp(tr.Z[i0],tr.Z[i1],g);
    const yaw = angLerp(tr.yaw[i0], tr.yaw[i1], g) * Math.PI/180;
    const sc = worldToScene(x,y,z,S);
    _q.setFromAxisAngle(_up, yaw);
    _p.set(sc[0], sc[1]+CAP_H/2, sc[2]); _m.compose(_p,_q,_s); bodies.setMatrixAt(i,_m);
    _q.setFromAxisAngle(_up, yaw).multiply(_noseTilt);
    _p.set(sc[0]+Math.cos(yaw)*0.4, sc[1]+CAP_H*0.62, sc[2]-Math.sin(yaw)*0.4);
    _m.compose(_p,_q,_s); noses.setMatrixAt(i,_m);
    const lb=labels[i].sprite; lb.visible=true; lb.position.set(sc[0], sc[1]+CAP_H+0.8, sc[2]);
    lb.quaternion.copy(camera.quaternion);
  }
  bodies.instanceMatrix.needsUpdate=true; noses.instanceMatrix.needsUpdate=true;
  updateUtility(s, clock);
  updateFlash(i0, i1, f);
}

// ---- seek / UI ----
function seek(t){ clock=Math.max(0, Math.min(state?state.duration:0, t)); update(); syncScrub(); }
function syncScrub(){ if(!state) return; $('#scrub').value=clock; $('#fill').style.width=
  (state.duration? clock/state.duration*100:0)+'%'; $('#time').textContent=fmt(clock)+' / '+fmt(state.duration); }
function syncPlayBtn(){ $('#play').textContent = playing ? '⏸ Pause' : '▶ Play'; }

$('#play').onclick=()=>{ if(clock>=state.duration) clock=0; playing=!playing; syncPlayBtn(); };
document.querySelectorAll('.spd').forEach(b=>b.onclick=()=>{ speed=parseFloat(b.dataset.s);
  document.querySelectorAll('.spd').forEach(x=>x.classList.toggle('on', x===b)); });
const scrub=$('#scrub');
scrub.addEventListener('pointerdown',()=>seeking=true);
addEventListener('pointerup',()=>seeking=false);
scrub.addEventListener('input',()=>{ seek(parseFloat(scrub.value)); });

// ---- main loop with FPS + update-cost instrumentation ----
let last=performance.now(), fpsT=last, frames=0, updAcc=0;
function loop(now){
  requestAnimationFrame(loop);
  const dt=(now-last)/1000; last=now;
  if (playing && state){ clock+=dt*speed; if(clock>=state.duration){ clock=state.duration; playing=false; syncPlayBtn(); } }
  const u0=performance.now(); update(); updAcc+=performance.now()-u0;
  if (!seeking) syncScrub();
  controls.update(); renderer.render(scene, camera);
  frames++;
  if (now-fpsT>=500){ const fps=frames/((now-fpsT)/1000);
    $('#fps').textContent=fps.toFixed(0); $('#upd').textContent=(updAcc/frames).toFixed(3);
    window.__fps=fps; window.__updMs=updAcc/frames; frames=0; updAcc=0; fpsT=now; }
}

addEventListener('resize',()=>{ camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth,innerHeight); });

async function init(){
  MANIFEST = await (await fetch('manifest.json')).json();
  $('#title').textContent = MANIFEST.map + ' · 3D replay';
  const sel=$('#roundSel');
  MANIFEST.rounds.forEach((r,idx)=>{ const o=document.createElement('option');
    o.value=idx; o.textContent='Round '+r.n; sel.appendChild(o); });
  sel.onchange=async()=>{ playing=false; syncPlayBtn(); await loadRound(MANIFEST.rounds[+sel.value]);
    playing=true; syncPlayBtn(); };
  buildGreybox();
  // optional deep-link: ?round=<1-based>&t=<seconds> (seeks + pauses on that moment)
  const q=new URLSearchParams(location.search);
  const qr=parseInt(q.get('round')), qt=parseFloat(q.get('t'));
  flashFocusName = q.get('flash');   // ?flash=<player name> -> drives the full-screen flash overlay
  let idx = MANIFEST.rounds.findIndex(r=>r.n===qr); if (idx<0) idx=0;
  sel.value=idx;
  await loadRound(MANIFEST.rounds[idx]);
  if (!isNaN(qt)){ playing=false; seek(qt); }
  syncPlayBtn();
  requestAnimationFrame(loop);
}
init().catch(e=>fail(e && e.message || String(e)));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: build3d_app.py <demo.dem>")
    build(args[0])
