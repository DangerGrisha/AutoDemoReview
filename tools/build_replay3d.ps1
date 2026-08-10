# Build the 3D replay pipeline: extract3d (native) + viewer (wasm) + static assets.
# PowerShell 5.1-safe (no && / ternary). Run from anywhere; paths are script-relative.
#
# Prereqs (see docs/REPLAY3D.md): rustup with stable toolchain + wasm32-unknown-unknown
# target, wasm-bindgen-cli matching Cargo.lock's wasm-bindgen (0.2.126).
# On this machine Rust lives on D: (C: is full); the env block below makes the
# script work either way -- it only overrides when those paths exist.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

if (Test-Path 'D:\rust\rustup') { $env:RUSTUP_HOME = 'D:\rust\rustup' }
if (Test-Path 'D:\rust\cargo')  { $env:CARGO_HOME  = 'D:\rust\cargo'; $env:Path = "D:\rust\cargo\bin;$env:Path" }
if (Test-Path 'D:\rust\target') { $env:CARGO_TARGET_DIR = 'D:\rust\target' }
# GNU toolchain needs binutils (as/dlltool) on PATH for raw-dylib import libs
if (Test-Path 'D:\mingw-tmp\mingw64\bin') { $env:Path = "D:\mingw-tmp\mingw64\bin;$env:Path" }
# binaryen (wasm-opt) — any versioned unpack under D:\rust (see docs/REPLAY3D.md)
$binaryenBin = Get-ChildItem 'D:\rust\binaryen-*\bin' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
if ($binaryenBin) { $env:Path = "$($binaryenBin.FullName);$env:Path" }

if ($env:CARGO_TARGET_DIR) { $target = $env:CARGO_TARGET_DIR } else { $target = Join-Path $repo 'rust\target' }
$pkg    = Join-Path $repo 'web\static\replay3d\pkg'
$assets = Join-Path $repo 'web\static\replay3d\assets'

Set-Location (Join-Path $repo 'rust')

Write-Host '=== 1/6 extract3d (native release) ===' -ForegroundColor Cyan
cargo build --release -p extract3d
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host '=== 2/6 viewer (wasm release) ===' -ForegroundColor Cyan
cargo build --release -p viewer --target wasm32-unknown-unknown
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host '=== 3/6 wasm-bindgen -> web/static/replay3d/pkg ===' -ForegroundColor Cyan
wasm-bindgen --target web --no-typescript --out-dir $pkg --out-name viewer `
    (Join-Path $target 'wasm32-unknown-unknown\release\viewer.wasm')
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host '=== 4/6 wasm-opt -Oz (binaryen) ===' -ForegroundColor Cyan
if (Get-Command wasm-opt -ErrorAction SilentlyContinue) {
    # --all-features: accept everything rustc/wasm-bindgen emitted (bulk-memory,
    # simd128, nontrapping-fptoint per rust/.cargo/config.toml, reference types).
    $wasmFile = Join-Path $pkg 'viewer_bg.wasm'
    wasm-opt -Oz --all-features --strip-debug --strip-producers -o $wasmFile $wasmFile
    if ($LASTEXITCODE -ne 0) { exit 1 }
} else {
    Write-Warning 'wasm-opt not found - shipping UNoptimized wasm (unpack binaryen to D:\rust\binaryen-<ver>)'
}

Write-Host '=== 5/6 radar PNGs -> asset root ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Force $assets | Out-Null
Copy-Item (Join-Path $repo 'src\demoreview\assets\*.png') $assets -Force

# optional dev mirror so rust/viewer/www keeps working standalone (skip the
# .br/.gz siblings - the dev python server serves plain files only)
$devpkg = Join-Path $repo 'rust\viewer\www\pkg'
New-Item -ItemType Directory -Force $devpkg | Out-Null
Copy-Item (Join-Path $pkg '*') $devpkg -Force -Exclude '*.br','*.gz'
# sfx.js (procedural match audio) is a plain page-side module with ONE canonical
# copy under web/static/replay3d/; mirror it next to the dev page too
Copy-Item (Join-Path $repo 'web\static\replay3d\sfx.js') `
    (Join-Path $repo 'rust\viewer\www\sfx.js') -Force

Write-Host '=== 6/6 precompress .br/.gz siblings for compressed serving ===' -ForegroundColor Cyan
python (Join-Path $repo 'tools\precompress.py') `
    (Join-Path $pkg 'viewer_bg.wasm') (Join-Path $pkg 'viewer.js') `
    (Join-Path $repo 'web\static\replay3d\sfx.js') `
    (Join-Path $repo 'web\static\replay3d\assets\maps\*\world.glb') `
    (Join-Path $repo 'web\static\replay3d\assets\models\*.glb')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - compressed serving will fall back to raw files' }

$wasm = Get-Item (Join-Path $pkg 'viewer_bg.wasm')
Write-Host ("done: extract3d at {0}\release\extract3d.exe; viewer_bg.wasm {1:N1} MB" -f $target, ($wasm.Length/1MB)) -ForegroundColor Green
