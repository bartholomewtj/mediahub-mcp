"""compose/scripts/register-mcp.ps1 and .sh: run against a fake MEDIAHUB_HOME and a fake `claude` on PATH.

Nothing here touches the real Claude/Codex config or the real user environment: MEDIAHUB_HOME, CODEX_HOME,
HOME and USERPROFILE all point into a throw-away folder under %TEMP%, a fake `claude` that only records its
arguments is first on PATH (and the test checks that it is the one that resolves), and the PowerShell codex
run uses -NoPersistEnv so the real user environment variables are never written.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "compose" / "scripts"
TOKEN = "tok_Zq8W3xV7nB2mK9pL4cD6hF1sA5gJ0r"
URL = "http://127.0.0.1:8000/mcp"


def _git_bash() -> str | None:
    for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.exists(p):
            return p
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and "windowsapps" not in found.lower():  # not WSL's launcher
        return found
    return None


BASH, PWSH = _git_bash(), shutil.which("pwsh")
RUNNERS = [pytest.param("sh", marks=pytest.mark.skipif(not BASH, reason="no bash")),
           pytest.param("ps1", marks=pytest.mark.skipif(not PWSH, reason="no pwsh"))]


def fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


class Env:
    def __init__(self, root: Path, runner: str):
        self.root, self.runner = root, runner
        self.home = root / "mediahub-home"
        self.user = root / "user"
        self.codex = root / "codex-home"
        self.bin = root / "bin"
        self.args = root / "claude-args.txt"
        for d in (self.home, self.user, self.codex, self.bin):
            d.mkdir()
        (self.home / "secrets.env").write_text(f"SONARR_API_KEY=abc\nMCP_TOKEN={TOKEN}\nQBIT_PASS=\n", encoding="utf-8")
        self._fake_claude()

    def _fake_claude(self):
        if self.runner == "sh":
            f = self.bin / "claude"
            f.write_text('#!/usr/bin/env bash\n{ echo "CALL"; printf "%s\\n" "$@"; } >> "$FAKE_ARGS"\n'
                         '[ "$2" = remove ] && exit 1\necho "fake claude saw: $*"\n', encoding="utf-8", newline="\n")
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        else:
            (self.bin / "claude.ps1").write_text(
                'Add-Content -LiteralPath $env:FAKE_ARGS -Value (@("CALL") + $args)\n'
                'if ($args[1] -eq "remove") { exit 1 }\n'
                'Write-Output ("fake claude saw: " + ($args -join " "))\n', encoding="utf-8")

    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        sep = ";" if os.name == "nt" else ":"
        e.update(MEDIAHUB_HOME=fwd(self.home) if self.runner == "sh" else str(self.home),
                 CODEX_HOME=fwd(self.codex) if self.runner == "sh" else str(self.codex),
                 HOME=fwd(self.user), USERPROFILE=str(self.user), FAKE_ARGS=fwd(self.args),
                 PATH=str(self.bin) + sep + e.get("PATH", ""))
        return e

    def run(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
        script = SCRIPTS / f"register-mcp.{self.runner}"
        if self.runner == "sh":
            cmd = [BASH, fwd(script), *args]
        else:
            cmd = [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
        return subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=timeout, cwd=self.root)

    def fake_is_first(self):
        """Never let a test reach the real claude."""
        probe = ([BASH, "-c", "command -v claude"] if self.runner == "sh"
                 else [PWSH, "-NoProfile", "-Command", "(Get-Command claude).Source"])
        out = subprocess.run(probe, env=self.env(), capture_output=True, text=True, timeout=60).stdout.strip()
        norm = out.replace("\\", "/").lower()
        assert "mediahub-register-" in norm and "/bin/claude" in norm, out

    def codex_toml(self) -> str:
        return (self.codex / "config.toml").read_text(encoding="utf-8")

    def files_containing_token(self) -> set[str]:
        hits = set()
        for p in self.root.rglob("*"):
            if p.is_file() and TOKEN in p.read_text(encoding="utf-8", errors="ignore"):
                hits.add(p.name)
        return hits


@pytest.fixture(params=RUNNERS)
def env(request):
    root = Path(tempfile.mkdtemp(prefix="mediahub-register-"))  # under %TEMP%
    try:
        yield Env(root, request.param)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _no_token(res: subprocess.CompletedProcess):
    assert TOKEN not in res.stdout and TOKEN not in res.stderr


def test_claude_registers_with_the_header_and_never_prints_the_token(env):
    env.fake_is_first()
    res = env.run("claude")
    assert res.returncode == 0, res.stdout + res.stderr
    _no_token(res)
    calls = [c.split("\n") for c in env.args.read_text(encoding="utf-8").split("CALL\n") if c.strip()]
    calls = [[a.rstrip("\r") for a in c if a.strip()] for c in calls]
    assert calls[0] == ["mcp", "remove", "--scope", "user", "mediahub"]        # old entry removed first; its failure ignored
    assert calls[1] == ["mcp", "add", "--transport", "http", "--scope", "user", "mediahub", URL,
                        "--header", f"Authorization: Bearer {TOKEN}"]
    assert "Registered 'mediahub' with Claude Code" in res.stdout
    assert "fake claude saw" in res.stdout and "***" in res.stdout   # the fake echoed the token; the script hid it
    assert env.files_containing_token() == {"secrets.env", "claude-args.txt"}


def test_claude_url_override(env):
    res = env.run("claude", "--url", "http://127.0.0.1:9000/mcp")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "http://127.0.0.1:9000/mcp" in env.args.read_text(encoding="utf-8")
    _no_token(res)


def test_json_writes_the_snippet_and_prints_only_its_path(env):
    res = env.run("json")
    assert res.returncode == 0, res.stdout + res.stderr
    dest = env.home / "mcp-client.json"
    assert res.stdout.strip().replace("\\", "/").lower() == fwd(dest).lower()
    assert len(res.stdout.strip().splitlines()) == 1
    _no_token(res)
    assert json.loads(dest.read_text(encoding="utf-8")) == {
        "mcpServers": {"mediahub": {"type": "http", "url": URL, "headers": {"Authorization": f"Bearer {TOKEN}"}}}}
    if os.name != "nt":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert env.files_containing_token() == {"secrets.env", "mcp-client.json"}


CODEX_BEFORE = '''model = "o3"

[mcp_servers.other]
url = "https://example.test/mcp"

[mcp_servers.mediahub]
url = "http://old.example/mcp"
bearer_token_env_var = "OLD"

[mcp_servers.mediahub.http_headers]
X = "y"

[profiles.fast]
model = "mini"
'''


def test_codex_edits_config_toml_idempotently(env):
    flag = "-NoPersistEnv" if env.runner == "ps1" else "--no-persist-env"
    (env.codex / "config.toml").write_text(CODEX_BEFORE, encoding="utf-8", newline="\n")
    res = env.run("codex", flag)
    assert res.returncode == 0, res.stdout + res.stderr
    _no_token(res)
    first = env.codex_toml()
    assert first == ('model = "o3"\n\n[mcp_servers.other]\nurl = "https://example.test/mcp"\n\n'
                     '[profiles.fast]\nmodel = "mini"\n\n'
                     f'[mcp_servers.mediahub]\nurl = "{URL}"\nbearer_token_env_var = "MEDIAHUB_MCP_TOKEN"\n')
    assert "written" in res.stdout and "MEDIAHUB_MCP_TOKEN" in res.stdout
    res = env.run("codex", flag)
    assert res.returncode == 0 and env.codex_toml() == first and "already up to date" in res.stdout
    assert first.count("[mcp_servers.mediahub]") == 1
    assert env.files_containing_token() == {"secrets.env"}      # codex gets the token only through its env var


def test_codex_creates_the_config_and_honours_url(env):
    flag = "-NoPersistEnv" if env.runner == "ps1" else "--no-persist-env"
    res = env.run("codex", flag, "--url", "https://mh.example.test/mcp")
    assert res.returncode == 0, res.stdout + res.stderr
    assert env.codex_toml() == ('[mcp_servers.mediahub]\nurl = "https://mh.example.test/mcp"\n'
                                'bearer_token_env_var = "MEDIAHUB_MCP_TOKEN"\n')
    assert not (env.user / ".profile").exists()


@pytest.mark.skipif(not BASH, reason="no bash")
def test_codex_sh_appends_the_profile_export_once_without_a_copy_of_the_token():
    root = Path(tempfile.mkdtemp(prefix="mediahub-register-"))
    try:
        e = Env(root, "sh")
        assert e.run("codex").returncode == 0
        profile = e.user / ".profile"
        text = profile.read_text(encoding="utf-8")
        assert text.count("MEDIAHUB_MCP_TOKEN") == 1 and TOKEN not in text
        assert fwd(e.home / "secrets.env") in text
        res = e.run("codex")
        assert "already sets MEDIAHUB_MCP_TOKEN" in res.stdout
        assert profile.read_text(encoding="utf-8") == text
        # and the line works: it exports the token from secrets.env
        got = subprocess.run([BASH, "-c", f". '{fwd(profile)}'; printf %s \"$MEDIAHUB_MCP_TOKEN\""],
                             env=e.env(), capture_output=True, text=True).stdout
        assert got == TOKEN
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_or_empty_token_is_an_error_without_leaks(env):
    (env.home / "secrets.env").write_text("MCP_TOKEN=\nSONARR_API_KEY=abc\n", encoding="utf-8")
    res = env.run("json")
    assert res.returncode != 0 and "MCP_TOKEN is empty" in res.stdout + res.stderr
    assert not (env.home / "mcp-client.json").exists()
    (env.home / "secrets.env").unlink()
    res = env.run("json")
    assert res.returncode != 0 and "missing" in res.stdout + res.stderr


def test_unknown_client_and_bad_url_are_refused(env):
    assert env.run("nonexistent").returncode != 0
    res = env.run("json", "--url", "ftp://nope")
    assert res.returncode != 0 and not (env.home / "mcp-client.json").exists()
