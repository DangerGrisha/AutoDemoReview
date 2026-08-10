<#
.SYNOPSIS
  Convert CS2 maps into viewer-ready world.glb files (batch, resumable).

.DESCRIPTION
  One map = three stages, all documented in docs/REPLAY3D.md:

    1. Source2Viewer-CLI  -> raw\<map>\      (geometry-only glTF; no materials,
                                              CS2 ships VCS 71 which VRF can't read)
    2. tools\prepare_map.py -> web\static\replay3d\assets\maps\<map>\world.glb
                                              (reorient, drop tool brushes /
                                               overlays / foliage, merge+chunk,
                                               flat surface colours + baked AO)
    3. tools\precompress.py -> world.glb.br / .gz  (what the app actually serves)

  Resumable: a map whose world.glb already exists is skipped unless -Force, and
  an existing raw\<map> export is reused instead of re-exported. Size guard: if
  a finished map exceeds -MaxMB, it is rebuilt once with vertex-clustering
  decimation (-FallbackSimplify units) -- de_ancient needs this (212 -> 52 MB),
  de_mirage does not.

.EXAMPLE
  .\tools\convert_maps.ps1 de_inferno
  .\tools\convert_maps.ps1                      # the whole active pool
  .\tools\convert_maps.ps1 de_nuke -Force -KeepRaw
#>
[CmdletBinding()]
param(
    # Maps to convert (default: the current competitive pool + reserves).
    [Parameter(Position = 0)]
    [string[]]$Maps = @('de_inferno', 'de_dust2', 'de_nuke', 'de_anubis',
                        'de_overpass', 'de_vertigo', 'de_train'),

    # CS2 maps directory (…\game\csgo\maps). Auto-detected when omitted.
    [string]$MapsDir = $env:CS2_MAPS_DIR,

    # Rebuild even when web\…\<map>\world.glb already exists.
    [switch]$Force,

    # Keep raw\<map> (the multi-hundred-MB Source2Viewer export) afterwards.
    [switch]$KeepRaw,

    # Rebuild with decimation when the first result is bigger than this.
    [int]$MaxMB = 80,

    # Vertex-clustering cell size (world units) used by that rebuild.
    [double]$FallbackSimplify = 5.0
)

$ErrorActionPreference = 'Stop'
# `powershell -File script.ps1 -Maps a,b,c` hands the whole list over as ONE
# string (unlike an in-session call, where PowerShell builds the array itself),
# so split on commas here -- otherwise the batch looks for an "a,b,c.vpk".
$Maps = $Maps | ForEach-Object { $_ -split '\s*,\s*' } | Where-Object { $_ }

$repo = Split-Path -Parent $PSScriptRoot
$s2v = Join-Path $repo 'tools\s2v\Source2Viewer-CLI.exe'
$assets = Join-Path $repo 'web\static\replay3d\assets\maps'

function Find-MapsDir {
    $candidates = New-Object System.Collections.Generic.List[string]
    $steamRoots = @(
        "$env:ProgramFiles(x86)\Steam", "$env:ProgramFiles\Steam",
        'C:\Steam', 'D:\Steam', 'D:\games\Steam', 'E:\Steam'
    )
    # Steam's own library index is authoritative when it exists. String joins,
    # not Join-Path: the candidate list names drives that may not exist here,
    # and Join-Path throws DriveNotFound on those.
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
        $p = "$lib\steamapps\common\Counter-Strike Global Offensive\game\csgo\maps"
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    return $null
}

if (-not $MapsDir) { $MapsDir = Find-MapsDir }
if (-not $MapsDir -or -not (Test-Path $MapsDir)) {
    throw "CS2 maps directory not found. Pass -MapsDir '<…\game\csgo\maps>' or set CS2_MAPS_DIR."
}
if (-not (Test-Path $s2v)) {
    throw "Source2Viewer-CLI not found at $s2v -- see docs/REPLAY3D.md (unzip cli-windows-x64.zip into tools\s2v\)."
}

Write-Host "CS2 maps:  $MapsDir"
Write-Host "output:    $assets"
Write-Host "maps:      $($Maps -join ', ')`n"

$results = @()
$runStart = Get-Date

foreach ($map in $Maps) {
    $mapStart = Get-Date
    $vpk = Join-Path $MapsDir "$map.vpk"
    $rawDir = Join-Path $repo "raw\$map"
    $outGlb = Join-Path $assets "$map\world.glb"

    Write-Host "=== $map ===" -ForegroundColor Cyan

    if (-not (Test-Path $vpk)) {
        Write-Warning "$map.vpk not found in $MapsDir -- skipped"
        $results += [pscustomobject]@{ Map = $map; MB = $null; Status = 'no vpk'; Minutes = 0 }
        continue
    }
    if ((Test-Path $outGlb) -and -not $Force) {
        $mb = [math]::Round((Get-Item $outGlb).Length / 1MB, 1)
        Write-Host "  already built ($mb MB) -- skipping (use -Force to rebuild)"
        $results += [pscustomobject]@{ Map = $map; MB = $mb; Status = 'kept'; Minutes = 0 }
        continue
    }

    # -- 1. export (reuse an existing raw export; it is the slow, idempotent part)
    if ((Test-Path $rawDir) -and (Get-ChildItem $rawDir -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Write-Host "  reusing export $rawDir"
    } else {
        Write-Host "  exporting geometry (several minutes, hundreds of MB)..."
        & $s2v -i $vpk -e 'vwrld_c' -d --gltf_export_format gltf -o $rawDir | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Source2Viewer failed for $map (exit $LASTEXITCODE) -- skipped"
            $results += [pscustomobject]@{ Map = $map; MB = $null; Status = "s2v exit $LASTEXITCODE"; Minutes = 0 }
            continue
        }
    }

    # -- 2. prepare (+ one decimated retry when the result is too heavy to ship)
    $prepArgs = @((Join-Path $repo 'tools\prepare_map.py'), $rawDir, $map,
                  '--glb', '--slim-attrs', '--surface-colors', '--drop-foliage')
    Write-Host "  preparing..."
    & python @prepArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "prepare_map.py failed for $map (exit $LASTEXITCODE) -- skipped"
        $results += [pscustomobject]@{ Map = $map; MB = $null; Status = "prepare exit $LASTEXITCODE"; Minutes = 0 }
        continue
    }

    $status = 'built'
    $mb = [math]::Round((Get-Item $outGlb).Length / 1MB, 1)
    if ($mb -gt $MaxMB) {
        Write-Host "  $mb MB > $MaxMB MB -- rebuilding with --simplify $FallbackSimplify"
        & python @prepArgs --simplify $FallbackSimplify
        if ($LASTEXITCODE -eq 0) {
            $mb = [math]::Round((Get-Item $outGlb).Length / 1MB, 1)
            $status = "built (simplify $FallbackSimplify)"
        } else {
            Write-Warning "decimated rebuild failed for $map; keeping the full-size map"
        }
    }

    # -- 3. compressed siblings (minutes on a 50 MB map -- never interrupt)
    Write-Host "  compressing..."
    & python (Join-Path $repo 'tools\precompress.py') $outGlb

    if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }

    $mins = [math]::Round(((Get-Date) - $mapStart).TotalMinutes, 1)
    Write-Host "  done: $mb MB in $mins min`n" -ForegroundColor Green
    $results += [pscustomobject]@{ Map = $map; MB = $mb; Status = $status; Minutes = $mins }
}

Write-Host "`n==== summary ($([math]::Round(((Get-Date) - $runStart).TotalMinutes, 1)) min total) ===="
$results | Format-Table -AutoSize
