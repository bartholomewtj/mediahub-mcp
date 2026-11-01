# Health check from Windows against the compose stack (published on localhost).
# Pings every service, checks the /media mount, then runs the server's deep
# checks (GET /api/checks). Exit 1 on any failure.
#
# Follows stack.env (or the environment): MEDIAHUB_PORT_BIND gives the dashboard address
# (default 127.0.0.1:8000) and MEDIAHUB_PREFIX the container name prefix. MEDIAHUB_URL
# overrides the address.
#
#   .\verify-stack.ps1
#   .\verify-stack.ps1 -AllowLowDisk   # don't fail on the disk-guard check

[CmdletBinding()]
param([switch]$AllowLowDisk, [switch]$Fresh)

$ErrorActionPreference = 'Continue'
$fail = 0

# Settings: the environment first, then <MEDIAHUB_HOME>/stack.env (last KEY=value wins, like mh.ps1).
$mhHome = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
$stackEnv = Join-Path $mhHome 'stack.env'
function Setting($name, $default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v }
    $v = $default
    if (Test-Path $stackEnv) {
        foreach ($line in Get-Content $stackEnv) {
            if ($line -match "^\s*$name\s*=\s*(.*)$") { $v = $Matches[1].Trim().Trim('"', "'").Trim(); if (-not $v) { $v = $default } }
        }
    }
    $v
}
$prefix = Setting 'MEDIAHUB_PREFIX' ''
# The dashboard URL from MEDIAHUB_PORT_BIND ("127.0.0.1:8100", "0.0.0.0:8100" or just "8100").
$base = Setting 'MEDIAHUB_URL' ''
if (-not $base) {
    $bind = Setting 'MEDIAHUB_PORT_BIND' '127.0.0.1:8000'
    $i = $bind.LastIndexOf(':')
    $bindHost = if ($i -gt 0) { $bind.Substring(0, $i) } else { '127.0.0.1' }
    $bindPort = $bind.Substring($i + 1)
    if ($bindHost -in '0.0.0.0', '[::]', '::', '') { $bindHost = '127.0.0.1' }
    $base = "http://${bindHost}:$bindPort"
}
$base = $base.TrimEnd('/')

function Check {
    param([string]$Name, [string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -lt 400) { Write-Host "OK   $Name ($Url)"; return }
    } catch {
        # 401/403 still proves the service is up
        $code = $_.Exception.Response.StatusCode.value__
        if ($code -in 401, 403) { Write-Host "OK   $Name ($Url) [$code]"; return }
    }
    Write-Host "DOWN $Name ($Url)"
    $script:fail = 1
}

# The bundled apps exist only with MEDIAHUB_STACK=full; core mode has just MediaHub (the
# deep checks below cover your own apps).
if ((& (Join-Path (Split-Path $PSScriptRoot -Parent) 'mh.ps1') config --services) -contains 'sonarr') {
    # Media mount visible and writable from a container that uses it.
    $probe = docker exec "${prefix}sonarr" sh -c 'ls /media >/dev/null && touch /media/Downloads/.write-test && rm /media/Downloads/.write-test && echo ok' 2>&1
    if ($probe -match 'ok') { Write-Host 'OK   /media bind mount (read + write)' } else { Write-Host "FAIL /media bind mount: $probe"; $fail = 1 }

    Check sonarr       'http://localhost:8989/ping'
    Check radarr       'http://localhost:7878/ping'
    Check prowlarr     'http://localhost:9696/ping'
    Check qbittorrent  'http://localhost:8090/'
    Check plex         'http://localhost:32400/identity'
    Check flaresolverr 'http://localhost:8191/health'
    Check bazarr       'http://localhost:6767/api/system/ping'
}
Check mediahub     "$base/api/config"

# MCP answers, and refuses requests without the token.
try {
    Invoke-WebRequest "$base/mcp" -Method Post -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop | Out-Null
    Write-Host 'FAIL /mcp accepted a request without a token'; $fail = 1
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 401) { Write-Host 'OK   /mcp requires the token' } else { Write-Host "FAIL /mcp returned $code"; $fail = 1 }
}

# Deep checks from inside the stack (authenticated reads of every service).
try {
    $c = Invoke-RestMethod "$base/api/checks" -TimeoutSec 90
    foreach ($row in $c.checks) {
        $pending = $Fresh -and ($row.check -in 'sonarr indexers', 'radarr indexers', 'prowlarr indexers', 'plex', 'plex claimed' -or $row.check -like 'plex library*')
        $tag = if ($row.ok) { 'OK  ' } elseif ($AllowLowDisk -and $row.check -eq 'media disk free') { 'WARN' } elseif ($pending) { 'PENDING' } else { 'FAIL' }
        Write-Host "$tag $($row.check) $(if ($row.detail) { '- ' + $row.detail })"
        if ($tag -eq 'FAIL') { $fail = 1 }
    }
} catch { Write-Host "FAIL /api/checks: $_"; $fail = 1 }

if ($fail) { Write-Host 'RESULT: FAIL'; exit 1 }
if ($Fresh) { Write-Host 'RESULT: OK (fresh install: PENDING items are for the user: indexers, Plex claim)' } else { Write-Host 'RESULT: all OK' }
exit 0
