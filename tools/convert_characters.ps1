<#
.SYNOPSIS
  Build the real CS2 player models the 3D replay shows (ct.glb, t.glb).

.DESCRIPTION
  One model = three stages, all documented in docs/REPLAY3D.md:

    1. Source2Viewer-CLI  -> raw\characters\<model>\  (model, textures and clips)
    2. tools\prepare_character.py -> web\static\replay3d\assets\models\cs2_v1\<side>.glb
    3. tools\precompress.py -> <side>.glb.br / .gz    (what the app actually serves)

  The results are mirrored next to the dev page. They are gitignored on purpose:
  these are Valve's assets and the repo is public, so every machine that serves
  them builds them from its own CS2 install. Takes about 30 seconds in all. A
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

    # Keep raw\characters (about 60 MB per model of PNGs) afterwards.
    [switch]$KeepRaw
)

$ErrorActionPreference = 'Stop'

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
    [pscustomobject]@{ Model = 'ctm_sas'; Side = 'ct' },
    [pscustomobject]@{ Model = 'tm_phoenix'; Side = 't' }
)

function Find-Pak01 {
    $candidates = New-Object System.Collections.Generic.List[string]
    $steamRoots = @(
        "$env:ProgramFiles(x86)\Steam", "$env:ProgramFiles\Steam",
        'C:\Steam', 'D:\Steam', 'D:\games\Steam', 'E:\Steam'
    )
    # Same search as convert_maps.ps1: Steam's library index first, string joins
    # because the candidate list names drives that may not exist here.
    foreach ($root in $steamRoots) {
        $vdf = "$root\steamapps\libraryfolders.vdf"
        if (Test-Path $vdf) {
            foreach ($m in [regex]::Matches((Get-Content $vdf -Raw), '"path"\s+"([^"]+)"')) {
                $candidates.Add(($m.Groups[1].Value -replace '\\\\', '\'))
            }
        }
        $candidates.Add($root)
    }
    foreach ($lib in $candidates) {
        $p = "$lib\steamapps\common\Counter-Strike Global Offensive\game\csgo\pak01_dir.vpk"
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    return $null
}

if (-not $Vpk) { $Vpk = Find-Pak01 }
if (-not $Vpk -or -not (Test-Path $Vpk)) {
    throw "CS2 pak01_dir.vpk not found. Pass -Vpk '<…\game\csgo\pak01_dir.vpk>' or set CS2_PAK01_VPK."
}
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}

$clips = (Get-Content $clipList | Where-Object { $_ -and -not $_.StartsWith('#') }) -join ','
New-Item -ItemType Directory -Force -Path $outDir, $devDir | Out-Null

foreach ($m in $models) {
    $rawDir = Join-Path $repo "raw\characters\$($m.Model)"
    $outGlb = Join-Path $outDir "$($m.Side).glb"
    Write-Host "=== $($m.Model) -> $($m.Side).glb ===" -ForegroundColor Cyan

    # Always a fresh export: prepare_character.py must not build from a stale one.
    Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue
    # For a single file -o names the output file without its extension, not a folder.
    & $s2v -i $Vpk -f "agents/models/$($m.Model)/$($m.Model).vmdl_c" -d `
        --gltf_export_format gltf --gltf_export_materials --gltf_textures_adapt `
        --gltf_export_animations --gltf_animation_list $clips `
        -o (Join-Path $rawDir $m.Model) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed for $($m.Model) (exit $LASTEXITCODE)" }

    & $python (Join-Path $repo 'tools\prepare_character.py') $rawDir $outGlb
    if ($LASTEXITCODE -ne 0) { throw "prepare_character.py failed for $($m.Model) (exit $LASTEXITCODE)" }

    Copy-Item $outGlb $devDir -Force
    if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }
}

& $python (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir '*.glb')
if ($LASTEXITCODE -ne 0) { Write-Warning 'precompress failed - the app will serve the raw .glb files' }
