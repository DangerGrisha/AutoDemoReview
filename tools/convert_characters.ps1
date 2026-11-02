<#
.SYNOPSIS
  Build the real CS2 player models and what they do (t_rig2, ct_rig2, actions_rig2 .glb).

.DESCRIPTION
  All documented in docs/REPLAY3D.md:

    1. Source2Viewer-CLI  -> raw\character_clips\  (every world clip, once)
    2. Source2Viewer-CLI  -> raw\characters\<model>\  (model, textures and walking clips)
    3. tools\prepare_character.py -> web\static\replay3d\assets\models\cs2_v1\<side>_rig2.glb
    4. tools\prepare_actions.py -> actions_rig2.glb (shared: shots, reloads, deaths)
    5. tools\precompress.py -> *.glb.br / .gz       (what the app actually serves)

  The results are mirrored next to the dev page. They are gitignored on purpose:
  these are Valve's assets and the repo is public, so every machine that serves
  them builds them from its own CS2 install. Takes about a minute in all, and
  raw\character_clips needs about 250 MB while it runs. A
  machine that never runs this keeps the fallback soldier (models/player.glb).

  Needs Source2Viewer 20.0 or newer in tools\s2v\v20\: older versions cannot export
  the animation-graph clips (.vnmclip) that CS2 moved to.

.EXAMPLE
  .\tools\convert_characters.ps1
  .\tools\convert_characters.ps1 -KeepRaw
#>
[CmdletBinding()]
param(
    # pak01_dir.vpk of CS2 (…\game\csgo\pak01_dir.vpk). Auto-detected when omitted.
    [string]$Vpk = $env:CS2_PAK01_VPK,

    # Keep raw\characters (about 60 MB per model of PNGs) and raw\character_clips afterwards.
    [switch]$KeepRaw
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\cs2_paths.ps1"

$repo = Split-Path -Parent $PSScriptRoot
$s2v = Join-Path $repo 'tools\s2v\v20\Source2Viewer-CLI.exe'
$outDir = Join-Path $repo 'web\static\replay3d\assets\models\cs2_v1'
$devDir = Join-Path $repo 'rust\viewer\www\assets\models\cs2_v1'
$clipList = Join-Path $repo 'rust\viewer\src\player_clips.txt'

# The project venv has Pillow, numpy and brotli (requirements-tools.txt).
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

# Base models only: the variants are the agent skins players buy.
$models = @(
    [pscustomobject]@{ Model = 'ctm_sas'; Out = 'ct_rig2.glb' },
    [pscustomobject]@{ Model = 'tm_phoenix'; Out = 't_rig2.glb' }
)

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}

$clips = (Get-Content $clipList | Where-Object { $_ -and -not $_.StartsWith('#') }) -join ','
New-Item -ItemType Directory -Force -Path $outDir, $devDir | Out-Null

# The clips in their own files: the deaths are not in the models' animation graphs, so
# exporting a model does not reach them. -f is one file, --vpk_filepath is a whole folder.
$clipsDir = Join-Path $repo 'raw\character_clips'
Write-Host "=== world clips ===" -ForegroundColor Cyan
Remove-Item $clipsDir -Recurse -Force -ErrorAction SilentlyContinue
& $s2v -i $Vpk --vpk_filepath 'animation/anims/world/' --vpk_extensions vnmclip_c -d `
    --gltf_export_format gltf --gltf_export_animations -o $clipsDir | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed on the clips (exit $LASTEXITCODE)" }

foreach ($m in $models) {
    $rawDir = Join-Path $repo "raw\characters\$($m.Model)"
    $outGlb = Join-Path $outDir $m.Out
    Write-Host "=== $($m.Model) -> $($m.Out) ===" -ForegroundColor Cyan

    # Always a fresh export: prepare_character.py must not build from a stale one.
    Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue
    # For a single file -o names the output file without its extension, not a folder.
    & $s2v -i $Vpk -f "agents/models/$($m.Model)/$($m.Model).vmdl_c" -d `
        --gltf_export_format gltf --gltf_export_materials --gltf_textures_adapt `
        --gltf_export_animations --gltf_animation_list $clips `
        -o (Join-Path $rawDir $m.Model) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for $($m.Model) (exit $LASTEXITCODE)" }

    & $python (Join-Path $repo 'tools\prepare_character.py') $rawDir $outGlb --clips $clipsDir
    if ($LASTEXITCODE -ne 0) { throw "prepare_character.py failed for $($m.Model) (exit $LASTEXITCODE)" }

    Copy-Item $outGlb $devDir -Force
    if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }
}

& $python (Join-Path $repo 'tools\prepare_actions.py') $clipsDir $outDir
if ($LASTEXITCODE -ne 0) { throw "prepare_actions.py failed (exit $LASTEXITCODE)" }
Copy-Item (Join-Path $outDir 'actions_rig2.glb') $devDir -Force
if (-not $KeepRaw) { Remove-Item $clipsDir -Recurse -Force -ErrorAction SilentlyContinue }

# The names from before the gun moved under the chest. Nothing reads them any more.
foreach ($dir in @($outDir, $devDir)) {
    foreach ($stale in @('t.glb*', 'ct.glb*')) {
        Remove-Item (Join-Path $dir $stale) -ErrorAction SilentlyContinue
    }
}

& $python (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir '*.glb')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - the app will serve the raw .glb files' }
