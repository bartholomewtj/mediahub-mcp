# Copy app-generated API keys into <MEDIAHUB_HOME>\secrets.env (default ~/.mediahub), then
# recreate the dashboard container so it uses them. Run after a fresh app volume (Bazarr,
# Sonarr, ...). A wrapper for `mh run --rm setup secrets` (app/setup.py), which prints key
# names only. It also fills an empty MCP_TOKEN, and an empty QBIT_PASS if qBittorrent is
# stopped (see README, First run). Same as sync-runtime-secrets.sh.
#
#   .\sync-runtime-secrets.ps1

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$mhHome = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
$secrets = Join-Path $mhHome 'secrets.env'
$mh = Join-Path $repo 'mh.ps1'
if (-not (Test-Path $secrets)) { throw "missing $secrets (copy compose\secrets.env.example there first)" }

# MEDIAHUB_STACK=core has no bundled apps and no setup service.
if (-not ((& $mh config --services 2>$null) -contains 'sonarr')) {
    Write-Host 'No bundled apps (MEDIAHUB_STACK=core?): nothing to sync.'
    exit 0
}

& $mh run --rm setup secrets
$rc = $LASTEXITCODE
& $mh up -d mediahub | Out-Null
Write-Host 'Dashboard container recreated with the new keys.'
exit $rc
