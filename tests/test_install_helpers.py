"""Installer helpers: python -m app.setup allowed-tools | bot-prompt | config-diff | remap-roots | version."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from app import config, setup, setup_tools

ROOT = Path(__file__).parent.parent


# ------------------------------------------------------------------ allowed-tools

def _owner_prompt_tools() -> str:
    found = list((ROOT / "examples").glob("*/media-bot-prompt.md"))
    if not found:
        pytest.skip("the owner's example prompt is not in this tree")
    text = found[0].read_text(encoding="utf-8")
    m = re.search(r"^\s*(mcp__mediahub__\S+)$", text, re.M)
    assert m, "no allowed-tools string in the owner's prompt"
    return m.group(1)


def test_allowed_tools_is_every_registered_tool_in_registration_order():
    import asyncio
    from app import mcp_server
    names = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    got = setup_tools.allowed_tools()
    assert got == ",".join(f"mcp__mediahub__{n}" for n in names)
    assert len(names) == len(set(names)) == 19
    assert " " not in got and not got.endswith(",")


def test_owner_prompt_allows_exactly_the_registered_tools():
    # Same tools. The owner's prompt lists them in a different (grouped) order than they are registered
    # in; claude --allowedTools does not care about order, so this compares as sets.
    doc, live = _owner_prompt_tools().split(","), setup_tools.allowed_tools().split(",")
    assert len(doc) == len(set(doc))
    assert set(doc) == set(live)


def test_allowed_tools_cli(capsys):
    assert setup.main(["allowed-tools"]) == 0
    assert capsys.readouterr().out.strip() == setup_tools.allowed_tools()


# ------------------------------------------------------------------ bot-prompt

CHANNELS = ["none", "telegram", "webhook"]
MODES = ["code", "no_code", "disabled"]


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_bot_prompt_renders_completely(channel, mode, shell):
    text = setup_tools.render_bot_prompt("/srv/mediahub", shell, channel, mode, owner="Alex", disk_guard_gb=50.0)
    assert "{{" not in text and "}}" not in text
    assert setup_tools.allowed_tools() in text
    assert "Alex" in text
    assert "50 GB guard" in text
    assert ("Set-Location /srv/mediahub" if shell == "powershell" else "cd /srv/mediahub") in text
    assert ("in PowerShell" if shell == "powershell" else "in bash") in text
    assert "alert relay" in text if channel == "webhook" else "alert relay" not in text
    # The safety content survives every combination.
    for must in ("--permission-mode dontAsk", "untrusted", "never look for it", "fresh session",
                 "ONLY for a title Alex named", "Never request, remove or delete anything from an alert alone"):
        assert must in text, must


def test_bot_prompt_alerts_and_delete_wording():
    def render(channel, mode):
        return setup_tools.render_bot_prompt("/x", "bash", channel, mode, owner="Alex", tools="mcp__mediahub__status")
    assert "sends no alerts" in render("none", "disabled")
    assert "Alerts arrive on Telegram, not through you" in render("telegram", "code")
    assert 'sends a "MediaHub alert" to Alex\'s alert relay, which forwards it to you' in render("webhook", "no_code")
    assert "No-code mode" in render("none", "no_code")
    assert "code_sent" in render("telegram", "code") and "No-code mode" not in render("telegram", "code")
    dis = render("none", "disabled")
    assert "is disabled on this install" in dis and "DELETE_ANY_REQUIRE_CODE=false" in dis


def test_bot_prompt_quotes_a_path_with_spaces_and_rejects_bad_input():
    text = setup_tools.render_bot_prompt(r"C:\My Files\mediahub", "powershell", "none", "code", tools="t")
    assert 'Set-Location "C:\\My Files\\mediahub"' in text
    with pytest.raises(ValueError):
        setup_tools.render_bot_prompt("/x", "zsh", "none", "code", tools="t")
    with pytest.raises(ValueError):
        setup_tools.render_bot_prompt("/x", "bash", "carrier-pigeon", "code", tools="t")
    with pytest.raises(ValueError, match="unknown placeholder"):
        setup_tools.render_bot_prompt("/x", "bash", "none", "code", tools="t", template="{{NOPE}}")


def test_bot_prompt_fills_once_so_values_are_not_rescanned():
    out = setup_tools.render_bot_prompt("/x", "bash", "none", "code", owner="{{ALERTS}}", tools="t",
                                        template="{{OWNER_NAME}}")
    assert out == "{{ALERTS}}"


def test_template_uses_only_known_placeholders():
    text = (ROOT / "templates" / "bot-prompt.md").read_text(encoding="utf-8")
    found = set(setup_tools.PLACEHOLDER.findall(text))
    assert found == {"OWNER_NAME", "REPO_DIR", "SHELL", "CD_COMMAND", "ALLOWED_TOOLS", "DISK_GUARD_GB",
                     "DELETE_ANY_MODE", "ALERTS"}


@pytest.mark.parametrize("channel, tokens, expect", [
    ("", {}, "sends no alerts"),
    ("telegram", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, "on Telegram"),
])
def test_bot_prompt_cli_reads_the_config(monkeypatch, capsys, channel, tokens, expect):
    monkeypatch.setattr(config, "OWNER_NAME", "Alex")
    monkeypatch.setattr(config, "DISK_GUARD_GB", 80.0)
    monkeypatch.setattr(config, "NOTIFIER", channel)
    for k, v in tokens.items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "DELETE_ANY_REQUIRE_CODE", True)
    assert setup.main(["bot-prompt", "--repo-dir", "/srv/mh", "--shell", "bash"]) == 0
    out = capsys.readouterr().out
    assert expect in out and "80 GB guard" in out and "Alex" in out and "{{" not in out
    assert ("code_sent" in out) if tokens else ("is disabled on this install" in out)


def test_bot_prompt_cli_no_code_mode(monkeypatch, capsys):
    monkeypatch.setattr(config, "DELETE_ANY_REQUIRE_CODE", False)
    setup.main(["bot-prompt", "--repo-dir", "C:\\mh", "--shell", "powershell"])
    out = capsys.readouterr().out
    assert "No-code mode" in out and "Set-Location C:\\mh" in out


def test_bot_prompt_cli_requires_its_options():
    with pytest.raises(SystemExit):
        setup.main(["bot-prompt", "--shell", "bash"])
    with pytest.raises(SystemExit):
        setup.main(["bot-prompt", "--repo-dir", "/x", "--shell", "fish"])


# ------------------------------------------------------------------ Dockerfile ships what the helpers read

def test_dockerfile_copies_the_files_the_helpers_read():
    text = (ROOT / "compose" / "dashboard" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^COPY templates \./templates\s*$", text, re.M)
    assert re.search(r"^COPY VERSION \./VERSION\s*$", text, re.M)
    assert re.search(r"^COPY compose/stack\.env\.example compose/secrets\.env\.example \./compose-examples/\s*$", text, re.M)
    assert re.search(r"^COPY app \./app\s*$", text, re.M)
    assert not (ROOT / ".dockerignore").exists()  # a .dockerignore would have to let these through
    # WORKDIR is /opt/mediahub, which is config.ROOT inside the image.
    assert "WORKDIR /opt/mediahub" in text


# ------------------------------------------------------------------ config-diff

def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_config_diff_new_optional_unknown_and_never_values(tmp_path, capsys):
    ex, home = tmp_path / "ex", tmp_path / "home"
    ex.mkdir(), home.mkdir()
    _write(ex / "stack.env.example",
           "# Write values as KEY=value\n# Channel: none | telegram\nMEDIA_ROOT=/srv/media\nTZ=Etc/UTC\n"
           "NEWSETTING=1\n# MEDIAHUB_VPN=none\n# MEDIAHUB_PORT_BIND=127.0.0.1:8000\n")
    _write(ex / "secrets.env.example", "MCP_TOKEN=\nPLEX_TOKEN=\n# EXTRA_KEY=x\n")
    _write(home / "stack.env", "MEDIA_ROOT=/mnt/m\nTZ=Europe/Paris\nMEDIAHUB_VPN=gluetun\nOLD_SETTING=1\n  export SPACED = 2\n")
    _write(home / "secrets.env", "MCP_TOKEN=SUPERSECRETVALUE\n# PLEX_TOKEN=commented-live-does-not-count\nOWN_KEY=SECRETTWO\n")
    assert setup_tools.config_diff(home, ex) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "NEW stack.env NEWSETTING",
        "OPTIONAL stack.env MEDIAHUB_PORT_BIND",
        "UNKNOWN stack.env OLD_SETTING",
        "UNKNOWN stack.env SPACED",
        "NEW secrets.env PLEX_TOKEN",
        "OPTIONAL secrets.env EXTRA_KEY",
        "UNKNOWN secrets.env OWN_KEY",
    ]
    assert "SUPERSECRETVALUE" not in "".join(lines) and "SECRETTWO" not in "".join(lines)


def test_config_diff_in_sync_and_missing_live_file(tmp_path, capsys):
    ex, home = tmp_path / "ex", tmp_path / "home"
    ex.mkdir(), home.mkdir()
    _write(ex / "stack.env.example", "A=1\n# B=2\n")
    _write(ex / "secrets.env.example", "S=\n")
    _write(home / "stack.env", "A=1\nB=2\n")  # an optional key that is set is fine
    assert setup_tools.config_diff(home, ex) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "OK   stack.env"
    assert lines[1].startswith("NOTE secrets.env") and lines[2] == "NEW secrets.env S"


def test_config_diff_against_the_shipped_examples(tmp_path, capsys):
    _write(tmp_path / "stack.env", "MEDIA_ROOT=/m\nMEDIAHUB_STACK=full\n")
    _write(tmp_path / "secrets.env", "")
    setup.main(["config-diff", "--home", str(tmp_path), "--examples", str(ROOT / "compose")])
    out = capsys.readouterr().out.splitlines()
    assert "NEW stack.env DISK_GUARD_GB" in out and "NEW secrets.env MCP_TOKEN" in out
    assert "OPTIONAL stack.env MEDIAHUB_VPN" in out
    assert not any(l.startswith("UNKNOWN") for l in out)
    assert all(re.fullmatch(r"(NEW|OPTIONAL|UNKNOWN) (stack|secrets)\.env [A-Z][A-Z0-9_]*", l) for l in out)
    assert not any(l.endswith(" KEY") for l in out)  # prose like "KEY=value" in comments is not a setting


# ------------------------------------------------------------------ version / licence / changelog

def test_version_licence_changelog(capsys):
    assert setup.main(["version"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", printed)
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert lic.startswith("MIT License") and "Copyright (c) 2026 bartholomewtj" in lic
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.search(rf"^## {re.escape(version)} — (unreleased|\d{{4}}-\d{{2}}-\d{{2}})$",
                     (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)


# ------------------------------------------------------------------ remap-roots

class FakeArr:
    """Sonarr/Radarr as far as remap-roots needs: list, root folders, bulk editor. The editor follows the
    app's real behaviour (moveFiles false): new Path = rootFolderPath + the path relative to the best
    registered root folder of the old path, else its last folder name."""

    def __init__(self, app: str, roots: list[str], items: dict[int, tuple[str, str]], editor_works: bool = True):
        self.app, self.roots, self.items = app, list(roots), dict(items)
        self.editor_works = editor_works
        self.puts: list[dict] = []
        self.posts: list[dict] = []
        self.listing = "series" if app == "sonarr" else "movie"

    def best_root(self, path: str) -> str:
        cands = [r for r in self.roots if path.startswith(r + "/")]
        return max(cands, key=len) if cands else path.rsplit("/", 1)[0]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        if path == f"/api/v3/{self.listing}" and m == "GET":
            return httpx.Response(200, json=[
                {"id": i, "title": t, "path": p, "rootFolderPath": self.best_root(p)} for i, (t, p) in self.items.items()])
        if path == "/api/v3/rootfolder" and m == "GET":
            return httpx.Response(200, json=[{"id": n, "path": r} for n, r in enumerate(self.roots)])
        if path == "/api/v3/rootfolder" and m == "POST":
            self.posts.append(body)
            self.roots.append(body["path"])
            return httpx.Response(201, json=body)
        if path == f"/api/v3/{self.listing}/editor" and m == "PUT":
            self.puts.append(body)
            assert body["moveFiles"] is False
            ids = body["seriesIds" if self.app == "sonarr" else "movieIds"]
            if self.editor_works:
                for i in ids:
                    title, old = self.items[i]
                    rel = old[len(self.best_root(old)) + 1:]
                    self.items[i] = (title, body["rootFolderPath"] + "/" + rel)
            return httpx.Response(202 if self.app == "sonarr" else 200, json=[])
        raise AssertionError((m, path))


@pytest.fixture
def arr_urls(monkeypatch):
    for name, v in {"SONARR_URL": "http://sonarr.test", "RADARR_URL": "http://radarr.test",
                    "SONARR_API_KEY": "s", "RADARR_API_KEY": "r"}.items():
        monkeypatch.setattr(config, name, v)


def _remap(fake: FakeArr, apply: bool, old="/tv", new="/media/TV", capsys=None):
    lines: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(fake.handler)) as c:
        rc = setup_tools.remap_roots(fake.app, old, new, apply, client=c, out=lines.append)
    return rc, lines


SONARR_ITEMS = {1: ("Alpha", "/tv/Alpha"), 2: ("Beta", "/tv/Beta"), 3: ("Anime One", "/tv/Anime/Anime One"),
                4: ("Elsewhere", "/other/Elsewhere"), 5: ("Lookalike", "/tvshows/Lookalike")}


def test_remap_dry_run_changes_nothing(arr_urls):
    fake = FakeArr("sonarr", ["/tv"], SONARR_ITEMS)
    rc, lines = _remap(fake, apply=False)
    assert rc == 0
    assert fake.puts == [] and fake.posts == [] and fake.items == SONARR_ITEMS
    assert lines[0] == "sonarr: 3 series under /tv (would become /media/TV)"
    assert "  Anime One: /tv/Anime/Anime One -> /media/TV/Anime/Anime One" in lines
    assert "  would add root folder /media/TV" in lines
    assert "Dry run" in lines[-1]
    assert not any("Elsewhere" in l or "Lookalike" in l for l in lines)  # /tvshows is not under /tv


def test_remap_dry_run_lists_at_most_ten_titles(arr_urls):
    items = {i: (f"Show {i}", f"/tv/Show {i}") for i in range(1, 26)}
    rc, lines = _remap(FakeArr("sonarr", ["/tv"], items), apply=False)
    assert lines[0].startswith("sonarr: 25 series")
    assert len([l for l in lines if l.startswith("  Show ")]) == 10 and "  ... and 15 more" in lines


def test_remap_apply_sonarr_keeps_subfolders_and_adds_the_root(arr_urls):
    fake = FakeArr("sonarr", ["/tv"], SONARR_ITEMS)
    rc, lines = _remap(fake, apply=True)
    assert rc == 0, lines
    assert fake.posts == [{"path": "/media/TV"}]
    assert fake.items[1][1] == "/media/TV/Alpha" and fake.items[2][1] == "/media/TV/Beta"
    assert fake.items[3][1] == "/media/TV/Anime/Anime One"   # subfolder kept
    assert fake.items[4] == SONARR_ITEMS[4] and fake.items[5] == SONARR_ITEMS[5]
    # one canary first, then the rest of the batch; always moveFiles false, only the editor fields
    assert [(p["seriesIds"], p["rootFolderPath"]) for p in fake.puts] == [([1], "/media/TV"), ([2, 3], "/media/TV")]
    assert all(set(p) == {"seriesIds", "rootFolderPath", "moveFiles"} for p in fake.puts)
    # run again: nothing left under /tv, nothing to do
    rc, lines = _remap(fake, apply=True)
    assert rc == 0 and lines == ["sonarr: 0 series under /tv (would become /media/TV)"] and len(fake.puts) == 2


def test_remap_batches_per_root_folder_when_the_subfolder_is_its_own_root(arr_urls):
    fake = FakeArr("sonarr", ["/tv", "/tv/Anime"], SONARR_ITEMS)
    rc, lines = _remap(fake, apply=True)
    assert rc == 0, lines
    assert {p["rootFolderPath"] for p in fake.puts} == {"/media/TV", "/media/TV/Anime"}
    assert sorted(r["path"] for r in fake.posts) == ["/media/TV", "/media/TV/Anime"]
    assert fake.items[3][1] == "/media/TV/Anime/Anime One"


def test_remap_does_not_add_a_root_that_exists(arr_urls):
    fake = FakeArr("sonarr", ["/tv", "/media/TV"], {1: ("Alpha", "/tv/Alpha")})
    rc, lines = _remap(fake, apply=True)
    assert rc == 0 and fake.posts == [] and fake.items[1][1] == "/media/TV/Alpha"


def test_remap_radarr_uses_the_movie_editor(arr_urls):
    fake = FakeArr("radarr", ["/movies"], {1: ("Film", "/movies/Film (2020)"), 2: ("Other", "/x/Other")})
    rc, lines = _remap(fake, apply=True, old="/movies/", new="/media/Movies/")
    assert rc == 0, lines
    assert fake.puts == [{"movieIds": [1], "rootFolderPath": "/media/Movies", "moveFiles": False}]
    assert fake.items[1][1] == "/media/Movies/Film (2020)" and fake.items[2][1] == "/x/Other"
    assert lines[0] == "radarr: 1 movies under /movies (would become /media/Movies)"


def test_remap_stops_when_the_app_does_not_produce_the_expected_path(arr_urls):
    fake = FakeArr("sonarr", ["/tv"], SONARR_ITEMS, editor_works=False)
    rc, lines = _remap(fake, apply=True)
    assert rc == 1 and len(fake.puts) == 1  # the canary only; the rest never went in
    assert "did not get the expected path" in lines[-1] and "Nothing else was changed" in lines[-1]


def test_remap_skips_items_whose_root_folder_is_not_under_from(arr_urls):
    # --from is deeper than the registered root: the editor can only re-root, so these are reported, not touched.
    fake = FakeArr("sonarr", ["/tv"], {1: ("Alpha", "/tv/Anime/Alpha")})
    rc, lines = _remap(fake, apply=True, old="/tv/Anime", new="/media/Anime")
    assert rc == 0 and fake.puts == [] and fake.posts == []
    assert "1 skipped" in lines[0] and any(l.startswith("  SKIP Alpha") for l in lines)


def test_remap_reports_a_refused_root_folder(arr_urls):
    fake = FakeArr("sonarr", ["/tv"], {1: ("Alpha", "/tv/Alpha")})
    orig = fake.handler

    def refuse(req):
        if req.method == "POST":
            return httpx.Response(400, json=[{"errorMessage": "Folder '/media/TV' does not exist"}])
        return orig(req)
    with httpx.Client(transport=httpx.MockTransport(refuse)) as c:
        lines: list[str] = []
        rc = setup_tools.remap_roots("sonarr", "/tv", "/media/TV", True, client=c, out=lines.append)
    assert rc == 1 and "does not exist" in lines[-1] and fake.puts == []


def test_remap_cli_validates_arguments(arr_urls, capsys):
    with pytest.raises(SystemExit):
        setup.main(["remap-roots", "--app", "plex", "--from", "/a", "--to", "/b"])
    with pytest.raises(SystemExit):
        setup.main(["remap-roots", "--app", "sonarr", "--from", "/a"])
    assert setup.main(["remap-roots", "--app", "sonarr", "--from", "tv", "--to", "/b"]) == 1
    assert "absolute container paths" in capsys.readouterr().out
