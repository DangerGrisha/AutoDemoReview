<#
.SYNOPSIS
  Build the first-person arms and export the clips each weapon's first-person rig needs.

.DESCRIPTION
  Run this first and tools\convert_weapons.ps1 after it, which builds a rig per weapon from the
  clips this leaves in raw\viewmodel\clips.

    1. Source2Viewer-CLI -> raw\viewmodel\clips\  (every folder tools\viewmodel_clips.txt names)
    2. Source2Viewer-CLI -> raw\viewmodel\<agent>\  (the model each side's arms come from)
    3. tools\prepare_viewmodel.py -> web\static\replay3d\assets\models\cs2_v1\viewmodel\arms_*.glb
    4. tools\precompress.py -> .glb.br / .gz

  The results are mirrored next to the dev page. They are gitignored on purpose: these are Valve's
  assets and the repo is public. A machine that never runs this keeps the still weapon in first
  person.

.EXAMPLE
  .\tools\convert_viewmodel.ps1
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
$outDir = Join-Path $repo 'web\static\replay3d\assets\models\cs2_v1\viewmodel'
$devDir = Join-Path $repo 'rust\viewer\www\assets\models\cs2_v1\viewmodel'
$list = Join-Path $repo 'tools\viewmodel_clips.txt'
$rawRoot = Join-Path $repo 'raw\viewmodel'
$clips = Join-Path $rawRoot 'clips'

$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}
New-Item -ItemType Directory -Force -Path $outDir, $devDir | Out-Null

# Always a fresh export: a clip left from an older game version would not match the new models.
Remove-Item $clips -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $clips | Out-Null
$folders = Get-Content $list | Where-Object { $_ -and -not $_.StartsWith('#') } |
    ForEach-Object { ($_ -split ' ')[1] } | Sort-Object -Unique
foreach ($folder in $folders) {
    Write-Host "=== clips: $folder ===" -ForegroundColor Cyan
    & $s2v -i $Vpk --vpk_filepath "animation/anims/viewmodel/$folder/" --vpk_extensions vnmclip_c -d `
        --gltf_export_format gltf --gltf_export_animations -o $clips | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for the clips in $folder (exit $LASTEXITCODE)" }
}

foreach ($side in @(@('ctm_sas', 'arms_ct'), @('tm_phoenix', 'arms_t'))) {
    $agent, $name = $side
    Write-Host "=== arms: $agent -> $name.glb ===" -ForegroundColor Cyan
    $rawDir = Join-Path $rawRoot $agent
    Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
    # For a single file -o names the output file without its extension, not a folder. Exporting no
    # animation by name still brings the skeleton, which the arms' skin needs.
    & $s2v -i $Vpk -f "agents/models/$agent/$agent.vmdl_c" -d --gltf_export_format gltf --gltf_export_materials `
        --gltf_textures_adapt --gltf_export_animations --gltf_animation_list nothing_at_all `
        -o (Join-Path $rawDir $agent) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for $agent (exit $LASTEXITCODE)" }

    $outGlb = Join-Path $outDir "$name.glb"
    & $python (Join-Path $repo 'tools\prepare_viewmodel.py') $rawDir $clips $outGlb
    if ($LASTEXITCODE -ne 0) { throw "prepare_viewmodel.py failed for $agent (exit $LASTEXITCODE)" }
    Copy-Item $outGlb $devDir -Force
    Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue
}

& $python (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir 'arms_*.glb')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - the app will serve the raw .glb files' }
Write-Host "Now run tools\convert_weapons.ps1, which builds each weapon's rig from $clips."
