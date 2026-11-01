"""Alert channels (none / telegram / webhook), their safe defaults, and the delete_any gate."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import checks, config, notify, tools

TOKEN = "123456:SECRET-token_ABC"
CHAT = "-100987654321"
HOOK_URL = "https://relay.example.invalid/hook"
HOOK_KEY = "hook-key-xyz"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cfg(monkeypatch):
    """cfg(NOTIFIER=..., TELEGRAM_BOT_TOKEN=...) sets config values (the rest stay blank)."""
    def setter(**values):
        for k, v in values.items():
            monkeypatch.setattr(config, k, v)
    return setter


@pytest.fixture
def telegram(cfg):
    cfg(NOTIFIER="telegram", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)


@pytest.fixture
def webhook(cfg):
    cfg(NOTIFIER="webhook", WEBHOOK_URL=HOOK_URL, WEBHOOK_KEY=HOOK_KEY)


@pytest.fixture(autouse=True)
def fresh_budgets():
    notify._sent.clear()
    notify._code_sent.clear()


class FakeClient:
    """Stands in for httpx.AsyncClient; records posts, answers `status` or raises `error`."""
    calls: list[dict] = []
    status = 200
    error: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls.append({"url": url, "json": json, "headers": headers})
        if type(self).error:
            raise type(self).error
        return httpx.Response(type(self).status, request=httpx.Request("POST", url))


@pytest.fixture
def http(monkeypatch):
    FakeClient.calls = []
    FakeClient.status = 200
    FakeClient.error = None
    monkeypatch.setattr(notify.httpx, "AsyncClient", FakeClient)
    return FakeClient


ALERT = notify.Alert("stuck_download", "Show S01E02", "Still stuck after 2 swaps.")


# --- channel selection -------------------------------------------------------

@pytest.mark.parametrize("env,expected,on", [
    ({}, "none", False),
    ({"WEBHOOK_URL": HOOK_URL, "WEBHOOK_KEY": HOOK_KEY}, "webhook", True),
    ({"WEBHOOK_URL": HOOK_URL}, "none", False),                       # a key is needed too
    ({"WEBHOOK_KEY": HOOK_KEY}, "none", False),
    ({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}, "telegram", True),
    ({"TELEGRAM_BOT_TOKEN": TOKEN}, "none", False),                   # chat id needed too
    ({"TELEGRAM_CHAT_ID": CHAT}, "none", False),
    # both configured and NOTIFIER unset: webhook wins (keeps an existing install unchanged)
    ({"WEBHOOK_URL": HOOK_URL, "WEBHOOK_KEY": HOOK_KEY, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT},
     "webhook", True),
    # explicit choice beats the auto rule
    ({"NOTIFIER": "telegram", "WEBHOOK_URL": HOOK_URL, "WEBHOOK_KEY": HOOK_KEY,
      "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}, "telegram", True),
    ({"NOTIFIER": "none", "WEBHOOK_URL": HOOK_URL, "WEBHOOK_KEY": HOOK_KEY}, "none", False),
    ({"NOTIFIER": "webhook", "WEBHOOK_URL": HOOK_URL, "WEBHOOK_KEY": HOOK_KEY}, "webhook", True),
    # explicit but without credentials: that channel, not enabled
    ({"NOTIFIER": "telegram"}, "telegram", False),
    ({"NOTIFIER": "webhook", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}, "webhook", False),
    # unknown value: nothing is sent
    ({"NOTIFIER": "carrier-pigeon", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}, "none", False),
])
def test_channel_selection(cfg, env, expected, on):
    cfg(**env)
    assert notify.channel() == expected
    assert notify.enabled() is on


def test_config_reads_webhook_settings(monkeypatch):
    import importlib
    for name in ("WEBHOOK_URL", "WEBHOOK_KEY", "WEBHOOK_EXTRA_JSON", "NOTIFIER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEDIAHUB_SECRETS", "/nonexistent/secrets.env")
    try:
        importlib.reload(config)
        assert (config.WEBHOOK_URL, config.WEBHOOK_KEY, config.WEBHOOK_EXTRA_JSON) == ("", "", "")
        monkeypatch.setenv("WEBHOOK_URL", "http://new")
        monkeypatch.setenv("WEBHOOK_KEY", "newkey")
        monkeypatch.setenv("WEBHOOK_EXTRA_JSON", '{"a": 1}')
        monkeypatch.setenv("NOTIFIER", " Telegram ")
        importlib.reload(config)
        assert (config.WEBHOOK_URL, config.WEBHOOK_KEY) == ("http://new", "newkey")
        assert config.WEBHOOK_EXTRA_JSON == '{"a": 1}'
        assert config.NOTIFIER == "telegram"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_code_is_required_by_default():
    import importlib
    import os
    with patch.dict(os.environ):
        os.environ.pop("DELETE_ANY_REQUIRE_CODE", None)
        try:
            importlib.reload(config)
            assert config.DELETE_ANY_REQUIRE_CODE is True
        finally:
            importlib.reload(config)


# --- none --------------------------------------------------------------------

def test_none_sends_nothing(http, cfg):
    cfg(NOTIFIER="none", WEBHOOK_URL=HOOK_URL, WEBHOOK_KEY=HOOK_KEY)
    assert notify.enabled() is False
    assert run(notify.send([ALERT], "status")) is False
    assert run(notify.send_code("A1", "Delete X", "123456", 10)) is False
    assert http.calls == []


def test_no_config_at_all_sends_nothing(http):
    assert notify.channel() == "none"
    assert run(notify.send([ALERT])) is False
    assert http.calls == []


# --- telegram ----------------------------------------------------------------

def test_telegram_payload_shape(http, telegram):
    assert run(notify.send([ALERT], "1 download(s) active")) is True
    (call,) = http.calls
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert set(call["json"]) == {"chat_id", "text", "disable_web_page_preview"}   # in particular: no parse_mode
    assert call["json"]["chat_id"] == CHAT
    assert call["json"]["disable_web_page_preview"] is True
    assert not call["headers"]
    text = call["json"]["text"]
    assert text.splitlines()[0] == "MediaHub"
    assert "- Show S01E02: Still stuck after 2 swaps." in text
    assert 'Suggestion: Ask your media agent: "look at the stuck download for Show S01E02".' in text
    assert "Stack now: 1 download(s) active" in text


def test_telegram_alert_is_written_for_the_owner(telegram):
    text = notify.render([ALERT, notify.Alert("weekly_cleanup", "Weekly cleanup", "Could free 40 GB.")], None)
    assert "Forward" not in text and "relay" not in text.lower()
    assert "Never request, remove or delete" not in text
    assert "{owner}" not in text and "{Owner}" not in text
    assert "Show the user these" not in text          # the relay-addressed weekly wording is not used


def test_telegram_titles_still_go_through_safe_title(telegram):
    text = notify.render([notify.Alert("stuck_download", "Evil <b>bold</b> [x](http://e.com) `c` {owner}", "d")], None)
    for bad in ("<", ">", "[", "]", "`", "{", "}"):
        assert bad not in text.replace("MediaHub", "")


def test_telegram_code_message(http, telegram):
    assert run(notify.send_code("A7", "Delete Copper Valley season(s) 2 (20.0 GB)", "482913", 10)) is True
    text = http.calls[0]["json"]["text"]
    assert "parse_mode" not in http.calls[0]["json"]
    for part in ("A7", "Delete Copper Valley season(s) 2 (20.0 GB)", "482913", "valid 10 minutes",
                 "Only type this into your own message to your media bot if you asked for this delete. "
                 "If you didn't, ignore it."):
        assert part in text
    assert "Do NOT forward" not in text


def test_telegram_truncates_to_the_cap(http, telegram):
    alerts = [notify.Alert("stuck_download", "T" * 80 + str(i), "d" * 300) for i in range(40)]
    assert run(notify.send(alerts, "status")) is True
    text = http.calls[0]["json"]["text"]
    assert len(text) <= notify.TELEGRAM_MAX_CHARS
    assert text.startswith("MediaHub")
    assert text.endswith("[...]")
    assert len(notify.render(alerts, None, via="telegram")) <= notify.TELEGRAM_MAX_CHARS


def test_telegram_truncation_counts_utf16_units(telegram):
    # astral characters are two UTF-16 units each; \w matches these letters
    alerts = [notify.Alert("stuck_download", "\U0001D4D0" * 40 + str(i), "\U0001D4D0" * 100) for i in range(20)]
    text = notify.render(alerts, None)
    assert len(text.encode("utf-16-le")) // 2 <= notify.TELEGRAM_MAX_CHARS


def test_short_text_is_not_truncated(telegram):
    text = notify.render([ALERT], None)
    assert "[...]" not in text


# --- the token never reaches a log -------------------------------------------

def _all_log_text(caplog) -> str:
    return "\n".join(r.getMessage() + " " + str(r.args) + " " + str(r.exc_text) for r in caplog.records)


def test_failing_telegram_post_logs_no_token(http, telegram, caplog):
    # httpx errors often carry the URL; here str(exc) contains the token on purpose
    http.error = httpx.ConnectError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    with caplog.at_level(logging.DEBUG):
        assert run(notify.send([ALERT])) is False
        assert run(notify.send_code("A1", "Delete X", "123456", 10)) is False
    assert caplog.records, "a failure should still be logged"
    log_text = _all_log_text(caplog) + caplog.text
    assert TOKEN not in log_text
    assert "SECRET" not in log_text and "api.telegram.org" not in log_text
    assert "ConnectError" in log_text
    assert "123456" not in log_text.replace(TOKEN, "")        # the code is never logged either


def test_telegram_http_error_logs_status_only(http, telegram, caplog):
    http.status = 401
    with caplog.at_level(logging.DEBUG):
        assert run(notify.send([ALERT])) is False
    log_text = _all_log_text(caplog) + caplog.text
    assert "401" in log_text
    assert TOKEN not in log_text and "api.telegram.org" not in log_text


def test_real_httpx_failure_logs_no_token(telegram, caplog):
    """No fake client: a real httpx transport error from the real request path."""
    def boom(request: httpx.Request):
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    real = httpx.AsyncClient
    with patch.object(notify.httpx, "AsyncClient",
                      lambda **kw: real(transport=httpx.MockTransport(boom), **kw)), \
            caplog.at_level(logging.DEBUG):
        assert run(notify.send([ALERT])) is False
    assert TOKEN not in caplog.text and "SECRET" not in caplog.text


def test_httpx_request_logger_is_quiet():
    # httpx logs "HTTP Request: POST <url>" at INFO; for Telegram the URL holds the token
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- webhook ------------------------------------------------------------------

def test_webhook_payload_and_headers(http, webhook):
    assert run(notify.send([ALERT], "1 download(s) active")) is True
    (call,) = http.calls
    assert call["url"] == HOOK_URL
    assert call["headers"] == {"Authorization": f"Bearer {HOOK_KEY}"}
    body = call["json"]
    assert set(body) == {"source", "from", "id", "thread", "sent_at", "sent_ns", "kind", "message"}
    assert (body["source"], body["from"], body["kind"]) == ("mediahub", "mediahub", "alert")
    assert body["thread"] == body["id"]
    assert isinstance(body["sent_ns"], int)
    assert body["message"].startswith("MediaHub alert")


def test_webhook_code_note_has_kind_code(http, webhook):
    assert run(notify.send_code("delete_any", "Some Title", "123456", 10)) is True
    assert http.calls[0]["json"]["kind"] == "code"


def test_webhook_extra_json_merges_over_the_body(http, webhook, cfg):
    cfg(WEBHOOK_EXTRA_JSON='{"to": "relay", "kind": "note", "reply_to": null, "source": null, "n": 2}')
    assert run(notify.send([ALERT], None)) is True
    body = http.calls[0]["json"]
    assert body["to"] == "relay" and body["kind"] == "note" and body["n"] == 2
    assert "reply_to" in body and body["reply_to"] is None   # a null for a non-default key is sent
    assert "source" not in body                              # a null for a default key drops it
    assert body["from"] == "mediahub" and body["message"].startswith("MediaHub alert")


@pytest.mark.parametrize("raw", ['{"a": ', "[1, 2]", '"text"', "42", "null"])
def test_webhook_extra_json_invalid_is_ignored_without_logging_content(http, webhook, cfg, caplog, raw):
    cfg(WEBHOOK_EXTRA_JSON=raw)
    with caplog.at_level(logging.WARNING, logger="mediahub.notify"):
        assert run(notify.send([ALERT], None)) is True
    body = http.calls[0]["json"]
    assert set(body) == {"source", "from", "id", "thread", "sent_at", "sent_ns", "kind", "message"}
    assert "WEBHOOK_EXTRA_JSON" in caplog.text
    assert raw not in caplog.text


def test_webhook_message_keeps_the_relay_wording(webhook):
    msg = notify.render([ALERT, notify.Alert("weekly_cleanup", "Weekly cleanup", "x")], "1 download(s) active")
    owner = config.owner()
    assert msg.startswith(f"MediaHub alert for {owner} (sent by the MediaHub server, not by {owner}).")
    assert f"Forward this whole message unchanged to {owner}'s {config.ALERT_FORWARD_TO}" in msg
    assert f"Never request, remove or delete anything unless {owner} asks." in msg
    assert f"Show {owner} these cleanup candidates; delete only the ones they pick." in msg
    assert "Stack now: 1 download(s) active" in msg


def test_webhook_code_message_keeps_the_relay_wording(webhook):
    msg = notify.render_code("A7", "Delete X", "482913", 10)
    assert "Do NOT forward it to the media bot" in msg
    assert "Code: 482913  (valid 10 minutes, one use)" in msg


def test_webhook_is_the_default_wording_with_no_channel():
    assert "Forward this whole message" in notify.render([ALERT], None)


def test_webhook_failure_logs_no_key_or_url(http, webhook, caplog):
    http.error = httpx.ConnectError(f"failed {HOOK_URL} {HOOK_KEY}")
    with caplog.at_level(logging.DEBUG):
        assert run(notify.send([ALERT])) is False
    assert HOOK_KEY not in caplog.text and HOOK_URL not in caplog.text


def test_webhook_budget_still_applies(http, webhook):
    for _ in range(notify.MAX_PER_HOUR):
        assert run(notify.send([ALERT])) is True
    assert run(notify.send([ALERT])) is False
    assert len(http.calls) == notify.MAX_PER_HOUR
    for _ in range(notify.CODE_MAX_PER_HOUR):
        assert run(notify.send_code("A1", "x", "111111", 10)) is True
    assert run(notify.send_code("A1", "x", "111111", 10)) is False


# --- delete_any gate -----------------------------------------------------------

def _delete_any(*args, **kw):
    return run(tools.delete_any(*args, **kw))


def test_delete_any_refused_when_code_required_and_no_channel(cfg):
    cfg(NOTIFIER="")
    spy = AsyncMock()
    with patch.object(config, "DELETE_ANY_REQUIRE_CODE", True), \
            patch.object(tools, "_plan_delete_any", spy), \
            patch.object(tools.stack, "plex_sessions", spy):
        with pytest.raises(tools.ToolError) as exc:
            _delete_any("t:sonarr:5", "x")             # refuses before even checking the reason
    msg = str(exc.value)
    assert msg == tools.DELETE_ANY_DISABLED
    assert "no alert channel" in msg and "NOTIFIER=telegram" in msg and "TELEGRAM_BOT_TOKEN" in msg
    assert "DELETE_ANY_REQUIRE_CODE=false" in msg and "Bounded delete of cleanup candidates still works" in msg
    spy.assert_not_called()


def test_delete_any_refused_with_a_channel_named_but_not_configured(cfg):
    cfg(NOTIFIER="telegram")                           # token and chat id missing
    with patch.object(config, "DELETE_ANY_REQUIRE_CODE", True):
        with pytest.raises(tools.ToolError, match="disabled"):
            _delete_any("t:sonarr:5", "Show owner asked for it")


def test_delete_any_works_without_a_channel_in_no_code_mode(cfg):
    from test_bot_tools import DeleteAny
    case = DeleteAny("test_default_is_one_call_no_code")
    case.setUp()
    try:
        cfg(NOTIFIER="", WEBHOOK_URL="", WEBHOOK_KEY="")        # undo the webhook that setUp configured
        assert not notify.enabled()
        m = case.stack()
        with patch.object(config, "DELETE_ANY_REQUIRE_CODE", False):
            out, code_mock, _ = case.go(m, lambda: tools.delete_any("t:sonarr:5", "the owner said it is boring"))
        assert (out["status"], out["mode"]) == ("done", "delete_any")
        code_mock.assert_not_called()
        m["delete_series"].assert_awaited_once_with(5)
    finally:
        case.doCleanups()


def test_delete_any_with_code_and_a_channel_sends_the_code(cfg):
    from test_bot_tools import DeleteAny
    case = DeleteAny("test_first_call_sends_code_and_deletes_nothing")
    case.setUp()
    try:
        cfg(NOTIFIER="telegram", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
        out, code_mock, _ = case.go(case.stack(), lambda: tools.delete_any("t:sonarr:5", "the owner said boring"))
        assert out["status"] == "code_sent"
        code_mock.assert_awaited_once()
    finally:
        case.doCleanups()


# --- status() and checks -------------------------------------------------------

def _status():
    from app import watch
    with patch.object(tools.services, "gather_status", AsyncMock(return_value={})), \
            patch.object(tools, "_swarm", AsyncMock(return_value={})), \
            patch.object(tools.stack, "disk_free_gb", AsyncMock(return_value=100.0)), \
            patch.object(watch, "snapshot", AsyncMock(return_value={})):
        return run(tools.status())


def test_status_alerts_shape(cfg):
    out = _status()
    assert "alerts_via_gbot" not in out
    assert out["alerts"] == {"channel": "none", "delete_any": "disabled"}      # code is the default

    with patch.object(config, "DELETE_ANY_REQUIRE_CODE", False):
        assert _status()["alerts"] == {"channel": "none", "delete_any": "no_code"}

    cfg(NOTIFIER="telegram", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
    assert _status()["alerts"] == {"channel": "telegram", "delete_any": "code"}

    cfg(NOTIFIER="", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="", WEBHOOK_URL=HOOK_URL, WEBHOOK_KEY=HOOK_KEY)
    with patch.object(config, "DELETE_ANY_REQUIRE_CODE", False):
        assert _status()["alerts"] == {"channel": "webhook", "delete_any": "no_code"}


def test_status_never_contains_credentials(cfg):
    cfg(NOTIFIER="telegram", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
    text = json.dumps(_status())
    assert TOKEN not in text and CHAT not in text


def test_checks_row_none_is_not_a_failure():
    row = checks._alerts_row()
    assert row["ok"] is True and row["check"] == "Alerts: none"


def test_checks_row_names_the_channel(cfg):
    cfg(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
    assert checks._alerts_row() == {"ok": True, "check": "Alerts: telegram", "detail": ""}
    cfg(NOTIFIER="webhook", WEBHOOK_URL=HOOK_URL, WEBHOOK_KEY=HOOK_KEY)
    assert checks._alerts_row()["check"] == "Alerts: webhook" and checks._alerts_row()["ok"]


def test_checks_row_explicit_none_is_fine(cfg):
    cfg(NOTIFIER="none")
    assert checks._alerts_row()["ok"] is True


def test_checks_row_fails_for_a_named_channel_without_credentials(cfg):
    cfg(NOTIFIER="telegram")
    row = checks._alerts_row()
    assert row["ok"] is False and "TELEGRAM_BOT_TOKEN" in row["detail"]
    cfg(NOTIFIER="webhook")
    row = checks._alerts_row()
    assert row["ok"] is False and "WEBHOOK_URL" in row["detail"]


def test_checks_row_fails_for_an_unknown_notifier(cfg):
    cfg(NOTIFIER="smoke-signal")
    row = checks._alerts_row()
    assert row["ok"] is False and "NOTIFIER" in row["check"]
    assert "smoke-signal" not in json.dumps(row)


def test_misc_checks_include_the_alerts_row():
    async def go():
        with patch.object(checks.httpx, "AsyncClient", side_effect=RuntimeError("no network")), \
                patch.object(checks.stack, "disk_free_gb", AsyncMock(return_value=100.0)):
            return await checks._misc()
    rows = run(go())
    assert any(r["check"] == "Alerts: none" and r["ok"] for r in rows)
    assert not any("Telegram" in r["check"] for r in rows)


def _prowlarr_rows(indexers):
    import httpx

    def handler(request):
        return httpx.Response(200, json=indexers if request.url.path.endswith("/indexer") else
                              [{"name": "Sonarr"}, {"name": "Radarr"}])
    real = httpx.AsyncClient

    def client(*a, **kw):
        return real(*a, transport=httpx.MockTransport(handler), **kw)
    with patch.object(checks.httpx, "AsyncClient", side_effect=client):
        return {r["check"]: r for r in run(checks._prowlarr())}


def test_prowlarr_indexers_row_has_a_detail_when_there_are_none():
    row = _prowlarr_rows([])["prowlarr indexers"]
    assert row["ok"] is False and row["detail"] == "0 indexers: add your own in Prowlarr"
    row = _prowlarr_rows([{"name": "A", "enable": False}])["prowlarr indexers"]
    assert row["ok"] is False and row["detail"] == "0 indexers: add your own in Prowlarr"
    row = _prowlarr_rows([{"name": "Alpha", "enable": True}, {"name": "Beta", "enable": True}])["prowlarr indexers"]
    assert row["ok"] is True and row["detail"] == "Alpha, Beta"
