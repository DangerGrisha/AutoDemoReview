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

  -Textured builds the real-looking map instead: Source2Viewer 20 with materials,
  lightmaps and the sky -> tools\prepare_map_textured.py -> assets\maps_v2\<map>\.
  These files come from CS2 and are gitignored, so never commit them. The site
  uses them when they exist and keeps the plain map as its fallback, so the plain
  maps stay needed too. See docs/REPLAY3D.md.

.EXAMPLE
  .\tools\convert_maps.ps1 de_inferno
  .\tools\convert_maps.ps1                      # the whole active pool
  .\tools\convert_maps.ps1 de_nuke -Force -KeepRaw
  .\tools\convert_maps.ps1 -Textured            # all nine maps, textured
  .\tools\convert_maps.ps1 de_mirage -Textured -Force
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
    [double]$FallbackSimplify = 5.0,

    # Build the textured map into assets\maps_v2 instead of the plain one.
    [switch]$Textured,

    # Largest texture side for -Textured. Smaller saves download at the cost of detail.
    [int]$MaxTexture = 2048
)

$ErrorActionPreference = 'Stop'
# `powershell -File script.ps1 -Maps a,b,c` hands the whole list over as ONE
# string (unlike an in-session call, where PowerShell builds the array itself),
# so split on commas here -- otherwise the batch looks for an "a,b,c.vpk".
$Maps = $Maps | ForEach-Object { $_ -split '\s*,\s*' } | Where-Object { $_ }

$repo = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot\cs2_paths.ps1"
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

function Export-Textured {
    # Everything prepare_map_textured.py reads, into one folder. Throws on a failed step.
    param([string]$Map, [string]$Vpk, [string]$Out)
    $s2v20 = Join-Path $repo 'tools\s2v\v20\Source2Viewer-CLI.exe'
    if (-not (Test-Path $s2v20)) {
        throw "Source2Viewer 20 not found at $s2v20 -- 19.x cannot read CS2's materials, see docs/REPLAY3D.md."
    }
    & $s2v20 -i $Vpk -e 'vwrld_c' -d --gltf_export_format gltf --gltf_export_materials --gltf_export_extras -o $Out | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "world export failed (exit $LASTEXITCODE)" }

    # With one file picked by -f, -o names the file itself and not a folder.
    & $s2v20 -i $Vpk -f "maps/$Map/lightmaps/irradiance.vtex_c" -d -o (Join-Path $Out 'irradiance.exr') | Out-Null
    & $s2v20 -i $Vpk -f "maps/$Map/lightmaps/direct_light_shadows.vtex_c" -d -o (Join-Path $Out 'direct_light_shadows.png') | Out-Null
    foreach ($f in 'irradiance.exr', 'direct_light_shadows.png') {
        if (-not (Test-Path (Join-Path $Out $f))) { throw "$f did not export: does this map have baked light?" }
    }

    & $s2v20 -i $Vpk -f "maps/$Map/world.vwrld_c" -a 2>$null | Out-File (Join-Path $Out 'world.txt') -Encoding utf8

    # The sky is named by the map's env_sky and its material, and lives in the game's main archive.
    $ents = (& $s2v20 -i $Vpk -f "maps/$Map/entities/default_ents.vents_c" -a 2>$null) -join "`n"
    $sky = [regex]::Match($ents, 'skyname\s*=\s*resource_name:"([^"]+\.vmat)"').Groups[1].Value
    if ($sky) {
        $pak = Resolve-Pak01
        $vmat = (& $s2v20 -i $pak -f ($sky + '_c') -a 2>$null) -join "`n"
        $tex = [regex]::Match($vmat, 'resource:"([^"]+\.vtex)"').Groups[1].Value
        if ($tex) {
            & $s2v20 -i $pak -f ($tex + '_c') -d -o (Join-Path $Out 'sky.exr') | Out-Null
        }
    }
    if (-not (Test-Path (Join-Path $Out 'sky.exr'))) { Write-Warning "$Map`: no sky texture, the viewer will show its plain background" }

    # Exposure is in the entities, the tone curve in the post-processing file they name. A
    # map keeps that file in its own archive or in the game's. Only its top is needed: most
    # of it is the colour grade, 400 KB of hex.
    $ents | Out-File (Join-Path $Out 'ents.txt') -Encoding utf8
    $post = [regex]::Match($ents, 'postprocessing\s*=\s*resource_name:"([^"]+\.vpost)"').Groups[1].Value
    if ($post) {
        foreach ($archive in $Vpk, (Resolve-Pak01)) {
            $lines = @(& $s2v20 -i $archive -f ($post + '_c') -a 2>$null)
            if ($lines -match 'm_flWhitePoint') {
                $lines[0..119] | Out-File (Join-Path $Out 'post.txt') -Encoding utf8
                break
            }
        }
    }
}

if ($Textured) {
    # the plain batch's default list lacks de_mirage and de_ancient, which were converted first
    if (-not $PSBoundParameters.ContainsKey('Maps')) {
        $Maps = @('de_mirage', 'de_inferno', 'de_dust2', 'de_nuke', 'de_anubis',
                  'de_overpass', 'de_vertigo', 'de_train', 'de_ancient')
    }
    $assetsV2 = Join-Path $repo 'web\static\replay3d\assets\maps_v2'
    $py = Join-Path $repo '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) { $py = 'python' }
    $results = @()
    foreach ($map in $Maps) {
        $start = Get-Date
        Write-Host "=== $map (textured) ===" -ForegroundColor Cyan
        $vpk = Join-Path $MapsDir "$map.vpk"
        $rawDir = Join-Path $repo "raw\${map}_v2"
        $outDir = Join-Path $assetsV2 $map
        if (-not (Test-Path $vpk)) {
            Write-Warning "$map.vpk not found in $MapsDir -- skipped"
            $results += [pscustomobject]@{ Map = $map; MB = $null; Status = 'no vpk'; Minutes = 0 }
            continue
        }
        if ((Test-Path (Join-Path $outDir 'world.c2map')) -and -not $Force) {
            Write-Host "  already built -- skipping (use -Force to rebuild)"
            $results += [pscustomobject]@{ Map = $map; MB = $null; Status = 'kept'; Minutes = 0 }
            continue
        }
        try {
            if (Test-Path $rawDir) { Remove-Item $rawDir -Recurse -Force }
            Export-Textured -Map $map -Vpk $vpk -Out $rawDir
            & $py -u (Join-Path $repo 'tools\prepare_map_textured.py') $rawDir $map --max-size $MaxTexture
            if ($LASTEXITCODE -ne 0) { throw "prepare_map_textured.py failed (exit $LASTEXITCODE)" }
            # a glob: 350 textures named one by one would not fit on a Windows command line
            & $py (Join-Path $repo 'tools\precompress.py') (Join-Path $outDir 'world.c2map') (Join-Path $outDir 'lightmap.ktx2') (Join-Path $outDir 'sky.ktx2') (Join-Path $outDir 'tex\*.ktx2') | Out-Null
            if (-not $KeepRaw) { Remove-Item $rawDir -Recurse -Force -ErrorAction SilentlyContinue }
            $wire = (Get-ChildItem $outDir -Recurse -File -Filter '*.br' | Measure-Object Length -Sum).Sum
            $mb = [math]::Round($wire / 1MB, 1)
            $mins = [math]::Round(((Get-Date) - $start).TotalMinutes, 1)
            Write-Host "  done: $mb MB on the wire in $mins min`n" -ForegroundColor Green
            $results += [pscustomobject]@{ Map = $map; MB = $mb; Status = 'built'; Minutes = $mins }
        } catch {
            Write-Warning "$map failed: $_"
            $results += [pscustomobject]@{ Map = $map; MB = $null; Status = 'failed'; Minutes = 0 }
        }
    }
    $results | Format-Table -AutoSize
    return
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
