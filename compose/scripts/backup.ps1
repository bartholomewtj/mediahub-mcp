# Tar every named volume into <MEDIAHUB_HOME>\backups\<stamp>\ (default ~/.mediahub).
# Stops the stack first so the SQLite files are consistent, then starts it
# again. Keeps the newest -Keep backups. The weekly scheduled task
# "MediaHub backup" runs this.
#
#   .\backup.ps1            # stop, back up, start
#   .\backup.ps1 -NoStop    # hot copy (WAL files included; fine for a quick safety net)

[CmdletBinding()]
param([switch]$NoStop, [int]$Keep = 4)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$mhHome = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
$mh = Join-Path $repo 'mh.ps1'
$root = Join-Path $mhHome 'backups'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dest = Join-Path $root $stamp
New-Item -ItemType Directory -Path $dest -Force | Out-Null

Push-Location $repo
try {
    if (-not $NoStop) { & $mh stop | Out-Null }
    $vols = docker volume ls --format '{{.Name}}' | Where-Object { $_ -like 'mediahub_*' }
    foreach ($v in $vols) {
        docker run --rm -v "${v}:/src:ro" -v "${dest}:/dst" alpine:3.20 `
            tar -czf "/dst/$v.tgz" -C /src .
        if ($LASTEXITCODE -ne 0) { throw "backup of $v failed" }
        Write-Host ("{0,-32} {1,8:N1} MB" -f $v, ((Get-Item (Join-Path $dest "$v.tgz")).Length / 1MB))
    }
} finally {
    if (-not $NoStop) { & $mh start | Out-Null }
    Pop-Location
}
Write-Host "Backup in $dest"

Get-ChildItem $root -Directory |
    Where-Object { $_.Name -match '^\d{8}-\d{6}$' } |
    Sort-Object Name -Descending |
    Select-Object -Skip $Keep |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force; Write-Host "pruned $($_.Name)" }
