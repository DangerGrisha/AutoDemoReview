"""Phase B: generate a static Three.js scene from one frozen moment of the match.

    python src/demoreview/build3d_scene.py demos/match.dem [round] [--kill]

Reads the Phase-A per-round binary (output/<stem>/rNN.3dr), picks a frozen tick (by default
the frame just before round 1's first kill so there's documented ground truth), and writes a
self-contained output/<stem>/scene3d.html: greybox de_mirage (radar-textured ground +
data-derived elevation planes + rough walls), all 10 players placed via the ONE coordinate
transform (web/coords3d.mjs) as instanced meshes coloured CT-blue / T-orange and oriented by
yaw, with orbit-camera controls. No playback, no interpolation, no utility rendering — this
phase only proves the coordinate mapping is correct.

Three.js loads from a CDN via an import-map; the round data, decoder, transform and radar
image are all inlined (base64 / data-URI), so the only network need is three.js itself. To
open reliably, serve locally:  (cd output/<stem> && python -m http.server) then browse to it.
"""

import base64
import json
import os
import sys
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


def _first_kill(parser):
    """(tick, atk_sid, vic_sid, atk_name, vic_name, weapon, round0) for round 1's first kill."""
    d = parser.parse_event("player_death", player=["team_num", "X", "Y"],
                           other=["total_rounds_played", "is_warmup_period"])
    d = d[(~d["is_warmup_period"].astype(bool)) & (d["total_rounds_played"] == 0)]
    row = d.iloc[0]
    return {"tick": int(row["tick"]), "atkSid": str(row["attacker_steamid"]),
            "vicSid": str(row["user_steamid"]), "atkName": str(row["attacker_name"]),
            "vicName": str(row["user_name"]), "weapon": str(row["weapon"])}


def _elevation_levels(model):
    """Data-derived distinct floor heights (world Z) from alive samples across the round."""
    bins = {}
    for tr in model["tracks"]:
        for i, fl in enumerate(tr["flags"]):
            if fl & F_ALIVE:
                b = round(tr["Z"][i] / 40.0) * 40
                bins.setdefault(b, []).append(tr["Z"][i])
    total = sum(len(v) for v in bins.values()) or 1
    levels = [median(v) for b, v in bins.items() if len(v) / total >= 0.02]
    return sorted(levels)


def _play_bbox(model):
    """World-space bounding box of alive players across the round (min/max x,y)."""
    xs, ys = [], []
    for tr in model["tracks"]:
        for i, fl in enumerate(tr["flags"]):
            if fl & F_ALIVE:
                xs.append(tr["X"][i]); ys.append(tr["Y"][i])
    return {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}


def build(demo_path, round_no=1):
    stem = os.path.splitext(os.path.basename(demo_path))[0]
    out_dir = os.path.join("output", stem)
    dr_path = os.path.join(out_dir, "r%02d.3dr" % round_no)
    if not os.path.isfile(dr_path):
        sys.exit("missing %s -- run build3d.py first to generate the .3dr sidecars" % dr_path)

    print("Parsing %s for ground-truth kill ..." % demo_path)
    parser = DemoParser(demo_path)
    kill = _first_kill(parser)

    data = open(dr_path, "rb").read()
    model = binfmt.decode_round(data)
    start, stride, n = model["startTick"], model["sampleStride"], model["nSamples"]
    frame = max(0, min(n - 1, (kill["tick"] - start) // stride - 1))

    info = maps.map_info(model["map"])
    if not info:
        sys.exit("no radar calibration for map %s" % model["map"])

    levels = _elevation_levels(model)
    bbox = _play_bbox(model)
    ground_world_z = levels[0] if levels else 0.0

    config = {
        "map": model["map"], "round": model["round"], "frame": frame,
        "killTick": kill["tick"], "startTick": start, "stride": stride,
        "kill": kill,
        "calibration": {"pos_x": info["pos_x"], "pos_y": info["pos_y"],
                        "scale": info["scale"], "size": info["size"]},
        "elevationWorldZ": levels,
        "groundWorldZ": ground_world_z,
        "bbox": bbox,
        "teamColors": {"2": "#f59e0b", "3": "#3b82f6"},  # T orange / CT blue
    }

    coords_src = _strip_exports(open(os.path.join(WEB_DIR, "coords3d.mjs")).read())
    decoder_src = _strip_exports(open(os.path.join(WEB_DIR, "replay3d_decoder.mjs")).read())
    radar_uri = maps.radar_data_uri(info)
    data_b64 = base64.b64encode(data).decode("ascii")

    html = _TEMPLATE
    html = html.replace("__TITLE__", "%s R%d 3D" % (model["map"], model["round"]))
    html = html.replace("__CONFIG_JSON__", json.dumps(config))
    html = html.replace("__RADAR_URI__", radar_uri or "")
    html = html.replace("__DATA_B64__", data_b64)
    html = html.replace("/*__COORDS__*/", coords_src)
    html = html.replace("/*__DECODER__*/", decoder_src)

    out_path = os.path.join(out_dir, "scene3d.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("map=%s round=%d frame=%d (tick~%d, kill@%d)  levels=%s"
          % (model["map"], model["round"], frame, start + frame * stride, kill["tick"],
             ["%.0f" % z for z in levels]))
    print("frozen moment: %s -> %s (%s)" % (kill["atkName"], kill["vicName"], kill["weapon"]))
    print("wrote %s (%d bytes)" % (out_path, len(html)))
    print("open it:  (cd %s && python -m http.server 8000) then http://localhost:8000/scene3d.html"
          % out_dir)
    return out_path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  html,body { margin:0; height:100%; background:#0b0e14; color:#e6e9ef;
    font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; overflow:hidden; }
  #hud { position:fixed; top:12px; left:12px; z-index:10; background:rgba(12,16,24,.82);
    border:1px solid #232a36; border-radius:10px; padding:12px 14px; max-width:340px;
    backdrop-filter:blur(6px); }
  #hud h1 { margin:0 0 6px; font-size:14px; }
  #hud .k { color:#8b93a5; }
  #hud .legend span { display:inline-flex; align-items:center; gap:5px; margin-right:12px; }
  #hud .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  #hint { position:fixed; bottom:12px; left:12px; z-index:10; color:#8b93a5;
    background:rgba(12,16,24,.7); padding:6px 10px; border-radius:8px; }
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
  <h1 id="title"></h1>
  <div id="meta"></div>
  <div class="legend" style="margin-top:8px">
    <span><i class="dot" style="background:#3b82f6"></i>CT</span>
    <span><i class="dot" style="background:#f59e0b"></i>T</span>
    <span><i class="dot" style="background:#22c55e"></i>attacker</span>
    <span><i class="dot" style="background:#ef4444"></i>victim</span>
  </div>
</div>
<div id="hint">drag to orbit &middot; scroll to zoom &middot; right-drag to pan &middot; static frame (no playback)</div>
<div id="err"></div>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/*__COORDS__*/
/*__DECODER__*/

const CONFIG = __CONFIG_JSON__;
const RADAR_URI = "__RADAR_URI__";
const DATA_B64 = "__DATA_B64__";

function fail(msg){ const e=document.getElementById('err'); e.style.display='grid';
  e.textContent = 'Scene error: ' + msg; console.error(msg); }

try {
  // ---- decode the round (exercises the real Phase-A decoder) ----
  const bin = atob(DATA_B64);
  const bytes = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
  const { header, tracks } = decode(bytes.buffer);
  const F = CONFIG.frame;
  const S = SCENE_SCALE;

  // ---- renderer / scene / camera ----
  const renderer = new THREE.WebGLRenderer({ antialias:true });
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  renderer.setSize(innerWidth, innerHeight);
  renderer.setClearColor(0x0b0e14);
  document.body.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xbfd4ff, 0x20242c, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(40, 80, 20); scene.add(sun);

  const camera = new THREE.PerspectiveCamera(55, innerWidth/innerHeight, 0.1, 4000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  // scene center = middle of the play bbox, at ground height
  const cworld = worldToScene((CONFIG.bbox.minX+CONFIG.bbox.maxX)/2,
                              (CONFIG.bbox.minY+CONFIG.bbox.maxY)/2, CONFIG.groundWorldZ, S);
  const center = new THREE.Vector3(cworld[0], cworld[1], cworld[2]);
  controls.target.copy(center);
  camera.position.set(center.x, center.y + 55, center.z + 55);

  // ---- greybox: radar-textured ground plane ----
  const cal = CONFIG.calibration;
  const rect = radarGroundRect(cal, S);
  const groundY = CONFIG.groundWorldZ * S;
  if (RADAR_URI) {
    const tex = new THREE.TextureLoader().load(RADAR_URI);
    tex.colorSpace = THREE.SRGBColorSpace;
    const g = new THREE.Mesh(
      new THREE.PlaneGeometry(rect.halfX*2, rect.halfZ*2),
      new THREE.MeshBasicMaterial({ map:tex }));
    g.rotation.x = -Math.PI/2;                 // XY plane -> ground; +Y(img top)->-Z(north)
    g.position.set(rect.cx, groundY - 0.02, rect.cz);
    scene.add(g);
  }
  // faint grid for depth cue
  const grid = new THREE.GridHelper(rect.halfX*2, 40, 0x2a3342, 0x1b2230);
  grid.position.set(rect.cx, groundY - 0.015, rect.cz); scene.add(grid);

  // ---- data-derived elevation level planes (translucent) ----
  CONFIG.elevationWorldZ.forEach((wz, i) => {
    const p = new THREE.Mesh(
      new THREE.PlaneGeometry(rect.halfX*1.6, rect.halfZ*1.6),
      new THREE.MeshBasicMaterial({ color:0x5b6472, transparent:true, opacity:0.10,
        side:THREE.DoubleSide, depthWrite:false }));
    p.rotation.x = -Math.PI/2;
    p.position.set(rect.cx, wz*S, rect.cz);
    scene.add(p);
  });

  // ---- rough perimeter walls around the play area (approximate greybox) ----
  const wallMat = new THREE.MeshStandardMaterial({ color:0x3a4152, roughness:1 });
  const b = CONFIG.bbox, wallH = 180*S, pad = 60;
  const corners = [
    worldToScene(b.minX-pad, b.minY-pad, CONFIG.groundWorldZ, S),
    worldToScene(b.maxX+pad, b.minY-pad, CONFIG.groundWorldZ, S),
    worldToScene(b.maxX+pad, b.maxY+pad, CONFIG.groundWorldZ, S),
    worldToScene(b.minX-pad, b.maxY+pad, CONFIG.groundWorldZ, S),
  ];
  for (let i=0;i<4;i++){
    const a = corners[i], c = corners[(i+1)%4];
    const dx=c[0]-a[0], dz=c[2]-a[2], len=Math.hypot(dx,dz);
    const w = new THREE.Mesh(new THREE.BoxGeometry(len, wallH, 0.3), wallMat);
    w.position.set((a[0]+c[0])/2, groundY+wallH/2, (a[2]+c[2])/2);
    w.rotation.y = Math.atan2(dz, dx) * -1;
    scene.add(w);
  }

  // ---- players: instanced capsules (body) + cones (nose = facing) ----
  const N = tracks.length;
  const bodyGeo = new THREE.CapsuleGeometry(0.35, 1.0, 4, 10);
  const noseGeo = new THREE.ConeGeometry(0.18, 0.5, 10);
  const bodies = new THREE.InstancedMesh(bodyGeo,
    new THREE.MeshStandardMaterial({ roughness:0.7 }), N);
  const noses = new THREE.InstancedMesh(noseGeo,
    new THREE.MeshStandardMaterial({ roughness:0.7 }), N);
  scene.add(bodies); scene.add(noses);   // setColorAt() lazily creates instanceColor

  const CAP_H = 0.35*2 + 1.0;                     // capsule total height (scene units)
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0,1,0);
  const pos = new THREE.Vector3(), scl = new THREE.Vector3(1,1,1), hide = new THREE.Vector3(0,0,0);
  const col = new THREE.Color();
  const labels = [];

  function makeLabel(text, color){
    const c = document.createElement('canvas'); c.width=256; c.height=64;
    const g = c.getContext('2d');
    g.font='bold 30px sans-serif'; g.textAlign='center'; g.textBaseline='middle';
    g.lineWidth=5; g.strokeStyle='rgba(0,0,0,.85)'; g.strokeText(text,128,32);
    g.fillStyle=color; g.fillText(text,128,32);
    const t = new THREE.CanvasTexture(c); t.colorSpace=THREE.SRGBColorSpace;
    const s = new THREE.Sprite(new THREE.SpriteMaterial({map:t, depthTest:false}));
    s.scale.set(4,1,1); return s;
  }

  for (let i=0;i<N;i++){
    const tr = tracks[i], pl = header.players[i];
    const alive = (tr.flags[F] & 0x02) !== 0;
    const isAtk = pl.sid === CONFIG.kill.atkSid, isVic = pl.sid === CONFIG.kill.vicSid;
    const teamHex = CONFIG.teamColors[String(pl.team)] || "#cccccc";
    col.set(isAtk ? 0x22c55e : isVic ? 0xef4444 : teamHex);
    bodies.setColorAt(i, col); noses.setColorAt(i, col);

    if (!alive){ m.compose(hide, q, hide); bodies.setMatrixAt(i,m); noses.setMatrixAt(i,m); continue; }

    const foot = worldToScene(tr.X[F], tr.Y[F], tr.Z[F], S);
    const yaw = yawToSceneRotationY(tr.yaw[F]);
    q.setFromAxisAngle(up, yaw);

    pos.set(foot[0], foot[1] + CAP_H/2, foot[2]);
    m.compose(pos, q, scl); bodies.setMatrixAt(i, m);
    // nose: cone points +Y by default -> tip it to +X (forward) then apply yaw, at chest height
    const noseQ = new THREE.Quaternion().setFromAxisAngle(up, yaw)
      .multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0,0,1), -Math.PI/2));
    pos.set(foot[0], foot[1] + CAP_H*0.62, foot[2]);
    const noseM = new THREE.Matrix4().compose(
      new THREE.Vector3().copy(pos).add(new THREE.Vector3(Math.cos(yaw)*0.4,0,-Math.sin(yaw)*0.4)),
      noseQ, scl);
    noses.setMatrixAt(i, noseM);

    const label = makeLabel(pl.name || '?', isAtk?'#22c55e':isVic?'#ef4444':'#ffffff');
    label.position.set(foot[0], foot[1] + CAP_H + 0.8, foot[2]);
    scene.add(label); labels.push(label);
  }
  bodies.instanceMatrix.needsUpdate = true; noses.instanceMatrix.needsUpdate = true;
  bodies.instanceColor.needsUpdate = true; noses.instanceColor.needsUpdate = true;

  // ---- HUD ----
  document.getElementById('title').textContent =
    CONFIG.map + '  ·  Round ' + CONFIG.round;
  document.getElementById('meta').innerHTML =
    '<div class="k">frozen frame ' + CONFIG.frame + ' (tick ~' +
      (CONFIG.startTick + CONFIG.frame*CONFIG.stride) + ')</div>' +
    '<div style="margin-top:4px">' + CONFIG.kill.atkName +
      ' <span class="k">→</span> ' + CONFIG.kill.vicName +
      ' <span class="k">(' + CONFIG.kill.weapon + ')</span></div>';

  addEventListener('resize', () => {
    camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
  });
  (function loop(){ requestAnimationFrame(loop); controls.update();
    labels.forEach(l=>l.quaternion.copy(camera.quaternion));
    renderer.render(scene, camera); })();
} catch (e) { fail(e && e.message || String(e)); }
</script>
</body>
</html>
"""


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: build3d_scene.py <demo.dem> [round]")
    rn = int(args[1]) if len(args) > 1 else 1
    build(args[0], rn)
