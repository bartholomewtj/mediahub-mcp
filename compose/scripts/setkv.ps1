# setkv.ps1 <file> <KEY> <value>: set KEY=VALUE in a file under MEDIAHUB_HOME (default $HOME\.mediahub).
#
# Same semantics as setkv.sh: replaces the first `KEY=` line, else the first commented `# KEY=` line,
# else appends `KEY=VALUE`; drops further `KEY=` duplicates; then checks that exactly one `KEY=` line
# exists with the new value. On success it prints `OK setkv <KEY>` (the key name, never the value). On failure: a `FAIL setkv: ...` line on stderr and exit code 1.
#
#   .\compose\scripts\setkv.ps1 stack.env TZ 'Europe/London'
param(
    [Parameter(Mandatory, Position = 0)][string]$File,
    [Parameter(Mandatory, Position = 1)][string]$Key,
    [Parameter(Mandatory, Position = 2)][AllowEmptyString()][string]$Value
)
$ErrorActionPreference = 'Stop'
function Fail([string]$msg) { [Console]::Error.WriteLine("FAIL setkv: $msg"); exit 1 }

$home_ = if ($env:MEDIAHUB_HOME) { $env:MEDIAHUB_HOME } else { Join-Path $HOME '.mediahub' }
$f = Join-Path $home_ $File
if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Fail "no such file $f" }
try {
    $lines = [Collections.Generic.List[string]]::new([string[]][IO.File]::ReadAllLines($f))
    $hit = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].StartsWith("$Key=", [StringComparison]::Ordinal)) { $hit = $i; break } }
    if ($hit -lt 0) {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].StartsWith('#', [StringComparison]::Ordinal) -and $lines[$i].Substring(1).TrimStart(" `t").StartsWith("$Key=", [StringComparison]::Ordinal)) { $hit = $i; break }
        }
    }
    if ($hit -ge 0) {
        $lines[$hit] = "$Key=$Value"
        for ($i = $lines.Count - 1; $i -gt $hit; $i--) { if ($lines[$i].StartsWith("$Key=", [StringComparison]::Ordinal)) { $lines.RemoveAt($i) } }
    } else { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllLines($f, $lines)
    $now = [IO.File]::ReadAllLines($f)
} catch { Fail "could not write $f ($($_.Exception.Message))" }
$n = @($now | Where-Object { $_.StartsWith("$Key=", [StringComparison]::Ordinal) }).Count
if ($n -ne 1 -or $now -cnotcontains "$Key=$Value") { Fail "$Key in $File is not set exactly once to the new value" }
Write-Output "OK setkv $Key"
