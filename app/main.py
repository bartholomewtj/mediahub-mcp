"""MediaHub — the MCP server for a home media stack, plus a read-only status page.

    python -m uvicorn app.main:app --port 8000

- POST /mcp     the agent interface (bearer token; see mcp_server.py)
- GET  /        read-only status page (no actions)
- GET  /api/*   the JSON the page reads, and /api/checks for verify-stack.ps1

The page has no login and no buttons: every action goes through the MCP,
where the trust rules live. Published on 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import checks, config, mcp_server, services, stack, watch as watch_mod

STATIC = Path(__file__).parent / "static"

_mcp_app = mcp_server.http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    task = asyncio.create_task(watch_mod.run(stop))
    async with mcp_server.mcp.session_manager.run():
        yield
    stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="MediaHub", docs_url=None, redoc_url=None, lifespan=lifespan)

# MCP: /mcp, bearer token required. The route comes from the MCP SDK's
# Starlette app; adding it directly (rather than mounting) keeps the exact
# path /mcp with no redirect, which some clients will not follow on POST.
app.router.routes.extend(_mcp_app.routes)


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.rstrip("/") == "/mcp":
        if not config.MCP_TOKEN:
            return JSONResponse({"error": "MCP disabled: no MCP_TOKEN"}, status_code=503)
        sent = request.headers.get("authorization", "")
        if not hmac.compare_digest(sent.encode(), f"Bearer {config.MCP_TOKEN}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    """Everything the page renders, in one call."""
    data = dict(await services.gather_status())
    watch = await watch_mod.snapshot()
    data["watch"] = watch
    try:
        data["disk_free_gb"] = await stack.disk_free_gb()
    except Exception:
        data["disk_free_gb"] = None
    data["disk_guard_gb"] = config.DISK_GUARD_GB
    if watch.get("problems"):
        data["problems"] = list(data.get("problems") or []) + list(watch["problems"])
        order = {"error": 0, "warning": 1, "notice": 2, "info": 3}
        data["problems"].sort(key=lambda p: order.get(p["severity"], 9))
    return data


@app.get("/api/checks")
async def deep_checks() -> dict[str, Any]:
    """Deep health checks (verify-stack.ps1 calls this). No secrets in the output."""
    return await checks.run_all()


@app.get("/api/watch")
async def watch_status() -> dict[str, Any]:
    """The watch loop's last pass: counts, action log, escalations."""
    return await watch_mod.snapshot()


@app.get("/api/config")
async def client_config() -> dict[str, Any]:
    """Non-secret settings the page needs. Never returns keys."""
    return {
        "links": {
            "Sonarr": config.SONARR_URL,
            "Radarr": config.RADARR_URL,
            "Prowlarr": config.PROWLARR_URL,
            "Plex": config.PLEX_URL,
            "qBittorrent": config.QBIT_URL,
            "Bazarr": config.BAZARR_URL,
        },
    }
