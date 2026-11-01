# Apply plex-lan.py to the plex volume: stop Plex, edit Preferences.xml, start Plex.
# Windows-only: it works around Docker Desktop's NAT (every client looks remote
# to Plex). Linux hosts do not need it, so there is no bash version.
#
#   .\plex-lan.ps1 -HostIp <host-lan-ip>

[CmdletBinding()]
param([Parameter(Mandatory)][string]$HostIp)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$mh = Join-Path $repo 'mh.ps1'
$script = Join-Path $PSScriptRoot 'plex-lan.py'
$prefs = '/config/Library/Application Support/Plex Media Server/Preferences.xml'

Push-Location $repo
try {
    & $mh stop plex | Out-Null
    docker run --rm -v mediahub_plex-config:/config -v "${script}:/plex-lan.py:ro" python:3.12-slim `
        python /plex-lan.py --prefs "$prefs" --host-ip $HostIp
    if ($LASTEXITCODE -ne 0) { throw 'plex-lan.py failed' }
} finally {
    & $mh start plex | Out-Null
    Pop-Location
}
