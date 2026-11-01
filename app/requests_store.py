"""Requests made through the MCP, persisted in data/requests.json.

The watch loop rescues these if nothing gets grabbed, and the hourly request
cap counts them. The lock here is short: it never spans network I/O, so a
request() never waits behind the watch loop's indexer searches, and a pass
never overwrites a request recorded while it was running.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import config

KEEP = timedelta(days=14)
_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _path() -> Path:
    return config.REQUESTS_PATH


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(rows: list[dict[str, Any]]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(p)


def _prune(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        at = _parse(r.get("requested_at"))
        if at and now - at <= KEEP:
            out.append(r)
    return out


async def snapshot(now: datetime | None = None) -> list[dict[str, Any]]:
    async with _lock:
        return _prune(_read(), now or _now())


async def add(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    row = {**row, "requested_at": now.isoformat(), "attempts": {}}
    async with _lock:
        rows = _prune(_read(), now)
        rows.append(row)
        _write(rows)
    return row


async def count_since(delta: timedelta, now: datetime | None = None) -> int:
    now = now or _now()
    rows = await snapshot(now)
    return sum(1 for r in rows if (at := _parse(r.get("requested_at"))) and now - at <= delta)


def _grabs_path() -> Path:
    return config.REQUESTS_PATH.with_name("agent-grabs.json")


async def agent_grabs(target: str, now: datetime | None = None) -> int:
    """How many agent grabs this target has had in the last 14 days."""
    now = now or _now()
    async with _lock:
        try:
            data = json.loads(_grabs_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    stamps = [_parse(s) for s in (data.get(target) or [])]
    return sum(1 for s in stamps if s and now - s <= KEEP)


async def add_agent_grab(target: str, now: datetime | None = None) -> None:
    now = now or _now()
    async with _lock:
        try:
            data = json.loads(_grabs_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data = {k: [s for s in v if (p := _parse(s)) and now - p <= KEEP] for k, v in data.items()}
        data.setdefault(target, []).append(now.isoformat())
        p = _grabs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: v for k, v in data.items() if v}, indent=2), encoding="utf-8")
        tmp.replace(p)


async def update(match: Callable[[dict[str, Any]], bool],
                 fn: Callable[[dict[str, Any]], None],
                 now: datetime | None = None) -> int:
    """Fresh read-modify-write of every row where match(row); returns how many."""
    now = now or _now()
    async with _lock:
        rows = _prune(_read(), now)
        n = 0
        for r in rows:
            if match(r):
                fn(r)
                n += 1
        if n:
            _write(rows)
        return n
