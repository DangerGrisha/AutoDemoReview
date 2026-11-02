# Dot-source this from a conversion script: . "$PSScriptRoot\cs2_paths.ps1"

function Find-Pak01 {
    <#
    .SYNOPSIS
      Path of CS2's pak01_dir.vpk from Steam's library index or the usual folders, or $null.
    #>
    $candidates = New-Object System.Collections.Generic.List[string]
    $steamRoots = @(
        "$env:ProgramFiles(x86)\Steam", "$env:ProgramFiles\Steam",
        'C:\Steam', 'D:\Steam', 'D:\games\Steam', 'E:\Steam'
    )
    # String joins rather than Join-Path, which throws on a drive that does not exist.
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

function Resolve-Pak01 {
    <#
    .SYNOPSIS
      The given pak01_dir.vpk, or the one found on this machine. Stops the script if there is none.
    #>
    param([string]$Vpk)
    if (-not $Vpk) { $Vpk = Find-Pak01 }
    if (-not $Vpk -or -not (Test-Path $Vpk)) {
        throw "CS2 pak01_dir.vpk not found. Pass -Vpk '<...\game\csgo\pak01_dir.vpk>' or set CS2_PAK01_VPK."
    }
    return $Vpk
}
