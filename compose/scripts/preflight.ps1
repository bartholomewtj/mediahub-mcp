# preflight.ps1 [-MediaPath <folder>]: collect the facts INSTALL.md Phase 0/1 needs. Changes nothing.
#
# Prints the same labelled sections as preflight.sh (`== name ==`); the agent reads them and decides.
# Exit 0 always. Needs nothing installed: a missing tool is a line saying so, never a crash.
#
#   .\compose\scripts\preflight.ps1
#   .\compose\scripts\preflight.ps1 D:/Media
param([Parameter(Position = 0)][string]$MediaPath = '')
$ErrorActionPreference = 'Continue'
$MinCompose = [version]'2.24.4'
$Ports = 8000, 8989, 7878, 9696, 8090, 6767, 8191, 32400
$Apps = 'sonarr|radarr|prowlarr|plex|qbittorrent|bazarr|jellyfin|jellyseerr|overseerr|transmission|deluge|sabnzbd|nzbget'
$onWindows = [Environment]::OSVersion.Platform -eq 'Win32NT'
$docker = [bool](Get-Command docker -ErrorAction SilentlyContinue)
$dockerUp = $false

Write-Output '== OS =='
Write-Output "os: $([Environment]::OSVersion.VersionString)"
Write-Output "powershell: $($PSVersionTable.PSVersion)"
if ($onWindows) {
    $policy = Get-ExecutionPolicy
    Write-Output "execution policy: $policy"
    if ($policy -in 'Restricted', 'AllSigned') { Write-Output 'NOTE the repo .ps1 scripts will not run: ask the user (Set-ExecutionPolicy -Scope CurrentUser RemoteSigned)' }
    Write-Output 'supported: yes'
} else { Write-Output 'supported: not on Windows: use preflight.sh' }

Write-Output '== tools =='
if (Get-Command git -ErrorAction SilentlyContinue) { Write-Output "git: $(git --version)" } else { Write-Output 'git: absent' }

Write-Output '== docker =='
if ($docker) {
    Write-Output "docker: $(docker --version 2>&1)"
    $cv = "$(docker compose version --short 2>$null)".Trim() -replace '^v', '' -replace '-.*$', ''
    $parsed = $null
    if ($cv -and [version]::TryParse($cv, [ref]$parsed)) {
        if ($parsed -ge $MinCompose) { Write-Output "compose: $cv (minimum $MinCompose`: ok)" }
        else { Write-Output "compose: $cv (minimum $MinCompose`: TOO OLD, ask the user to upgrade)" }
    } else { Write-Output "compose: absent (the docker compose plugin is missing; minimum $MinCompose)" }
} else { Write-Output 'docker: absent'; Write-Output 'compose: absent' }

Write-Output '== docker running =='
if ($docker) {
    $info = "$(docker info --format '{{.ServerVersion}} {{.OSType}}' 2>&1)".Trim()
    if ($LASTEXITCODE -eq 0 -and $info) {
        $dockerUp = $true
        Write-Output "running: yes (server and OS type: $info)"
        if ($info -notmatch ' linux$') { Write-Output 'NOTE the OS type must be linux (Docker Desktop: switch to Linux containers)' }
    } else { Write-Output "running: no ($(($info -split "`n")[0]))" }
} else { Write-Output 'running: no (docker is absent)' }

Write-Output '== disk =='
try {
    $root = if ($MediaPath) { $MediaPath } else { (Get-Location).Path }
    $letter = if ($root -match '^([A-Za-z]):') { $Matches[1] } else { $null }
    $drive = if ($letter) { Get-PSDrive -Name $letter -PSProvider FileSystem -ErrorAction Stop } else { Get-PSDrive -PSProvider FileSystem | Select-Object -First 1 }
    Write-Output "path: $root"
    Write-Output ('drive {0}: free {1} GiB of {2} GiB' -f $drive.Name, [math]::Round($drive.Free / 1GB), [math]::Round(($drive.Free + $drive.Used) / 1GB))
} catch { Write-Output "could not read the disk ($($_.Exception.Message))" }

Write-Output '== ports =='
$listen = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue
if ($null -eq $listen -and -not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) {
    Write-Output 'could not check ports (no Get-NetTCPConnection)'
} else {
    $busy = @($listen | Where-Object { $_.LocalPort -in $Ports })
    if ($busy.Count) {
        $busy | ForEach-Object { Write-Output ('in use: {0}:{1} {2}' -f $_.LocalAddress, $_.LocalPort, (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName) }
    } else { Write-Output 'ports free' }
}

Write-Output '== existing stack =='
Write-Output '-- containers (any state, matched by name or image)'
if ($dockerUp) {
    $c = @(docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>&1 | Select-String -Pattern $Apps)
    if ($c.Count) { $c | ForEach-Object { $_.Line } } else { Write-Output '(none found)' }
} else { Write-Output '(docker not usable: this check did not run)' }
Write-Output '-- services'
$s = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match $Apps -or $_.DisplayName -match $Apps })
if ($s.Count) { $s | ForEach-Object { '{0} {1} {2}' -f $_.Name, $_.Status, $_.StartType } } else { Write-Output '(none found)' }
Write-Output '-- processes'
$p = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $Apps })
if ($p.Count) { $p | ForEach-Object { '{0} {1}' -f $_.Id, $_.ProcessName } } else { Write-Output '(none found)' }

Write-Output '== mediahub =='
$mhHome = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
if (Test-Path -LiteralPath (Join-Path $mhHome 'stack.env')) { Write-Output "settings file: present ($(Join-Path $mhHome 'stack.env'))" } else { Write-Output 'settings file: none' }
if ($dockerUp) {
    $cn = "$(docker ps -a --filter label=com.docker.compose.project=mediahub --format '{{.Names}}' 2>$null)".Trim() -replace '\s+', ' '
    $vn = "$(docker volume ls -q --filter name=mediahub_ 2>$null)".Trim() -replace '\s+', ' '
    Write-Output "containers: $(if ($cn) { $cn } else { 'none' })"
    Write-Output "volumes: $(if ($vn) { $vn } else { 'none' })"
} else { Write-Output 'containers: unknown (docker not usable)'; Write-Output 'volumes: unknown (docker not usable)' }

Write-Output '== timezone =='
$iana = $null
try {
    $tzId = (Get-TimeZone).Id
    if ([TimeZoneInfo]::TryConvertWindowsIdToIanaId($tzId, [ref]$iana) -and $iana) { Write-Output "timezone: $iana" }
    elseif ($tzId -match '/') { Write-Output "timezone: $tzId" }
    else { Write-Output 'timezone: unknown'; Write-Output "NOTE the Windows zone is '$tzId': ask the user for the IANA name (for example Europe/London)" }
} catch { Write-Output 'timezone: unknown'; Write-Output 'NOTE ask the user for the IANA name (suggest Etc/UTC)' }

Write-Output '== uid/gid =='
Write-Output 'uid/gid: not used on Windows (PUID and PGID stay 1000)'

Write-Output '== claude cli =='
$cl = Get-Command claude -ErrorAction SilentlyContinue
if ($cl) { Write-Output "claude: present ($($cl.Source))" } else { Write-Output 'claude: absent' }
exit 0
