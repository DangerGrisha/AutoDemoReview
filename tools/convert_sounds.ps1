<#
.SYNOPSIS
  Build the real CS2 sounds the 3D replay plays.

.DESCRIPTION
  Two stages, listed in tools\sound_events.txt:

    1. Source2Viewer-CLI  -> raw\sounds\  (the sound event definitions and the sound files)
    2. tools\prepare_sounds.py -> web\static\replay3d\assets\sounds_v1\  (Ogg Opus + index.json)

  The results are mirrored next to the dev page. They are gitignored on purpose:
  these are Valve's assets and the repo is public, so every machine that serves
  them builds them from its own CS2 install. A machine that never runs this keeps
  the synthesized gunfire from web\static\replay3d\sfx.js.

  Needs ffmpeg with libopus on PATH. Takes about four minutes.

.EXAMPLE
  .\tools\convert_sounds.ps1
  .\tools\convert_sounds.ps1 -KeepRaw
#>
[CmdletBinding()]
param(
    # pak01_dir.vpk of CS2 (...\game\csgo\pak01_dir.vpk). Auto-detected when omitted.
    [string]$Vpk = $env:CS2_PAK01_VPK,

    # Keep raw\sounds afterwards, to re-run prepare_sounds.py without extracting again.
    [switch]$KeepRaw
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\cs2_paths.ps1"

$repo = Split-Path -Parent $PSScriptRoot
$s2v = Join-Path $repo 'tools\s2v\v20\Source2Viewer-CLI.exe'
$rawDir = Join-Path $repo 'raw\sounds'
$outDir = Join-Path $repo 'web\static\replay3d\assets\sounds_v1'
$devDir = Join-Path $repo 'rust\viewer\www\assets\sounds_v1'

# The project venv is enough: prepare_sounds.py only needs the standard library.
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'ffmpeg not found on PATH.' }

# Always fresh: an old file left here would stay on the site with nothing playing it.
Remove-Item $rawDir, $outDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $rawDir 'events'), $outDir, $devDir | Out-Null

foreach ($file in 'game_sounds_weapons', 'game_sounds_player', 'game_sounds_physics', 'game_sounds_footsteps') {
    & $s2v -i $Vpk -f "soundevents/$file.vsndevts_c" -a | Set-Content -Encoding UTF8 (Join-Path $rawDir "events\$file.txt")
    if ($LASTEXITCODE -ne 0) { throw "Source2Viewer could not dump $file (exit $LASTEXITCODE)" }
}
& $s2v -i $Vpk -f 'sounds/weapons/,sounds/player/,sounds/physics/,sounds/items/,sounds/ui/' -e vsnd_c -d -o (Join-Path $rawDir 'files') | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed extracting sounds (exit $LASTEXITCODE)" }

& $python (Join-Path $repo 'tools\prepare_sounds.py') $rawDir $outDir
if ($LASTEXITCODE -ne 0) { throw "prepare_sounds.py failed (exit $LASTEXITCODE)" }

Remove-Item $devDir -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item $outDir $devDir -Recurse -Force
if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }
