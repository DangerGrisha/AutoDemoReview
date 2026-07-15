# 3D replay on real CS2 maps

The 3D replay renders every round in the browser (Bevy → WASM/WebGL2): players
as team-colored capsules, grenades with throw arcs and timed effects, a kill
timeline, and — when a converted map is installed — the **real map geometry**,
perfectly registered with player positions. Without a map it falls back to the
radar greybox.

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
  world units (`rust/viewer/src/coords.rs`). Source2Viewer exports glTF in
  exactly `(x, z, −y)` with no scaling, so the map is spawned with a single
  uniform `Transform::from_scale(sceneScale)` — never enable Bevy's
  `GltfPlugin::convert_coordinates`.
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
3. **Python deps** for map preparation: `pip install pygltflib Pillow`.

Then:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_replay3d.ps1
```

This produces `extract3d.exe`, `web/static/replay3d/pkg/` (the wasm viewer),
and copies the 8 radar PNGs into the viewer's asset root.

The web app finds extract3d via `EXTRACT3D_BIN` in `.env` (defaults to
`rust/target/release/extract3d.exe`; on this machine the target dir is
relocated, so `.env` pins `EXTRACT3D_BIN=D:\rust\target\release\extract3d.exe`).

## Converting a map (once per map, on any PC with CS2 installed)

1. Download the **Source2Viewer CLI** (`cli-windows-x64.zip`) from
   <https://github.com/ValveResourceFormat/ValveResourceFormat/releases/latest>
   and unzip into `tools/s2v/` (gitignored). Run `--help` once to confirm the
   flag names for your version.
2. Export the map's world to glTF (paths for a default Steam install):

   ```powershell
   .\tools\s2v\Source2Viewer-CLI.exe `
     -i "C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\maps\de_mirage.vpk" `
     -e "vwrld_c" -d --recursive --gltf_export_materials -o raw\de_mirage
   ```

3. Prepare it for the viewer (filters invisible tool brushes, strips
   normal/MR/AO maps, downscales textures to ≤1024, packs one .glb):

   ```powershell
   python tools\prepare_map.py raw\de_mirage de_mirage --glb
   ```

   This writes `web/static/replay3d/assets/maps/de_mirage/world.glb`. From the
   next page load, every match on that map shows real geometry (plus a
   Map/Radar toggle). Options: `--max-size 512` (slow GPUs), `--geometry-only`
   (tiny, untextured), `--keep-maps` (keep normal/MR/AO), `--dev` (also mirror
   into `rust/viewer/www/assets/` for the standalone dev page).

   Already-converted exports from the CS2MapViewer project work as input too —
   point `prepare_map.py` at the directory containing the world `.gltf`.

## Dev page (no FastAPI)

`rust/viewer/www/index.html` runs the viewer standalone: build (the build
script mirrors the pkg), run `extract3d demos\match.dem --out-dir rust\viewer\www`
so `manifest.json` + rounds land next to the page, then serve `rust/viewer/www`
with any static server. `?map=maps/de_mirage/world.glb` injects a map placed
under `rust/viewer/www/assets/`.

## Env vars (web/.env)

| var | default | meaning |
|---|---|---|
| `EXTRACT3D_BIN` | `rust/target/release/extract3d(.exe)` | extract3d binary; missing → uploads still work, no 3D |
| `EXTRACT3D_TIMEOUT_S` | `900` | kill a hung extraction |

Artifacts live in `web/data/replay3d/<match_id>/`; deleting a directory removes
that match's 3D replay (it comes back on re-upload of the same .dem).
