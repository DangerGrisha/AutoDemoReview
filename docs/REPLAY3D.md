# 3D replay on real CS2 maps

The 3D replay renders every round in the browser (Bevy → WASM/WebGL2): players
as team-colored humanoid models, dynamic grenade effects (churning smoke volumes,
flickering molotov fire beds, HE core + ground shockwave) with throw arcs, a kill
timeline, and — when a converted map is installed — the **real map geometry**,
perfectly registered with player positions. Without a map it falls back to the
radar greybox. Players are drawn **x-ray** (on top of walls and smoke) so an
analyst always sees everyone, like a demo watched with wallhack.

## How it fits together

```
upload .dem ──► web/app.py ──► pipeline.parse_demo_file()   (Python, 2D report)
                    └────────► replay3d.extract()            (best-effort)
                                   └─ rust/target/release/extract3d
                                        └─ web/data/replay3d/<match_id>/
                                             manifest.json + rNN.3dr
match page ── "3D replay" link ──► GET /matches/{id}/3d  (templates/replay3d.html)
    the page fetches:  /static/replay3d/pkg/viewer.js + viewer_bg.wasm   (Bevy viewer)
                       /matches/{id}/replay3d/manifest.json + rNN.3dr    (auth-gated)
    the viewer fetches (via its asset root /static/replay3d/assets/):
                       <map>.png                        radar ground texture
                       maps/<map_name>/world.glb        real map world (optional)
```

- Coordinates: the viewer renders `scene = (x, z, −y)·sceneScale` of Source
  world units (`rust/viewer/src/coords.rs`). The map must reach the viewer in
  raw `(x, z, −y)` Source units, so it is spawned with a single uniform
  `Transform::from_scale(sceneScale)` — never enable Bevy's
  `GltfPlugin::convert_coordinates`. **Note:** Source2Viewer 19.x bakes a global
  axis+scale into every world node — it maps Source `(x,y,z)` to glTF
  `0.0254·(y, z, x)`, i.e. an inch→metre scale **and** a 90° yaw — so a raw
  export is ~39× too small *and* rotated, and players walk off it.
  `tools/prepare_map.py` reads that root matrix `M_root` and wraps the scene in
  `CONTRACT · M_root⁻¹` (see `orient_scene_to_contract`), landing the geometry in
  exactly `(x, z, −y)` raw units. It is derived from the export's own matrix, so
  it self-corrects if a future Source2Viewer changes the convention. Verified
  end-to-end on de_mirage: decoded `.3dr` player tracks sit on the exported
  geometry (median ~18 u) and match the radar.
- Matches analyzed **before** this feature have no artifacts (the raw .dem is
  never kept). Re-uploading the same demo backfills them.
- Supported maps (radar calibration in `rust/extract/src/mapscal.rs`, ported
  from `src/demoreview/maps.py`): mirage, inferno, nuke, overpass, ancient,
  anubis, vertigo, train. Other maps: extract3d exits nonzero → no 3D replay,
  the 2D report is unaffected.

## Build (once per code change)

Prereqs — all installable without admin rights:

1. **Rust**: `rustup` with the stable toolchain and the wasm target
   (`rustup target add wasm32-unknown-unknown`).
   *On this machine C: is nearly full, so everything lives on D::*
   `RUSTUP_HOME=D:\rust\rustup`, `CARGO_HOME=D:\rust\cargo`,
   `CARGO_TARGET_DIR=D:\rust\target` (set as user env vars), and the GNU
   toolchain (`stable-x86_64-pc-windows-gnu`) is the default so no Visual
   Studio install is needed. It needs binutils on PATH: a portable MinGW-w64
   (WinLibs) lives at `D:\mingw-tmp\mingw64\bin`.
2. **wasm-bindgen-cli** matching `rust/Cargo.lock`'s wasm-bindgen — currently
   **0.2.126** (prebuilt binary in `D:\rust\cargo\bin`). If `cargo update`
   ever bumps wasm-bindgen, install the matching CLI version.
3. **Python deps** for map preparation: `pip install pygltflib Pillow`, plus
   `pip install brotli` for the `.br` precompression step (build-time only —
   the server never needs it at runtime).
4. **binaryen (wasm-opt)** — optional but strongly recommended (−15–30% wasm):
   download the latest `binaryen-version_*-x86_64-windows.tar.gz` from
   <https://github.com/WebAssembly/binaryen/releases>, verify the `.sha256`,
   and `tar -xf` it into `D:\rust\` (no admin needed). The build script picks
   up any `D:\rust\binaryen-*\bin` automatically and merely warns when absent.

Then:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_replay3d.ps1
```

This produces `extract3d.exe`, `web/static/replay3d/pkg/` (the wasm viewer —
run through `wasm-opt -Oz` when binaryen is installed), copies the 8 radar
PNGs into the viewer's asset root, mirrors `sfx.js` next to the dev page, and
writes `.br`/`.gz` siblings next to the wasm/js/sfx/glb via
`tools/precompress.py` (served by `CompressedStaticFiles` in
`web/app.py`; siblings are gitignored and regenerated per build).

The web app finds extract3d via `EXTRACT3D_BIN` in `.env` (defaults to
`rust/target/release/extract3d.exe`; on this machine the target dir is
relocated, so `.env` pins `EXTRACT3D_BIN=D:\rust\target\release\extract3d.exe`).

## Size & performance (what keeps first load fast)

The first load used to be ~123 MB uncompressed (74 MB wasm + 52 MB glb) with a
dead screen; these layers, together, bring it to ~15–20 MB on the wire with a
progress overlay. If you touch any of them, keep the others in mind:

- **Cargo profile** (`rust/Cargo.toml`): `opt-level="z"`, fat LTO,
  `codegen-units=1`, `strip="symbols"`, `panic="abort"` (no unwinding landing
  pads; don't run `cargo test --release` — tests need unwind and use the dev
  profile anyway). `rust/.cargo/config.toml` adds wasm target-features
  (`+bulk-memory,+nontrapping-fptoint,+simd128` — every WebGL2-era browser has
  them; wasm-opt is invoked with `--all-features` to match).
- **Bevy features** (`rust/viewer/Cargo.toml`): hand-picked list instead of the
  `3d` umbrella, which silently pulls text/fonts, tonemapping+SMAA LUTs,
  ktx2+zstd, morph targets, picking, gamepad, clipboard, post-processing — all
  unused here. If a new viewer feature needs one of those, add the single
  feature back rather than restoring the umbrella.
- **wasm-opt -Oz** (build script step 4/6): binaryen post-pass over the
  wasm-bindgen output (which is otherwise ~10% LARGER than the raw cargo
  output and never optimized).
- **Compressed serving**: `tools/precompress.py` writes `.br`/`.gz` siblings
  (build step 6/6); `CompressedStaticFiles` in `web/app.py` serves them with
  the original `Content-Type` (`application/wasm` keeps streaming instantiation
  alive), `Vary: Accept-Encoding`, and `Cache-Control: public, max-age=604800`.
  Bevy wasm compresses ~9–11× under brotli; f32 geometry ~2.5×. **Siblings are
  trusted blindly** — always regenerate them after rebuilding the wasm or a map
  (the build script does; a manual wasm swap must re-run precompress).
- **Parallel load + progress overlay** (both HTML pages): wasm, world.glb,
  manifest and the first round download concurrently (the glb fetch warms the
  HTTP cache for Bevy's own request); two progress bars replace the dead
  screen and hide on the first rendered frame.
- **Map merge/chunk pass** (`tools/prepare_map.py`, see the map section):
  68 chunks / 1.31 M verts / u16 indices instead of 3 172 draw calls and a
  17.5× vertex blow-up in Bevy's loader.
- **Viewer runtime** (`rust/viewer/src`): map meshes load GPU-only
  (`RenderAssetUsages::RENDER_WORLD` in `spawn_map` — nothing CPU-reads the
  map; halves its RAM), and both cameras run `Msaa::Sample2` instead of Bevy's
  default 4× (two full-screen WebGL2 passes; one-word revert if edges bother).

## Converting a map (once per map, on any PC with CS2 installed)

**The short version — `tools/convert_maps.ps1`** runs all three stages below
back to back for a whole list of maps, and is what you actually want unless you
are debugging the pipeline:

```powershell
.\tools\convert_maps.ps1 de_inferno          # one map
.\tools\convert_maps.ps1                     # the default pool: inferno, dust2,
                                             # nuke, anubis, overpass, vertigo, train
```

It finds the CS2 install itself (`libraryfolders.vdf`, or `-MapsDir` /
`CS2_MAPS_DIR`), skips maps whose `world.glb` already exists (`-Force` to
rebuild), reuses an existing `raw\<map>` export instead of re-exporting, deletes
that export afterwards (`-KeepRaw` to keep it), and — this is the part worth
knowing — **rebuilds once with `--simplify` when the result exceeds `-MaxMB`
(default 80)**, which is exactly what de_ancient needs (212 → 52 MB) and
de_mirage does not. Budget ~15–25 min and ~1 GB of scratch per map; the stages
themselves are:

> **Reorientation is load-bearing — and de_nuke proved it.** The pass that undoes
> Source2Viewer's inch→metre scale used to pick the global frame as the matrix
> carried by a *majority* of scene roots. On de_nuke the placed props outnumber
> the world roots (the frame won only 3844/8667), the pass bailed out, and the
> map stayed **in metres** — ~39× too small. Nothing errored: `--simplify 5` then
> meant 5 *metres*, which collapsed 3.25 M vertices to 3155 and produced a
> perfectly valid 0.2 MB glb of nothing. The frame is now identified by SHAPE —
> a pure axis-swap with one uniform scale and **zero translation**, since any
> placed prop carries a translation — so it is found even when a minority of
> roots carry it. Two guards now shout if this ever slips again: a scene under
> 1000 units across ("looks like metres"), and decimation that keeps under 5% of
> the vertices.

1. Download the **Source2Viewer CLI** (`cli-windows-x64.zip`) from
   <https://github.com/ValveResourceFormat/ValveResourceFormat/releases/latest>
   and unzip into `tools/s2v/` (gitignored). Run `--help` once — the flag names
   drift between versions (the two below changed in 19.x).
2. Export the map's world to glTF (paths for a default Steam install). Note the
   flags for **Source2Viewer 19.2**: glTF export is driven by
   `--gltf_export_format gltf` (not a bare `--gltf_export_materials`), and
   `--recursive` is rejected for a single `.vpk` input, so it is dropped:

   ```powershell
   .\tools\s2v\Source2Viewer-CLI.exe `
     -i "C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\maps\de_mirage.vpk" `
     -e "vwrld_c" -d --gltf_export_format gltf -o raw\de_mirage
   ```

   **Textures / materials:** adding `--gltf_export_materials` currently throws
   `Only VCS file versions 59 through 70 are supported ... 71` — CS2 shipped
   shader version 71, newer than VRF 19.2 supports. So we export **geometry
   only** (no `--gltf_export_materials`); the map renders untextured but with
   correct geometry. Re-add materials once VRF supports VCS 71.

3. Prepare it for the viewer (reorients+rescales the export into the viewer's
   `(x,z,-y)` contract, filters tool brushes, drops decal/overlay meshes, and —
   with `--slim-attrs` — drops vertex attributes an untextured map never uses,
   then packs one `.glb`):

   ```powershell
   python tools\prepare_map.py raw\de_mirage de_mirage --glb --slim-attrs --dev
   ```

   This writes `web/static/replay3d/assets/maps/de_mirage/world.glb` (and, with
   `--dev`, mirrors it into `rust/viewer/www/assets/maps/` for the standalone
   page). From the next page load, every match on that map shows real geometry
   (plus a Map/Radar toggle). Options: `--slim-attrs` (keep only POSITION+NORMAL
   — ~halves an untextured map, geometry unchanged), `--max-size 512` (slow
   GPUs), `--geometry-only` (strip base-colour too), `--keep-maps` (keep
   normal/MR/AO). For de_mirage the merged geometry-only `world.glb` is ~39 MB.

   **Merge & chunk (default on).** The final pipeline stage bakes every mesh
   node's world transform into its vertices, trims each primitive to the
   vertices its indices actually use, welds bit-exact `(position, normal)`
   duplicates, and rebins the triangles into an `--grid N` (default 8×8)
   spatial grid of ≤65 535-vertex chunks with u16 indices and tight AABBs.
   Why: primitives in the export share big vertex pools, and Bevy's glTF
   loader uploads the FULL pool per primitive — de_mirage ballooned to
   25.7 M uploaded vertices (17.5× the 1.47 M unique) ≈ 617 MB of GPU buffers
   and ~3 200 draw calls. After the pass: **68 chunks, 1.31 M vertices,
   ~35 MB GPU, effective frustum culling**. Instanced props (multiple nodes →
   one mesh) are baked per instance, so nothing visible is lost. Flags:
   `--no-merge` (bypass), `--weld-position-only` (aggressive weld + recomputed
   normals, softens hard edges — off by default), and a standalone mode
   `python tools\prepare_map.py --refine <in.glb> [--out <out.glb>]` to apply
   merge/chunk to an already-prepared world.glb.

   **Flat surface colours ("1×1 textures", `--surface-colors`).** CS2's real
   textures cannot be exported (VCS 71, see above), so instead of one grey mass
   every surface gets ONE flat colour derived from the geometry: a triangle's
   facing and height pick a class — `ceiling` / `floor_low` / `floor_mid` /
   `floor_high` / `ramp` / `wall` (`SURFACE_PALETTE`, sandstone-ish for de_*) —
   and the floor tiers split at the 55th/85th percentile of *this* map's
   up-facing heights, so the levels of the map read apart. The colours ride on a
   **`COLOR_0` vertex attribute (u8×4)**, not on extra materials, so the chunk
   and draw-call count is unchanged (68 chunks, 1 material); Bevy's PBR shader
   multiplies `COLOR_0` into `base_color`, so the material carries the palette's
   BRIGHTEST colour and each vertex stores its colour relative to it — a viewer
   that ignored `COLOR_0` would fall back to one flat sandstone, never white.
   Cost on de_mirage: +5.2 MB raw (39.2 → 44.5 MB) but only **+0.4 MB brotli**
   (14.0 → 14.4 MB) — flat colours compress to almost nothing.

   **Baked shading is what makes corners readable.** Flat colours alone are not
   enough: the viewer lights the map with a near-isotropic ambient
   (`AMBIENT_MAP`=1800 against a 4200 lux directional in `lib.rs`), so two
   perpendicular walls come out equally bright — a doorway or a corner reads as
   one blank surface. Re-lighting needs a Rust rebuild; baking the shading into
   `COLOR_0` does not, and it holds regardless of runtime lighting. Two
   multipliers ride on top of the palette:
   - **`--ao` (default 0.55)** — ambient occlusion from a voxel occupancy grid
     (`--voxel`, default 32 u = a player's width): every vertex probes the
     occupancy of a 3×3×3 box (contact/crevices) and a 9×9×9 box (room
     enclosure) 1.5 voxels off the surface along its normal, and both measures
     are normalized against *this map's* own p20..p97 — absolute occupancy is
     meaningless as it scales with voxel size and map density. Corners, niches,
     doorways and corridors go darker, which is what shows depth.
   - **`--sun` (default 0.38)** — Lambert against a fixed direction, so two
     walls meeting at a corner differ in brightness with no real light.

   Measured on de_mirage: 6 flat colours → **1011 distinct vertex colours**,
   shading from 0.45 in deep corners to 1.0 in the open (median 0.84). The bake
   is a ~14 s numpy pass over 1.3 M vertices.

   To colour a map that is already built (no CS2 install needed, ~14 s):

   ```powershell
   python tools\prepare_map.py --colorize web\static\replay3d\assets\maps\de_mirage\world.glb
   python tools\precompress.py "web\static\replay3d\assets\maps\*\world.glb"   # siblings are trusted blindly
   ```

   Brotli on a 44 MB map takes **minutes** — do not kill it: a half-written
   `.br`/`.gz` sibling is served as if it were valid. If one is ever suspect,
   verify with `brotli.decompress(...)` against the raw file, or delete the
   sibling (the server then falls back to the raw glb) and re-run.

   `--palette pal.json` overrides any class; re-running on an already-coloured
   glb is refused (it would orphan the previous `COLOR_0` views and only grow
   the buffer) — run it on the pre-colour file.

   **Overlays** (posters, signs, blood/dirt splats CS2 projects onto surfaces —
   Source2Viewer names them `*_mesh_overlay*`) are **dropped by default**: with
   no texture export possible (VCS 71) they render as opaque grey "poster-walls"
   floating on the real walls. `--keep-overlays` retains them. (The reference
   CS2MapViewer renders them instead, alpha-testing their real textures — which
   we can't, hence the removal.)

   Already-converted exports from the CS2MapViewer project work as input too —
   point `prepare_map.py` at the directory containing the world `.gltf`.

## Maps travel through Git LFS

Converted `world.glb` files are large binaries and would be expensive to
regenerate (they need a local CS2 install + Source2Viewer), so they are tracked
in **Git LFS** (`.gitattributes`: `*.glb filter=lfs`), while the wasm bundle and
radar PNGs stay gitignored and are rebuilt. This is the piece that broke past
"copied it through GitHub and the map was gone" transfers.

- **Fresh clone:** run `git lfs install` once, then `git lfs pull` to fetch the
  real `.glb` bytes. Without LFS you get small text pointer files, not meshes.
- **What travels vs what you rebuild locally:**
  - via git LFS: `web/static/replay3d/assets/maps/<map>/world.glb`
  - rebuild with `tools/build_replay3d.ps1`: `extract3d(.exe)` + the wasm bundle
    under `web/static/replay3d/pkg/` + the radar PNG copies
  - recreate from `.env.example`: `.env` (never committed)
- Only `.glb` under `assets/maps/` plus the fallback `assets/models/player.glb` are
  tracked; the radar PNG copies in the same `assets/` dir stay ignored (`.gitignore`),
  and so do the real player models (see "Player models").

## Dev page (no FastAPI)

`rust/viewer/www/index.html` runs the viewer standalone: build (the build
script mirrors the pkg), run `extract3d demos\match.dem --out-dir rust\viewer\www`
so `manifest.json` + rounds land next to the page, then serve `rust/viewer/www`
with any static server. `?map=maps/de_mirage/world.glb` injects a map placed
under `rust/viewer/www/assets/`.

## Spectator camera (per-player POV, CS2-demo style)

Besides the free orbit cam, the viewer can lock onto any player and watch from
their POV, like a CS2 demo. Pick a player in the right-side panel (or **1–0**),
**←/→** cycles through living players, **X** toggles first-/third-person, **F**
(or Esc) returns to free cam. First-person shows a crosshair and hides the
followed player's own model + nametag.

- **Rust (`viewer`):** `Viewer.cam_mode: CamMode::{Free, Follow{slot, first_person}}`
  (state.rs), driven from JS via the `set_camera_follow(slot, first_person)` /
  `set_camera_free()` wasm exports. `update_players` publishes each living
  player's interpolated eye pose (feet + yaw + **pitch** + crouch) into the
  `PlayerPoses` resource; `spectator_camera` (runs right after `orbit_camera`,
  which no-ops while following) places the camera. First-person sits at the eye
  and looks along Source's `AngleVectors` forward `(cosP·cosY, cosP·sinY, −sinP)`
  re-labelled through `world_dir_to_scene`; third-person is a chase cam behind
  the shoulders.
- **Empirical constants** (scene.rs / lib.rs, verify by eye, one-liners each):
  `EYE_STAND=64` / `EYE_CROUCH=46` (Source view offsets, world units),
  `PITCH_SIGN` (Source is +pitch = down; flip if a demo reads inverted),
  `CHASE_DIST=135` / `CHASE_UP=35` (third-person framing). The horizontal aim
  reuses the same yaw the player models use, so it tracks the model calibration.
- **Per-frame payload** is **10 floats/player** — `{x, y, onScreen, alive,
  health, weapon, worldX, worldY, yawDeg, present}`. `onScreen` gates the
  floating 3D nametag; `alive`/`health` are real regardless of projection, so
  the spectator panel shows every player's true state (not just those on
  screen). `worldX/worldY/yawDeg` are RAW demo-world values (pre scene-scale)
  for the page's live radar — dead players hold their frozen death coords,
  `present=0` means "not in this round, draw nothing". Tracer records follow at
  `6 + nPlayers*PSTRIDE`. Both the wasm writer (`report_frame`) and the two HTML
  readers (`www/index.html`, `web/templates/replay3d.html`, `PSTRIDE` const)
  share this layout — bump the prod page's `?v=` cache-buster on the pkg URLs
  whenever it changes (the static pkg is cached for 7 days).

## CS2-style HUD (score bar · live radar · kill feed)

All page-side (no new wasm scene work), in both HTMLs:

- **Top-center score bar** — CT/T running score entering the round, round
  number, playback clock, and per-team alive pips driven from the frame
  payload. The score comes from new optional per-round manifest keys
  `winner` ("CT"/"T"), `scoreCT`, `scoreT` (emitted by `manifest.rs` only when
  the source had `round_end` winner enrichment, so synthetic-fixture manifests
  are byte-identical). The score is attributed to TEAM IDENTITY (sids of the
  round-1 CT roster) and re-expressed for the sides each team holds in that
  round, so halftime/overtime swaps come out right with no MR-format logic.
  The round dropdown also shows each round's winner. Old manifests (no
  `winner`) degrade to a score of "–". The score **counts up live**: the moment
  the clock reaches the round's end the winner's number bumps +1 and tints in
  the team color (scrub back and it reverts). While playback is running the
  page also **auto-advances** to the next round ~1.5 s after the round ends
  (pausing or scrubbing cancels it), so a match plays through end-to-end and
  the score keeps counting without touching the dropdown.
- **Live radar (top-left)** — a 2D canvas drawing the match's radar PNG
  (`manifest.radarAsset`, same asset the greybox mode textures) with per-frame
  team-colored dots + white view-direction ticks, an ✕ frozen at each death
  spot, and a highlight ring around the followed player. Projection uses the
  manifest calibration exactly like `mapscal::world_to_radar`:
  `px = (worldX − pos_x)/scale`, `py = (pos_y − worldY)/scale`, scaled to the
  canvas. The #hud panel moved down below it.
- **C4 panel (top-center, under the score bar)** — "непонятно что бомба
  запущена": the plant used to be invisible. New per-round manifest key `bomb`
  (emitted only for rounds that had a plant, so old manifests and the synthetic
  fixtures keep their exact shape): `{plant, site?, defused?|exploded?}` in the
  same sample units as the kill marks. The page shows 💣 + site + a fuse
  countdown that turns amber under 10 s, red and pulsing under 5, then resolves
  to ✂️ **defused** or 💥 **exploded**; a red mark on the scrub bar seeks to the
  plant. Like the feed it is recomputed from the clock each frame, so scrubbing
  lands on the right state. **The countdown targets the round's actual explosion
  tick when there is one** — measured gap is ~41.0 s, not the nominal 40 (the
  event fires when the planting animation completes); `C4_FUSE`=40 is only the
  fallback for rounds that were defused or ran out. Pipeline: `dp2.rs` now
  requests `bomb_planted`/`bomb_defused`/`bomb_exploded` in the same event pass,
  `DemoSource::bomb_events()` (default-empty, so test sources are unaffected)
  yields `BombEvent`, and `build_models` pairs each round's plant with the
  defuse/explosion that closes it into `Meta3d::bombs`. **Bombsite letters are
  usually absent**: CS2 sends `site` as an entity index, so only a literal
  "A"/"B" or 0/1 is mapped and anything else shows as just "planted".
- **Kill feed (top-right, left of the scoreboard)** — manifest kill marks
  replayed as a CS2-style feed: entries appear when the clock passes the kill
  and expire after 6 s, with a fade in the last second; derived from the clock
  each frame, so scrubbing forward/back stays consistent. Victim team color is
  resolved by name from the round roster. Each entry shows **how** the kill
  happened: weapon tag pill (`w`, mapped from the event's internal name by
  `weapons::event_weapon_tag` — "ak47"→AK, "inferno"→🔥, "planted_c4"→💣 …)
  plus CS2-order badges — 🙈 attacker was flashed (`ab`), NS noscope (`ns`),
  🌫 through smoke (`ts`), 🧱 wallbang (`wb`, penetration count > 0),
  🎯 headshot (`hs`). All keys are emitted into the manifest kill marks only
  when set (old manifests fall back to the plain "atk ✕ vic" row), and the
  scrub-bar kill marks get the same detail in their tooltips.

## Weapon models (3rd person + 1st-person viewmodel)

Procedural low-poly weapon kits (`viewer/src/weapons_viz.rs`) — no external
assets, cuboids + a sphere in the same stylized look as the untextured map.
One silhouette per weapon CLASS, resolved from the round's `weaponTable` tag
each frame: rifle (AK/M4/…), sniper (AWP/SCOUT/autos, with scope), SMG,
pistol, shotgun, MG, knife (bright blade), C4 (brick + red LED), and colored
grenade spheres (per-type palette matching the page UI). Unknown tags draw
nothing rather than guessing.

- **Third person:** a `WeaponMount` child on every `PlayerBody` at chest height
  (`MOUNT_POS`, `THIRD_SCALE` consts), barrel along the body yaw (+X). On a real
  CS2 model `setup_player_anim` moves the mount onto the `wpn` bone so the kit
  follows the hands (`bone_mount` holds the per-class grip offset). Parts sit
  on layers `[0, x-ray]` like the player meshes, so held weapons show through
  walls too. `tint_players` explicitly skips `WeaponPart` meshes (guns stay
  gunmetal, not team-painted).
- **First person:** a `ViewModel` anchor child of the main camera (bottom-right:
  `VM_POS`/`VM_YAW`/`VM_SCALE`), filled with the followed player's current kit
  only while spectating in 1st person — weapon swaps mid-round show up live.
- Kits swap only when the weapon CLASS changes (cheap: compare + respawn a few
  cuboids). Dead/absent players show nothing (mount inherits the hidden body).

## Sound (procedural, page-side)

`web/static/replay3d/sfx.js` — the whole match soundtrack is **synthesized in the
browser** with the Web Audio API. Nothing is downloaded and no sample ships with
the repo: gunfire, footsteps, explosions and stings are built from filtered
noise bursts and swept oscillators, in the same spirit as the procedural weapon
kits in `weapons_viz.rs`. It needs **no wasm work** — the module reads data the
page already has, so it can be iterated on with nothing but a page reload.

- **The default mix is GUNFIRE ONLY.** The first pass also ran a room-tone bed
  (brown noise) and molotov/smoke noise loops; in practice that was a constant
  loud fan hum and a painful hiss, so all three were **removed from the code** —
  a decoy's fake gunfire is the only surviving loop. Footsteps, impacts,
  HE/flash and the POV sounds are still implemented but start at **0** and are
  opt-in from the ⚙ mixer. A zeroed bus is checked in `claim()` before any node
  is built, so muted categories cost nothing. `DEFAULTS` changes must bump
  `STORE_KEY` (`replay3d.sfx.v2`), otherwise a stored older mix resurrects the
  very sounds that were turned off.
- **Canonical copy + dev mirror.** The tracked file is
  `web/static/replay3d/sfx.js`; `build_replay3d.ps1` copies it to
  `rust/viewer/www/sfx.js` (gitignored, like the pkg mirror) and precompresses
  the prod copy. **Bump the `?v=` token on the prod page's `sfx.js` URL whenever
  you change it** — `/static` is cached for 7 days.
- **What makes sound, and from which data:**
  | sound | source |
  |---|---|
  | gunshots (per weapon CLASS: rifle/sniper/SMG/pistol/deagle/shotgun/MG, silenced variants, knife swish, Zeus arc) | round header `shots[] {s, p}` + the shooter's live `weapon` from the frame payload |
  | footsteps (walk vs run) | distance travelled between frames (`worldX/worldY`), one step per `STEP_LEN`=76 u |
  | HE blast, flash pop | header `utility[]` `detSample`/`pos` |
  | decoy's fake gunfire | that item's `duration` window |
  | grenade throw whoosh + pin | `throwSample`/`throwPos` |
  | bullet impacts, own "hurt", body fall | per-frame `health` drops and `alive` 1→0 |
  | kill ding (higher on a headshot), your own death thud, C4 detonation | manifest `kills[]` (`w:"💣"` = the bomb) |
  | round-start beeps, round-end sting (major for CT, darker for T) | `durationS` + `winner` |
  | flash ringing + everything ducking and muffling | `flashOpacity` from the payload |
  | weapon-swap click, UI ticks | payload `weapon` changes / button presses |
- **Positioning.** The "ear" is the spectated player: true distance attenuation,
  an air-absorption low-pass, a speed-of-sound delay (`1 u = 1 inch`, so ~74 ms
  per 1000 u — switchable) and stereo pan from that player's own yaw. In **free
  camera** the wasm never reports the camera pose, so the ear falls back to the
  centroid of the living players and the pan comes from the event's screen x
  (which is exact). See `NOTE_CAM_POSE` in the file: adding `{camX, camY,
  camYaw}` to `report_frame` would make free-cam audio exact too.
- **Scrub-safe by construction.** Every sound is scheduled off the **playback
  clock**; a clock jump larger than `MAX_STEP_S` (0.4 s × speed) counts as a
  scrub, so the engine re-anchors its event cursor silently instead of dumping
  the skipped events at once. Pausing silences the loops. Speed changes need no
  pitch work — events simply arrive closer together (`setSpeed` only widens the
  scrub threshold and compresses within-frame spacing).
- **Load protection.** Voices are capped by *scheduled end time*
  (`MAX_VOICES`=96 — never an `onended` counter, which leaks the moment one
  callback is missed and silences everything; one gunshot costs 4 voices), at
  most `STEP_BUDGET`=4 new footsteps per frame, and distant footsteps drop their
  high-frequency layer. Measured on r01 of the dev demo: all 76 shots play, 4
  voices each, nothing dropped; with footsteps on and 10 players sprinting a
  whole round it peaks at ~29 one-shots/s.
- **UI.** The module mounts its own **🔊 Sound** button + **⚙ mixer** popover
  into the bottom bar (per-bus sliders: gunfire, footsteps, utility, impacts,
  own/POV, interface, room tone, reverb; toggles for the speed-of-sound delay
  and the flash ringing), persisted in `localStorage` under `replay3d.sfx`.
  **M** mutes. Browsers only allow audio after a gesture, so the context starts
  on the first click/keypress.
- **Page hooks** (identical in both HTMLs): `SFX.setRound(header, meta)` in
  `loadRound`, `SFX.frame(d, nP)` in `__replayFrame`, `SFX.setListener` in
  `applyCam`, `SFX.setSpeed` on the speed buttons, and `seekTo()` — a wrapper
  around `seek()` that calls `SFX.resync()`, used by the scrubber and every
  kill/utility mark.
- **Not implemented for lack of data:** bomb plant/defuse beeps, reloads, and
  scoped-in clicks — there are no such events in the manifest or the codec
  (`F_SCOPED`/`F_DEFUSING` exist in the codec's flags but never reach the page).

## Player models (real CS2 characters, with a fallback)

Each side wears a real CS2 model with CS2's own animations: `ctm_sas` for CT and
`tm_phoenix` for T, the base models and not the agent skins players buy. Without
those files the viewer uses `models/player.glb`, a Mixamo soldier painted in the
team colour with just idle, walk and run.

**They are never committed.** These are Valve's assets and this repo is public, so
`web/static/replay3d/assets/models/cs2_v1/` is gitignored and every machine that
serves them builds them from its own CS2 install. `REPLAY3D_CS2_MODELS=0` switches
back to the soldier without deleting anything, which is the plan if Valve ever asks
for them to go. Valve's terms only allow its content in fan work on a non-commercial
basis, so this has to change before the site ever charges for access.

```powershell
.\tools\convert_characters.ps1   # about 30 seconds; needs CS2 + Source2Viewer 20+
```

Unzip Source2Viewer's `cli-windows-x64.zip` into `tools\s2v\v20\` first. It sits next
to the older copy that the map conversion was tuned against. Version 20 is required:
CS2 moved its clips to animation-graph files (`.vnmclip`) that older versions cannot
export. Run `pip install -r requirements-tools.txt` for the Python side.

**Where the assets are.** `characters/models/*.vmdl_c` are now one-bone stubs. The
real models are under `agents/models/`, while their textures stayed in
`characters/models/*/materials/`. Clips are in `animation/anims/world/<class>/`.

**What `tools/prepare_character.py` does**, and why it does not share a pipeline with
`prepare_map.py` (whose `--slim-attrs` and flat colours would wreck a skinned model):

- keeps only the third-person meshes, and trims each primitive to its used vertices,
  because bevy_gltf uploads a primitive's whole vertex pool;
- strips jiggle, eye and helper bones (94 down to 79), folding their weights onto the
  nearest kept ancestor, and collapses the identical per-mesh skins into one;
- keeps the clips in `rust/viewer/src/player_clips.txt`. The viewer finds them by
  name, and a Rust test checks that this list and the poses in `player_anim.rs` agree;
- bakes a track that holds one value in every clip into the rest pose. A track that
  differs between clips stays even if it is constant inside each, or a crossfade
  would keep the previous clip's value;
- gives idle clips a one-second length. They export as a single key, and Bevy loops
  with `seek % duration`, so a zero-length clip would turn the clock into NaN;
- keeps every second key (15 per second) and writes textures as JPEG atlases (colour
  2048 px, normals 1024 px), dropping AO, metal and roughness maps.

The exports are in metres, Y up, facing +Z, with feet at Y=0 (`CS2_SCALE` and
`yaw_offset` in `scene.rs`). The T-pose reports 78 Source units, but what matters is
the animated pose: the head sits at 64 units standing and 47 crouching, which matches
`EYE_STAND` and `EYE_CROUCH`.

**Load each file once.** `scene.rs` loads the root `Gltf` and takes the clips from it.
Do not call `asset_server.load(GltfAssetLabel::Animation(i)...)` per clip: Bevy re-reads
and re-decodes the whole file for every labelled load. With 78 clips on two models that
was 79 full loads per file and half a minute at 5 FPS after the loading screen.

**Keep what Bevy repeats small.** Two more costs scale badly, and on ten players on this
machine they took the model from 137 FPS on the soldier down to 91:

- Bevy makes one entity per glTF primitive, and each gets its own draws (two, with x-ray)
  and its own copy of every joint matrix. So `prepare_character.py` merges the whole
  character into one primitive with one material and texture atlases. This only works
  while the source materials are opaque and do not tile; the script stops if one tiles.
- Bevy walks every animation graph node for every animated bone, every frame. So no graph
  holds the 78 clips: each player has a graph of `CLIP_NODES` nodes, and
  `drive_player_anim` writes the next clip into the next node.

**Which clip plays** (`player_anim.rs`, unit-tested without a renderer): each weapon
class has `idle`, `idle_crouch` and `walk_*`, `run_*`, `crouch_*` in 8 directions.
`drive_player_anim` measures ground velocity over +-2 samples, since one 16 Hz step is
too noisy, and turns it into a direction relative to the player's yaw. Gait and
direction both have hysteresis so clips do not flicker at a threshold. Clips run on
the replay clock, so pausing holds the pose and slow motion slows the legs. Clip rate
follows ground speed so the feet do not slide. The clips carry no root motion and report
a velocity of 0, so the nominal speeds in `player_anim.rs` were measured from how fast a
planted foot moves relative to the body. That also confirms the directions: `e` is the
character's right.

**Team colour fades with distance** (`fade_team_colour`). Up close a player shows the real
textures. From about 20 scene units out it is painted in the team colour like the soldier,
because from the overview a sand-coloured T is a few pixels on a sand-coloured map.

All of this reads channels the replay format already has (position, yaw, `F_CROUCH`,
weapon), so old `.3dr` files get the new models with no re-upload.

**Not done yet:** death, defuse and firing clips (all available in `animation/anims/
world/shared/` and the weapon folders), aim pitch on the spine, jumps, and showing the
agent a player actually wore (`agent_skin` in the parser, never extracted).

## Players, grenades & the x-ray overlay (rendering)

- **X-ray players (wallhack view).** A second, higher-`order` camera (`XrayCam`,
  spawned in `setup`) renders *only* the player render layer (1) with
  `ClearColorConfig::None`, so it composites over the main view with its own fresh
  depth buffer — players therefore always draw on top of walls and smoke.
  `tint_players` puts each player mesh on layers `[0, 1]` (world + overlay) and
  adds a subtle team-colour `emissive` so the through-wall silhouettes read as
  chams. `sync_xray_camera` copies the main camera's transform onto the overlay
  each frame; the overlay has its own bright flat `AmbientLight` (`XRAY_AMBIENT`).
  Toggle from JS via the `set_xray(bool)` wasm export (the **X-ray** button / **V**
  key); default on. Everything else (map, smoke, gizmos) stays on layer 0, so only
  players are ever duplicated.
- **Dynamic grenades** (`util.rs` build + `lib.rs` `update_utility`, all animated
  from the playback clock so scrubbing stays correct):
  - *Smoke* — `SMOKE_PUFFS` lit spheres on a `0.5*r_s` shell (`puff_offset`), each
    `0.5*r_s` across, so the cloud's outer radius is exactly **r_s (144u = CS 1:1)**
    and the big overlapping puffs read as one solid mass, not balloons. Blooms in
    at deploy, holds full size, thins out at the end; only a tiny slow drift — no
    per-puff scale pulse. Dense (players draw x-ray on top, so opacity is safe).
  - *Molotov* — `MOLOTOV_FLAMES` cone tongues scattered across the fire bed, fast
    out-of-phase flicker (lick height + sway) over a gently breathing ground glow.
  - *HE* — a bright core sphere that expands and fades fast **plus** a flat
    `Annulus` ground shockwave that races outward and thins to nothing.
  - *Flash / decoy* — unchanged pop / pulsing marker.
- **Shot tracers.** `weapon_fire` events flow through the whole pipeline:
  - *extractor* (`dp2.rs` adds `weapon_fire` to the event pass; `lib.rs` bins each
    into its round as `{s: fractional sample, p: shooter slot}`, keeping only
    bullet weapons — classified from the shooter's own **track** weapon-tag via
    `is_bullet_weapon_tag`, so it never depends on the event's weapon field);
  - *codec* carries a per-round `shots` array in the C2R3 header, exactly like
    `utility` — but written **only when non-empty**, so shot-less rounds and the
    golden fixtures keep byte-identical headers;
  - *viewer* (`build_shots`) reconstructs each shot's muzzle ray from the shooter's
    interpolated eye pose (the same `AngleVectors` math as the POV camera) into a
    fixed world-space line; `draw_tracers` renders it as a **travelling streak**:
    a hot near-white head races the beam in `TRACER_TRAVEL` s (0.045) with a
    team-tinted trailing dash (`TRACER_STREAK_FRAC`=0.42 of the beam), the whole
    streak fading out by `TRACER_LIFE` s (0.20), plus a brief muzzle-flash sphere
    (suppressed in the shooter's own first-person POV). Timing is in playback-
    clock seconds, so scrubbing lands on the right shots and slo-mo stretches the
    streaks. Drawn on a dedicated `TracerGizmos` config group (width
    `TRACER_WIDTH`=3.5 px, slight negative depth bias) so the thin default-group
    gizmos (grid, throw arcs) are unaffected. Beam length `TRACER_LEN_WU` (3000 u).
  - Matches analysed before this feature (old `.3dr`) simply have no `shots` key →
    no tracers; re-uploading the demo backfills them.

## Env vars (web/.env)

| var | default | meaning |
|---|---|---|
| `EXTRACT3D_BIN` | `rust/target/release/extract3d(.exe)` | extract3d binary; missing → uploads still work, no 3D |
| `EXTRACT3D_TIMEOUT_S` | `900` | kill a hung extraction |
| `REPLAY3D_CS2_MODELS` | `1` | `0` shows the fallback soldier instead of the real CS2 player models |

Artifacts live in `web/data/replay3d/<match_id>/`; deleting a directory removes
that match's 3D replay (it comes back on re-upload of the same .dem).
