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
   `(x,z,-y)` contract, filters tool brushes, and — with `--slim-attrs` — drops
   vertex attributes an untextured map never uses, then packs one `.glb`):

   ```powershell
   python tools\prepare_map.py raw\de_mirage de_mirage --glb --slim-attrs --dev
   ```

   This writes `web/static/replay3d/assets/maps/de_mirage/world.glb` (and, with
   `--dev`, mirrors it into `rust/viewer/www/assets/maps/` for the standalone
   page). From the next page load, every match on that map shows real geometry
   (plus a Map/Radar toggle). Options: `--slim-attrs` (keep only POSITION+NORMAL
   — ~halves an untextured map, geometry unchanged), `--max-size 512` (slow
   GPUs), `--geometry-only` (strip base-colour too), `--keep-maps` (keep
   normal/MR/AO). For de_mirage the geometry-only `world.glb` is ~52 MB.

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
- Only `.glb` under `assets/maps/` is tracked; the radar PNG copies in the same
  `assets/` dir stay ignored (`.gitignore`).

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
