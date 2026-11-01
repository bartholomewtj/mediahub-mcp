"""Tests never depend on the machine's alert settings (env vars or ~/.mediahub/secrets.env)."""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def no_alert_channel(monkeypatch):
    for name in ("NOTIFIER", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_URL", "WEBHOOK_KEY"):
        monkeypatch.setattr(config, name, "")
