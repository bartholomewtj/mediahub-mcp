"""`python -m app.setup mcp-ping`: the client-less check of /mcp. Never prints the token."""

from __future__ import annotations

import json

import httpx
import pytest

from app import config, setup, setup_tools

TOKEN = "s3cret-token-value-0123456789"
STATUS = {
    "disk_free_gb": 900,
    "services": [{"name": "Sonarr", "up": True}, {"name": "Radarr", "up": True},
                 {"name": "Plex", "up": False}],
    "alerts": {"channel": "telegram", "delete_any": "code"},
}


def _sse(msg: dict) -> str:
    return f"event: message\r\ndata: {json.dumps(msg)}\r\n\r\n"


def _server(sse: bool, *, token: str = TOKEN, status_error: bool = False, seen: list | None = None):
    """A fake /mcp: needs the bearer token and (after initialize) the session id."""
    def reply(req: httpx.Request, msg: dict) -> httpx.Response:
        if sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse(msg))
        return httpx.Response(200, json=msg)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if req.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if req.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(req.content)
        method = body.get("method")
        if method == "initialize":
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}
            r = reply(req, msg)
            r.headers["mcp-session-id"] = "sess-1"
            return r
        assert req.headers.get("mcp-session-id") == "sess-1"
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/call" and body["params"]["name"] == "status"
        if status_error:
            return reply(req, {"jsonrpc": "2.0", "id": body["id"],
                               "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]}})
        result = {"content": [{"type": "text", "text": json.dumps(STATUS)}], "isError": False}
        if sse:   # a priming notification before the answer, as a streaming server may send
            note = _sse({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
            ans = _sse({"jsonrpc": "2.0", "id": body["id"], "result": result})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=note + ans)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_ok_prints_the_two_summary_lines_and_never_the_token(sse):
    lines, seen = [], []
    rc = setup_tools.mcp_ping("http://mh/mcp", TOKEN, _server(sse, seen=seen), out=lines.append)
    assert rc == 0
    assert lines == ["OK mcp status: services up 2/3 (Plex), alerts telegram, delete_any code",
                     "OK mcp rejects calls without the token"]
    assert TOKEN not in "\n".join(lines)
    assert [r.headers.get("authorization") for r in seen][-1] is None   # the last call had no token


def test_structured_content_is_used_too():
    def handler(req):
        body = json.loads(req.content) if req.content else {}
        if req.headers.get("authorization") is None:
            return httpx.Response(401)
        if body.get("method") == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": STATUS}})
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(202)

    lines = []
    assert setup_tools.mcp_ping("http://mh/mcp", TOKEN, httpx.MockTransport(handler), out=lines.append) == 0
    assert lines[0].startswith("OK mcp status: services up 2/3")


def test_wrong_token_fails_and_the_open_check_still_runs():
    lines = []
    rc = setup_tools.mcp_ping("http://mh/mcp", "stale-token", _server(False), out=lines.append)
    assert rc == 1
    assert lines[0].startswith("FAIL mcp status: HTTP 401")
    assert lines[1] == "OK mcp rejects calls without the token"
    assert "stale-token" not in "\n".join(lines)


def test_server_that_accepts_calls_without_the_token_fails():
    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if body["method"] == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2,
                                             "result": {"structuredContent": STATUS}})
        return httpx.Response(202)

    lines = []
    rc = setup_tools.mcp_ping("http://mh/mcp", TOKEN, httpx.MockTransport(handler), out=lines.append)
    assert rc == 1
    assert lines[0].startswith("OK mcp status")
    assert lines[1] == "FAIL mcp without the token: HTTP 200, wanted 401"


def test_tool_error_fails():
    lines = []
    assert setup_tools.mcp_ping("http://mh/mcp", TOKEN, _server(False, status_error=True), out=lines.append) == 1
    assert lines[0] == "FAIL mcp status: the status tool returned an error"
    assert "boom" not in "\n".join(lines)


def test_empty_token_fails_without_calling_with_one():
    lines, seen = [], []
    rc = setup_tools.mcp_ping("http://mh/mcp", "", _server(False, seen=seen), out=lines.append)
    assert rc == 1 and lines[0].startswith("FAIL mcp status: MCP_TOKEN is empty")
    assert lines[1] == "OK mcp rejects calls without the token"


def test_unreachable_server_is_one_fail_line():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    lines = []
    rc = setup_tools.mcp_ping("http://mh/mcp", TOKEN, httpx.MockTransport(handler), out=lines.append)
    assert rc == 1 and len(lines) == 1 and lines[0].startswith("FAIL mcp status: cannot reach http://mh/mcp")


def test_main_uses_the_loaded_token_and_the_env_url(monkeypatch, capsys):
    monkeypatch.setattr(config, "MCP_TOKEN", TOKEN)
    monkeypatch.setenv("MEDIAHUB_MCP_URL", "http://example.invalid/mcp")
    real = httpx.Client
    monkeypatch.setattr(setup_tools.httpx, "Client",
                        lambda **kw: real(**{**kw, "transport": _server(True)}))
    assert setup.main(["mcp-ping"]) == 0
    out = capsys.readouterr().out
    assert "OK mcp status: services up 2/3 (Plex)" in out and TOKEN not in out
