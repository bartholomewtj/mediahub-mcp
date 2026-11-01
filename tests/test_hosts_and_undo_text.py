"""MCP_ALLOWED_HOSTS reaches the transport security settings; the by-hand undo text follows
MEDIA_ROOT (host path style) and the library folder names recorded with each delete."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.requests import Request

from app import config, mcp_server, tools

BS = chr(92)


def win(text: str) -> str:
    """The same text with Windows separators (tests are written with slashes)."""
    return text.replace("/", BS)


# ------------------------------------------------------------------ MCP_ALLOWED_HOSTS

def test_default_allow_list_is_localhost_only(monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", [])
    s = mcp_server.transport_security()
    assert s.enable_dns_rebinding_protection
    assert s.allowed_hosts == ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    assert s.allowed_origins == ["http://127.0.0.1:*", "http://localhost:*"]


def test_extra_hosts_are_appended_to_hosts_and_origins(monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["hub.example.net", "hub.tail1234.ts.net:*"])
    s = mcp_server.transport_security()
    assert s.allowed_hosts[:4] == ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    assert s.allowed_hosts[4:] == ["hub.example.net", "hub.tail1234.ts.net:*"]
    assert "http://localhost:*" in s.allowed_origins
    for o in ("http://hub.example.net", "https://hub.example.net",
              "http://hub.tail1234.ts.net:*", "https://hub.tail1234.ts.net:*"):
        assert o in s.allowed_origins


def _status(settings, host, origin=None):
    from mcp.server.transport_security import TransportSecurityMiddleware
    headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
    req = Request({"type": "http", "method": "GET", "path": "/mcp", "headers": headers})
    res = asyncio.run(TransportSecurityMiddleware(settings).validate_request(req))
    return res.status_code if res else 200


def test_proxy_host_is_421_until_listed(monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", [])
    assert _status(mcp_server.transport_security(), "hub.example.net") == 421
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["hub.example.net"])
    s = mcp_server.transport_security()
    assert _status(s, "hub.example.net", "https://hub.example.net") == 200
    assert _status(s, "localhost:8000") == 200
    assert _status(s, "evil.example.org") == 421
    assert _status(s, "hub.example.net", "https://evil.example.org") == 403


def test_config_parses_the_comma_list(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " a.example.net , b.example.net:* ,, ")
    assert config._names("MCP_ALLOWED_HOSTS") == ["a.example.net", "b.example.net:*"]


# ------------------------------------------------------------------ undo text

@pytest.fixture
def log_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(config, "REQUESTS_PATH", Path(d) / "requests.json")
        yield Path(d)


def _row(**kw):
    return {"at": datetime.now(timezone.utc).isoformat(), "kind": "delete", "mode": "delete_any",
            "title": "t", "summary": "s", **kw}


SHOW = _row(title_name="Copper Valley", app="sonarr", tmdb_id=910001, seasons=[2],
            folder="/media/Shows/Copper Valley")
FILM = _row(title_name="Glass Orchard", app="radarr", tmdb_id=910002, folder="/media/Films/Glass Orchard (2017)")


def steps(row):
    return " ".join(tools._undo_steps(row, True, "2030-01-08"))


@pytest.mark.parametrize("root, shown", [("C:/Media", "C:/Media"), (win("C:/Media"), "C:/Media"),
                                         (win("C:/Media/"), "C:/Media"), ("D:/Media/", "D:/Media")])
def test_windows_media_root_gets_backslashes_and_the_recorded_folder(monkeypatch, log_dir, root, shown):
    monkeypatch.setattr(config, "MEDIA_ROOT_HOST", root)
    text = steps(SHOW)
    assert win(f"in {shown}/.recyclebin.") in text
    assert win(f"back to {shown}/Shows/Copper Valley/Season NN (one folder per season: 02)") in text
    assert "/" not in text.split("Move them back to")[1].split("(one folder")[0]
    assert win(f"back to {shown}/Films/<the movie's folder>") in steps(FILM)


def test_owner_output_is_unchanged(monkeypatch, log_dir):
    """His MEDIA_ROOT is C:/Media and his TV folder is Shows: the text matches what it said before."""
    monkeypatch.setattr(config, "MEDIA_ROOT_HOST", "C:/Media")
    assert tools._undo_steps(SHOW, True, "2030-01-08")[:2] == [
        win("1. Before 2030-01-08, find the season files for Copper Valley in C:/Media/.recyclebin."),
        win("2. Move them back to C:/Media/Shows/Copper Valley/Season NN (one folder per season: 02).")]
    film = dict(FILM, folder="/media/Movies/Glass Orchard (2017)")
    (log_dir / "deletions.json").write_text(json.dumps([SHOW, film]), encoding="utf-8")
    assert asyncio.run(tools.deleted())["undo"] == (
        "Until the undo date the files are in " + win("C:/Media/.recyclebin/<title>") + ". Move that folder back into "
        + win("C:/Media/Shows") + " (or " + win("C:/Media/Movies") + "), then request the title again with the "
        "same seasons; Sonarr/Radarr pick up the files already on disk.")


def test_linux_media_root_gets_slashes(monkeypatch, log_dir):
    monkeypatch.setattr(config, "MEDIA_ROOT_HOST", "/srv/media/")
    text = steps(dict(SHOW, folder="/media/TV/Copper Valley"))
    assert "in /srv/media/.recyclebin." in text
    assert "back to /srv/media/TV/Copper Valley/Season NN (one folder per season: 02)" in text
    assert BS not in text
    assert "back to /srv/media/Films/<the movie's folder>" in steps(FILM)
    (log_dir / "deletions.json").write_text(json.dumps([dict(SHOW, folder="/media/TV/Copper Valley")]), encoding="utf-8")
    undo = asyncio.run(tools.deleted())["undo"]
    assert "/srv/media/.recyclebin/<title>" in undo and "/srv/media/TV (or /srv/media/Movies)" in undo
    assert BS not in undo


def test_unset_media_root_has_a_placeholder(monkeypatch, log_dir):
    monkeypatch.setattr(config, "MEDIA_ROOT_HOST", "<your media folder>")
    assert "in <your media folder>/.recyclebin." in steps(SHOW)


def test_library_folder_comes_from_the_record_then_siblings_then_default(monkeypatch, log_dir):
    monkeypatch.setattr(config, "MEDIA_ROOT_HOST", "/m")
    from_files = _row(title_name="X", app="sonarr", seasons=[1],
                      files=[{"path": "/media/Telly/X/Season 01/e.mkv"}])
    assert "back to /m/Telly/X/" in steps(from_files)
    old = _row(title_name="Old", app="sonarr", seasons=[1])           # nothing recorded
    assert "back to /m/TV/Old/" in steps(old)                         # setup's default
    (log_dir / "deletions.json").write_text(json.dumps([SHOW]), encoding="utf-8")
    assert "back to /m/Shows/Old/" in steps(old)                      # same app's other delete
    assert "back to /m/Movies/<" in steps(dict(old, app="radarr"))    # not the TV one
