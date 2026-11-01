"""The scripts the playbooks call are run for real: `compose/scripts/setkv.sh` and `preflight.sh` under every
POSIX shell found (Git Bash's sh and bash, a system sh, busybox) and the `.ps1` twins when PowerShell exists.
`setkv` once used GNU-only `sed 0,/re/` and silently did nothing on BusyBox (Alpine); it was also pasted
as a 27-line function into four blocks, which cannot be sent through `bash -lc '...'` wrappers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SETKV_SH = ROOT / "compose" / "scripts" / "setkv.sh"
SETKV_PS1 = ROOT / "compose" / "scripts" / "setkv.ps1"

# GNU-only forms that fail, or silently do nothing, on BusyBox.
NOT_PORTABLE = [r"sed [^\n]*0,/", r"\bpgrep\b", r"\bdf\b[^\n]* -x\b", r"\bss -ltnH", r"grep -P\b", r"readlink -f"]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")


# ------------------------------------------------------------------ portability and modes

@pytest.mark.parametrize("name", ["INSTALL.md", "UPDATE.md", "UNINSTALL.md"])
def test_bash_blocks_avoid_gnu_only_forms(name):
    bash = "\n".join(re.findall(r"```bash\n(.*?)```", _read(name), re.S))
    assert bash
    hits = [pat for pat in NOT_PORTABLE if re.search(pat, bash)]
    assert not hits, hits


def test_every_tracked_shell_script_is_executable():
    """A script committed 100644 makes `chmod +x` (an old INSTALL step) dirty the tree."""
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "-s"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    scripts = [ln.split(None, 3) for ln in out.splitlines() if ln.rstrip().endswith(".sh")]
    assert len(scripts) >= 5
    paths = [path.strip() for *_, path in scripts]
    assert "compose/scripts/setkv.sh" in paths and "compose/scripts/preflight.sh" in paths
    bad = [f"{path.strip()} is {mode}" for mode, _, _, path in scripts if mode != "100755"]
    assert not bad, bad


# ------------------------------------------------------------------ bash blocks go through `bash -lc '...'`

QUOTE_OK = "# quote-ok"   # a line holding this marker may keep a single quote (there should be none)


def _bash_blocks(name: str) -> list[tuple[int, str]]:
    """(first line number, text) of every ```bash block, indented ones included."""
    out, cur, start = [], None, 0
    for n, line in enumerate(_read(name).splitlines(), 1):
        s = line.strip()
        if cur is None and s == "```bash":
            cur, start = [], n + 1
        elif cur is not None and s.startswith("```"):
            out.append((start, "\n".join(cur)))
            cur = None
        elif cur is not None:
            cur.append(line)
    return out


@pytest.mark.parametrize("name", ["INSTALL.md", "UPDATE.md", "UNINSTALL.md"])
def test_no_single_quote_inside_bash_blocks(name):
    """A single quote cannot be sent through `bash -lc '<cmd>'`: long or quoted logic lives in scripts."""
    blocks = _bash_blocks(name)
    assert blocks
    bad = [f"{name}:{start + i}: {line.strip()}" for start, text in blocks
           for i, line in enumerate(text.splitlines()) if "'" in line and QUOTE_OK not in line]
    assert not bad, bad


def test_install_says_every_bash_block_is_wrapper_safe():
    text = _read("INSTALL.md")
    assert re.search(r"[Ee]very block of the bash track is safe to send through a `bash -lc '\.\.\.'` wrapper", text)


def test_log_filter_guidance_is_for_raw_container_logs_only():
    text = _read("INSTALL.md")
    head = text.split("## Phase 0")[0]
    assert re.search(r"needs \*\*no\*\*\s+filtering", head) and "updated: ..." in head
    assert "logs --tail 80 <service> 2>&1 | grep -viE" in head and "container logs" in head


# ------------------------------------------------------------------ the playbooks call the scripts

@pytest.mark.parametrize("name", ["INSTALL.md", "UPDATE.md"])
def test_playbooks_call_the_setkv_scripts_and_paste_no_function(name):
    text = _read(name)
    assert not re.search(r"setkv\(\)|function Set-MhKey|setkv-begin|Set-MhKey-begin", text)
    assert not re.search(r"(?m)^(?:setkv|Set-MhKey) ", text), "call the script files, not a pasted function"
    assert "compose/scripts/setkv.sh" in text and r".\compose\scripts\setkv.ps1" in text


def test_every_setkv_call_in_the_playbooks_has_the_right_shape():
    sh = re.compile(r"""compose/scripts/setkv\.sh (stack|secrets)\.env [A-Z][A-Z0-9_]* ("[^"']*"|[a-z]+)""")
    ps = re.compile(r"\.\\compose\\scripts\\setkv\.ps1 (stack|secrets)\.env [A-Z][A-Z0-9_]* ('.*'|[a-z]+)")
    seen = 0
    for name in ("INSTALL.md", "UPDATE.md"):
        for line in _read(name).splitlines():
            if line.startswith("compose/scripts/setkv.sh "):
                assert sh.fullmatch(line), line
                seen += 1
            if line.startswith(r".\compose\scripts\setkv.ps1 "):
                assert ps.fullmatch(line), line
                seen += 1
    assert seen >= 20


def test_setkv_script_files_are_clean():
    raw = SETKV_SH.read_bytes()
    assert raw.split(b"\n", 1)[0] == b"#!/bin/sh"
    assert b"\r" not in raw, "CRLF would break the shebang"
    assert not re.search(rb"\[\[|\blocal\b|\bfunction\b|<\(", raw.split(b"awk '")[0])
    assert SETKV_PS1.exists()


# ------------------------------------------------------------------ setkv.sh

def _posix_shells() -> list[list[str]]:
    """Real POSIX shells to run setkv under: Git for Windows' sh/bash (never WSL's bash.exe), sh, busybox."""
    found: list[list[str]] = []
    for cand in (r"C:\Program Files\Git\usr\bin\sh.exe", r"C:\Program Files\Git\bin\bash.exe",
                 shutil.which("sh"), shutil.which("bash")):
        if cand and Path(cand).exists() and "system32" not in cand.lower() and [cand] not in found:
            found.append([cand])
    if shutil.which("busybox"):
        found.append([shutil.which("busybox"), "sh"])
    return found


SHELLS = _posix_shells()
START = ("# stack.env\n# MEDIA_ROOT=/old\n#   TZ=UTC\nPUID=1000\nPLEX_CLAIM_X=keep\nDUP=1\nmid=lower\nDUP=2\n"
         "#PLEX_CLAIM=\n")


def _setup(tmp: Path, content: str) -> tuple[Path, Path]:
    home = tmp / "home"
    (home / ".mediahub").mkdir(parents=True, exist_ok=True)
    target = home / ".mediahub" / "stack.env"
    target.write_bytes(content.encode())
    return home, target


def _run(shell: list[str], tmp: Path, *calls: tuple[str, ...], content: str = START):
    """Run setkv.sh once per call (file, key, value); return the last result and the file's final text."""
    home, target = _setup(tmp, content)
    env = {**os.environ, "HOME": home.as_posix()}
    env.pop("MEDIAHUB_HOME", None)
    r = None
    for call in calls:
        r = subprocess.run([*shell, SETKV_SH.as_posix(), *call], capture_output=True, text=True, env=env, cwd=tmp)
        if r.returncode:
            break
    return r, target.read_bytes().decode()


def _shell_id(shell: list[str]) -> str:
    return Path(shell[0]).name + (" sh" if len(shell) > 1 else "")


@pytest.mark.skipif(not SHELLS, reason="no POSIX shell (Git Bash, sh, busybox) available")
@pytest.mark.parametrize("shell", SHELLS, ids=_shell_id)
class TestSetkv:
    def test_existing_commented_and_absent_keys(self, shell, tmp_path):
        r, got = _run(shell, tmp_path, ("stack.env", "PUID", "1001"), ("stack.env", "MEDIA_ROOT", "/srv/media"),
                      ("stack.env", "TZ", "Europe/London"), ("stack.env", "OWNER_NAME", "Ann"))
        assert r.returncode == 0, r.stderr
        assert got == ("# stack.env\nMEDIA_ROOT=/srv/media\nTZ=Europe/London\nPUID=1001\nPLEX_CLAIM_X=keep\n"
                       "DUP=1\nmid=lower\nDUP=2\n#PLEX_CLAIM=\nOWNER_NAME=Ann\n")

    @pytest.mark.parametrize("value", [
        "/mnt/my disk/a&b", "a|b & c/d", "D:\\Media\\x", "x\\n y &amp; \\1 $HOME `id` 'q' \"dq\"",
        " lead", "=eq=", "/mnt/with spaces/Media & Co", "trail ",
    ])
    def test_awkward_values_are_written_verbatim(self, shell, tmp_path, value):
        r, got = _run(shell, tmp_path, ("stack.env", "PUID", value))
        assert r.returncode == 0, r.stderr
        assert f"\nPUID={value}\n" in got and got.count("\nPUID=") == 1
        assert got.count("PLEX_CLAIM_X=keep") == 1   # nothing else touched

    def test_empty_value(self, shell, tmp_path):
        r, got = _run(shell, tmp_path, ("stack.env", "PUID", ""))
        assert r.returncode == 0, r.stderr
        assert "\nPUID=\n" in got and got.count("PUID=") == 1

    def test_commented_key_is_used_only_when_there_is_no_active_line(self, shell, tmp_path):
        r, got = _run(shell, tmp_path, ("stack.env", "PLEX_CLAIM", "claim-abc"), content="A=1\n#PLEX_CLAIM=\nB=2\n")
        assert r.returncode == 0 and got == "A=1\nPLEX_CLAIM=claim-abc\nB=2\n"
        r, got = _run(shell, tmp_path / "b", ("stack.env", "PLEX_CLAIM", ""), content="# PLEX_CLAIM=x\nPLEX_CLAIM=old\n")
        assert r.returncode == 0 and got == "# PLEX_CLAIM=x\nPLEX_CLAIM=\n"

    def test_duplicates_collapse_to_one_line(self, shell, tmp_path):
        r, got = _run(shell, tmp_path, ("stack.env", "DUP", "3"))
        assert r.returncode == 0 and got.count("DUP=") == 1 and "\nDUP=3\n" in got

    def test_empty_file_and_missing_final_newline(self, shell, tmp_path):
        r, got = _run(shell, tmp_path, ("stack.env", "A", "1"), content="")
        assert r.returncode == 0 and got == "A=1\n"
        r, got = _run(shell, tmp_path / "b", ("stack.env", "B", "2"), content="A=1")
        assert r.returncode == 0 and got == "A=1\nB=2\n"

    def test_missing_file_fails_loudly(self, shell, tmp_path):
        r, _ = _run(shell, tmp_path, ("nope.env", "A", "1"))
        assert r.returncode != 0 and "FAIL setkv" in r.stderr

    def test_wrong_argument_count_fails(self, shell, tmp_path):
        r, _ = _run(shell, tmp_path, ("stack.env", "A"))
        assert r.returncode != 0 and "usage" in r.stderr

    def test_mediahub_home_overrides_home(self, shell, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "stack.env").write_bytes(b"A=1\n")
        home, target = _setup(tmp_path, START)
        env = {**os.environ, "HOME": home.as_posix(), "MEDIAHUB_HOME": other.as_posix()}
        r = subprocess.run([*shell, SETKV_SH.as_posix(), "stack.env", "A", "2"], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert (other / "stack.env").read_bytes() == b"A=2\n" and target.read_bytes().decode() == START

    def test_leaves_no_temp_file(self, shell, tmp_path):
        _run(shell, tmp_path, ("stack.env", "A", "1"))
        assert [p.name for p in (tmp_path / "home" / ".mediahub").iterdir()] == ["stack.env"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_keeps_file_permissions(self, shell, tmp_path):
        home, target = _setup(tmp_path, START)
        target.chmod(0o600)
        env = {**os.environ, "HOME": home.as_posix()}
        r = subprocess.run([*shell, SETKV_SH.as_posix(), "stack.env", "A", "1"], capture_output=True, text=True, env=env)
        assert r.returncode == 0
        assert target.stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------------ setkv.ps1 (PowerShell)

PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _run_ps(tmp: Path, content: str, *calls: tuple[str, ...]):
    folder = tmp / "mh"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stack.env").write_text(content, encoding="utf-8", newline="")
    env = {**os.environ, "MEDIAHUB_HOME": str(folder)}
    r = None
    for call in calls:
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SETKV_PS1), *call],
                           capture_output=True, text=True, env=env)
        if r.returncode:
            break
    return r, (folder / "stack.env").read_bytes().decode().replace("\r\n", "\n")


@pytest.mark.skipif(not PWSH, reason="no PowerShell available")
class TestSetkvPowerShell:
    def test_same_results_as_the_shell_script(self, tmp_path):
        r, got = _run_ps(tmp_path, START, ("stack.env", "PUID", "1001"), ("stack.env", "MEDIA_ROOT", "D:/Media & x"),
                         ("stack.env", "DUP", "3"), ("stack.env", "OWNER_NAME", "Ann"), ("stack.env", "TZ", ""))
        assert r.returncode == 0, r.stdout + r.stderr
        assert got == ("# stack.env\nMEDIA_ROOT=D:/Media & x\nTZ=\nPUID=1001\nPLEX_CLAIM_X=keep\nDUP=3\nmid=lower\n"
                       "#PLEX_CLAIM=\nOWNER_NAME=Ann\n")

    def test_commented_key_only_when_no_active_line(self, tmp_path):
        r, got = _run_ps(tmp_path, "A=1\n#PLEX_CLAIM=\nB=2\n", ("stack.env", "PLEX_CLAIM", "claim-abc"))
        assert r.returncode == 0 and got == "A=1\nPLEX_CLAIM=claim-abc\nB=2\n"
        r, got = _run_ps(tmp_path / "b", "# PLEX_CLAIM=x\nPLEX_CLAIM=old\n", ("stack.env", "PLEX_CLAIM", ""))
        assert r.returncode == 0 and got == "# PLEX_CLAIM=x\nPLEX_CLAIM=\n"

    def test_empty_file(self, tmp_path):
        r, got = _run_ps(tmp_path, "", ("stack.env", "A", "1"))
        assert r.returncode == 0 and got == "A=1\n"

    def test_missing_file_fails_with_exit_1_and_message(self, tmp_path):
        r, _ = _run_ps(tmp_path, START, ("nope.env", "A", "1"))
        assert r.returncode == 1 and "FAIL setkv" in r.stderr


# ------------------------------------------------------------------ Phase 2 (Get the code): tag, else default branch, else stay

def _phase2(kind: str) -> str:
    text = _read("INSTALL.md")
    phase = text.split("## Phase 2: Get the code")[1].split("## Phase 3")[0]
    return re.search(rf"```{kind}\n(.*?)```", phase, re.S).group(1)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=trunk", *args],
                       cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _checkout_for_phase2(tmp: Path, scenario: str) -> Path:
    """Repos whose default branch is `trunk` (no `main` anywhere), as a clone with or without origin/HEAD,
    with or without tags, or a repo with no remote at all."""
    src = tmp / "src"
    src.mkdir()
    _git(src, "init", "-q")
    (src / "VERSION").write_text("0.1.0\n")
    (src / "compose" / "scripts").mkdir(parents=True)
    (src / "compose" / "mh.sh").write_text("#!/bin/sh\n")
    (src / "compose" / "scripts" / "x.sh").write_text("#!/bin/sh\n")
    _git(src, "add", "-A")
    _git(src, "update-index", "--chmod=+x", "compose/mh.sh", "compose/scripts/x.sh")
    _git(src, "commit", "-q", "-m", "one")
    if scenario == "no-remote":
        _git(src, "checkout", "-q", "-b", "feature")
        return src
    if scenario == "tags":
        _git(src, "tag", "v0.1.0")
        (src / "VERSION").write_text("0.2.0-rc1\n")
        _git(src, "commit", "-qam", "two")
        _git(src, "tag", "v0.2.0-rc1")
    clone = tmp / "clone"
    _git(tmp, "clone", "-q", str(src), "clone")
    if scenario == "no-origin-head":
        _git(clone, "remote", "set-head", "origin", "-d")
        _git(clone, "config", "remote.origin.followRemoteHEAD", "never")   # git >= 2.48 would re-create it on fetch
    if scenario == "tags":
        _git(clone, "checkout", "-q", "--detach")
    else:
        _git(clone, "checkout", "-q", "-b", "scratch")      # not on the default branch
    return clone


@pytest.mark.skipif(not SHELLS or not shutil.which("git"), reason="no POSIX shell or git")
@pytest.mark.parametrize("scenario, branch, version_line", [
    ("origin-head", "trunk", "version 0.1.0"),         # no tags: the remote's default branch, not `main`
    ("no-origin-head", "scratch", "version 0.1.0"),    # no origin/HEAD: stay where we are
    ("no-remote", "feature", "version 0.1.0"),         # a bundle / local repo: stay where we are
    ("tags", None, "version 0.1.0"),                   # newest release tag wins; the -rc tag is skipped
])
def test_phase2_bash_never_needs_main(tmp_path, scenario, branch, version_line):
    repo = _checkout_for_phase2(tmp_path, scenario)
    r = subprocess.run([*SHELLS[0], "-c", _phase2("bash")], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "main" not in (r.stdout + r.stderr).replace("remain", ""), r.stdout + r.stderr
    assert version_line in r.stdout, r.stdout
    head = _git(repo, "branch", "--show-current")
    assert head == (branch or "")
    if scenario == "tags":
        assert _git(repo, "describe", "--tags", "--exact-match") == "v0.1.0"
    if scenario in ("no-origin-head", "no-remote"):
        assert "staying on" in r.stdout


@pytest.mark.skipif(not PWSH or not shutil.which("git"), reason="no PowerShell or git")
@pytest.mark.parametrize("scenario, branch", [
    ("origin-head", "trunk"), ("no-origin-head", "scratch"), ("no-remote", "feature"), ("tags", ""),
])
def test_phase2_powershell_never_needs_main(tmp_path, scenario, branch):
    repo = _checkout_for_phase2(tmp_path, scenario)
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", _phase2("powershell")],
                       cwd=repo, capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "pathspec 'main'" not in out and "main' did not match" not in out, out
    assert "version 0.1.0" in r.stdout, out
    assert _git(repo, "branch", "--show-current") == branch
    if scenario in ("no-origin-head", "no-remote"):
        assert "staying on" in r.stdout


# ------------------------------------------------------------------ setkv prints OK <KEY>, never the value

@pytest.mark.skipif(not SHELLS, reason="no POSIX shell (Git Bash, sh, busybox) available")
@pytest.mark.parametrize("shell", SHELLS, ids=_shell_id)
def test_setkv_sh_prints_ok_and_the_key_but_not_the_value(shell, tmp_path):
    r, _ = _run(shell, tmp_path, ("stack.env", "PUID", "s3cr3t-value"))
    assert r.returncode == 0 and r.stdout.strip() == "OK setkv PUID"
    assert "s3cr3t-value" not in r.stdout + r.stderr


@pytest.mark.skipif(not PWSH, reason="no PowerShell available")
def test_setkv_ps1_prints_ok_and_the_key_but_not_the_value(tmp_path):
    r, _ = _run_ps(tmp_path, START, ("stack.env", "PUID", "s3cr3t-value"))
    assert r.returncode == 0 and r.stdout.strip() == "OK setkv PUID"
    assert "s3cr3t-value" not in r.stdout + r.stderr


# ------------------------------------------------------------------ preflight.sh / preflight.ps1

PREFLIGHT_SH = ROOT / "compose" / "scripts" / "preflight.sh"
PREFLIGHT_PS1 = ROOT / "compose" / "scripts" / "preflight.ps1"
SECTIONS = ["OS", "tools", "docker", "docker running", "disk", "ports", "existing stack", "mediahub", "timezone",
            "uid/gid", "claude cli"]
STUB_DOCKER_SH = """#!/bin/sh
case "$1" in
  --version) echo "Docker version 0.0.0-stub" ;;
  compose) echo "${STUB_COMPOSE:-2.30.1}" ;;
  info) if [ "${STUB_INFO:-1.0 linux}" = FAIL ]; then echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2; exit 1; fi
        echo "${STUB_INFO:-1.0 linux}" ;;
  ps) [ -n "$STUB_PS" ] && printf '%s\\n' "$STUB_PS" ;;
  volume) [ -n "$STUB_VOLUMES" ] && printf '%s\\n' "$STUB_VOLUMES" ;;
esac
exit 0
"""
FAIL_SH = "#!/bin/sh\nexit 1\n"


def _stub(folder: Path, **scripts: str) -> Path:
    """A directory of fake tools (docker, ss, netstat, claude) to put first on PATH. Never the real docker."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in scripts.items():
        f = folder / name
        f.write_bytes(body.encode())
        f.chmod(0o755)
    return folder


def _preflight(shell: list[str], tmp: Path, *args: str, stub: Path | None = None, **env) -> subprocess.CompletedProcess:
    stub = stub or _stub(tmp / "stub", docker=STUB_DOCKER_SH)
    full = {**os.environ, "HOME": str(tmp / "home"), "MEDIAHUB_HOME": str(tmp / "mh"),
            "PATH": str(stub) + os.pathsep + os.environ.get("PATH", "")}
    for k in ("TZ", "STUB_COMPOSE", "STUB_INFO", "STUB_PS", "STUB_VOLUMES"):
        full.pop(k, None)
    full.update(env)
    return subprocess.run([*shell, PREFLIGHT_SH.as_posix(), *args], capture_output=True, text=True, env=full, cwd=tmp)


def _sections(out: str) -> dict[str, str]:
    parts = re.split(r"(?m)^== (.+) ==$", out)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_preflight_scripts_exist_are_clean_and_executable():
    raw = PREFLIGHT_SH.read_bytes()
    assert raw.split(b"\n", 1)[0] == b"#!/bin/sh"
    assert b"\r" not in raw, "CRLF would break the shebang"
    assert not re.search(rb"\[\[|\blocal\b|<\(|\bPIPESTATUS\b|\bsort -V\b|\becho -e\b", raw)
    for pat in NOT_PORTABLE:
        assert not re.search(pat, raw.decode()), pat
    assert PREFLIGHT_PS1.exists()
    if shutil.which("git") and (ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-s", "compose/scripts/preflight.sh"], cwd=ROOT,
                             capture_output=True, text=True).stdout
        assert out.startswith("100755 "), out


def test_preflight_minimum_compose_version_matches_the_playbook():
    assert "MIN_COMPOSE=2.24.4" in PREFLIGHT_SH.read_text() and "[version]'2.24.4'" in PREFLIGHT_PS1.read_text()
    assert "2.24.4" in _read("INSTALL.md")


@pytest.mark.skipif(not SHELLS, reason="no POSIX shell (Git Bash, sh, busybox) available")
@pytest.mark.parametrize("shell", SHELLS, ids=_shell_id)
class TestPreflightSh:
    def test_prints_every_section_in_order_and_exits_0(self, shell, tmp_path):
        r = _preflight(shell, tmp_path, str(tmp_path / "media"), TZ="Europe/London")
        assert r.returncode == 0, r.stderr
        assert list(_sections(r.stdout)) == SECTIONS, r.stdout

    def test_docker_and_compose_version_against_the_minimum(self, shell, tmp_path):
        for ver, verdict in [("2.30.1", "ok"), ("v2.24.4", "ok"), ("2.24.10", "ok"), ("v5.0.1-desktop.1", "ok"),
                             ("2.24.3", "TOO OLD"), ("2.9.9", "TOO OLD"), ("1.29.2", "TOO OLD")]:
            sec = _sections(_preflight(shell, tmp_path, STUB_COMPOSE=ver).stdout)["docker"]
            assert "docker: Docker version 0.0.0-stub" in sec
            assert f"(minimum 2.24.4: {verdict}" in sec, (ver, sec)

    def test_docker_running_and_os_type(self, shell, tmp_path):
        def sec(**e):
            return _sections(_preflight(shell, tmp_path, **e).stdout)["docker running"]
        assert "running: yes (server and OS type: 27.0.1 linux)" in sec(STUB_INFO="27.0.1 linux")
        assert "NOTE the OS type must be linux" in sec(STUB_INFO="27.0.1 windows")
        assert "running: no (Cannot connect to the Docker daemon" in sec(STUB_INFO="FAIL")

    def test_disk_uses_the_given_path_or_its_nearest_parent(self, shell, tmp_path):
        (tmp_path / "media").mkdir()
        here = _sections(_preflight(shell, tmp_path, str(tmp_path / "media")).stdout)["disk"]
        assert "path:" in here and "Filesystem" in here and "does not exist yet" not in here
        gone = _sections(_preflight(shell, tmp_path, str(tmp_path / "nope" / "deeper")).stdout)["disk"]
        assert "does not exist yet" in gone and "Filesystem" in gone
        assert "Filesystem" in _sections(_preflight(shell, tmp_path).stdout)["disk"]   # default: /

    def test_ports_fallback_chain(self, shell, tmp_path):
        ss = ("#!/bin/sh\nprintf 'State Recv-Q Send-Q Local Peer\\nLISTEN 0 4096 127.0.0.1:8989 0.0.0.0:*\\n"
              "LISTEN 0 4096 [::]:22 [::]:*\\nLISTEN 0 4096 *:32400 *:*\\n'\n")
        netstat = ("#!/bin/sh\nprintf 'Active Internet connections\\nProto Recv-Q Send-Q Local Address Foreign Address State\\n"
                   "tcp 0 0 0.0.0.0:7878 0.0.0.0:* LISTEN\\ntcp 0 0 :::80 :::* LISTEN\\n'\n")
        quiet = "#!/bin/sh\nprintf 'State Local\\nLISTEN 0 1 0.0.0.0:22 0.0.0.0:*\\n'\n"
        s1 = _stub(tmp_path / "a", docker=STUB_DOCKER_SH, ss=ss)
        sec = _sections(_preflight(shell, tmp_path, stub=s1).stdout)["ports"]
        assert "checked with: ss" in sec and "in use: 8989 32400 " in sec
        s2 = _stub(tmp_path / "b", docker=STUB_DOCKER_SH, ss=FAIL_SH, netstat=netstat)
        sec = _sections(_preflight(shell, tmp_path, stub=s2).stdout)["ports"]
        assert "checked with: netstat" in sec and "in use: 7878" in sec
        s3 = _stub(tmp_path / "c", docker=STUB_DOCKER_SH, ss=FAIL_SH, netstat=FAIL_SH)
        sec = _sections(_preflight(shell, tmp_path, stub=s3).stdout)["ports"]
        if Path("/proc/net/tcp").exists():
            assert "checked with: /proc/net/tcp" in sec
        else:
            assert "could not check ports" in sec
        s4 = _stub(tmp_path / "d", docker=STUB_DOCKER_SH, ss=quiet)
        assert "ports free" in _sections(_preflight(shell, tmp_path, stub=s4).stdout)["ports"]

    def test_existing_stack_and_mediahub_sections(self, shell, tmp_path):
        ps = "sonarr\tlscr.io/linuxserver/sonarr:4\tUp 2 hours\t8989/tcp\nmyweb\tnginx:1\tUp\t80/tcp"
        sec = _sections(_preflight(shell, tmp_path, STUB_PS=ps).stdout)["existing stack"]
        assert "-- containers" in sec and "sonarr\tlscr.io/linuxserver/sonarr:4" in sec and "nginx" not in sec
        assert "-- services" in sec and "-- processes" in sec
        assert "(none found)" in _sections(_preflight(shell, tmp_path).stdout)["existing stack"]
        down = _sections(_preflight(shell, tmp_path, STUB_INFO="FAIL").stdout)
        assert "docker not usable: this check did not run" in down["existing stack"]
        assert "containers: unknown" in down["mediahub"]
        assert "settings file: none" in _sections(_preflight(shell, tmp_path).stdout)["mediahub"]
        (tmp_path / "mh").mkdir()
        (tmp_path / "mh" / "stack.env").write_text("A=1\n")
        sec = _sections(_preflight(shell, tmp_path, STUB_VOLUMES="mediahub_sonarr-config").stdout)["mediahub"]
        assert "settings file: present" in sec and "volumes: mediahub_sonarr-config" in sec and "containers: none" in sec
        assert "A=1" not in sec

    def test_timezone(self, shell, tmp_path):
        assert "timezone: Europe/London" in _sections(_preflight(shell, tmp_path, TZ="Europe/London").stdout)["timezone"]
        assert "timezone: Europe/Paris" in _sections(_preflight(shell, tmp_path, TZ=":Europe/Paris").stdout)["timezone"]

    def test_uid_gid_and_claude_cli(self, shell, tmp_path):
        r = _preflight(shell, tmp_path)
        assert re.search(r"user: \S+ uid: \d+ gid: \d+", _sections(r.stdout)["uid/gid"])
        stub = _stub(tmp_path / "x", docker=STUB_DOCKER_SH, claude="#!/bin/sh\necho claude stub\n")
        assert "claude: present" in _sections(_preflight(shell, tmp_path, stub=stub).stdout)["claude cli"]
        if not shutil.which("claude"):
            assert "claude: absent" in _sections(r.stdout)["claude cli"]

    def test_usage(self, shell, tmp_path):
        r = _preflight(shell, tmp_path, "a", "b")
        assert r.returncode == 2 and "usage" in r.stderr
        r = _preflight(shell, tmp_path, "--help")
        assert r.returncode == 0 and "usage" in r.stdout

    def test_changes_nothing(self, shell, tmp_path):
        (tmp_path / "home").mkdir()
        (tmp_path / "mh").mkdir()
        _stub(tmp_path / "stub", docker=STUB_DOCKER_SH)
        before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
        _preflight(shell, tmp_path, str(tmp_path / "newdir"))
        assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


STUB_DOCKER_CMD = (
    '@echo off\r\nif "%1"=="--version" (echo Docker version 0.0.0-stub & exit /b 0)\r\n'
    'if "%1"=="compose" (echo 2.20.0-desktop.1 & exit /b 0)\r\n'
    'echo Cannot connect to the Docker daemon 1>&2\r\nexit /b 1\r\n')


@pytest.mark.skipif(not PWSH, reason="no PowerShell available")
def test_preflight_ps1_prints_every_section_and_exits_0(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    if os.name == "nt":
        (stub / "docker.cmd").write_bytes(STUB_DOCKER_CMD.encode())
    else:
        (stub / "docker").write_bytes(b'#!/bin/sh\ncase "$1" in --version) echo Docker version 0.0.0-stub;; '
                                      b'compose) echo 2.20.0;; *) echo "Cannot connect to the Docker daemon" >&2; exit 1;; esac\n')
        (stub / "docker").chmod(0o755)
    env = {**os.environ, "MEDIAHUB_HOME": str(tmp_path / "mh"), "PATH": str(stub) + os.pathsep + os.environ.get("PATH", "")}
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(PREFLIGHT_PS1), str(tmp_path)],
                       capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    sections = _sections(r.stdout.replace("\r\n", "\n"))
    assert list(sections) == SECTIONS, r.stdout
    assert "TOO OLD" in sections["docker"] and "running: no (" in sections["docker running"]
    assert "docker not usable" in sections["existing stack"] and "containers: unknown" in sections["mediahub"]
    assert "settings file: none" in sections["mediahub"]
    assert "claude:" in sections["claude cli"]
