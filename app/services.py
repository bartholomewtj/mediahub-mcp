"""The status snapshot: Sonarr, Radarr, Prowlarr, Plex, qBittorrent and Bazarr in one poll.

Every function here is defensive: if a service is down or slow it returns a
"down" result rather than raising, so one dead service never blanks the whole
dashboard. They're all polled concurrently, so a page load takes about as long
as the slowest single service instead of the sum of them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config

log = logging.getLogger("mediahub.services")

# One poll of every service costs several seconds and wakes every app.
# The page and MCP status share this snapshot instead of each firing
# their own. Actions call invalidate_status_cache() so a click is not stale.
STATUS_CACHE_SECONDS = 45.0
_status_cache: dict[str, Any] | None = None
_status_cache_at: float = 0.0
_status_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _bytes_to_gb(n: float | None) -> float | None:
    if n is None:
        return None
    return round(n / (1024 ** 3), 1)


def _down(name: str, url: str, error: str) -> dict[str, Any]:
    return {"name": name, "url": url, "up": False, "version": None, "error": error}


def _skip_managed_health(source: str) -> bool:
    """Health checks that are expected noise on pinned stacks (Docker pins)."""
    if not config.PINNED_STACK:
        return False
    return source in ("UpdateCheck",)


def _friendly_error(exc: Exception) -> str:
    """Turn an httpx exception into something a non-developer can act on."""
    if isinstance(exc, httpx.ConnectError):
        return "Can't connect — is the service running?"
    if isinstance(exc, httpx.TimeoutException):
        return f"No response within {config.TIMEOUT:.0f}s"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "Rejected the API key — check .env"
        return f"Returned HTTP {code}"
    return str(exc) or exc.__class__.__name__


# --------------------------------------------------------------------------
# Sonarr / Radarr  (identical v3 API, so one set of functions covers both)
# --------------------------------------------------------------------------

async def _arr_get(client: httpx.AsyncClient, base: str, key: str, path: str, **params):
    r = await client.get(
        f"{base}/api/v3/{path}",
        headers={"X-Api-Key": key},
        params=params or None,
    )
    r.raise_for_status()
    return r.json()


async def _arr_queue(client: httpx.AsyncClient, base: str, key: str) -> list[dict[str, Any]]:
    """Every queue page, not just the first 100. A full-series grab overflows one page."""
    records: list[dict[str, Any]] = []
    page = 1
    while page <= 20:
        data = await _arr_get(
            client, base, key, "queue",
            page=page, pageSize=100,
            includeUnknownSeriesItems=True, includeUnknownMovieItems=True,
        )
        batch = data.get("records") or [] if isinstance(data, dict) else []
        records.extend(batch)
        total = data.get("totalRecords") if isinstance(data, dict) else None
        if not batch or (total is not None and len(records) >= total):
            break
        page += 1
    return records


async def _arr_snapshot(client: httpx.AsyncClient, name: str, base: str, key: str) -> dict[str, Any]:
    """Status + health warnings + download queue + disk space for one *arr app."""
    if not key:
        return {
            "service": _down(name, base, "No API key set — run sync-runtime-secrets.ps1 or edit secrets.env"),
            "problems": [], "queue": [], "disks": [],
        }

    try:
        status = await _arr_get(client, base, key, "system/status")
    except Exception as exc:
        return {
            "service": _down(name, base, _friendly_error(exc)),
            "problems": [], "queue": [], "disks": [],
        }

    service = {
        "name": name,
        "url": base,
        "up": True,
        "version": status.get("version"),
        "error": None,
    }

    # These three are independent — fetch them at the same time.
    health, queue_records, diskspace = await asyncio.gather(
        _arr_get(client, base, key, "health"),
        _arr_queue(client, base, key),
        _arr_get(client, base, key, "diskspace"),
        return_exceptions=True,
    )
    # diskspace hits SMB-backed paths and often loses the shared-client race.
    if not isinstance(diskspace, list):
        try:
            async with httpx.AsyncClient(
                timeout=config.TIMEOUT * 2, follow_redirects=True,
            ) as disk_client:
                diskspace = await _arr_get(disk_client, base, key, "diskspace")
        except Exception:
            diskspace = []

    problems: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    disks: list[dict[str, Any]] = []

    # --- health checks (the *arr apps' own self-diagnosis) ---
    if isinstance(health, list):
        for item in health:
            source = item.get("source") or "Health check"
            if _skip_managed_health(source):
                continue
            problems.append({
                "id": f"{name.lower()}:health:{source}",
                "service": name,
                "kind": "health",
                "severity": (item.get("type") or "warning").lower(),
                "title": item.get("source") or "Health check",
                "detail": item.get("message") or "",
                "link": item.get("wikiUrl"),
                # An indexer warning here is really a Prowlarr question, so it
                # gets Prowlarr's test button.
                "actions": _indexer_actions(source),
            })

    # --- download queue ---
    if isinstance(queue_records, list):
        for rec in queue_records:
            size = rec.get("size") or 0
            left = rec.get("sizeleft") or 0
            percent = round((size - left) / size * 100, 1) if size else 0.0
            quality = rec.get("quality") or {}
            qinfo = quality.get("quality") if isinstance(quality, dict) else {}
            if not isinstance(qinfo, dict):
                qinfo = {}

            queue.append({
                "service": name,
                "id": rec.get("id"),
                "title": rec.get("title") or "(untitled)",
                "status": rec.get("status"),
                "state": rec.get("trackedDownloadState"),
                "percent": percent,
                "size_gb": _bytes_to_gb(size),
                "size": size,
                "sizeleft": left,
                "eta": rec.get("timeleft"),
                "indexer": rec.get("indexer"),
                "download_id": rec.get("downloadId"),
                "movie_id": rec.get("movieId"),
                "episode_id": rec.get("episodeId"),
                "series_id": rec.get("seriesId"),
                "resolution": qinfo.get("resolution"),
                "quality_name": qinfo.get("name"),
                "added": rec.get("added"),
                "protocol": rec.get("protocol"),
                "tracked_status": rec.get("trackedDownloadStatus"),
            })

            # An item is a problem if the *arr app flagged it, not just if it's slow.
            tracked = (rec.get("trackedDownloadStatus") or "ok").lower()
            if tracked in ("warning", "error"):
                messages = []
                for sm in rec.get("statusMessages") or []:
                    messages.extend(sm.get("messages") or [])
                if rec.get("errorMessage"):
                    messages.append(rec["errorMessage"])

                problems.append({
                    "id": f"{name.lower()}:queue:{rec.get('id')}",
                    "service": name,
                    "kind": "queue",
                    "severity": "error" if tracked == "error" else "warning",
                    "title": rec.get("title") or "(untitled)",
                    "detail": " | ".join(messages) or f"Stuck in state: {rec.get('trackedDownloadState')}",
                    "link": None,
                    # These map to buttons in the UI.
                    "actions": [
                        {"label": "Remove + blocklist", "action": "queue_remove",
                         "params": {"service": name.lower(), "id": rec.get("id")}},
                        {"label": "Re-check downloads", "action": "queue_refresh",
                         "params": {"service": name.lower()}},
                    ],
                })

    # --- disk space ---
    if isinstance(diskspace, list):
        for d in diskspace:
            total = d.get("totalSpace") or 0
            free = d.get("freeSpace") or 0
            if not total:
                continue
            used_pct = round((total - free) / total * 100, 1)
            disks.append({
                "service": name,
                "path": d.get("path"),
                "label": d.get("label") or "",
                "free_gb": _bytes_to_gb(free),
                "total_gb": _bytes_to_gb(total),
                "used_percent": used_pct,
            })
            if used_pct >= 95:
                problems.append({
                    "id": f"{name.lower()}:disk:{d.get('path')}",
                    "service": name,
                    "kind": "disk",
                    "severity": "error" if used_pct >= 98 else "warning",
                    "title": f"Disk {d.get('path')} is {used_pct}% full",
                    "detail": f"{_bytes_to_gb(free)} GB free of {_bytes_to_gb(total)} GB.",
                    "link": None,
                    "actions": [],
                })

    return {"service": service, "problems": problems, "queue": queue, "disks": disks}


def _arr_creds(which: str) -> tuple[str, str]:
    if which == "sonarr":
        return config.SONARR_URL, config.SONARR_API_KEY
    return config.RADARR_URL, config.RADARR_API_KEY


async def arr_queue_remove(which: str, item_id: int, search_again: bool = True) -> str:
    """Remove a stuck item from the queue and blocklist it.

    search_again=True is the dashboard button: *arr picks the next release.
    search_again=False is the watch loop: we are about to grab a specific one.
    """
    base, key = _arr_creds(which)
    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        r = await client.delete(
            f"{base}/api/v3/queue/{item_id}",
            headers={"X-Api-Key": key},
            params={
                "removeFromClient": "true",
                "blocklist": "true",
                "skipRedownload": "false" if search_again else "true",
            },
        )
        r.raise_for_status()
    if search_again:
        return "Removed and blocklisted. A replacement search has been queued."
    return "Removed and blocklisted."


async def _arr_run_command(client: httpx.AsyncClient, base: str, key: str, command: str) -> None:
    r = await client.post(
        f"{base}/api/v3/command",
        headers={"X-Api-Key": key},
        json={"name": command},
    )
    r.raise_for_status()


async def arr_queue_refresh(which: str) -> str:
    """Ask the *arr app to re-inspect its download client right now."""
    base, key = _arr_creds(which)
    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        await _arr_run_command(client, base, key, "RefreshMonitoredDownloads")
    return "Re-checking downloads — give it a few seconds, then refresh."


# Indexer searches are slow — well past the 8s dashboard timeout.
_SEARCH_TIMEOUT = 90


async def arr_search_releases(
    which: str, *, movie_id: int | None = None, episode_id: int | None = None,
) -> list[dict[str, Any]]:
    """Hit indexers for one movie or episode. Can take up to a minute."""
    base, key = _arr_creds(which)
    params: dict[str, Any] = {}
    if movie_id:
        params["movieId"] = movie_id
    if episode_id:
        params["episodeId"] = episode_id
    if not params:
        return []
    async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(
            f"{base}/api/v3/release", headers={"X-Api-Key": key}, params=params,
        )
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


async def arr_grab_release(which: str, guid: str, indexer_id: int) -> None:
    """Grab one specific release. guid + indexerId is what *arr needs."""
    base, key = _arr_creds(which)
    async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT, follow_redirects=True) as client:
        r = await client.post(
            f"{base}/api/v3/release",
            headers={"X-Api-Key": key},
            json={"guid": guid, "indexerId": indexer_id},
        )
        r.raise_for_status()


async def arr_get_movie(movie_id: int) -> dict[str, Any] | None:
    base, key = _arr_creds("radarr")
    if not key:
        return None
    async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
        r = await client.get(
            f"{base}/api/v3/movie/{movie_id}", headers={"X-Api-Key": key},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def arr_get_series(series_id: int) -> dict[str, Any] | None:
    base, key = _arr_creds("sonarr")
    if not key:
        return None
    async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
        r = await client.get(
            f"{base}/api/v3/series/{series_id}", headers={"X-Api-Key": key},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def arr_get_episodes(series_id: int) -> list[dict[str, Any]]:
    base, key = _arr_creds("sonarr")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
        r = await client.get(
            f"{base}/api/v3/episode",
            headers={"X-Api-Key": key},
            params={"seriesId": series_id},
        )
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


async def prowlarr_is_up() -> bool:
    base, key = config.PROWLARR_URL, config.PROWLARR_API_KEY
    if not key:
        return False
    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
            r = await client.get(
                f"{base}/api/v1/system/status", headers={"X-Api-Key": key},
            )
            r.raise_for_status()
        return True
    except Exception:
        return False


async def refresh_all() -> str:
    """Kick everything that holds a cached answer, so the next poll is honest.

    Most of the dashboard is already live — disk space, the queue, playback and
    torrents are read fresh from each service on each dashboard poll. The exception is
    Sonarr and Radarr, which cache two things and only redo them on a timer:
    their health checks, and their picture of what the download client is doing.
    So those are the two we tell them to redo now.

    Nothing here changes or deletes anything — both commands are just "look
    again".
    """
    invalidate_status_cache()
    targets = [
        ("Sonarr", config.SONARR_URL, config.SONARR_API_KEY),
        ("Radarr", config.RADARR_URL, config.RADARR_API_KEY),
    ]
    commands = ("CheckHealth", "RefreshMonitoredDownloads")

    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        results = await asyncio.gather(
            *(
                _arr_run_command(client, base, key, command)
                for _, base, key in targets
                for command in commands
            ),
            return_exceptions=True,
        )

    # One result per (app, command) pair — an app counts as failed if either
    # of its two commands didn't land.
    failed = []
    for i, (name, _, _) in enumerate(targets):
        chunk = results[i * len(commands):(i + 1) * len(commands)]
        problem = next((r for r in chunk if isinstance(r, BaseException)), None)
        if problem is not None:
            failed.append(f"{name} ({_friendly_error(problem)})")

    if len(failed) == len(targets):
        return "Couldn't reach either app: " + ", ".join(failed)
    if failed:
        return f"Refreshing. Couldn't reach {failed[0]}."
    return "Refreshed. Disks and downloads are current; health results land in a few seconds."


# --------------------------------------------------------------------------
# Prowlarr  (v1 API — same shape as the *arr apps, different version prefix)
# --------------------------------------------------------------------------

async def prowlarr_snapshot(client: httpx.AsyncClient) -> dict[str, Any]:
    """Status and health warnings. Prowlarr has no queue or disks of its own."""
    base, key = config.PROWLARR_URL, config.PROWLARR_API_KEY
    if not key:
        return {"service": _down("Prowlarr", base, "No API key set — run sync-runtime-secrets.ps1 or edit secrets.env"),
                "problems": []}

    headers = {"X-Api-Key": key}
    try:
        r = await client.get(f"{base}/api/v1/system/status", headers=headers)
        r.raise_for_status()
        status = r.json()
    except Exception as exc:
        return {"service": _down("Prowlarr", base, _friendly_error(exc)), "problems": []}

    service = {"name": "Prowlarr", "url": base, "up": True,
               "version": status.get("version"), "error": None}

    problems: list[dict[str, Any]] = []
    try:
        r = await client.get(f"{base}/api/v1/health", headers=headers)
        r.raise_for_status()
        for item in r.json() or []:
            source = item.get("source") or "Health check"
            if _skip_managed_health(source):
                continue
            problems.append({
                "id": f"prowlarr:health:{source}",
                "service": "Prowlarr",
                "kind": "health",
                "severity": (item.get("type") or "warning").lower(),
                "title": source,
                "detail": item.get("message") or "",
                "link": item.get("wikiUrl"),
                "actions": _indexer_actions(source),
            })
    except Exception:
        pass  # Health is a nice-to-have; being up is the signal.

    return {"service": service, "problems": problems}


def _indexer_actions(source: str) -> list[dict[str, Any]]:
    """The Test-all button, but only on warnings that are actually about indexers.

    Used by Sonarr and Radarr too: when they report an indexer failing, Prowlarr
    is where you test it, so their warning gets the button as well.
    """
    if not config.PROWLARR_API_KEY or "indexer" not in source.lower():
        return []
    return [{"label": "Test all indexers", "action": "prowlarr_test", "params": {}}]


async def prowlarr_test_indexers() -> str:
    """Ask Prowlarr to test every indexer now, and say which ones answered.

    Read-only: this is the same check as the Test button in Prowlarr's own UI.
    It doesn't change settings or disable anything.
    """
    base, key = config.PROWLARR_URL, config.PROWLARR_API_KEY
    if not key:
        return "No Prowlarr API key set — edit secrets.env."

    # Testing six indexers over the network is slow; well past the 8s default.
    async with httpx.AsyncClient(timeout=90) as client:
        try:
            r = await client.post(f"{base}/api/v1/indexer/testall", headers={"X-Api-Key": key})
            r.raise_for_status()
            results = r.json() or []
        except httpx.HTTPStatusError:
            indexers = (await client.get(f"{base}/api/v1/indexer", headers={"X-Api-Key": key})).json() or []
            enabled = [i for i in indexers if i.get("enable")]
            if not enabled:
                return "No enabled indexers to test."
            r = await client.post(
                f"{base}/api/v1/indexer/test",
                headers={"X-Api-Key": key},
                json=enabled,
            )
            r.raise_for_status()
            results = r.json() or []

    if not results:
        return "Prowlarr has no indexers configured."

    failed = [str(i.get("id")) for i in results if not i.get("isValid")]
    if not failed:
        return (f"All {len(results)} indexers responded. Sonarr and Radarr clear their "
                "own warning within about an hour of the indexer working again.")

    names = await _prowlarr_indexer_names(client_key=key, base=base, ids=failed)
    return f"{len(failed)} of {len(results)} indexers failed: {', '.join(names)}."


async def _prowlarr_indexer_names(client_key: str, base: str, ids: list[str]) -> list[str]:
    """Turn indexer ids into names, so the message says "Example" not "9"."""
    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
            r = await client.get(f"{base}/api/v1/indexer", headers={"X-Api-Key": client_key})
            r.raise_for_status()
            by_id = {str(i.get("id")): i.get("name") for i in r.json() or []}
        return [by_id.get(i) or f"#{i}" for i in ids]
    except Exception:
        return [f"#{i}" for i in ids]


# --------------------------------------------------------------------------
# Plex
# --------------------------------------------------------------------------

async def plex_snapshot(client: httpx.AsyncClient) -> dict[str, Any]:
    base, token = config.PLEX_URL, config.PLEX_TOKEN
    if not token:
        return {"service": _down("Plex", base, "No token set — edit secrets.env"), "now_playing": []}

    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    try:
        r = await client.get(f"{base}/", headers=headers)
        r.raise_for_status()
        container = r.json().get("MediaContainer", {})
    except Exception as exc:
        return {"service": _down("Plex", base, _friendly_error(exc)), "now_playing": []}

    service = {
        "name": "Plex",
        "url": base,
        "up": True,
        "version": container.get("version"),
        "error": None,
    }

    now_playing: list[dict[str, Any]] = []
    try:
        r = await client.get(f"{base}/status/sessions", headers=headers)
        r.raise_for_status()
        sessions = r.json().get("MediaContainer", {}).get("Metadata", []) or []
        for s in sessions:
            # Episodes carry the show name in grandparentTitle.
            title = s.get("grandparentTitle") or s.get("title") or "(unknown)"
            sub = s.get("title") if s.get("grandparentTitle") else None
            player = (s.get("Player") or {})
            now_playing.append({
                "title": title,
                "subtitle": sub,
                "user": (s.get("User") or {}).get("title"),
                "player": player.get("title"),
                "state": player.get("state"),
            })
    except Exception:
        pass  # Playback info is a nice-to-have; the server being up is the signal.

    return {"service": service, "now_playing": now_playing}


# --------------------------------------------------------------------------
# qBittorrent  (cookie auth — log in, then reuse the session cookie)
# --------------------------------------------------------------------------

async def qbit_snapshot(client: httpx.AsyncClient) -> dict[str, Any]:
    base = config.QBIT_URL
    empty = {"service": None, "torrents": [], "problems": [], "transfer": {}}

    try:
        # qBittorrent can be configured to skip auth on localhost, in which case
        # the login call is unnecessary — but harmless, so we always try it.
        if config.QBIT_USER:
            r = await client.post(
                f"{base}/api/v2/auth/login",
                data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
                headers={"Referer": base},
            )
            # Success is 204 with no body from qBittorrent 5.2 on, 200 "Ok." before;
            # a wrong password is 401 (5.2+) or 200 "Fails.".
            if r.status_code == 401 or (r.status_code != 204 and "Ok." not in r.text):
                return {**empty, "service": _down("qBittorrent", base,
                                                  "Login rejected — check QBIT_USER / QBIT_PASS")}

        r = await client.get(f"{base}/api/v2/app/version")
        r.raise_for_status()
        version = r.text.strip()
    except Exception as exc:
        return {**empty, "service": _down("qBittorrent", base, _friendly_error(exc))}

    service = {"name": "qBittorrent", "url": base, "up": True, "version": version, "error": None}

    torrents: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    transfer: dict[str, Any] = {}

    # States qBittorrent uses to mean "this is not going to finish on its own".
    BAD_STATES = {
        "error": "Torrent is in an error state",
        "missingFiles": "Files are missing from disk",
        "stalledDL": "Downloading but no peers are sending data",
        "pausedDL": "Paused before finishing",
    }

    try:
        info_res, transfer_res = await asyncio.gather(
            client.get(f"{base}/api/v2/torrents/info"),
            client.get(f"{base}/api/v2/transfer/info"),
            return_exceptions=True,
        )
        if isinstance(transfer_res, httpx.Response) and transfer_res.is_success:
            t = transfer_res.json()
            transfer = {
                "dl_speed_mbps": round((t.get("dl_info_speed") or 0) / 1_000_000, 2),
                "up_speed_mbps": round((t.get("up_info_speed") or 0) / 1_000_000, 2),
            }
        if isinstance(info_res, httpx.Response) and info_res.is_success:
            for t in info_res.json():
                state = t.get("state") or ""
                torrents.append({
                    "hash": t.get("hash"),
                    "name": t.get("name"),
                    "state": state,
                    "percent": round((t.get("progress") or 0) * 100, 1),
                    "size_gb": _bytes_to_gb(t.get("size")),
                    "dl_speed_mbps": round((t.get("dlspeed") or 0) / 1_000_000, 2),
                    "seeders": t.get("num_seeds") or 0,
                    "dlspeed": t.get("dlspeed") or 0,
                    "downloaded": t.get("downloaded") or 0,
                    "amount_left": t.get("amount_left") or 0,
                    "added_on": t.get("added_on"),
                })
                if state in BAD_STATES:
                    problems.append({
                        "id": f"qbittorrent:torrent:{t.get('hash')}",
                        "service": "qBittorrent",
                        "kind": "torrent",
                        "severity": "error" if state in ("error", "missingFiles") else "warning",
                        "title": t.get("name") or "(unnamed torrent)",
                        "detail": f"{BAD_STATES[state]} (state: {state}, {round((t.get('progress') or 0) * 100)}% done).",
                        "link": None,
                        "actions": [
                            {"label": "Force recheck", "action": "qbit_recheck",
                             "params": {"hash": t.get("hash")}},
                            {"label": "Resume", "action": "qbit_resume",
                             "params": {"hash": t.get("hash")}},
                        ],
                    })
    except Exception:
        pass

    return {"service": service, "torrents": torrents, "problems": problems, "transfer": transfer}


async def _qbit_action(path: str, torrent_hash: str, done_message: str) -> str:
    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        if config.QBIT_USER:
            await client.post(
                f"{config.QBIT_URL}/api/v2/auth/login",
                data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
                headers={"Referer": config.QBIT_URL},
            )
        r = await client.post(
            f"{config.QBIT_URL}/api/v2/torrents/{path}",
            data={"hashes": torrent_hash},
            headers={"Referer": config.QBIT_URL},
        )
        r.raise_for_status()
    return done_message


async def qbit_recheck(torrent_hash: str) -> str:
    return await _qbit_action("recheck", torrent_hash,
                              "Recheck started — qBittorrent is re-verifying the files on disk.")


async def qbit_resume(torrent_hash: str) -> str:
    """qBittorrent 5 renamed torrents/resume to torrents/start; try the new name first."""
    try:
        return await _qbit_action("start", torrent_hash, "Resume sent.")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return await _qbit_action("resume", torrent_hash, "Resume sent.")


# --------------------------------------------------------------------------
# Bazarr  (English subtitles next to each file)
# --------------------------------------------------------------------------

async def bazarr_snapshot(client: httpx.AsyncClient) -> dict[str, Any]:
    """Up/down + version. Missing-sub counts are a nice-to-have on the card."""
    base, key = config.BAZARR_URL, config.BAZARR_API_KEY
    if not key:
        return {"service": _down("Bazarr", base, "No API key set — run sync-runtime-secrets.ps1 or edit secrets.env")}

    try:
        # Bazarr 1.6 can block its authenticated status endpoint while startup
        # jobs hold the database. The anonymous ping is the intended liveness
        # check and does not contend with those jobs.
        r = await client.get(f"{base}/api/system/ping")
        r.raise_for_status()
    except Exception as exc:
        return {"service": _down("Bazarr", base, _friendly_error(exc))}

    return {
        "service": {
            "name": "Bazarr",
            "url": base,
            "up": True,
            "version": None,
            "error": None,
        }
    }


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00").replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def invalidate_status_cache() -> None:
    """Drop the shared snapshot. The next read polls the stack again."""
    global _status_cache_at
    _status_cache_at = 0.0


async def gather_status() -> dict[str, Any]:
    """Poll every service at once and fold the results into one payload.

    Repeat reads inside STATUS_CACHE_SECONDS return the same snapshot, so the
    page and repeated MCP status calls do not each wake every service.
    """
    global _status_cache, _status_cache_at
    now = time.monotonic()
    if _status_cache is not None and now - _status_cache_at < STATUS_CACHE_SECONDS:
        return _status_cache
    async with _status_lock:
        now = time.monotonic()
        if _status_cache is not None and now - _status_cache_at < STATUS_CACHE_SECONDS:
            return _status_cache
        data = await _gather_status()
        _status_cache = data
        _status_cache_at = time.monotonic()
        return data


async def _gather_status() -> dict[str, Any]:
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(
        timeout=config.TIMEOUT, follow_redirects=True, limits=limits,
    ) as client:
        sonarr, radarr, prowlarr, plex, qbit, bazarr = await asyncio.gather(
            _arr_snapshot(client, "Sonarr", config.SONARR_URL, config.SONARR_API_KEY),
            _arr_snapshot(client, "Radarr", config.RADARR_URL, config.RADARR_API_KEY),
            prowlarr_snapshot(client),
            plex_snapshot(client),
            qbit_snapshot(client),
            bazarr_snapshot(client),
        )

    services = [sonarr["service"], radarr["service"], prowlarr["service"], plex["service"]]
    if qbit["service"]:
        services.append(qbit["service"])
    if bazarr["service"]:
        services.append(bazarr["service"])

    problems = (sonarr["problems"] + radarr["problems"]
                + prowlarr["problems"] + qbit["problems"])
    # Errors first, then warnings — the top of the list is what needs attention.
    order = {"error": 0, "warning": 1, "notice": 2, "info": 3}
    problems.sort(key=lambda p: order.get(p["severity"], 9))

    return {
        "services": services,
        "problems": problems,
        "queue": sonarr["queue"] + radarr["queue"],
        "disks": sonarr["disks"] + radarr["disks"],
        "torrents": qbit["torrents"],
        "transfer": qbit["transfer"],
        "now_playing": plex["now_playing"],
    }
