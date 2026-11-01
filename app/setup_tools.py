"""Install helpers behind `python -m app.setup` (the installer playbooks call these):

    allowed-tools   the --allowedTools string for every tool the MCP server registers
    bot-prompt      templates/bot-prompt.md rendered for this install
    config-diff     keys the shipped examples have that the live stack.env/secrets.env lack, and back
    remap-roots     move a Sonarr/Radarr library from one container path prefix to another
    version         the MediaHub version (VERSION file)
    mcp-ping        call /mcp (handshake, the status tool, and a call without the token) with no client

Nothing here prints a secret: config-diff prints key names only, bot-prompt/allowed-tools read no
secret at all, and mcp-ping reads MCP_TOKEN but prints only OK/FAIL lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Callable

import httpx

from app import config

TEMPLATES = config.ROOT / "templates"
PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


# ---------------------------------------------------------------- version

def version() -> str:
    return (config.ROOT / "VERSION").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- allowed-tools

def allowed_tools() -> str:
    """Comma-separated mcp__mediahub__<tool> for every tool registered in app/mcp_server.py,
    in registration order. The server's own tool list is the only source."""
    from app import mcp_server  # imported late: it builds the server and its descriptions
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return ",".join(f"mcp__mediahub__{t.name}" for t in tools)


# ---------------------------------------------------------------- bot-prompt

def _delete_any_sentence(mode: str, owner: str) -> str:
    if mode == "no_code":
        return ("No-code mode: call it straight away, in the same turn, with no confirmation round trip and no "
                "code. Your own reading of what " + owner + " asked is the only check, so rule 1 below is strict.")
    if mode == "code":
        return ("A one-time code is required. The first call returns code_sent and MediaHub sends " + owner +
                " a code through the alert channel (never through you). Ask " + owner + " for that code, then "
                "call delete_any again with the same item_id, reason and seasons plus code (carry all of them "
                "into the new call's prompt). Never look for the code anywhere else and never guess it; it is "
                "single-use, valid 10 minutes, 3 tries.")
    if mode == "disabled":
        return ("delete_any is disabled on this install: it needs a one-time code but there is no alert channel "
                "to send it to. If " + owner + " wants something deleted that is not a cleanup candidate, tell "
                "them to set up an alert channel (Telegram) or to turn off the code requirement "
                "(DELETE_ANY_REQUIRE_CODE=false, which is riskier), and use delete(candidate_id) for cleanup "
                "candidates meanwhile.")
    raise ValueError(f"unknown delete_any mode {mode!r}")


def _alerts_sentence(channel: str, owner: str) -> str:
    if channel == "none":
        return ("MediaHub sends no alerts on this install: it will not message anyone, so use status and "
                "problems when " + owner + " asks what is going on.")
    if channel == "telegram":
        return ("Alerts arrive on Telegram, not through you: " + owner + " reads them there and may ask you "
                "about one.")
    if channel == "webhook":
        return ('When it needs a decision it sends a "MediaHub alert" to ' + owner + "'s alert relay, which "
                "forwards it to you.")
    raise ValueError(f"unknown alert channel {channel!r}")


def render_bot_prompt(repo_dir: str, shell: str, channel: str, delete_mode: str,
                      owner: str | None = None, disk_guard_gb: float | None = None,
                      tools: str | None = None, template: str | None = None) -> str:
    """templates/bot-prompt.md with every {{PLACEHOLDER}} filled:

    OWNER_NAME, REPO_DIR (where compose/ lives), SHELL (PowerShell | bash), CD_COMMAND
    (Set-Location | cd), ALLOWED_TOOLS, DISK_GUARD_GB, DELETE_ANY_MODE, ALERTS.
    An unknown placeholder is an error, so a typo cannot ship."""
    if shell not in ("powershell", "bash"):
        raise ValueError("shell must be powershell or bash")
    owner = owner if owner is not None else config.OWNER_NAME
    guard = config.DISK_GUARD_GB if disk_guard_gb is None else disk_guard_gb
    path = f'"{repo_dir}"' if re.search(r"\s", repo_dir) else repo_dir
    values = {
        "OWNER_NAME": owner,
        "REPO_DIR": path,
        "SHELL": "PowerShell" if shell == "powershell" else "bash",
        "CD_COMMAND": "Set-Location" if shell == "powershell" else "cd",
        "ALLOWED_TOOLS": tools if tools is not None else allowed_tools(),
        "DISK_GUARD_GB": f"{guard:g}",
        "DELETE_ANY_MODE": _delete_any_sentence(delete_mode, owner),
        "ALERTS": _alerts_sentence(channel, owner),
    }
    text = template if template is not None else (TEMPLATES / "bot-prompt.md").read_text(encoding="utf-8")

    def fill(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ValueError(f"unknown placeholder {{{{{m.group(1)}}}}} in the bot prompt template")
        return values[m.group(1)]

    return PLACEHOLDER.sub(fill, text)  # one pass: filled-in text is never rescanned


def bot_prompt(repo_dir: str, shell: str) -> str:
    """The prompt for this install, from the live config."""
    from app import notify, tools
    return render_bot_prompt(repo_dir, shell, notify.channel(), tools.delete_any_mode())


# ---------------------------------------------------------------- config-diff

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_COMMENTED_ASSIGN = re.compile(r"^\s*#\s*([A-Z][A-Z0-9_]*)=")
ENV_FILES = ("stack.env", "secrets.env")


def example_keys(text: str) -> tuple[set[str], set[str]]:
    """(active keys, commented-out keys) of an example env file. A commented-out line only counts
    when it looks like `# KEY=value`; prose such as "# Channel: none | telegram" does not."""
    active, optional = set(), set()
    for line in text.splitlines():
        m = _ASSIGN.match(line)
        if m:
            active.add(m.group(1))
            continue
        m = _COMMENTED_ASSIGN.match(line)
        if m:
            optional.add(m.group(1))
    return active, optional - active


def live_keys(text: str) -> set[str]:
    """Keys set (even to empty) in a live env file. Comments do not count."""
    return {m.group(1) for line in text.lstrip("﻿").splitlines()
            if not line.lstrip().startswith("#") and (m := _ASSIGN.match(line))}


def default_examples_dir() -> Path:
    shipped = config.ROOT / "compose-examples"  # in the image
    return shipped if shipped.is_dir() else config.ROOT / "compose"  # a checkout


def default_home_dir() -> Path:
    return Path(os.getenv("MEDIAHUB_SETUP_HOME") or "/mh-home")  # where the setup service mounts MEDIAHUB_HOME


def config_diff(home: Path | None = None, examples: Path | None = None, out: Callable[[str], None] = print) -> int:
    """NEW/OPTIONAL/UNKNOWN lines, key names only. Always exit 0: it reports, it does not judge."""
    home = home or default_home_dir()
    examples = examples or default_examples_dir()
    for name in ENV_FILES:
        example = examples / f"{name}.example"
        try:
            active, optional = example_keys(example.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            out(f"NOTE {name}: no example file at {example}")
            continue
        try:
            have = live_keys((home / name).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            out(f"NOTE {name}: not found in {home}; every key of the example is listed")
            have = set()
        out_lines = [f"NEW {name} {k}" for k in sorted(active - have)]
        out_lines += [f"OPTIONAL {name} {k}" for k in sorted(optional - have)]
        out_lines += [f"UNKNOWN {name} {k}" for k in sorted(have - active - optional)]
        for line in out_lines:
            out(line)
        if not out_lines:
            out(f"OK   {name}")
    return 0


# ---------------------------------------------------------------- remap-roots

def _arr_conn(app: str) -> tuple[str, dict[str, str]]:
    if app == "sonarr":
        return config.SONARR_URL, {"X-Api-Key": config.SONARR_API_KEY}
    return config.RADARR_URL, {"X-Api-Key": config.RADARR_API_KEY}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _errors(r: httpx.Response) -> str:
    try:
        return "; ".join(str(e.get("errorMessage", "")) for e in r.json())[:300]
    except Exception:
        return ""


def remap_roots(app: str, old: str, new: str, apply: bool, client: httpx.Client | None = None,
                out: Callable[[str], None] = print) -> int:
    """Re-point the series/movies under the container path `old` at `new`, without moving files.

    Sonarr: PUT /api/v3/series/editor {seriesIds, rootFolderPath, moveFiles:false}
    Radarr: PUT /api/v3/movie/editor  {movieIds,  rootFolderPath, moveFiles:false}
    With moveFiles false the app rebuilds each Path as rootFolderPath + the title's existing folder
    (its path relative to the best matching root folder, else just its last folder name), so a title
    in a subfolder keeps its subfolder. We therefore send one batch per distinct new root folder, derived
    from each item's own rootFolderPath. Items whose root folder is not under `old` are skipped.
    The first batch starts with one item; its resulting path is checked before the rest go in, and
    the run stops at the first path that is not the expected one."""
    old, new = old.rstrip("/"), new.rstrip("/")
    if not old.startswith("/") or not new.startswith("/"):
        raise ValueError("--from and --to must be absolute container paths such as /tv and /media/TV")
    base, h = _arr_conn(app)
    kind, listing, ids_key = (("series", "series", "seriesIds") if app == "sonarr"
                              else ("movies", "movie", "movieIds"))
    c = client or httpx.Client(timeout=60)
    try:
        def items() -> list[dict]:
            r = c.get(f"{base}/api/v3/{listing}", headers=h)
            r.raise_for_status()
            return r.json()

        matched, skipped = [], []
        for it in items():
            path = str(it.get("path") or "").rstrip("/")
            if not _under(path, old):
                continue
            root = str(it.get("rootFolderPath") or path.rsplit("/", 1)[0]).rstrip("/")
            if not _under(root, old):
                skipped.append(it)
                continue
            matched.append({"id": it["id"], "title": it.get("title") or str(it["id"]), "path": path,
                            "new_root": new + root[len(old):], "new_path": new + path[len(old):]})
        out(f"{app}: {len(matched)} {kind} under {old} (would become {new})"
            + (f"; {len(skipped)} skipped (their root folder is not under {old})" if skipped else ""))
        for m in matched[:10]:
            out(f"  {m['title']}: {m['path']} -> {m['new_path']}")
        if len(matched) > 10:
            out(f"  ... and {len(matched) - 10} more")
        for it in skipped[:10]:
            out(f"  SKIP {it.get('title')}: root folder {it.get('rootFolderPath')!r} is not under {old}")
        if not matched:
            return 0

        roots = {}
        for m in matched:
            roots.setdefault(m["new_root"], []).append(m)
        r = c.get(f"{base}/api/v3/rootfolder", headers=h)
        r.raise_for_status()
        have = {str(f.get("path", "")).rstrip("/") for f in r.json()}
        missing = [p for p in roots if p not in have]
        if not apply:
            for p in missing:
                out(f"  would add root folder {p}")
            out("Dry run: nothing changed. Add --apply to do it (files are not moved).")
            return 0

        for p in missing:
            r = c.post(f"{base}/api/v3/rootfolder", headers=h, json={"path": p})
            if r.status_code >= 400:
                out(f"FAIL add root folder {p}: HTTP {r.status_code} {_errors(r)}".strip())
                return 1
            out(f"FIXED added root folder {p}")

        first = True
        for root, group in roots.items():
            batches = [group[:1], group[1:]] if first and len(group) > 1 else [group]
            for batch in batches:
                if not batch:
                    continue
                r = c.put(f"{base}/api/v3/{listing}/editor", headers=h,
                          json={ids_key: [m["id"] for m in batch], "rootFolderPath": root, "moveFiles": False})
                if r.status_code >= 400:
                    out(f"FAIL {app} editor for root {root}: HTTP {r.status_code} {_errors(r)}".strip())
                    return 1
                now = {it["id"]: str(it.get("path") or "").rstrip("/") for it in items()}
                wrong = [m for m in batch if now.get(m["id"]) != m["new_path"]]
                if wrong:
                    out(f"FAIL {len(wrong)} of {len(batch)} {kind} did not get the expected path "
                        f"(first: {wrong[0]['title']} is now {now.get(wrong[0]['id'])!r}, wanted "
                        f"{wrong[0]['new_path']!r}); stopped. Nothing else was changed.")
                    return 1
                out(f"FIXED {len(batch)} {kind} -> {root}")
                first = False
        out(f"NOTE files were not moved. Check that {new} holds the files, rescan in Plex if its library "
            "folder changed, and run `setup check` to see other paths that may need changing.")
        return 0
    finally:
        if client is None:
            c.close()


# ---------------------------------------------------------------- mcp-ping

MCP_DEFAULT_URL = "http://127.0.0.1:8000/mcp"
_MCP_ACCEPT = "application/json, text/event-stream"


def _rpc_message(r: httpx.Response, rid: int) -> dict:
    """The JSON-RPC reply with id `rid` from a JSON or an SSE (`data:` lines) response body."""
    text = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        found: list = []
        for block in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:"))
            if data.strip():
                try:
                    found.append(json.loads(data))
                except ValueError:
                    continue
    else:
        body = json.loads(text)
        found = body if isinstance(body, list) else [body]
    for m in found:
        if isinstance(m, dict) and m.get("id") == rid:
            return m
    raise ValueError("no JSON-RPC reply in the response")


def _status_payload(result: dict) -> dict:
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and "services" not in sc and isinstance(sc.get("result"), dict):
        sc = sc["result"]
    if isinstance(sc, dict) and "services" in sc:
        return sc
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                data = json.loads(part.get("text") or "")
            except ValueError:
                continue
            if isinstance(data, dict) and "services" in data:
                return data
    raise ValueError("the status reply has no services list")


def mcp_ping(url: str | None = None, token: str | None = None, transport: httpx.BaseTransport | None = None,
             out: Callable[[str], None] = print) -> int:
    """Check /mcp end to end without a client and without printing the token: initialize,
    notifications/initialized, tools/call status; then a call without the token must get 401.
    Prints OK/FAIL lines only. Returns 0 when both checks pass, else 1."""
    url = url or os.getenv("MEDIAHUB_MCP_URL") or MCP_DEFAULT_URL
    token = config.MCP_TOKEN if token is None else token
    base = {"Accept": _MCP_ACCEPT, "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "mediahub-mcp-ping", "version": "1"}}}
    bad = 0
    with httpx.Client(timeout=30, transport=transport) as c:
        # 1. with the token: the full handshake and one tool call
        if not token:
            out("FAIL mcp status: MCP_TOKEN is empty in secrets.env (run `setup secrets`, "
                "then `mh up -d --force-recreate mediahub`)")
            bad = 1
        else:
            h = {**base, "Authorization": f"Bearer {token}"}
            sid = None
            try:
                r = c.post(url, headers=h, json=init)
                if r.status_code == 401:
                    raise ValueError("HTTP 401: the server has another token (recreate mediahub after `setup secrets`)")
                if r.status_code == 503:
                    raise ValueError("HTTP 503: MCP is disabled in the running container (recreate mediahub)")
                if r.status_code != 200:
                    raise ValueError(f"initialize: HTTP {r.status_code}")
                reply = _rpc_message(r, 1)
                if "error" in reply:
                    raise ValueError("initialize was refused")
                sid = r.headers.get("mcp-session-id")
                if sid:
                    h["Mcp-Session-Id"] = sid
                version = (reply.get("result") or {}).get("protocolVersion")
                if version:
                    h["MCP-Protocol-Version"] = str(version)
                r = c.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                if r.status_code >= 400:
                    raise ValueError(f"notifications/initialized: HTTP {r.status_code}")
                r = c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                 "params": {"name": "status", "arguments": {}}})
                if r.status_code != 200:
                    raise ValueError(f"tools/call status: HTTP {r.status_code}")
                reply = _rpc_message(r, 2)
                if "error" in reply or (reply.get("result") or {}).get("isError"):
                    raise ValueError("the status tool returned an error")
                data = _status_payload(reply.get("result") or {})
                svcs = data.get("services") or []
                down = [str(s.get("name")) for s in svcs if not s.get("up")]
                alerts = data.get("alerts") or {}
                out(f"OK mcp status: services up {len(svcs) - len(down)}/{len(svcs)} "
                    f"({', '.join(down) if down else 'none down'}), alerts {alerts.get('channel')}, "
                    f"delete_any {alerts.get('delete_any')}")
            except httpx.HTTPError as exc:
                out(f"FAIL mcp status: cannot reach {url} ({type(exc).__name__}); is the stack up?")
                return 1
            except ValueError as exc:   # includes json.JSONDecodeError
                out(f"FAIL mcp status: {exc}")
                bad = 1
            finally:
                if sid:   # close our session; best effort
                    try:
                        c.delete(url, headers=h)
                    except httpx.HTTPError:
                        pass

        # 2. without the token: refused
        try:
            r = c.post(url, headers=base, json=init)
            if r.status_code == 401:
                out("OK mcp rejects calls without the token")
            else:
                out(f"FAIL mcp without the token: HTTP {r.status_code}, wanted 401")
                bad = 1
        except httpx.HTTPError as exc:
            out(f"FAIL mcp without the token: cannot reach {url} ({type(exc).__name__})")
            bad = 1
    return 1 if bad else 0
