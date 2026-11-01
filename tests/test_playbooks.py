"""The agent playbooks (AGENTS.md, INSTALL.md, UPDATE.md, UNINSTALL.md, docs/connect-remote.md) name
only commands, files and settings that exist, name no tracker, and carry no personal details."""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from app import setup, setup_tools

ROOT = Path(__file__).parent.parent
PLAYBOOKS = ["AGENTS.md", "INSTALL.md", "UPDATE.md", "UNINSTALL.md", "docs/connect-remote.md"]
SUBCOMMANDS = {"secrets", "wire", "check", "allowed-tools", "bot-prompt", "config-diff", "remap-roots", "version", "mcp-ping"}


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def docs() -> dict[str, str]:
    return {name: _read(name) for name in PLAYBOOKS}


def _parser() -> argparse.ArgumentParser:
    """app.setup's real parser, captured from main() without running anything."""
    class Got(Exception):
        pass

    grabbed: dict[str, argparse.ArgumentParser] = {}
    orig = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        grabbed["p"] = self
        raise Got

    argparse.ArgumentParser.parse_args = grab
    try:
        with pytest.raises(Got):
            setup.main([])
    finally:
        argparse.ArgumentParser.parse_args = orig
    return grabbed["p"]


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return set(actions[0].choices)


# ------------------------------------------------------------------ setup subcommands

# `setup X`, `mh run --rm [-T] setup X`, `python -m app.setup X`
SETUP_CALL = re.compile(r"(?:run --rm(?: -T)? setup|app\.setup|`setup)\s+([a-z][a-z-]*)([^`\n]*)")
SHELL_END = re.compile(r"\s*(?:>|\||&&|;)|[`)]")


def _setup_calls(docs: dict[str, str]) -> list[tuple[str, str, str]]:
    out = []
    for name, text in docs.items():
        for m in SETUP_CALL.finditer(text):
            rest = re.sub(r"<[A-Za-z][^<>]*>", "x", m.group(2))  # placeholders first: their ">" is not a redirect
            rest = SHELL_END.split(rest, maxsplit=1)[0]
            out.append((name, m.group(1), rest))
    return out


def test_parser_has_exactly_the_documented_subcommands():
    assert _subcommands(_parser()) == SUBCOMMANDS


def test_every_setup_subcommand_the_playbooks_name_exists(docs):
    calls = _setup_calls(docs)
    assert len(calls) > 20, "the pattern found almost nothing: is it still right?"
    real = _subcommands(_parser())
    unknown = sorted({f"{n}: setup {sub}" for n, sub, _ in calls if sub not in real})
    assert not unknown, unknown


def test_every_setup_subcommand_is_covered_by_the_playbooks(docs):
    named = {sub for _, sub, _ in _setup_calls(docs)}
    assert SUBCOMMANDS <= named, f"not in any playbook: {sorted(SUBCOMMANDS - named)}"


def test_every_setup_call_parses(docs):
    """Flags and values as written (placeholders filled with a dummy) are accepted by the real parser."""
    parser = _parser()
    bad = []
    for name, sub, rest in _setup_calls(docs):
        args = [sub] + shlex.split(rest)
        if "..." in rest:
            continue  # an elided example ("setup bot-prompt ..."): only the subcommand is checked
        try:
            parser.parse_args(args)
        except SystemExit:
            bad.append(f"{name}: setup {' '.join(args)}")
    assert not bad, bad


# ------------------------------------------------------------------ files

REPO_PATH = re.compile(r"(?<![\w./:$-])((?:compose|docs|templates|skill|examples|app|tests)/[\w./-]*[\w-])")


def test_every_repo_path_the_playbooks_name_exists(docs):
    found, missing = 0, []
    for name, text in docs.items():
        for m in REPO_PATH.finditer(text.replace("\\", "/")):
            found += 1
            if not (ROOT / m.group(1)).exists():
                missing.append(f"{name}: {m.group(1)}")
    assert found > 30
    assert not missing, missing


def test_scripts_and_compose_files_named_exist(docs):
    text = "\n".join(docs.values()).replace("\\", "/")
    scripts = set(re.findall(r"compose/scripts/([\w.-]+\.(?:sh|ps1|py))", text))
    ymls = set(re.findall(r"compose/([\w.-]+\.yml)", text))
    assert {"backup.sh", "backup.ps1", "verify-stack.sh", "verify-stack.ps1",
            "register-mcp.sh", "register-mcp.ps1"} <= scripts
    for f in scripts:
        assert (ROOT / "compose" / "scripts" / f).is_file(), f
    for f in ymls:
        assert (ROOT / "compose" / f).is_file(), f


def test_register_mcp_clients_exist(docs):
    text = "\n".join(docs.values())
    clients = set(re.findall(r"register-mcp\.(?:sh|ps1)\s+([a-z]+)", text))
    assert clients == {"claude", "codex", "json"}
    for script in ("register-mcp.sh", "register-mcp.ps1"):
        body = _read(f"compose/scripts/{script}")
        assert all(c in body for c in clients)


@pytest.mark.parametrize("flag, script", [
    ("--allow-low-disk", "verify-stack.sh"), ("-AllowLowDisk", "verify-stack.ps1"),
    ("--fresh", "verify-stack.sh"), ("-Fresh", "verify-stack.ps1"),
    ("--no-stop", "backup.sh"), ("-NoStop", "backup.ps1"),
])
def test_script_flags_the_playbooks_use_exist(docs, flag, script):
    if any(flag in t for t in docs.values()):
        assert flag.lstrip("-").lower().replace("-", "") in _read(f"compose/scripts/{script}").lower().replace("-", "")


# ------------------------------------------------------------------ settings

def _example_keys(name: str) -> set[str]:
    active, optional = setup_tools.example_keys(_read(f"compose/{name}.example"))
    return active | optional


# Environment variables of the tooling, not settings in an env file.
NOT_SETTINGS = {
    "MEDIAHUB_HOME",        # where stack.env lives (mh.sh / mh.ps1)
    "MEDIAHUB_MCP_TOKEN",   # the Codex env var register-mcp sets
    "MEDIAHUB_URL",         # verify-stack.sh's dashboard address
    "MEDIAHUB_MCP_URL",     # mcp-ping's endpoint (default http://127.0.0.1:8000/mcp)
}


def test_setkv_keys_exist_in_that_files_example(docs):
    calls = re.findall(r"(?:setkv(?:\.sh|\.ps1)?|Set-MhKey)\s+(stack\.env|secrets\.env)\s+([A-Z][A-Z0-9_]*)",
                       "\n".join(docs.values()))
    assert len(calls) > 15
    bad = sorted({f"{f} {k}" for f, k in calls if k not in _example_keys(f)})
    assert not bad, bad


def test_every_setting_the_playbooks_name_is_in_an_example(docs):
    known = _example_keys("stack.env") | _example_keys("secrets.env") | _example_keys("vpn.env") | NOT_SETTINGS
    named = set()
    for text in docs.values():
        named |= set(re.findall(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\s*=(?!=)", text))
        named |= set(re.findall(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text))
    assert len(named) > 25
    assert not named - known, sorted(named - known)


def test_preflight_ports_match_the_published_ports():
    published = set()
    for f in ("docker-compose.yml", "stack.yml"):
        published |= {int(p) for p in re.findall(r'^\s*-\s*"[^"]*:(\d+)"', _read(f"compose/{f}"), re.M)}
    bash = re.search(r"^PORTS='([\d|]+)'$", _read("compose/scripts/preflight.sh"), re.M)
    ps = re.search(r"\$Ports = ([\d, ]+)", _read("compose/scripts/preflight.ps1"))
    assert bash and ps
    assert {int(p) for p in bash.group(1).split("|")} == published
    assert {int(p) for p in ps.group(1).split(",")} == published
    text = _read("INSTALL.md")
    for p in sorted(published):   # the prose in 0.6 names every port
        assert str(p) in text.split("### 0.6")[1].split("### 0.7")[0], p


def test_install_phase0_and_1_call_the_preflight_scripts_and_paste_no_probe():
    text = _read("INSTALL.md")
    phase0 = text.split("## Phase 0")[1].split("## Phase 1")[0]
    assert "compose/scripts/preflight.sh" in phase0 and r".\compose\scripts\preflight.ps1" in phase0
    for gone in ("NOCHECK", "timedatectl", "ss -ltn", "found()", "printf '2.24.4"):
        assert gone not in text, gone
    for section in ("OS", "tools", "docker", "docker running", "disk", "ports", "existing stack", "mediahub",
                    "timezone", "uid/gid", "claude cli"):
        assert f"== {section} ==" in text, section


def test_install_explains_the_polish_items():
    text = _read("INSTALL.md")
    squashed = re.sub(r"\s+", " ", text)
    # 8.2: what M in "services up N/M" counts
    assert "M counts the apps MediaHub talks to (Sonarr, Radarr, Prowlarr, Plex, qBittorrent and Bazarr" in squashed
    # 0.7 / 8.1: df -h is GiB, MediaHub says GB
    assert squashed.count("GiB") >= 2 and "100 GiB is 107 GB" in squashed
    # Phase 1: headless runs list the choices and carry on
    assert "do not wait: put the list in the hand-over" in squashed
    # Phase 2: a local origin breaks UPDATE.md
    phase2 = text.split("## Phase 2")[1].split("## Phase 3")[0]
    assert "git remote set-url origin <the real" in phase2 and "UPDATE.md" in phase2
    assert "git remote set-url origin" in _read("UPDATE.md")
    # 5.2: quiet pulls, trimmed output, exit code checked
    step = text.split("### 5.2 Start the stack")[1].split("### 5.3")[0]
    assert "pull --quiet --ignore-buildable" in step and "--quiet-pull" in step and "tail -n 40" in step
    assert 'echo "up exit code: $rc"' in step and "Select-Object -Last 40" in step and "LASTEXITCODE" in step
    upd = _read("UPDATE.md").split("## 7. Start")[1].split("## 8.")[0]
    assert "--quiet-pull" in upd and "up exit code" in upd


# ------------------------------------------------------------------ content rules

# Public torrent indexers/trackers. The package names none of them anywhere a stranger reads.
# A bracketed letter keeps each name out of a plain-text search of this file (the release gate scrubs for them).
TRACKERS = ["1337[x]", "ez[t]v", "ny[a]a", "lime[t]orrents", "rar[b]g", "y[t]s", "yify torrents", "pirate ?bay", "tpb",
            "torrent[g]alaxy", "torlock", "kickass", "kat\\.cr", "zooqle", "torrentleech", "iptorrents",
            "broadcasthenet", "passthepopcorn", "torrentday", "filelist", "rutracker", "kna[b]en", "bitsearch",
            "solidtorrents", "showrss", "magnetdl", "torrentz", "bt4g", "torrentfunk", "glodls", "idope"]
TRACKER = re.compile(r"\b(?:" + "|".join(TRACKERS) + r")\b", re.I)


@pytest.mark.parametrize("name", PLAYBOOKS + ["README.md", "CLAUDE.md", "skill/SKILL.md", "templates/bot-prompt.md"])
def test_no_tracker_names(name):
    hits = [f"{name}:{n}: {m.group(0)}" for n, line in enumerate(_read(name).splitlines(), 1)
            for m in TRACKER.finditer(line)]
    assert not hits, hits


@pytest.mark.parametrize("name", PLAYBOOKS)
def test_no_personal_details(name):
    text = _read(name)
    assert not re.search(r"\bBart\b", text)
    assert "claude" + "os" not in text.lower()


def test_agents_md_is_short_and_routes_every_task():
    text = _read("AGENTS.md")
    assert len(text.splitlines()) <= 80
    for target in ("INSTALL.md", "UPDATE.md", "UNINSTALL.md", "skill/SKILL.md", "docs/connect-remote.md"):
        assert target in text, target


def test_claude_md_points_at_agents_md():
    text = _read("CLAUDE.md")
    assert "AGENTS.md" in text and len(text.splitlines()) <= 3


def test_install_has_every_phase_in_order():
    heads = re.findall(r"^## Phase (\d+):", _read("INSTALL.md"), re.M)
    assert heads == [str(i) for i in range(10)]


@pytest.mark.parametrize("name", ["INSTALL.md", "UPDATE.md", "UNINSTALL.md"])
def test_every_step_has_both_shells(name):
    text = _read(name)
    assert text.count("```bash") == text.count("```powershell"), "a bash block without a PowerShell twin (or back)"
