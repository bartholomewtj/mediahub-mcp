"""Deep health checks — what smoke-test.py used to do, now in-process.

Used by status(deep=true) and GET /api/checks (which verify-stack.ps1 calls).
Each check returns {"ok": bool, "check": str, "detail": str}; nothing secret
is ever put in a detail.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from . import config, notify, stack, taste



def _row(ok: bool, check: str, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "check": check, "detail": detail}


async def _arr(app: str) -> list[dict[str, Any]]:
    out = []
    try:
        st = await stack.arr(app, "GET", "system/status")
        out.append(_row(True, f"{app} up", f"version {st.get('version')}"))
        for r in await stack.arr(app, "GET", "rootfolder") or []:
            out.append(_row(r.get("accessible", False), f"{app} root folder",
                            f"{r['path']} free {round((r.get('freeSpace') or 0) / 1e9)} GB"))
        for d in await stack.arr(app, "GET", "downloadclient") or []:
            if d.get("implementation") != "QBittorrent":
                continue
            layout = next((f.get("value") for f in d.get("fields") or [] if f.get("name") == "contentLayout"), None)
            out.append(_row(layout == 2, f"{app} qBittorrent layout", f"contentLayout={layout} (2=Subfolder)"))
        idx = await stack.arr(app, "GET", "indexer") or []
        out.append(_row(bool(idx), f"{app} indexers", f"{len(idx)} synced from Prowlarr"))
    except Exception as exc:
        out.append(_row(False, f"{app} up", str(exc)[:200]))
    return out


async def _prowlarr() -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            h = {"X-Api-Key": config.PROWLARR_API_KEY}
            idx = (await c.get(f"{config.PROWLARR_URL}/api/v1/indexer", headers=h)).json()
            apps = (await c.get(f"{config.PROWLARR_URL}/api/v1/applications", headers=h)).json()
        enabled = [i["name"] for i in idx if i.get("enable")]
        return [_row(bool(enabled), "prowlarr indexers", ", ".join(enabled) or "0 indexers: add your own in Prowlarr"),
                _row(len(apps) >= 2, "prowlarr apps", ", ".join(a["name"] for a in apps))]
    except Exception as exc:
        return [_row(False, "prowlarr", str(exc)[:200])]


async def _plex() -> list[dict[str, Any]]:
    out = []
    try:
        for s in await stack.plex_sections():
            mc = await stack.plex(f"/library/sections/{s['key']}/all", **{"X-Plex-Container-Size": 0})
            n = mc.get("totalSize") or mc.get("size") or 0
            out.append(_row(True, f"plex library {s.get('title')}", f"{n} items"))
        ident = await stack.plex("/identity")
        out.append(_row(bool(ident.get("claimed")), "plex claimed", f"version {ident.get('version')}"))
    except Exception as exc:
        out.append(_row(False, "plex", str(exc)[:200]))
    return out


async def _qbit() -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(base_url=config.QBIT_URL, timeout=15) as c:
            if config.QBIT_USER:
                await c.post("/api/v2/auth/login", data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
                             headers={"Referer": config.QBIT_URL})
            prefs = (await c.get("/api/v2/app/preferences")).json()
        return [_row(str(prefs.get("save_path", "")).startswith("/media"), "qbit save path", prefs.get("save_path", "")),
                _row(prefs.get("torrent_content_layout") == "Subfolder", "qbit content layout",
                     prefs.get("torrent_content_layout", "")),
                _row(prefs.get("dl_limit") == config.QBIT_DL_LIMIT_KIB * 1024
                     and prefs.get("up_limit") == config.QBIT_UP_LIMIT_KIB * 1024,
                     "qbit speed caps", f"{(prefs.get('dl_limit') or 0) // 1024}/{(prefs.get('up_limit') or 0) // 1024} KiB/s "
                     f"(policy {config.QBIT_DL_LIMIT_KIB}/{config.QBIT_UP_LIMIT_KIB})"),
                _row(bool(prefs.get("max_ratio_enabled")) and bool(prefs.get("max_seeding_time_enabled")),
                     "qbit seeding limits", f"ratio {prefs.get('max_ratio')}, {prefs.get('max_seeding_time')} min")]
    except Exception as exc:
        return [_row(False, "qbit", str(exc)[:200])]


def _alerts_row() -> dict[str, Any]:
    """"none" is a valid choice; only a named channel that cannot work is a failure."""
    named = config.NOTIFIER
    if named and named not in notify.CHANNELS:
        return _row(False, "Alerts: NOTIFIER is not valid", "use none, telegram or webhook")
    ch = notify.channel()
    if named and ch != "none" and not notify.enabled():
        need = ("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" if ch == "telegram"
                else "WEBHOOK_URL and WEBHOOK_KEY")
        return _row(False, f"Alerts: {ch}", f"NOTIFIER={ch} but {need} are not both set")
    return _row(True, f"Alerts: {ch}", "delete_any is disabled while the code is required and there is no channel"
                if ch == "none" and config.DELETE_ANY_REQUIRE_CODE else "")


async def _misc() -> list[dict[str, Any]]:
    out = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{config.BAZARR_URL}/api/system/ping")
        out.append(_row(r.status_code == 200, "bazarr ping", str(r.status_code)))
    except Exception as exc:
        out.append(_row(False, "bazarr ping", str(exc)[:200]))
    out.append(_alerts_row())
    out.append(_row(bool(config.MCP_TOKEN), "MCP token configured", ""))
    try:
        ledger = taste.load(config.TASTE_PATH)
        d = taste.duplicates(ledger)
        out.append(_row(not d, "taste.md one section per title",
                        f"{len(d)} titles in two sections" if d else f"{sum(len(v) for v in ledger.sections.values())} rows"))
    except Exception as exc:
        out.append(_row(False, "taste.md readable", str(exc)[:200]))
    try:
        free = await stack.disk_free_gb()
        out.append(_row(free is not None and free >= config.DISK_GUARD_GB, "media disk free",
                        f"{free} GB (guard {config.DISK_GUARD_GB:.0f} GB)"))
    except Exception as exc:
        out.append(_row(False, "media disk free", str(exc)[:200]))
    return out


async def run_all() -> dict[str, Any]:
    groups = await asyncio.gather(_arr("sonarr"), _arr("radarr"), _prowlarr(), _plex(), _qbit(), _misc())
    rows = [r for g in groups for r in g]
    return {"ok": all(r["ok"] for r in rows), "failed": sum(not r["ok"] for r in rows), "checks": rows}
