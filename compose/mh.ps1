# Run docker compose for this stack with the per-install settings.
#   .\mh.ps1 up -d      .\mh.ps1 ps      .\mh.ps1 logs -f mediahub
# MEDIAHUB_HOME defaults to ~/.mediahub and holds stack.env (settings) and secrets.env.
# stack.env picks the files: MEDIAHUB_STACK=full|core, MEDIAHUB_VPN=none|gluetun, PLEX_CLAIM.
$ErrorActionPreference = 'Stop'
if (-not $env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME = Join-Path $HOME '.mediahub' }
$stackEnv = Join-Path $env:MEDIAHUB_HOME 'stack.env'
if (-not (Test-Path $stackEnv)) {
    Write-Error "missing $stackEnv - copy compose/stack.env.example there and edit it (or set MEDIAHUB_HOME)"
    exit 1
}
# Last KEY=value wins; comments and blank lines are ignored.
function Setting($name, $default) {
    $v = $default
    foreach ($line in Get-Content $stackEnv) {
        if ($line -match "^\s*$name\s*=\s*(.*)$") { $v = $Matches[1].Trim().Trim('"', "'").Trim(); if (-not $v) { $v = $default } }
    }
    $v
}
$stack = Setting 'MEDIAHUB_STACK' 'full'
$vpn = Setting 'MEDIAHUB_VPN' 'none'
if ($stack -notin 'full', 'core') { Write-Error "MEDIAHUB_STACK=$stack in stack.env: use full or core"; exit 1 }
if ($vpn -notin 'none', 'gluetun') { Write-Error "MEDIAHUB_VPN=$vpn in stack.env: use none or gluetun"; exit 1 }
if ($vpn -eq 'gluetun' -and $stack -ne 'full') { Write-Error 'MEDIAHUB_VPN=gluetun needs MEDIAHUB_STACK=full (it routes the bundled qBittorrent)'; exit 1 }
$files = @('docker-compose.yml')
if ($stack -eq 'full') { $files += 'stack.yml' }
if ($vpn -eq 'gluetun') { $files += 'vpn.yml' }
# A one-time Plex claim code adds plex-claim.yml; without it the plex service is unchanged.
if ($stack -eq 'full' -and (Setting 'PLEX_CLAIM' '')) { $files += 'plex-claim.yml' }
$f = $files | ForEach-Object { '-f'; Join-Path $PSScriptRoot $_ }
docker compose --env-file $stackEnv @f @args
exit $LASTEXITCODE
