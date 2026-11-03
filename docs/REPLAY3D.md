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
- Matches analyzed **before** this feature have no artifacts, since demos were not
  kept back then. Re-uploading the same demo backfills them. From 2026-11 a gzipped
  copy of each demo stays in `web/data/demos/<match_id>.dem.gz` (`KEEP_DEMOS=0` turns
  that off), so new matches can be re-extracted when the extractor learns more. See
  [What a replay holds](#what-a-replay-holds).
- Supported maps (radar calibration in `rust/extract/src/mapscal.rs`, ported
  from `src/demoreview/maps.py`): mirage, inferno, nuke, overpass, ancient,
  anubis, vertigo, train. Other maps: extract3d exits nonzero → no 3D replay,
  the 2D report is unaffected.

## What a replay holds

`manifest.json` says `dataVersion`. `DATA_VERSION` lives in `rust/extract/src/lib.rs` and
in `web/replay3d.py`, and a test keeps them equal. A manifest without the key is version 1.
The file format is still C2R3 v1: version 1 replays open as they always did, and the golden
fixtures stay byte-identical, because everything new is optional.

| | Version 1 | Version 2 (from 2026-11) |
|---|---|---|
| Samples a second | 16 | 64 on a 64-tick demo (`--hz N` on the CLI picks another rate) |
| Grenade flight | invented arc between the two ends | `path` on the utility item |
| Grenade found by | thrower and time, up to 5 s of flight | its entity id, then thrower and time for a molotov |
| Smoke, fire and decoy end | constants (18, 7, 15 s) | `duration` is the real one when the end is in the demo |
| Bomb | plant time | also `bomb.pos`, and `bomb.site` from the planter's place name |
| Header `events`, `state`, flags `0x20`, `0x40` | no | yes |

**Version 3** (2026-11-03) adds one manifest key: `as` on a kill mark, the name of who
helped (did damage or flashed the victim), for the scoreboard's assists. Nothing in the
`.3dr` files changed. Matches 1, 2 and 27 are rebuilt; on others the A column says "–".

**Header keys, each present only when non-empty:**

- `state`: index-aligned with `players`. Each entry holds flat `[sample, value, sample,
  value, ...]` change lists: `armor`, `helmet`, `kit`, `money`, `ammo`. `ammo` is `-1` for what
  has no clip. Only `money` keeps changing after death.
- `events`: `{k, s, p, ...}` sorted by `s` (fractional sample, like `shots`), up to the
  round's last sample. `k` is one of `hurt` (`a` attacker slot, `d` health, `da` armour, `hg` hit
  group, `w` weapon tag), `reload`, `zoom`, `plant`, `plantAbort`, `defuse` (`kit`), `defuseAbort`,
  `bombDrop`, `bombPickup`, `pickup` (`item`).
- `path` on a utility item: flat `[sample, x, y, z, ...]` up to the detonation, thinned to
  within `PATH_TOLERANCE`=1 unit of the real flight, so a bounce keeps its corner.
- Flags: `F_WALKING`=`0x20`, `F_AIRBORNE`=`0x40`. `0x80` is still free.

**Checked against three real demos** (`hurt` against the health channel, `ammo` against shots):
all 659 hits agree with the health drop on de_mirage, and 2880 of 2881 shots have a clip
drop next to them. A jump lasts a median 0.64 s. Smokes last 22.06 s, not the 18 s we assumed.
Some smokes make a jump of 50-80 units in the last tick before they go off: that is in the
demo and too short to see.

**Not in the file, on purpose:**

- `player_footstep`: real demos carry it for only about 8% of a player's steps, measured
  against distance run. The page keeps deriving steps from distance.
- Reserve ammo: `total_ammo_left` read 0 on rifles in the rows checked.
- `is_in_reload`: `reload` events plus the `ammo` list say the same.
- Plant aborts: `bomb_abortplant` never appeared in the three demos (43 plant starts, 34
  plants), though it is asked for. A plant that stops has to be inferred from the player
  moving. `F_DEFUSING` already says when a defuse stops.

**What it costs** (the three matches with a local demo, 23-30 rounds):

| | 16 Hz | 64 Hz |
|---|---|---|
| Replay on disk | 2-2.7 MB | 6-8.7 MB |
| Replay on the wire | not compressed at all | 2-2.9 MB |
| Extraction | 11 s, 1.1 GB of memory | 21-39 s, 1.5-1.9 GB |
| Coach analysis of a match | 0.5 s | 2.2 s |
| FPS at 1080p on de_mirage, RTX 3070 | 87-125 | 88-140 (another match; no slower) |

**Rebuilding.** A re-upload replaces a replay older than `DATA_VERSION`. Otherwise:

```
python tools/rebuild_replays.py                  # stale replays of matches with a kept demo
python tools/rebuild_replays.py --force          # those matches, stale or not
python tools/rebuild_replays.py demos/a.dem ...  # the match each demo's sha256 belongs to
```

The server does not do this by itself, because of the memory and time it takes. `extract`
builds next to the final directory and moves the result in whole, so a failed rebuild keeps
the old replay. So does a replay in use on Windows, which refuses to rename an open directory.
Raising `DATA_VERSION` means running this after a deploy. A match without a kept demo stays
as it is until someone uploads its demo again.

**Compression.** `match_replay3d_artifact` sends a brotli or gzip copy made on first request
and kept next to the file (`r01.3dr.br`), so old replays gain from it too. Quality 9, because
someone is waiting: 33 ms for 148 KB, against 436 ms for 131 KB at quality 10.

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

## The textured map (`maps_v2`)

The plain map above is flat colours and baked AO. The textured map is the same world with
CS2's own textures, baked light and sky. It comes from CS2, so it is **gitignored** and each
machine that serves it builds it from its own install. The plain map stays needed: it is the
fallback and the "Лёгкая карта" button.

```powershell
.\tools\convert_maps.ps1 -Textured             # all nine maps, 5-22 min each
.\tools\convert_maps.ps1 de_mirage -Textured -Force
```

This needs **Source2Viewer 20** in `tools\s2v\v20\`, because 19.x cannot read CS2's materials,
and `etcpak` in the venv. The steps are the export (`Export-Textured`), then
`tools\prepare_map_textured.py`, then `tools\precompress.py`. The result goes to
`assets\maps_v2\<map>\`: `world.c2map` (geometry and material list), `lightmap.ktx2`,
`sky.ktx2` and `tex\*.ktx2`.

What each part does, and why:

- **Textures are KTX2 with BC compression.** No decoding in wasm's only thread, and about a
  quarter of the video memory of a JPEG. A GPU without BC (phones) gets the plain map, and the
  page does not offer the button there.
- **Everything in one `world.c2map`, with meshes merged by material and chunked** keeps the
  frame to about 570 draws at worst. The viewer parses it itself (`textured_map.rs`).
- **`map_material.wgsl` is Source 2's own lighting:** `albedo * (irradiance + sun * NdotL *
  visibility)`. There is **no division by pi**, because Source 2's lights and lightmaps already
  carry it. Dividing gave a sun a third too dim.
- **A lightmap has to be loaded with `is_srgb = false`.** Bevy ignores the format written in a
  KTX2 file and reads it as colour unless told otherwise. The sun's visibility is in the alpha,
  which sRGB does not touch, so a lightmap read as colour keeps the streets right and takes ten
  to twenty times the light out of the shade. It looks like a problem with exposure and is not.
  The same goes for blend masks.
- **The tone curve is the map's own:** Valve's filmic curve with the numbers from the `.vpost`.
  On de_mirage they make it a straight line up to the white point, and on de_ancient it bends.
  The picture is multiplied by 2.8 before the curve, and `m_flWhitePoint` is in stops, so
  1.5 means a white of 2^1.5. Read as a plain number, without the 2.8, it left de_mirage 1.5
  times too dark.
  `exposure` is the middle of `minexposure` and `maxexposure` on `post_processing_volume`. The
  game adapts between the two, which is at most 11% either way on de_mirage and de_nuke, but
  26% on de_ancient and 44% on de_anubis, where the middle looks pale outdoors.
  Bloom, fog and the colour grade are not implemented. The grade is small on de_mirage (3 out of
  255 on average) and larger on de_nuke (9), de_inferno (13), de_anubis (15) and de_vertigo (17).
- **The sun's shadow has its own channel.** `direct_light_shadows` holds up to four lights, and
  `bakedshadowindex` on `light_environment` names the sun's. de_mirage has the same thing in
  the first three, which hides a mistake here: mixing them left the other maps grey and without
  sun. de_train has a sun of zero brightness, so its UVs can't be checked against it.
- **The lightmap's UV set differs by primitive:** it is the last set, `TEXCOORD_1` on most and
  `TEXCOORD_2` where the material has a second UV of its own, as on over half of de_ancient.
- **Source2Viewer 20 exports lightmap UVs already scaled.** Applying `m_vLightmapUvScale`
  again looks plausible and is random: the sun lands on the wrong surfaces. So
  `choose_lightmap_scale` lets the data decide (of the surfaces that face the sun under open
  sky, a right mapping has far more in the sun than the atlas has on average), and **the bake
  stops** if neither scale fits.
- **The page hands the map's bytes to Bevy** by answering its first `fetch` of that URL. The
  page downloads the file itself for the progress bar, and the browser may not keep it: with
  5 GB free on the disk, Chrome did not cache de_nuke's 33 MB `world.c2map`, though it kept the
  lightmap. Without the hand-over that file comes down twice on every visit.
- **Players stand in the map's light.** The bake stores a coarse light grid. `MapLight` sets
  the viewer's sun and ambient so that Bevy's PBR gives the same pixels as the map's shader,
  and `fade_team_colour` multiplies a player's material by the light at their position,
  relative to what an average surface of a player gets in the open. Comparing with a floor
  facing the sun left a player in the shade black from behind.

### Checking the light against the game

The game ships its own screenshots of each map, in `pak01_dir.vpk`, at
`panorama/images/map_icons/screenshots/720p/<map>_<n>_png.vtex_c`. `set_camera_look(x, y, z,
pitch, yaw)` takes the arguments of `setpos` and `setang`, so a view can be lined up with them.
The same surfaces, in sRGB, picked by eye and with the framing only roughly the same:

| Surface | Screenshot | Viewer |
|---|---|---|
| de_mirage, A site floor in the sun | 250, 217, 169 | 250, 219, 167 |
| de_mirage, A site wall in the sun | 231, 186, 124 | 230, 184, 128 |
| de_mirage, Palace floor | 99, 59, 30 | 96, 59, 28 |
| de_nuke, site A concrete wall | 108, 106, 90 | 111, 107, 94 |

With the lightmap read as colour, that Palace floor read 17, 3, 0.

**Still different from those screenshots:** floors and painted metal shine in the game and are
matte here, and walls have no relief, since the map's shader has no normal maps and no
reflections. de_nuke's tanks come out too bright (158 against 124). Props with no lightmap read
the bake's coarse grid rather than the game's probes, which may be why; this has not been
checked. The sky is a little dimmer (176, 213, 224 against 201, 226, 255).

### What it costs (de_mirage, RTX 3070, 1920x1080)

| | measured |
|---|---|
| On the wire | 85 MB: textures 62.5, geometry 15.9, lightmap 7.0 |
| FPS with vsync off | 89 overview with x-ray, 96 overview, 108-136 following a player |
| Worst frame | about 570 draws, 1.75 M triangles |
| Later visits | about 3 s from a local server, more if the browser did not keep `world.c2map` |
| **First visit** | **about 20 s, of which 15 s is shader compilation** |

Those 15 s are **not the map**. The map's shaders take about 60 ms each. The time goes to three
variants of Bevy's `StandardMaterial` fragment shader, about 100 KB of GLSL each, at 5 s each
in ANGLE's D3D11 compiler: effects and primitives, skinned players, and weapons. The tab
freezes while `getProgramParameter` waits for them, and the plain map has the same pause. Chrome
keeps the compiled programs on disk, so it happens once per browser, until a shader changes.

### The nine maps

Measured with nothing else running. "First visit" is on a local server and includes those 15 s.
The site shows the textured map where the last column says so, and `REPLAY3D_CS2_MAPS=all`
shows them all.

| Map | Triangles | Materials | On the wire | FPS, vsync off | Worst draws | First visit | By default |
|---|---|---|---|---|---|---|---|
| de_mirage | 1.29 M | 374 | 85 MB | 89-136 | 567 | 20 s | textured |
| de_nuke | 3.60 M | 598 | 90 MB | 91-126 | 795 | 21 s | textured |
| de_vertigo | 1.99 M | 207 | 70 MB | not measured | - | - | plain, no match to check it on |
| de_anubis | 3.60 M | 367 | 165 MB | 87-140 | 629 | 22 s | plain, size |
| de_inferno | 8.66 M | 919 | 296 MB | 66-125 | 1496 | 31 s | plain, size |
| de_dust2 | 4.54 M | 691 | 298 MB | 78+ (*) | 1021 | 22 s | plain, size |
| de_overpass | 3.98 M | 846 | 305 MB | 74+ (*) | 1084 | 23 s | plain, size |
| de_ancient | 9.19 M | 297 | 305 MB | 106-123 | 658 | 26 s | plain, size |
| de_train | 8.25 M | 1252 | 409 MB | 50+ (*) | 1672 | 30 s | plain, size |

(*) No local match on this map: the number is the overview, the players are from another map.
de_vertigo's world sits about 11000 units up, so another map's players see none of it.

What is still wrong on them:

- **Only the first layer of `csgo_environment_blend` is drawn**, and `g_vTexCoordScale*` is not
  read. This is the material of the newer maps.
- **Some overlays are solid cards.** On de_inferno a red rectangle and a crest on a brown square
  sit on walls.
- **de_anubis looks pale outdoors** against its screenshots, which are more saturated. Its
  exposure range is the widest and its colour grade among the strongest, and neither is applied.
- **de_train has no sun at all** (`brightness = 0`). From above it is grey and readable. No match
  on this map is at hand to look closer.
- **Stationary lights are missing.** `directlight = 3` lights are drawn by the game at run time
  from the other channels of `direct_light_shadows`, and only their bounce is in the lightmap.
  There are 14 of them on de_nuke, 34 on de_train and none on de_mirage.

The size is mostly textures, and on the heaviest maps geometry too: brickwork modelled in
triangles takes de_inferno's `world.c2map` to 104 MB. `-MaxTexture 1024` and simplifying inside
each material are the untried ways down.

### When it is not used

Each of these has been run in a browser and ends in a working replay:

| Situation | Result |
|---|---|
| `REPLAY3D_CS2_MAPS=0`, or no `world.c2map` | plain map |
| "Лёгкая карта" pressed (remembered per browser) | plain map |
| No BC support | plain map |
| Corrupted `world.c2map` | plain map |
| Corrupted textures | textured map in flat material colours |
| Corrupted lightmap | textured map without baked light or shadow |
| Corrupted sky | textured map with the plain background |

**After rebuilding a map, keep the `.br` siblings in step:** `CompressedStaticFiles` trusts them
blindly. `convert_maps.ps1` does this, and `precompress.py` skips a sibling newer than its source.

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

Besides the free camera, the viewer can lock onto any player and watch from
their POV, like a CS2 demo. Pick a player in the right-side panel (or **1–0**),
**←/→** cycles through living players, **X** toggles first-/third-person, **F**
(or Esc) lets go of the player. First-person shows a crosshair and hides the
followed player's own model + nametag, and is drawn as the section "First person"
says. The camera keeps its player from one round to the next (by SteamID) and stays
free when it was free.

**The free camera moves two ways** (`orbit.rs`), kept as one orbit (a point, the angles
around it, a distance) so that switching never moves the camera:

- **Flight**, as CS2's free camera: **W/S** along the view (up and down with it), **A/D**
  level to the side, **E/Q** straight up and down, **Shift** 2.5 times faster (700 Hammer
  units a second without it), a held mouse button turns the view on the spot, the wheel
  moves along the view. Letting go of a player (**F**, Esc, or any of those keys while
  following) flies on from the view on screen.
- **Orbit**, as the page always had: drag turns around a point, the wheel zooms,
  right-drag pans. **F** in flight orbits what the view looks at: the first solid cell
  of the map's grid along the view (`.c2space`, see Effects), else the ground, else a
  point 1000 units ahead.

The page sends what is held with `set_fly_input(forward, right, up, fast)` rather than
letting Bevy read the keyboard, so typing in a field never flies and a key let go in
another window is released (`blur`, `visibilitychange`). All keys go by `KeyboardEvent.code`,
so a Russian layout works: `e.key` for W is "ц" there. `set_camera_orbit()`,
`set_camera_fly()` and `set_camera_overview()` (the first view, from above) switch on the
next frame, where the camera's place is known; `set_camera_free()` only lets go of a
player, and never overrides a switch asked for in the same frame.

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
  `CROUCH_S=0.2` (the eye moves between the two, worked out from the flag's history),
  `CHASE_DIST=135` / `CHASE_UP=35` (third-person framing). The horizontal aim
  reuses the same yaw the player models use, so it tracks the model calibration.
- **Per-frame payload** is **10 floats/player** — `{x, y, onScreen, alive,
  health, weapon, worldX, worldY, yawDeg, present}`. `onScreen` gates the
  floating 3D nametag; `alive`/`health` are real regardless of projection, so
  the spectator panel shows every player's true state (not just those on
  screen). `worldX/worldY/yawDeg` are RAW demo-world values (pre scene-scale)
  for the page's live radar — dead players hold their frozen death coords,
  `present=0` means "not in this round, draw nothing". They come after a header of
  7 floats, `{clock, duration, playing, flashOpacity, nPlayers, nTracers, scopeUp}`, and
  tracer records follow at `HDR + nPlayers*PSTRIDE`, 4 floats each, and the radar's marks
  fill the rest, `MSTRIDE`=5 floats each (`radar.rs`, below). Both the wasm writer (`report_frame`)
  and the two HTML readers (`www/index.html`, `web/templates/replay3d.html`, `HDR` and
  `PSTRIDE` consts) share this layout — bump the prod page's `?v=` cache-buster on the pkg URLs
  whenever it changes (the static pkg is cached for 7 days).

## As in CS2, or for analysis (the two looks)

Two buttons in the bottom bar, **C** on the keyboard. The choice is kept in this browser
(`localStorage` `replay3d.mode`); `?mode=cs2` or `?mode=analysis` overrides it for one visit.

| | As in CS2 (the default) | Analysis |
|---|---|---|
| Camera when the page opens | a player's eyes: the viewer's own, else the report's player (`replay3d.pov_sids`), else the first slot | the free camera over the map |
| Players through walls and smoke (x-ray, **V**) | off | on |
| Names over heads, thrower names on grenades | no | yes |
| Players painted in their team's colour from afar, a little self-lit | no | yes (`set_analysis_look`) |
| Coloured line along a grenade's flight | no | yes |
| C4 panel with the fuse counting down | no: in CS2 the beeps are the timer | yes |
| Radar while in a player's eyes | centred on them, turning with their view, half the map across | the whole map, north up |
| When the followed player dies | a living teammate, 2.5 s later while playing, at once when paused or scrubbed there; back to the player when a scrub brings them back to life | stays on the spot |

Switching to the CS2 look goes into the chosen player's eyes in first person. Switching to
analysis goes back to the first view from above. **V** and **F** still work within either.

Not one to one: CS2's spectator HUD has player cards along the bottom and its own radar
art, here the list stays on the right and the radar is the overview image; the scoreboard
has no ping, MVPs or score; the 1:30 for four players is the wingman default, not read from
the demo; and the move to a teammate after a death, and its 2.5 s, are this page's choice.

## CS2-style HUD (score bar · live radar · kill feed)

All page-side, in `web/templates/replay3d.html` (the dev page has the older HUD):

- **Round timer** counts down as the game's does: 1:55 from the end of the freeze (1:30
  with four players or fewer, as wingman), whole seconds rounded up, frozen at the round's
  end (`durationS` less the 3 s the replay runs on). Measured: match 2's round 4 ran out of
  time and its replay is 118.0 s. After the plant the bomb's icon stands in its place,
  blinking red until it is defused (green) or blows. The playback clock stays in the bar.
- **Scoreboard on Tab** (held, as in the game): kills, deaths, assists and headshot share up
  to this moment of the match, from the manifest's kill lists, and money from the round's
  `state` (data version 2). Counted every 200 ms while it shows, so scrubbing gives that
  moment's table. Suicides and the world count a death only; a team kill counts as a kill,
  since an old round's victims have no team.
- **Radar marks** (`radar.rs`, one more system before `report_frame`): a grenade in the
  air as a dot of its type's colour; a smoke and a fire as discs of their radius, coming in
  and going out with them; an HE or a flash as a ring racing out; the bomb. Who carries the
  bomb comes from `bombPickup`/`bombDrop` in a 64 Hz replay and from whoever is seen holding
  it, which is all an old replay has: the bomb is handed out before the replay starts, so
  the first carrier is the first one seen with it, and when a carrier dies without a drop
  event it lies where they fell. Planted, it sits at `bomb.pos` (or where its carrier stood)
  with a pulse; defused, in green; gone once it blows. Each dot carries the key that follows
  that player, 1–9 then 0.

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
- **Live radar (top-left)** — (marks and turning above) a 2D canvas drawing the match's radar PNG
  (`manifest.radarAsset`, same asset the greybox mode textures) with per-frame
  team-colored dots + white view-direction ticks, an ✕ frozen at each death
  spot, and a highlight ring around the followed player. Projection uses the
  manifest calibration exactly like `mapscal::world_to_radar`:
  `px = (worldX − pos_x)/scale`, `py = (pos_y − worldY)/scale`, scaled to the
  canvas. The #hud panel moved down below it.
- **C4 panel (top-center, under the score bar; analysis look only)** — "непонятно что бомба
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
  defuse/explosion that closes it into `Meta3d::bombs`. CS2 sends `site` as an entity
  index, so the letter comes from the planter's place name (`BombsiteA`) on data version 2.
  Older replays only map a literal "A"/"B" or 0/1, and otherwise show just "planted".
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

**Real CS2 weapons** go on the real player models' `wpn` bone when they are built:

```powershell
.\tools\convert_weapons.ps1      # about 7 minutes; needs CS2 + Source2Viewer 20+
.\tools\build_replay3d.ps1       # precompresses them
```

`rust/viewer/src/weapon_models.txt` lists the 44 models by weapon tag. The results
land in `assets/models/cs2_v1/weapons/`, gitignored for the same reason as the
player models below: 59 MB raw, 30 MB as Brotli, of which a round loads only the
guns in its `weaponTable` (11 files in round 1 of the test match, 17 by round 13).
They are static meshes and tracers start at their `muzzle_flash` point. Dual Berettas
show one pistol in a player's hands. A file that fails to load falls back to the kit
below, and `REPLAY3D_CS2_MODELS=0` turns the guns off along with the bodies. First person
has its own files, see the section "First person".

**The fallback** is the procedural low-poly weapon kits (`viewer/src/weapons_viz.rs`) —
no external assets, cuboids + a sphere in the same stylized look as the untextured map.
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
- **First person, when the real arms and rigs cannot show:** a `ViewModel` anchor child
  of the main camera (bottom-right: `VM_POS`/`VM_YAW`/`VM_SCALE`), filled with the followed
  player's current weapon. It stays hidden while `FpState.active` says the real ones are up.
- Kits swap only when the weapon CLASS changes (cheap: compare + respawn a few
  cuboids). Dead players hold nothing, and absent ones show nothing (mount inherits the hidden body).

## First person (the arms and a weapon that moves)

```powershell
.\tools\convert_viewmodel.ps1    # the clips and both sides' arms; about 2 minutes
.\tools\convert_weapons.ps1      # each weapon's rig along with its model; about 8 minutes
.\tools\build_replay3d.ps1       # precompresses them
```

`assets/models/cs2_v1/viewmodel/` is gitignored like the rest of `cs2_v1`. It holds
`arms_t.glb` and `arms_ct.glb` (about 1.2 MB each) and one rig per weapon, built by
`tools/prepare_viewmodel.py` from the list in `tools/viewmodel_clips.txt`, which was read off
CS2's own animation graphs (`animation/graphs/viewmodel/viewmodel_gun.vnmgraph+<weapon>`).
`rust/viewer/src/viewmodel.rs` asks for a rig the first time someone looks through the eyes
of a player who will hold that weapon in the round, so a page that never goes first person
downloads none of it. Any of it missing or broken leaves the still weapon from "Weapon
models" in its place, and `REPLAY3D_CS2_MODELS=0` turns it off with the rest.

**What it costs:** 46 files, 63 MB raw and 27 MB as Brotli, of which a visit loads the arms and
the rigs of whoever it looks through. On de_mirage in a fight at 1920x1080 on an RTX 3070, three
alternating pairs gave 87 FPS against 91 with the still weapon in first person, and 90-91 either
way in third person.

- **The arms are not `weapons/models/shared/arms/weapon_arms`.** That is the generic pair.
  Each agent model holds its own `firstperson_default_gloves_arms` and `firstperson_sleeves`,
  skinned to bones the first-person clips drive: their inverse bind matrices agree with
  `weapon_arms` on all 68 shared bones. Only the forearm's `*_TWIST` bones are in the models and not in the
  clips, so they follow their parents rigidly. If a wrist ever looks wrong, that is why.
- **A clip holds two skeletons**, `viewmodel.vnmskel` (arms and `wpn`) and
  `weapons/<weapon>.vnmskel` (bolt, magazine), as two scene roots. The second hangs from `wpn`
  (`m_attachToBoneID`), so a weapon's file nests its bones that way. The weapon's root bone
  stays at identity with no track, because the value on it is only the export's axis swap.
- **Weapons are rigid**, so each bone gets a plain static mesh and there is no new shader
  variant. Vertices go into the bone's frame through the skin's inverse bind matrix, since the
  model's root sits somewhere else than the clip's. The rigs have no textures: their UVs land on
  the third-person atlas, and the material is a copy of that one. Exporting the model with
  `--gltf_export_animations --gltf_animation_list nothing_at_all` is what brings the skeleton.
- **Bevy finds a bone by the path of names from the root.** The weapon's bones are spawned by
  hand under the arms' `wpn` with `AnimationTargetId::from_names` and `AnimatedBy`, so a file's
  node names are part of its format. The arms file needs a clip that moves nothing, because
  Bevy only hands out targets to a file with an animation.
- **One small graph per weapon**, swapped when the held weapon changes: Bevy walks every node
  for every animated bone. Shots take turns on two nodes, one per hand on the Dual Berettas and
  the same clip twice on any other gun, so a burst can crossfade from shot to shot.
- **What is on is a function of time**, like the players' clips: only events since the last
  weapon change count, firing ends a reload, and a pause or a scrub lands on the right pose.
  A knife only swings when it hits, because the replay keeps gun shots only.
- **Three cameras.** The arms have their own, `order: 2`, on `FP_LAYER` with its depth cleared,
  so they never go through a wall. It must keep the main camera's `Msaa` and `Tonemapping` or
  the first visit compiles new shaders. The sun needs that layer too, since Bevy filters lights
  by layer, and `AmbientLight` is per camera, so it is copied over. Both materials get
  `MapLight::at` where the player stands, as player bodies do.
- **Field of view.** The world is 90 degrees at 4:3, which is 73.7 vertical, in first person
  only; the rest stay at 55. The arms use `viewmodel_fov` 60 and offset (1, 1, -1), consts at the
  top of `viewmodel.rs`. Web sources disagree on CS2's stock values, and 68 with (2.5, 0, -1.5) is
  also common, so these are a choice. A tracer leaves the muzzle's spot on screen and not its
  place in the world, because the gun is drawn at another angle (`muzzle_on_screen`).
- **Scopes.** Field of view and whether the gun hides come from the game's
  `scripts/weapons.vdata` (`m_nZoomFOV1`, `m_nZoomFOV2`, `m_bHideViewModelWhenZoomed`). `F_SCOPED`
  says when, on old replays too. The second level needs the `zoom` events, so old replays stay at
  the first. The page draws the scope itself, without any of the game's textures.
- **Flash.** The channel holds one value per flash from the moment it lands and stays up 1.4
  times as long as that value says (checked on match 1), so the time left comes from the value.
  The curve is CS:GO's client as far as memory of it goes, so close and not known to be exact:
  white while 3 s or more are left, then the square of the share of those 3 s. Only the player looked through flashes the screen.
- **The gun's trail and sway** are the sums in Source's published `CalcViewModelLag` and
  `CalcViewmodelBob`, worked out from the track. CS2's own numbers are not public.
- **Numbers on screen.** Health is in every replay. Armour, money and the magazine come from the
  header's `state` lists, so old replays show health alone. The demo gives no reserve ammo.
- **Not like the game yet:** no muzzle flash or shells on the gun (Stage 6), no view kick since
  `aim_punch_angle` does not read, no `reload_empty` or slide-locked poses, and one model per
  gun, so the Dual Berettas are the one case where first person shows more than third person.
  P2000, CZ75, R8, G3SG1, UMP-45, MP5-SD, XM1014, Sawed-Off, M249 and Negev are built, but nobody
  holds one for long in the three local demos, so they have not been looked at. If a weapon's
  own clip names ever change, `tools/viewmodel_clips.txt` is the place to fix them.

## Sound (real CS2 sounds, with a synth behind them; page-side)

`sfx.js` reads the players from the frame report after the header length the page passes,
`SFX.frame(d, nP, HDR)`. It used to have 6 written in: when the header grew to 7 floats in
the first-person work, every footstep and the direction of every sound came from the
neighbouring float until this was found in the HUD work. **M** mutes, by key code too.

`web/static/replay3d/sfx.js` plays the match soundtrack from data the page already
has. It needs **no wasm work**, so it can be iterated on with nothing but a page
reload. Each sound has two sources behind one event logic:

**Real CS2 sounds**, built from a local CS2 install and never committed:

```powershell
.\tools\convert_sounds.ps1       # about 4 minutes; needs CS2, Source2Viewer 20+, ffmpeg
```

`tools/sound_events.txt` names CS2's own sound events. `tools/prepare_sounds.py` reads
those events' definitions and encodes their files to Ogg Opus in `assets/sounds_v1/`
(177 files, 3.5 MB), with an `index.json` holding what the game mixes them by: volume,
pitch, random pitch, voice limit and the volume-by-distance curve. Those are not tuned
here, so a gun's near and distant events crossfade the way CS2 crossfades them, and a
gun's next shot cuts its last with CS2's fade. `sfx.js` decodes on an
`OfflineAudioContext`, which needs no user gesture, so the sounds are ready before the
first click: everything but the guns at once and each round's guns as it loads, 36 MB
of memory after round 1 and 65 MB after 14 rounds. The most voices measured at once in
close firefights is 31 of the 96 allowed.

`REPLAY3D_CS2_SOUNDS=0` turns them off. So does a clone without the files.

**The synth** (filtered noise and swept oscillators) plays whatever has no decoded file:
no build, a browser that cannot decode Opus, a corrupted file, or a sound still loading.

- **Without the real sounds the default mix is GUNFIRE ONLY; with them, `SAMPLE_MIX`
  also starts footsteps, grenades, the bomb and impacts.** The first pass also ran a
  room-tone bed (brown noise) and molotov/smoke noise loops; in practice that was a
  constant loud fan hum and a painful hiss, so all three were **removed from the code** —
  a decoy's fake gunfire is the only surviving loop, nothing in `sound_events.txt` may
  loop, and a smoke plays only the first `SMOKE_S` seconds of CS2's 14 s hiss. The POV
  sounds and UI ticks start at **0** and are opt-in from the ⚙ mixer. A zeroed bus is
  checked in `claim()` before any node is built, so muted categories cost nothing.
  `DEFAULTS` changes must bump `STORE_KEY` (`replay3d.sfx.v3`), otherwise a stored older
  mix resurrects the very sounds that were turned off.
- **Level.** CS2's files are mastered hot, so samples play at `SAMPLE_HEADROOM` and a
  limiter ends the chain. Measured over close firefights in three rounds: peak 0.94, no
  clipped samples.
- **Canonical copy + dev mirror.** The tracked file is
  `web/static/replay3d/sfx.js`; `build_replay3d.ps1` copies it to
  `rust/viewer/www/sfx.js` (gitignored, like the pkg mirror) and precompresses
  the prod copy. **Bump the `?v=` token on the prod page's `sfx.js` URL whenever
  you change it** — `/static` is cached for 7 days.
- **What makes sound, and from which data:**
  | sound | source |
  |---|---|
  | gunshots (each gun its own real sound; the synth has one per class: rifle/sniper/SMG/pistol/deagle/shotgun/MG, silenced variants, knife swish, Zeus arc) | round header `shots[] {s, p}` + the shooter's live `weapon` from the frame payload |
  | footsteps (running only, walking is silent as in the game; always concrete, the floor material is not in the data) | distance travelled between frames (`worldX/worldY`), one step per `STEP_LEN`=76 u |
  | HE blast, flash pop, smoke, molotov ignite and fade-out | header `utility[]` `detSample`/`pos`/`duration` |
  | decoy's fake gunfire (as an AK: the data does not say what it imitates) | that item's `duration` window |
  | grenade throw | `throwSample`/`throwPos` |
  | bullet impacts, own "hurt", death sound | per-frame `health` drops and `alive` 1→0 |
  | body and headshot kill sounds, kill ding (higher on a headshot), your own death thud | manifest `kills[]` |
  | bomb plant, beeps speeding up, last warnings, defuse, blast | manifest `bomb {plant, defused, exploded}`, or a kill with `w:"💣"` for the blast in a round without it. The beep rate and warning times are remembered from the CS:GO code, not read from CS2 |
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
  into the bottom bar (per-bus sliders: gunfire, footsteps, grenades and C4, impacts,
  own/POV, interface, reverb; toggles for the speed-of-sound delay and the flash
  ringing), persisted in `localStorage` under `replay3d.sfx.v3`.
  **M** mutes. Browsers only allow audio after a gesture, so the context starts
  on the first click/keypress.
- **Page hooks** (identical in both HTMLs): `SFX.setRound(header, meta)` in
  `loadRound`, `SFX.frame(d, nP)` in `__replayFrame`, `SFX.setListener` in
  `applyCam`, `SFX.setSpeed` on the speed buttons, and `seekTo()` — a wrapper
  around `seek()` that calls `SFX.resync()`, used by the scrubber and every
  kill/utility mark. `SFX.useSamples(base)` before `mountUI`, so the sliders show
  the mix.
- **Not implemented for lack of data:** reloads, scoped-in clicks, grenade bounces and
  the plant and defuse start: there are no such events in the manifest or the codec
  (`F_SCOPED`/`F_DEFUSING` exist in the codec's flags but never reach the page). Walls
  do not muffle anything, as the page knows no geometry. The bomb sounds from `bomb.pos`.
  A replay from before data version 2 has none: there it is taken from whoever held the
  bomb at the plant, so a scrub past the plant leaves it at a fixed middle distance.

## Weapon icons (kill feed and player list)

```powershell
.\tools\convert_hud.ps1          # a few seconds; needs CS2 + Source2Viewer 20+
```

`tools/hud_icons.txt` lists CS2's equipment icons by weapon tag. They go to
`assets/hud_v1/<tag>.svg`, gitignored like the rest (44 files, 590 KB, loaded as they
appear). The page's `showWeapon()` keeps the text label until the icon has loaded, so a
clone without them, or a tag with no icon, stays text. There is no flag: the files decide.

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
.\tools\convert_characters.ps1   # about a minute and 250 MB of raw files while it runs; needs CS2 + Source2Viewer 20+
```

It writes `t_rig2.glb`, `ct_rig2.glb` and `actions_rig2.glb`. Static assets are cached for a
week by URL, so a rebuild that changes the bone tree has to change the file names too: an old
model with new clips would put the gun in the wrong place. The names live in `scene.rs`,
`web/replay3d.py`, the warm fetch in `replay3d.html` and the two Python tools. A machine that
only has the older `t.glb` and `ct.glb` gets the soldier until it runs the script again.

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
- moves `wpnPivot` from `root_motion` to `spine_3` and rewrites its tracks to match. The game
  keeps the gun in the hands with IK, which we do not have, so otherwise any blend of arms
  and legs and any bend of the spine would pull the gun out of the hands;
- leaves in the clips any track `actions_rig2.glb` moves, even if it holds one value in all
  the walking clips. Bevy leaves a bone alone when no playing clip has a track for it, so a
  value baked into the rest pose would never come back after a reload;
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
  holds all the clips: each player has a graph of 11 nodes (`player_rig.rs`), and
  `drive_player_anim` writes the next clip into the next node.

**Which clip plays** (`player_anim.rs`, unit-tested without a renderer): each weapon
class has `idle`, `idle_crouch` and `walk_*`, `run_*`, `crouch_*` in 8 directions.
`ground_motion` measures ground velocity over 0.125 s either way, since one step between
samples is too noisy, and in seconds because replays come at 16 and at 64 samples a second.
`drive_player_anim` turns it into a direction relative to the player's yaw. Gait and
direction both have hysteresis so clips do not flicker at a threshold. Clips run on
the replay clock, so pausing holds the pose and slow motion slows the legs. Clip rate
follows ground speed so the feet do not slide. The clips carry no root motion and report
a velocity of 0, so the nominal speeds in `player_anim.rs` were measured from how fast a
planted foot moves relative to the body. That also confirms the directions: `e` is the
character's right.

**Team colour fades with distance** (`fade_team_colour`). Up close a player shows the real
textures. From about 20 scene units out it is painted in the team colour like the soldier,
because from the overview a sand-coloured T is a few pixels on a sand-coloured map.

The walking reads channels every replay has (position, yaw, `F_CROUCH`, weapon), so old
`.3dr` files get the models with no re-upload.

### What a player does besides walking

`actions_rig2.glb` holds 284 clips on a copy of the skeleton's names and no mesh, once for both
sides. Bevy finds a clip's bones by the path of names from the root, so they fit either model.
They come from the clip files themselves (`vnmclip_c`, exported in bulk), because the deaths are
not in the models' animation graphs: the game uses ragdolls. `player_actions.txt` is the list,
read by `tools/prepare_actions.py` and by the viewer, so the two cannot disagree on a name.

```text
Add ── Blend ── walking and whole-body clips, crossfaded
    │        └─ upper-body clips, which have no leg tracks, so the legs keep walking
    ├─ a shot
    └─ a flinch or a landing
```

| What | Comes from | On old replays |
|---|---|---|
| Shot | `shots` | yes |
| Draw | the weapon channel changing | yes |
| Grenade throw | `utility[].throwSample`, 0.3 s before the grenade leaves the hand | yes |
| Flash arm | the flash channel, at 1 s or more | yes |
| Defuse | `F_DEFUSING` | yes |
| Death | `F_ALIVE` falling, `kills` for who and whether it was a headshot | yes, the fall chosen by attacker and headshot only |
| Flinch | `hurt` by hit group, or the health channel dropping | generic flinch only |
| Knife swing | a `hurt` or a kill with the knife. `shots` keeps guns only, so misses do not show | kills only |
| Reload, plant | `reload`, `plant` events | no |
| Jump, fall, landing | `F_AIRBORNE` | no |

**`player_actions.rs` works out a whole round at once and answers from the clock.** It is pure
and unit-tested without a renderer. Nothing depends on earlier frames, so a scrub, pause or slow
motion lands on the same pose as playing there: stills taken after seeking from either
direction differ by nothing. Timed clips run at speed 0 with their time written every frame.

**The fall** is chosen by whether the attacker was in front (from behind a body goes forward) and
by the hit group, varied per player and round without a random number, so a scrub gets the same
fall. A body stays until the round ends, off the x-ray layer, since only the living should show
through walls. A kill in mid-air comes down to where the player last stood. A dropout of the
alive flag shorter than 0.1 s, health back and all, is not a death: it does happen in real demos.

**The aim** (`apply_player_aim`) runs after the clips and before transforms propagate. No clip can
do it, since it has to follow the demo's pitch and the twist between legs and eyes. The legs lag
behind: `body_yaw_track` keeps a standing player's feet until twisted by 50 degrees and turns them
along quickly when moving, and never lets the twist pass 80. The spine takes the pitch up to 55
degrees and the head the rest. The turn is split between four bones by powers of one rotation, so
the chest ends up turned by exactly the whole aim and the gun, which hangs off it, points there.

**Things that cost time to find in Bevy 0.19:**

- A seek time equal to the clip's length wraps to 0 even at speed 0, which stood corpses back up.
  `held_time` stops just short of the end.
- An Add node computes `delta * base`, while CS2 authors additive clips as `base * delta`. The two
  differ by the base's rotation. Each additive clip has a `+non_additive` twin, so
  `prepare_actions.py` writes `pose(t) * reference^-1`, exact on the pose it was authored over.
- An additive track on a bone with no base value writes the delta as the pose, so added clips keep
  only tracks the walking clips also have, and no scale.
- A blend weight is a share of the total, so "all of the arms" over a walking clip is a large
  weight, not 1. Bones with no track in a clip are unaffected by it, which is why no bone masks.
- The children of an Add run in node order and the first is the base, so the Blend must be made
  before the added clips.

**Without the clips** (a missing or broken `actions_rig2.glb`) players walk as before and vanish on
death. The same goes for the soldier, whose bones the clips do not fit. A broken file only logs
a warning.

**What it costs**, on de_mirage at 1920x1080 on an RTX 3070, in three alternating pairs against
the previous build: 119 to 105 FPS at the start of a round and 84 to 77 in a fight, so 12% and 8%,
under the 20% this was allowed. 5.75 MB more on disk and 1.33 MB on the wire, loading in the
background without a long task of its own. Building a round's timelines does not show in the time
to change rounds.

**Not like the game:** a corpse plays a clip rather than a ragdoll, so it can sink into a wall or
hang over an edge. Ladders set `F_AIRBORNE`, so a climb shows the falling pose. The gun vanishes
on death. The agent a player actually wore is not shown (`agent_skin` in the parser, never
extracted).

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
- **Dynamic grenades, the simple shapes** (`util.rs` build + `lib.rs` `update_utility`,
  all animated from the playback clock so scrubbing stays correct). Since the effects
  below, these only show when the CS2 particle textures are missing, turned off or broken:
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
    a hot head races the beam in `TRACER_TRAVEL` s (0.045) with a trailing dash
    (`TRACER_STREAK_FRAC`=0.42 of the beam), the whole streak fading out by
    `TRACER_LIFE` s (0.20). It is one warm colour for both sides, as in CS2, and
    thin (`TRACER_WIDTH`=1.6 px, its own `TracerGizmos` group). A bullet that hit
    someone (a `hurt` event of the same moment) stops at their chest; any other stops
    where the map's solid-space grid has a wall, when the effects are on, with dust
    there. Timing is in playback-clock seconds, so scrubbing lands on the right shots
    and slo-mo stretches the streaks. Beam length `TRACER_LEN_WU` (3000 u). The
    muzzle flash is a sprite, see Effects; without them it is a small sphere.
  - Matches analysed before this feature (old `.3dr`) simply have no `shots` key →
    no tracers; re-uploading the demo backfills them.

## Effects (CS2's particle textures)

Smoke, fire, explosions, shots, blood and the bomb drawn with the textures CS2's own
particles use, and the loose models around them. Nothing of it is committed: like the
models it is built from a local CS2 and lives in ignored folders.

```powershell
powershell -ExecutionPolicy Bypass -File tools\convert_fx.ps1
```

It exports the textures listed in `tools/fx_sheets.txt` with Source2Viewer, and
`tools/prepare_fx.py` packs each into one atlas: frames spread over each sequence,
premultiplied in linear light, BC3 sRGB KTX2 with mipmaps, in
`web/static/replay3d/assets/fx_v1/` (and the dev copy under `rust/viewer/www/`). Then
`tools/prepare_space.py` writes `fx_v1/space/<map>.c2space` for every built textured
map, and everything is precompressed. Valve's particle systems themselves do not carry
over, so all the motion is the viewer's own (`effects.rs`).

**Drawing** (`fx.rs`, `fx.wgsl`). Each sprite is a camera-facing square the vertex shader
builds from its centre, with its frame, colour and opacity. Colour is premultiplied and
says how much a sprite covers what is behind it, so one material does both additive
glow and smoke that hides. Sprites are gathered per frame into one mesh per group and
sorted back to front; close to the camera a sprite fades instead of filling the screen.
Smoke and dust take the light a white wall would show at that spot on the textured map
(the baked light grid through the map's tone curve), so a smoke indoors is as dim as the
room around it.

**The solid-space grid** (`space.rs`, `.c2space`). 16-unit cells, solid where an opaque
surface of the textured map passes; glass, leaves and decals do not count. The layout
is in `tools/prepare_space.py`. A smoke floods the open cells around its centre, nearest
first, until it has as many cells as a dome 288 units across and 112 up would; so in
a corridor it runs further along it and never through a wall. Fire floods the floor the
same way, up to the molotov's radius. Bullets step through it to find the wall they hit.
Without a grid, smoke is that dome and fire a disc.

**What shows:**

| | |
|---|---|
| Smoke | about 50 soft puffs over the flooded cells, blooming out over 1.2 s and thinning over the last 2; a grey veil when the camera is inside one |
| Molotov / incendiary | flames over the floor cells, dark puffs rising, a warm glow |
| HE | flash, fireball, dark smoke, a ring of dust along the floor |
| Flash | a white flash |
| Shots | muzzle flash (bigger in first person, at the gun in the hands), dust where a bullet stops, blood out the far side of a player who was hit |
| Bomb | the real C4 lying where it was planted (or, before replays said where, where its carrier stood), its light blinking with the beeps of `sfx.js`, and an explosion three times an HE's when it blows |

**Models in the world** (`world_items.rs`, needs the real weapon models): grenades fly
their recorded path turning over, and a smoke or decoy lies where it landed while it
works; the gun a killed player held lies on its side in front of them until someone who
picks something up stands within 72 units of it (pickups are only in 64 Hz replays, so
on old ones it lies there to the end of the round); a dropped bomb lies flat until picked
up; the planted C4 lies flat until it blows. Each is set down by its own bounding box,
so nothing sinks into the floor. Brass shells fly out of the right of every gun fired
within 900 units of the camera and lie on the floor for 2 s.

**Player shadows.** Only players and what they hold cast into Bevy's shadow map: one
cascade, 1200 units from the camera. The map's shader takes the sun away under them,
only where the bake already has sun, so no spot is shadowed twice. On only while the
textured map shows; `set_player_shadows(false)` from the page turns them off.

**When it is not used.** `REPLAY3D_CS2_FX=0`, or any atlas missing (`cs2_fx_enabled`),
and the page tells the viewer to keep the simple shapes. An atlas that fails to load or
decode does the same on its own. A broken grid only takes the flooding away. (A `.br`
the browser cannot unpack is different: Bevy's web asset reader panics on any failed
fetch, for every asset, not only these.)

**What it costs** (de_mirage, RTX 3070, 1936x1231, three alternating pairs each):

| | |
|---|---|
| Download | atlases 1.9 MB in brotli (5.8 MB raw); grids 50–215 KB per map in brotli |
| Memory | a grid is one bit a cell: 1.4 MB on de_mirage, 25 MB on de_vertigo |
| FPS with effects vs the simple shapes | 101 vs 98 at round start, 84 vs 70 in a fight with smokes (the old shapes were the slower ones) |
| FPS with player shadows vs without | 102 vs 107 at round start, 86 vs 88 in a fight |

Not one to one: CS2's smoke is a volume that bullets and HEs push open, this one is
sprites; fire is placed by the floor, not by the game's fire cells, which the demo
parser does not give; guns fall where their owner died, not where they come to rest;
shells do not bounce.

## Env vars (web/.env)

| var | default | meaning |
|---|---|---|
| `EXTRACT3D_BIN` | `rust/target/release/extract3d(.exe)` | extract3d binary; missing → uploads still work, no 3D |
| `EXTRACT3D_TIMEOUT_S` | `900` | kill a hung extraction |
| `REPLAY3D_CS2_MODELS` | `1` | `0` shows the fallback soldier and simple weapons instead of the real CS2 player and weapon models |
| `REPLAY3D_CS2_SOUNDS` | `1` | `0` synthesizes gunfire instead of playing the real CS2 sounds |
| `REPLAY3D_CS2_FX` | `1` | `0` keeps the simple smoke, fire and explosion shapes instead of CS2's particle textures |
| `REPLAY3D_CS2_MAPS` | `1` | Textured maps from `assets/maps_v2/`: `1` shows the ones in `TEXTURED_MAPS_READY` (de_mirage, de_nuke), `all` every built one, `0` none |
| `KEEP_DEMOS` | `1` | `0` discards each demo after parsing instead of keeping a gzipped copy in `web/data/demos/` |

Artifacts live in `web/data/replay3d/<match_id>/`; deleting a directory removes
that match's 3D replay (it comes back on re-upload of the same .dem, or from
`tools/rebuild_replays.py` when its demo was kept).
