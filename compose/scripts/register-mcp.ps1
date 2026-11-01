# Register the MediaHub MCP server with an MCP client. The token is read from
# <MEDIAHUB_HOME>/secrets.env (default ~/.mediahub) into a variable and goes only into the
# client's own config. It is never printed.
#
#   .\register-mcp.ps1 claude              Claude Code, user scope (replaces an existing 'mediahub')
#   .\register-mcp.ps1 codex               Codex CLI: [mcp_servers.mediahub] in ~/.codex/config.toml (or
#                                          $env:CODEX_HOME) + the user env var MEDIAHUB_MCP_TOKEN
#   .\register-mcp.ps1 json                writes <MEDIAHUB_HOME>/mcp-client.json for any other client;
#                                          prints only its path
#   Options: -Url http://127.0.0.1:8000/mcp (also --url ...), -NoPersistEnv (codex: do not set the
#   user env var; you set MEDIAHUB_MCP_TOKEN yourself)
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('claude', 'codex', 'json')][string]$Client,
    [string]$Url = 'http://127.0.0.1:8000/mcp',
    [switch]$NoPersistEnv,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = 'Stop'

for ($i = 0; $i -lt $Rest.Count; $i++) {
    switch -Regex ($Rest[$i]) {
        '^--url$' { $i++; if ($i -ge $Rest.Count) { throw '--url needs a value' }; $Url = $Rest[$i] }
        '^--url=(.+)$' { $Url = $Matches[1] }
        '^--no-persist-env$' { $NoPersistEnv = $true }
        default { throw "unknown option $($Rest[$i])" }
    }
}
if ($Url -notmatch '^https?://[^\s"\\]+$') { throw "-Url must be an http(s) URL, got a value that is not" }

$MhHome = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
$secrets = Join-Path $MhHome 'secrets.env'
if (-not (Test-Path -LiteralPath $secrets)) { throw "missing $secrets - run the first-run setup first (README, First run)" }
$token = ''
foreach ($line in Get-Content -LiteralPath $secrets) {
    if ($line -match '^\s*MCP_TOKEN\s*=\s*(.*)$') { $token = $Matches[1].Trim().Trim('"', "'").Trim() }
}
if (-not $token) { throw "MCP_TOKEN is empty in $secrets - run: mh.ps1 run --rm setup secrets" }

function Hide($text) { if ($text) { ([string]$text).Replace($token, '***') } }

switch ($Client) {
    'claude' {
        if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw 'claude is not on PATH' }
        $ErrorActionPreference = 'Continue'
        & claude mcp remove --scope user mediahub 2>&1 | Out-Null   # failure (not registered) is fine
        $global:LASTEXITCODE = 0   # a .cmd/.ps1 shim may not set it
        $out = & claude mcp add --transport http --scope user mediahub $Url --header "Authorization: Bearer $token" 2>&1 | Out-String
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        $shown = (Hide $out).Trim()
        if ($shown) { Write-Output $shown }
        if ($code -ne 0) { throw "claude mcp add failed (exit $code)" }
        Write-Output "Registered 'mediahub' with Claude Code (user scope) at $Url. The token is in Claude's own config only."
    }
    'codex' {
        $cfgDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
        $cfg = Join-Path $cfgDir 'config.toml'
        New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
        $raw = if (Test-Path -LiteralPath $cfg) { [IO.File]::ReadAllText($cfg) } else { '' }
        $eol = if ($raw.Contains("`r`n")) { "`r`n" } else { "`n" }
        $lines = if ($raw) { $raw -split '\r?\n' } else { @() }
        if ($lines.Count -gt 0 -and $lines[-1] -eq '') { $lines = $lines[0..($lines.Count - 2)] }
        # Drop any existing [mcp_servers.mediahub] table (and its sub-tables), keep everything else.
        $kept = New-Object System.Collections.Generic.List[string]
        $blank = 0; $skip = $false; $sep = $false
        foreach ($l in $lines) {
            if ($l -match '^\s*\[') { $skip = $l -match '^\s*\[mcp_servers\.("?)mediahub\1(\]|\.)' }
            if ($skip) { $sep = $true; continue }
            if ($l.Trim() -eq '') { $blank++; continue }
            if ($blank -gt 0) { 1..$blank | ForEach-Object { $kept.Add('') } }
            elseif ($sep -and $kept.Count -gt 0 -and $l -match '^\s*\[') { $kept.Add('') }
            $blank = 0; $sep = $false; $kept.Add($l)
        }
        if ($kept.Count -gt 0) { $kept.Add('') }
        $kept.Add('[mcp_servers.mediahub]')
        $kept.Add("url = `"$Url`"")
        $kept.Add('bearer_token_env_var = "MEDIAHUB_MCP_TOKEN"')
        $text = ($kept -join $eol) + $eol
        if ($text -ceq $raw) { $did = 'already up to date' } else {
            [IO.File]::WriteAllText($cfg, $text, (New-Object Text.UTF8Encoding $false)); $did = 'written' }
        Write-Output "Codex config $cfg : [mcp_servers.mediahub] $did (url $Url, token from env var MEDIAHUB_MCP_TOKEN)."
        if ($NoPersistEnv) {
            Write-Output 'Skipped setting the user environment variable (-NoPersistEnv): set MEDIAHUB_MCP_TOKEN yourself.'
        } else {
            [Environment]::SetEnvironmentVariable('MEDIAHUB_MCP_TOKEN', $token, 'User')
            Write-Output 'Set the user environment variable MEDIAHUB_MCP_TOKEN. Restart Codex (and open terminals) to pick it up.'
        }
    }
    'json' {
        $dest = Join-Path $MhHome 'mcp-client.json'
        $doc = [ordered]@{ mcpServers = [ordered]@{ mediahub = [ordered]@{
            type = 'http'; url = $Url; headers = [ordered]@{ Authorization = "Bearer $token" } } } }
        $json = ($doc | ConvertTo-Json -Depth 6) + "`n"
        [IO.File]::WriteAllText($dest, $json, (New-Object Text.UTF8Encoding $false))
        if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) { & chmod 600 $dest }
        Write-Output $dest
    }
}
