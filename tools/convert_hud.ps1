<#
.SYNOPSIS
  Build the CS2 weapon icons of the 3D replay's kill feed and player list.

.DESCRIPTION
  Source2Viewer-CLI turns the icons listed in tools\hud_icons.txt back into SVG, and they
  go to web\static\replay3d\assets\hud_v1\, mirrored next to the dev page. They are
  gitignored on purpose: these are Valve's assets and the repo is public, so every machine
  that serves them builds them from its own CS2 install. A machine that never runs this
  keeps the text labels.

.EXAMPLE
  .\tools\convert_hud.ps1
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
$rawDir = Join-Path $repo 'raw\hud'
$outDir = Join-Path $repo 'web\static\replay3d\assets\hud_v1'
$devDir = Join-Path $repo 'rust\viewer\www\assets\hud_v1'

$Vpk = Resolve-Pak01 $Vpk
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI 20+ not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\v20\)."
}

# Always fresh: an old file left here would stay on the site with nothing asking for it.
Remove-Item $rawDir, $outDir, $devDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $rawDir, $outDir | Out-Null

& $s2v -i $Vpk -f 'panorama/images/icons/equipment/' -e vsvg_c -d -o $rawDir | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Source2Viewer failed extracting the icons (exit $LASTEXITCODE)" }

$icons = Join-Path $rawDir 'panorama\images\icons\equipment'
$missing = @()
$count = 0
foreach ($line in Get-Content (Join-Path $PSScriptRoot 'hud_icons.txt')) {
    if (-not $line.Trim() -or $line.StartsWith('#')) { continue }
    $name, $icon = $line.Trim() -split '\s+'
    $source = Join-Path $icons "$icon.svg"
    if (Test-Path $source) {
        Copy-Item $source (Join-Path $outDir "$name.svg")
        $count++
    } else {
        $missing += $icon
    }
}
Copy-Item $outDir $devDir -Recurse -Force
Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "$count icons in $outDir"
if ($missing) { Write-Warning "not in this CS2 build, these stay text: $($missing -join ', ')" }
