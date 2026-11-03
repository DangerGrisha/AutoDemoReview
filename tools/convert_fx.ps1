<#
.SYNOPSIS
  Build the 3D replay's effects from a local CS2 install: CS2's own particle textures, and
  which parts of each map are solid, for smoke, fire and bullet hits.

.DESCRIPTION
    1. Source2Viewer-CLI -> raw\fx\tex\  (every texture tools\fx_sheets.txt names)
    2. tools\prepare_fx.py -> web\static\replay3d\assets\fx_v1\<name>.ktx2
    3. tools\prepare_space.py -> web\static\replay3d\assets\fx_v1\space\<map>.c2space, one per
       textured map already built by tools\convert_maps.ps1 -Textured (whether or not it shows
       by default: the plain map has the same walls)
    4. tools\precompress.py -> .br / .gz

  The results are mirrored next to the dev page. They are gitignored on purpose: these are
  Valve's assets and the repo is public. A machine that never runs this keeps the simple smoke
  and fire, and bullets that fly the whole tracer length.

.EXAMPLE
  .\tools\convert_fx.ps1
#>
[CmdletBinding()]
param(
    # pak01_dir.vpk of CS2 (...\game\csgo\pak01_dir.vpk). Auto-detected when omitted.
    [string]$Vpk = $env:CS2_PAK01_VPK
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\cs2_paths.ps1"

$repo = Split-Path -Parent $PSScriptRoot
$s2v = Join-Path $repo 'tools\s2v\v20\Source2Viewer-CLI.exe'
$outDir = Join-Path $repo 'web\static\replay3d\assets\fx_v1'
$devDir = Join-Path $repo 'rust\viewer\www\assets\fx_v1'
$mapsDir = Join-Path $repo 'web\static\replay3d\assets\maps_v2'
$list = Join-Path $repo 'tools\fx_sheets.txt'
$raw = Join-Path $repo 'raw\fx\tex'

$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}

# Always a fresh export: frames left from an older game version would mix into the new sheets.
Remove-Item $raw -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $raw, $outDir, $devDir, (Join-Path $outDir 'space'), (Join-Path $devDir 'space') | Out-Null
$textures = Get-Content $list | Where-Object { $_ -and -not $_.StartsWith('#') } |
    ForEach-Object { (($_ -split '\s+')[1]) -split '\+' } | Sort-Object -Unique
foreach ($tex in $textures) {
    Write-Host "=== texture: $tex ===" -ForegroundColor Cyan
    # A sheet comes out as one PNG per frame plus an .mks naming them; a picture as one PNG.
    & $s2v -i $Vpk --vpk_filepath "$tex.vtex_c" -d -o $raw | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for $tex (exit $LASTEXITCODE)" }
}

Write-Host '=== atlases ===' -ForegroundColor Cyan
& $python (Join-Path $repo 'tools\prepare_fx.py') $raw $outDir
if ($LASTEXITCODE -ne 0) { throw "prepare_fx.py failed (exit $LASTEXITCODE)" }
Copy-Item (Join-Path $outDir '*.ktx2') $devDir -Force

foreach ($world in Get-ChildItem $mapsDir -Filter 'world.c2map' -Recurse -ErrorAction SilentlyContinue) {
    $map = $world.Directory.Name
    Write-Host "=== space: $map ===" -ForegroundColor Cyan
    $out = Join-Path $outDir "space\$map.c2space"
    & $python (Join-Path $repo 'tools\prepare_space.py') $world.FullName $out
    if ($LASTEXITCODE -ne 0) { throw "prepare_space.py failed for $map (exit $LASTEXITCODE)" }
    Copy-Item $out (Join-Path $devDir 'space') -Force
}

Remove-Item (Join-Path $repo 'raw\fx') -Recurse -Force -ErrorAction SilentlyContinue
& $python (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir '*.ktx2') (Join-Path $outDir 'space\*.c2space')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - the app will serve the raw files' }
