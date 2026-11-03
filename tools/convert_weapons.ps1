<#
.SYNOPSIS
  Build the real CS2 weapon models the 3D replay shows in players' hands.

.DESCRIPTION
  One weapon = three stages, for every line of rust\viewer\src\weapon_models.txt:

    1. Source2Viewer-CLI  -> raw\weapons\<file>\  (the glTF, textures and a text dump of the model)
    2. tools\prepare_weapon.py -> web\static\replay3d\assets\models\cs2_v1\weapons\<file>.glb, and
       its first-person rig in ...\viewmodel\<file>.glb, if tools\convert_viewmodel.ps1 has left
       the clips in raw\viewmodel\clips
    3. tools\precompress.py -> <file>.glb.br / .gz  (what the app actually serves)

  The results are mirrored next to the dev page. They are gitignored on purpose:
  these are Valve's assets and the repo is public, so every machine that serves
  them builds them from its own CS2 install. A machine that never runs this shows
  the simple box weapons from rust\viewer\src\weapons_viz.rs.

  Takes about 6 minutes, and raw\weapons needs about 2 GB while it runs.

.EXAMPLE
  .\tools\convert_weapons.ps1
  .\tools\convert_weapons.ps1 -Only ak47,awp -KeepRaw
#>
[CmdletBinding()]
param(
    # pak01_dir.vpk of CS2 (...\game\csgo\pak01_dir.vpk). Auto-detected when omitted.
    [string]$Vpk = $env:CS2_PAK01_VPK,

    # Build only these output files, by the second column of weapon_models.txt.
    [string[]]$Only,

    # Keep raw\weapons afterwards, to re-run prepare_weapon.py without exporting again.
    [switch]$KeepRaw
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\cs2_paths.ps1"

$repo = Split-Path -Parent $PSScriptRoot
$s2v = Join-Path $repo 'tools\s2v\v20\Source2Viewer-CLI.exe'
$outDir = Join-Path $repo 'web\static\replay3d\assets\models\cs2_v1\weapons'
$devDir = Join-Path $repo 'rust\viewer\www\assets\models\cs2_v1\weapons'
$rigDir = Join-Path $repo 'web\static\replay3d\assets\models\cs2_v1\viewmodel'
$devRigDir = Join-Path $repo 'rust\viewer\www\assets\models\cs2_v1\viewmodel'
$clips = Join-Path $repo 'raw\viewmodel\clips'
$list = Join-Path $repo 'rust\viewer\src\weapon_models.txt'

# The project venv has Pillow, numpy and brotli (requirements-tools.txt).
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}
New-Item -ItemType Directory -Force -Path $outDir, $devDir, $rigDir, $devRigDir | Out-Null
$withRigs = Test-Path $clips
if (-not $withRigs) {
    Write-Warning "No clips in $clips, so no first-person rigs: run tools\convert_viewmodel.ps1 first."
}

foreach ($line in Get-Content $list) {
    if (-not $line -or $line.StartsWith('#')) { continue }
    $tag, $file, $model = $line -split ' '
    if ($Only -and $file -notin $Only) { continue }

    $rawDir = Join-Path $repo "raw\weapons\$file"
    $outGlb = Join-Path $outDir "$file.glb"
    $vmdl = "weapons/models/$model.vmdl_c"
    Write-Host "=== $tag -> $file.glb ===" -ForegroundColor Cyan

    # Always a fresh export: prepare_weapon.py must not build from a stale one.
    Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
    # For a single file -o names the output file without its extension, not a folder. Exporting no
    # animation by name still brings the skeleton, which the first-person rig needs.
    & $s2v -i $Vpk -f $vmdl -d --gltf_export_format gltf --gltf_export_materials `
        --gltf_textures_adapt --gltf_export_animations --gltf_animation_list nothing_at_all `
        -o (Join-Path $rawDir $file) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for $vmdl (exit $LASTEXITCODE)" }
    # The bones and attachments only reach the text dump when no animation is exported.
    & $s2v -i $Vpk -f $vmdl -a | Set-Content -Encoding UTF8 (Join-Path $rawDir 'model.txt')
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer could not dump $vmdl (exit $LASTEXITCODE)" }

    $rigGlb = Join-Path $rigDir "$file.glb"
    $rigArgs = if ($withRigs) { @('--viewmodel', $rigGlb, '--viewmodel-file', $file, '--clips', $clips) } else { @() }
    & $python (Join-Path $repo 'tools\prepare_weapon.py') $rawDir $outGlb @rigArgs
    if ($LASTEXITCODE -ne 0) { throw "prepare_weapon.py failed for $file (exit $LASTEXITCODE)" }

    Copy-Item $outGlb $devDir -Force
    if ($withRigs) { Copy-Item $rigGlb $devRigDir -Force }
    if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }
}

& $python (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir '*.glb') (Join-Path $rigDir '*.glb')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - the app will serve the raw .glb files' }
