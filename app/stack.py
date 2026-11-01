"""Thin async wrappers over the stack's APIs, for the MCP tools and the watch loop.

services.py builds the dashboard snapshot; this module does single, targeted
reads and writes. Nothing here decides anything — policy lives in tools.py,
watch.py and health.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config

SEARCH_TIMEOUT = 90


class StackError(Exception):
    pass


# --------------------------------------------------------------------------
# *arr
# --------------------------------------------------------------------------

def _creds(app: str) -> tuple[str, str]:
    if app == "sonarr":
        return config.SONARR_URL, config.SONARR_API_KEY
    if app == "radarr":
        return config.RADARR_URL, config.RADARR_API_KEY
    raise StackError(f"unknown app {app}")


async def arr(app: str, method: str, path: str, *, params: dict | None = None,
              json: Any = None, timeout: float | None = None) -> Any:
    base, key = _creds(app)
    if not key:
        raise StackError(f"{app} has no API key")
    async with httpx.AsyncClient(timeout=timeout or config.TIMEOUT * 2, follow_redirects=True) as c:
        r = await c.request(method, f"{base}/api/v3/{path}", headers={"X-Api-Key": key},
                            params=params, json=json)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        detail = r.text[:300]
        raise StackError(f"{app} {method} {path} -> HTTP {r.status_code}: {detail}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


async def lookup_movie_tmdb(tmdb_id: int) -> dict | None:
    data = await arr("radarr", "GET", "movie/lookup/tmdb", params={"tmdbId": tmdb_id})
    return data if isinstance(data, dict) and data.get("tmdbId") else None


async def lookup(app: str, term: str) -> list[dict]:
    path = "movie/lookup" if app == "radarr" else "series/lookup"
    data = await arr(app, "GET", path, params={"term": term}, timeout=30)
    return data if isinstance(data, list) else []


async def lookup_series_tmdb(tmdb_id: int) -> dict | None:
    hits = await lookup("sonarr", f"tmdb:{tmdb_id}")
    for h in hits:
        if h.get("tmdbId") == tmdb_id or len(hits) == 1:
            return h
    return None


async def movies() -> list[dict]:
    return await arr("radarr", "GET", "movie", timeout=30) or []


async def series_list() -> list[dict]:
    return await arr("sonarr", "GET", "series", timeout=30) or []


async def get_movie(movie_id: int) -> dict | None:
    return await arr("radarr", "GET", f"movie/{movie_id}")


async def get_series(series_id: int) -> dict | None:
    return await arr("sonarr", "GET", f"series/{series_id}")


async def episodes(series_id: int) -> list[dict]:
    return await arr("sonarr", "GET", "episode", params={"seriesId": series_id}, timeout=30) or []


async def episode_files(series_id: int) -> list[dict]:
    return await arr("sonarr", "GET", "episodefile", params={"seriesId": series_id}, timeout=30) or []


async def profile_id(app: str, name: str) -> int:
    profiles = await arr(app, "GET", "qualityprofile") or []
    for p in profiles:
        if p.get("name") == name:
            return int(p["id"])
    if profiles:
        return int(profiles[0]["id"])
    raise StackError(f"{app} has no quality profiles")


async def root_folder(app: str) -> str:
    roots = await arr(app, "GET", "rootfolder") or []
    if not roots:
        raise StackError(f"{app} has no root folder")
    return roots[0]["path"]


async def disk_free_gb(app: str = "sonarr") -> float | None:
    """Free space where media lands, from the root folder record.

    Not /diskspace: inside Docker Desktop that also lists the VM's overlay
    filesystem, which has hundreds of GB free while C:\\Media is nearly full.
    """
    roots = await arr(app, "GET", "rootfolder") or []
    free = [r.get("freeSpace") for r in roots if isinstance(r.get("freeSpace"), (int, float))]
    return round(min(free) / 1e9, 1) if free else None


async def add_movie(lookup_obj: dict, *, profile: int, root: str, path: str | None = None,
                    search: bool = True) -> dict:
    body = {**lookup_obj, "qualityProfileId": profile, "rootFolderPath": root,
            "monitored": True, "minimumAvailability": "released",
            "addOptions": {"searchForMovie": search}}
    if path:
        body["path"] = path
    return await arr("radarr", "POST", "movie", json=body, timeout=60)


async def add_series(lookup_obj: dict, *, seasons: list[int], profile: int, root: str,
                     path: str | None = None) -> dict:
    wanted = set(seasons)
    season_list = [{**s, "monitored": s.get("seasonNumber") in wanted}
                   for s in lookup_obj.get("seasons") or []]
    body = {**lookup_obj, "seasons": season_list, "qualityProfileId": profile,
            "rootFolderPath": root, "monitored": True, "seasonFolder": True,
            "addOptions": {"monitor": "none", "searchForMissingEpisodes": False,
                           "searchForCutoffUnmetEpisodes": False}}
    if path:
        body["path"] = path
    return await arr("sonarr", "POST", "series", json=body, timeout=60)


async def put_series(series: dict) -> dict:
    return await arr("sonarr", "PUT", f"series/{series['id']}", json=series, timeout=60)


async def monitor_episodes(episode_ids: list[int], monitored: bool = True) -> None:
    if episode_ids:
        await arr("sonarr", "PUT", "episode/monitor",
                  json={"episodeIds": episode_ids, "monitored": monitored})


async def command(app: str, name: str, **body: Any) -> dict | None:
    return await arr(app, "POST", "command", json={"name": name, **body})


async def release_search(app: str, *, movie_id: int | None = None, episode_id: int | None = None,
                         series_id: int | None = None, season: int | None = None) -> list[dict]:
    params: dict[str, Any] = {}
    if movie_id:
        params["movieId"] = movie_id
    elif episode_id:
        params["episodeId"] = episode_id
    elif series_id is not None and season is not None:
        params["seriesId"] = series_id
        params["seasonNumber"] = season
    else:
        return []
    data = await arr(app, "GET", "release", params=params, timeout=SEARCH_TIMEOUT)
    return data if isinstance(data, list) else []


async def grab(app: str, guid: str, indexer_id: int) -> None:
    await arr(app, "POST", "release", json={"guid": guid, "indexerId": indexer_id},
              timeout=SEARCH_TIMEOUT)


async def push_release(app: str, *, title: str, download_url: str, protocol: str,
                       indexer: str | None, publish_date: str | None) -> list[dict]:
    """Hand a Prowlarr result to Sonarr/Radarr; they parse, decide and grab if approved."""
    body = {"title": title, "downloadUrl": download_url, "protocol": protocol,
            "publishDate": publish_date or datetime.now(timezone.utc).isoformat()}
    if indexer:
        body["indexer"] = indexer
    data = await arr(app, "POST", "release/push", json=body, timeout=SEARCH_TIMEOUT)
    if isinstance(data, dict):
        return [data]
    return data if isinstance(data, list) else []


async def queue(app: str) -> list[dict]:
    records: list[dict] = []
    page = 1
    while page <= 20:
        data = await arr(app, "GET", "queue", params={
            "page": page, "pageSize": 100,
            "includeUnknownSeriesItems": True, "includeUnknownMovieItems": True,
            **({"includeSeries": True, "includeEpisode": True} if app == "sonarr" else {"includeMovie": True}),
        }, timeout=30) or {}
        batch = data.get("records") or []
        records.extend(batch)
        if not batch or len(records) >= (data.get("totalRecords") or 0):
            break
        page += 1
    return records


async def queue_remove(app: str, item_id: int, *, blocklist: bool, search_again: bool) -> None:
    await arr(app, "DELETE", f"queue/{item_id}", params={
        "removeFromClient": "true", "blocklist": str(blocklist).lower(),
        "skipRedownload": str(not search_again).lower()})


async def queue_remove_bulk(app: str, ids: list[int], *, blocklist: bool, search_again: bool) -> None:
    if not ids:
        return
    await arr(app, "DELETE", "queue/bulk", params={
        "removeFromClient": "true", "blocklist": str(blocklist).lower(),
        "skipRedownload": str(not search_again).lower()}, json={"ids": ids})


_history_cache: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}


async def indexer_track_record(app: str, days: int = 90) -> dict[str, tuple[int, int]]:
    """indexer name -> (imported, failed) for grabs in the last `days`. Cached 1h."""
    hit = _history_cache.get(app)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = await arr(app, "GET", "history/since", params={"date": since}, timeout=60) or []
    by_dl: dict[str, set[str]] = {}
    grabs: list[dict] = []
    for r in recs:
        dl = r.get("downloadId")
        if dl:
            by_dl.setdefault(dl, set()).add(r.get("eventType") or "")
        if r.get("eventType") == "grabbed":
            grabs.append(r)
    seen: set[str] = set()
    counts: dict[str, list[int]] = {}
    for g in grabs:
        dl = g.get("downloadId")
        if not dl or dl in seen:
            continue
        seen.add(dl)
        events = by_dl.get(dl, set())
        name = ((g.get("data") or {}).get("indexer") or "?")
        c = counts.setdefault(name, [0, 0])
        if "downloadFolderImported" in events or "movieFileImported" in events:
            c[0] += 1
        elif "downloadFailed" in events:
            c[1] += 1
    out = {k: (v[0], v[1]) for k, v in counts.items()}
    _history_cache[app] = (time.monotonic(), out)
    return out


async def delete_movie(movie_id: int) -> None:
    await arr("radarr", "DELETE", f"movie/{movie_id}",
              params={"deleteFiles": "true", "addImportExclusion": "false"}, timeout=60)


async def delete_series(series_id: int) -> None:
    await arr("sonarr", "DELETE", f"series/{series_id}",
              params={"deleteFiles": "true", "addImportListExclusion": "false"}, timeout=60)


async def delete_episode_files(file_ids: list[int]) -> None:
    for fid in file_ids:
        await arr("sonarr", "DELETE", f"episodefile/{fid}", timeout=60)


# --------------------------------------------------------------------------
# Prowlarr
# --------------------------------------------------------------------------

async def prowlarr_search(query: str, *, categories: list[int], limit: int = 100) -> list[dict]:
    if not config.PROWLARR_API_KEY:
        raise StackError("prowlarr has no API key")
    params: list[tuple[str, Any]] = [("query", query), ("type", "search"), ("limit", limit)]
    params += [("categories", c) for c in categories]
    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as c:
        r = await c.get(f"{config.PROWLARR_URL}/api/v1/search",
                        headers={"X-Api-Key": config.PROWLARR_API_KEY}, params=params)
    if r.status_code >= 400:
        raise StackError(f"prowlarr search -> HTTP {r.status_code}")
    data = r.json()
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------
# qBittorrent
# --------------------------------------------------------------------------

async def qbit_torrents() -> list[dict]:
    """Raw torrents/info (keeps availability, num_seeds, progress, added_on).

    Generous timeout: qBittorrent answers slowly while it rechecks a big torrent.
    """
    async with httpx.AsyncClient(timeout=max(config.TIMEOUT, 25)) as c:
        if config.QBIT_USER:
            r = await c.post(f"{config.QBIT_URL}/api/v2/auth/login",
                             data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
                             headers={"Referer": config.QBIT_URL})
            r.raise_for_status()
        r = await c.get(f"{config.QBIT_URL}/api/v2/torrents/info")
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


async def qbit_delete(hashes: list[str], *, delete_files: bool) -> None:
    """Remove torrents. With delete_files, their files in /media/Downloads go too.

    Library files are separate hardlinks, so this never touches the library;
    it is how a delete actually frees space (the seeding copy shares blocks).
    """
    if not hashes:
        return
    async with httpx.AsyncClient(timeout=config.TIMEOUT * 2) as c:
        if config.QBIT_USER:
            r = await c.post(f"{config.QBIT_URL}/api/v2/auth/login",
                             data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
                             headers={"Referer": config.QBIT_URL})
            r.raise_for_status()
        r = await c.post(f"{config.QBIT_URL}/api/v2/torrents/delete",
                         data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()},
                         headers={"Referer": config.QBIT_URL})
        r.raise_for_status()


async def history_download_ids(app: str, *, series_id: int | None = None, movie_id: int | None = None,
                               seasons: list[int] | None = None) -> set[str]:
    """Torrent hashes that imported files for a series (optionally some seasons) or a movie."""
    if app == "sonarr":
        recs = await arr("sonarr", "GET", "history/series",
                         params={"seriesId": series_id, "includeEpisode": True}, timeout=60) or []
    else:
        recs = await arr("radarr", "GET", "history/movie", params={"movieId": movie_id}, timeout=60) or []
    out: set[str] = set()
    for r in recs:
        if r.get("eventType") not in ("downloadFolderImported", "grabbed"):
            continue
        if seasons is not None and ((r.get("episode") or {}).get("seasonNumber") not in seasons):
            continue
        if r.get("downloadId"):
            out.add(r["downloadId"].lower())
    return out


async def recycle_bin(app: str) -> str | None:
    cfg = await arr(app, "GET", "config/mediamanagement") or {}
    return cfg.get("recycleBin") or None


# --------------------------------------------------------------------------
# Plex
# --------------------------------------------------------------------------

async def plex(path: str, **params: Any) -> dict:
    if not config.PLEX_TOKEN:
        raise StackError("plex has no token")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{config.PLEX_URL}{path}", params=params or None,
                        headers={"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"})
    if r.status_code >= 400:
        raise StackError(f"plex {path} -> HTTP {r.status_code}")
    return (r.json() or {}).get("MediaContainer") or {}


async def plex_sections() -> list[dict]:
    return (await plex("/library/sections")).get("Directory") or []


async def plex_library(section_key: str, item_type: int) -> list[dict]:
    mc = await plex(f"/library/sections/{section_key}/all", type=item_type, includeGuids=1)
    return mc.get("Metadata") or []


async def plex_in_progress(section_key: str, item_type: int) -> list[dict]:
    """Movies (type 1) or episodes (type 4) that are part-way through (viewOffset > 0)."""
    mc = await plex(f"/library/sections/{section_key}/all", type=item_type, inProgress=1)
    return mc.get("Metadata") or []


async def plex_children(rating_key: str) -> list[dict]:
    return (await plex(f"/library/metadata/{rating_key}/children")).get("Metadata") or []


async def plex_leaves(rating_key: str) -> list[dict]:
    return (await plex(f"/library/metadata/{rating_key}/allLeaves")).get("Metadata") or []


async def plex_sessions() -> list[dict]:
    return (await plex("/status/sessions")).get("Metadata") or []


async def plex_history(size: int = 1000) -> list[dict]:
    mc = await plex("/status/sessions/history/all", **{
        "X-Plex-Container-Start": 0, "X-Plex-Container-Size": size, "sort": "viewedAt:desc"})
    return mc.get("Metadata") or []


def plex_guids(item: dict) -> dict[str, str]:
    """{'tmdb': '115', 'tvdb': '659', 'imdb': 'tt..'} from an includeGuids item."""
    out: dict[str, str] = {}
    for g in item.get("Guid") or []:
        gid = g.get("id") or ""
        if "://" in gid:
            scheme, val = gid.split("://", 1)
            out[scheme] = val
    return out
