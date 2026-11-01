"""OWNER_NAME templating: no personal name in shipped files, and agent-facing text uses the configured one."""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import pytest

from app import config, notify

ROOT = Path(__file__).parent.parent
PERSONAL_NAME = re.compile(r"\bBart\b")


def test_no_personal_name_in_shipped_files():
    hits = []
    for top in ("app", "skill", "compose", "templates"):
        for path in (ROOT / top).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue  # binary
            text = raw.decode("utf-8", errors="ignore")
            for n, line in enumerate(text.splitlines(), 1):
                if PERSONAL_NAME.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert not hits, "personal name found in: " + ", ".join(hits)


def test_personalise_default_owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_NAME", "the user")
    assert config.owner() == "the user"
    assert config.owner(True) == "The user"
    assert config.personalise("Ask {owner}. {Owner}'s call.") == "Ask the user. The user's call."


def test_personalise_named_owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_NAME", "Alex")
    assert config.personalise("Ask {owner}. {Owner}'s call.") == "Ask Alex. Alex's call."


def test_owner_name_env_parsing(monkeypatch):
    monkeypatch.setenv("OWNER_NAME", "  Sam  ")
    try:
        importlib.reload(config)
        assert config.OWNER_NAME == "Sam"
        monkeypatch.setenv("OWNER_NAME", "   ")
        importlib.reload(config)
        assert config.OWNER_NAME == "the user"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_personalise_leaves_other_braces_alone(monkeypatch):
    monkeypatch.setattr(config, "OWNER_NAME", "Alex")
    assert config.personalise('{"a": 1} {title} {owner}') == '{"a": 1} {title} Alex'


def test_alert_render_uses_owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_NAME", "Alex")
    msg = notify.render([notify.Alert("weekly_cleanup", "Weekly cleanup", "x")], None)
    code = notify.render_code("A1", "Some Show", "123456", 10)
    for text in (msg, code):
        assert "Alex" in text
        assert "{owner}" not in text and "{Owner}" not in text
    assert "delete only the ones they pick" in msg


@pytest.fixture
def ms_for_alex(monkeypatch):
    """app.mcp_server re-imported with OWNER_NAME=Alex; restored afterwards."""
    from app import mcp_server
    monkeypatch.setattr(config, "OWNER_NAME", "Alex")
    importlib.reload(mcp_server)
    yield mcp_server
    monkeypatch.undo()
    importlib.reload(mcp_server)


def _descriptions(ms) -> dict[str, str]:
    return {t.name: t.description or "" for t in asyncio.run(ms.mcp.list_tools())}


def test_mcp_text_renders_configured_name(ms_for_alex):
    ms = ms_for_alex
    assert "Alex's home media stack" in ms.INSTRUCTIONS
    descs = _descriptions(ms)
    assert len(descs) >= 18
    assert "Alex" in descs["taste"] and descs["taste"].startswith("Alex's taste ledger")
    assert "Alex" in descs["delete_any"]
    for text in [ms.INSTRUCTIONS, *descs.values()]:
        assert "{owner}" not in text and "{Owner}" not in text
        assert not PERSONAL_NAME.search(text)


def test_mcp_text_defaults_to_the_user(monkeypatch):
    from app import mcp_server as ms
    monkeypatch.setattr(config, "OWNER_NAME", "the user")
    importlib.reload(ms)
    try:
        assert "the user's home media stack" in ms.INSTRUCTIONS
        assert _descriptions(ms)["taste"].startswith("The user's taste ledger")
    finally:
        monkeypatch.undo()
        importlib.reload(ms)
