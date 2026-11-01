"""What the agent can do. Every MCP tool is a function here; mcp_server.py only wires them.

Trust rules enforced in this file, not in prompts:

- Text written by strangers (release/torrent names, *arr messages) is returned
  inside an `untrusted` object, cleaned and truncated, and is never accepted
  back as a parameter.
- Actions take server-issued ids (app/ids.py) or `release_key`s minted by a
  read in this process. Raw *arr ids, hashes and guids are never accepted.
- Safe actions are bounded: requests by a disk guard and an hourly cap, agent
  grabs by a per-target cap. Destructive actions (delete, remove+blocklist)
  are approved by the owner in an elicitation form when their client can show one.
  Clients that can't (the media bot, headless runs) get a bounded mode:
  cleanup candidates only, a daily cap, and a 7-day recycle bin for undo.
- delete_any is the one way to delete something that is not a cleanup
  candidate: a one-time code sent to the owner, a stated reason, 3 a day, same
  recycle bin. set_rating overwrites a taste rating and logs it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collections.abc import Awaitable, Callable

from . import config, health, ids, notify, requests_store, services, stack, taste

log = logging.getLogger("mediahub.tools")

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


class ToolError(Exception):
    """A refusal or failure the agent should read and relay."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def clean(text: Any, limit: int) -> str:
    s = _CTRL.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def untrusted(title: Any = None, reason: Any = None) -> dict[str, str]:
    """Stranger-written text, fenced. The release title is removed from the reason."""
    t = clean(title, 120) if title else ""
    r = str(reason or "")
    if title:
        r = r.replace(str(title), "<release>")
    out = {}
    if t:
        out["release_title"] = t
    if reason:
        out["reason"] = clean(r, 400)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gb(n: Any) -> float | None:
    return round(n / 1e9, 1) if isinstance(n, (int, float)) and n else None


async def _taste_ledger() -> taste.Ledger:
    try:
        return taste.load(config.TASTE_PATH)
    except Exception as exc:  # unreadable ledger must not break reads
        log.warning("taste.md unreadable: %s", exc)
        return taste.empty_ledger()


# release_key -> what to grab. Ephemeral: keys expire, a restart clears them.
_RELEASES: dict[str, dict[str, Any]] = {}
_RELEASE_TTL = 1800


def _mint_release(entry: dict[str, Any]) -> str:
    now = time.monotonic()
    for k in [k for k, v in _RELEASES.items() if now - v["at"] > _RELEASE_TTL]:
        _RELEASES.pop(k, None)
    key = "r_" + secrets.token_urlsafe(9)
    _RELEASES[key] = {**entry, "at": now}
    return key


# --------------------------------------------------------------------------
# status / problems / fix
# --------------------------------------------------------------------------

async def _swarm() -> dict[str, dict[str, Any]]:
    try:
        return {(t.get("hash") or "").lower(): t for t in await stack.qbit_torrents()}
    except Exception:
        return {}


def _group_queue(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per download (a season pack is many *arr records, one torrent)."""
    groups: dict[str, dict[str, Any]] = {}
    for q in queue:
        app = (q.get("service") or "").lower()
        dl = (q.get("download_id") or f"rec{q.get('id')}").lower()
        g = groups.setdefault(f"{app}:{dl}", {
            "app": app, "download_id": dl, "records": [], "title": q.get("title"),
            "percent": q.get("percent"), "eta": q.get("eta"), "size_gb": q.get("size_gb"),
            "indexer": q.get("indexer"), "quality": q.get("quality_name"),
            "status": q.get("status"), "state": q.get("state"),
        })
        g["records"].append(q)
    return list(groups.values())


async def status(deep: bool = False) -> dict[str, Any]:
    from . import watch  # late import: watch imports tools for escalation helpers
    data, swarm, free, wsnap = await asyncio.gather(
        services.gather_status(), _swarm(), stack.disk_free_gb(),
        watch.snapshot(), return_exceptions=True)
    if isinstance(data, BaseException):
        raise ToolError(f"could not read the stack: {data}")
    downloads = []
    for g in _group_queue(data.get("queue") or []):
        t = swarm.get(g["download_id"]) if isinstance(swarm, dict) else None
        downloads.append({
            "target_id": ids.make("q", g["app"], g["download_id"]),
            "app": g["app"],
            "untrusted": untrusted(g["title"]),
            "episodes": len(g["records"]),
            "percent": g["percent"], "eta": g["eta"], "size_gb": g["size_gb"],
            "indexer": g["indexer"], "quality": g["quality"],
            "swarm": None if not t else {
                "state": t.get("state"), "connected_seeds": t.get("num_seeds"),
                "availability": t.get("availability"),
                "speed_mbps": round((t.get("dlspeed") or 0) / 1e6, 2)},
        })
    out = {
        "disk_free_gb": free if not isinstance(free, BaseException) else None,
        "disk_guard_gb": config.DISK_GUARD_GB,
        "services": [{"name": s["name"], "up": s["up"], "version": s.get("version"),
                      "error": clean(s.get("error"), 200) or None}
                     for s in data.get("services") or []],
        "downloads": downloads,
        "playing_now": [{"title": clean(p.get("title"), 120), "user": clean(p.get("user"), 40),
                         "progress": p.get("progress")} for p in data.get("now_playing") or []],
        "watch_loop": None if isinstance(wsnap, BaseException) else {
            "last_run": wsnap.get("last_run"), "counts": wsnap.get("counts"),
            "recent_actions": [{"at": a.get("at"), "action": a.get("action"),
                                "untrusted": untrusted(a.get("title"), a.get("detail"))}
                               for a in (wsnap.get("actions") or [])[:8]]},
        "alerts": {"channel": notify.channel(), "delete_any": delete_any_mode()},
    }
    if deep:
        from . import checks
        out["checks"] = await checks.run_all()
    return out


_GLOBAL_ACTIONS = {"refresh_all", "prowlarr_test"}
_TARGET_ACTIONS = {"qbit_recheck", "qbit_resume", "queue_refresh"}


async def problems() -> dict[str, Any]:
    from . import watch
    data = await services.gather_status()
    wsnap = await watch.snapshot()
    queue = data.get("queue") or []
    by_record = {f"{(q.get('service') or '').lower()}:{q.get('id')}": q for q in queue}
    by_hash = {}
    for q in queue:
        if q.get("download_id"):
            by_hash.setdefault(q["download_id"].lower(), q)
    out = []
    for p in list(data.get("problems") or []) + list(wsnap.get("problems") or []):
        kind = p.get("kind")
        target = None
        actions: list[str] = []
        if kind == "queue":
            app = (p.get("service") or "").lower()
            rid = (p.get("id") or "").rsplit(":", 1)[-1]
            q = by_record.get(f"{app}:{rid}")
            if q and q.get("download_id"):
                target = ids.make("q", app, q["download_id"].lower())
                actions = ["queue_refresh", "releases", "remove_blocklist"]
        elif kind == "torrent":
            h = p["id"].rsplit(":", 1)[-1].lower()
            q = by_hash.get(h)
            if q:
                target = ids.make("q", (q.get("service") or "").lower(), h)
                actions = ["qbit_recheck", "qbit_resume", "releases", "remove_blocklist"]
        elif kind == "health":
            actions = ["refresh_all"] + (["prowlarr_test"] if any(
                a.get("action") == "prowlarr_test" for a in p.get("actions") or []) else [])
        elif kind == "watch":
            target = p.get("target_id")
            if target:
                actions = ["releases", "search", "grab", "remove_blocklist"]
        out.append({
            "service": p.get("service"), "kind": kind, "severity": p.get("severity"),
            "target_id": target, "actions": actions,
            "untrusted": untrusted(p.get("title"), p.get("detail")),
        })
    return {"problems": out, "count": len(out)}


async def _find_download(target: ids.Target) -> list[dict[str, Any]]:
    recs = [r for r in await stack.queue(target.app)
            if (r.get("downloadId") or "").lower() == target.key.lower()]
    if not recs:
        raise ToolError("that download is no longer in the queue")
    return recs


async def fix(action: str, target_id: str | None = None) -> dict[str, Any]:
    if action in _GLOBAL_ACTIONS:
        msg = await (services.refresh_all() if action == "refresh_all"
                     else services.prowlarr_test_indexers())
        services.invalidate_status_cache()
        return {"ok": True, "message": clean(msg, 400)}
    if action not in _TARGET_ACTIONS:
        if action == "remove_blocklist":
            raise ToolError(config.personalise("remove_blocklist is destructive: use the remove_blocklist tool ({Owner} confirms it)"))
        raise ToolError(f"unknown action {action!r}")
    if not target_id:
        raise ToolError("this action needs a target_id from status() or problems()")
    t = _decode(target_id, kinds={"q"})
    await _find_download(t)
    if action == "queue_refresh":
        msg = await services.arr_queue_refresh(t.app)
    elif action == "qbit_recheck":
        msg = await services.qbit_recheck(t.key)
    else:
        msg = await services.qbit_resume(t.key)
    services.invalidate_status_cache()
    return {"ok": True, "message": clean(msg, 400)}


def _decode(raw: str, *, kinds: set[str]) -> ids.Target:
    try:
        t = ids.decode(raw)
    except ids.BadId:
        raise ToolError("unknown target_id — use one returned by status, problems or cleanup_candidates")
    if t.kind not in kinds:
        raise ToolError("that target_id is the wrong kind for this action")
    return t


# --------------------------------------------------------------------------
# find / request
# --------------------------------------------------------------------------

_plex_ids_cache: tuple[float, dict[str, set[str]]] | None = None


async def _plex_ids() -> dict[str, set[str]]:
    """{'tmdb': {...}, 'tvdb': {...}} for everything in the Plex library (5 min cache)."""
    global _plex_ids_cache
    if _plex_ids_cache and time.monotonic() - _plex_ids_cache[0] < 300:
        return _plex_ids_cache[1]
    out: dict[str, set[str]] = {"tmdb": set(), "tvdb": set()}
    try:
        for sec in await stack.plex_sections():
            typ = 1 if sec.get("type") == "movie" else 2 if sec.get("type") == "show" else None
            if typ is None:
                continue
            for item in await stack.plex_library(sec["key"], typ):
                g = stack.plex_guids(item)
                for k in ("tmdb", "tvdb"):
                    if g.get(k):
                        out[k].add(g[k])
    except Exception as exc:
        log.warning("plex library read failed: %s", exc)
    _plex_ids_cache = (time.monotonic(), out)
    return out


def _estimate_gb(kind: str, runtime: int | None, episodes: int = 1) -> float:
    """Rough size at the default profile: 1080p ~50/40 MB/min (movie/TV), 720p about half."""
    rt = runtime or (110 if kind == "movie" else 50)
    rate = 0.050 if kind == "movie" else 0.040
    if "720" in config.DEFAULT_PROFILE:
        rate /= 2
    return round(rt * rate * max(1, episodes), 1)


async def find(query: str, type: str | None = None) -> dict[str, Any]:
    query = clean(query, 120)
    if not query:
        raise ToolError("empty query")
    kinds = [type] if type in ("movie", "tv") else ["movie", "tv"]
    ledger = await _taste_ledger()
    plex_ids = await _plex_ids()
    results: list[dict[str, Any]] = []
    for kind in kinds:
        app = "radarr" if kind == "movie" else "sonarr"
        try:
            hits = await stack.lookup(app, query)
            have = {(m.get("tmdbId") if kind == "movie" else m.get("tvdbId")): m
                    for m in await (stack.movies() if kind == "movie" else stack.series_list())}
        except Exception as exc:
            results.append({"type": kind, "error": clean(exc, 200)})
            continue
        for h in hits[:6]:
            key = h.get("tmdbId") if kind == "movie" else h.get("tvdbId")
            on = have.get(key)
            tmdb_id = h.get("tmdbId") or (on or {}).get("tmdbId") or None
            hit_taste = taste.find(ledger, int(tmdb_id)) if tmdb_id else None
            seasons = None
            if kind == "tv":
                seasons = [{"season": s.get("seasonNumber"),
                            "episodes": (s.get("statistics") or {}).get("totalEpisodeCount")}
                           for s in h.get("seasons") or [] if s.get("seasonNumber")]
            row = {
                "type": kind, "title": clean(h.get("title"), 120), "year": h.get("year"),
                "tmdb_id": tmdb_id, "tvdb_id": h.get("tvdbId") if kind == "tv" else None,
                "overview": clean(h.get("overview"), 300), "runtime": h.get("runtime"),
                "network_or_studio": clean(h.get("network") or h.get("studio"), 60) or None,
                "seasons": seasons,
                "in_plex": bool((tmdb_id and str(tmdb_id) in plex_ids["tmdb"]) or
                                (h.get("tvdbId") and str(h.get("tvdbId")) in plex_ids["tvdb"])),
                "on_stack": None,
                "taste": None if not hit_taste else {"section": hit_taste[0],
                                                     "verdict": hit_taste[1].verdict},
            }
            if on:
                stats = on.get("statistics") or {}
                row["on_stack"] = {
                    "monitored": on.get("monitored"),
                    "has_file": on.get("hasFile") if kind == "movie" else None,
                    "size_on_disk_gb": _gb(on.get("sizeOnDisk") or stats.get("sizeOnDisk")),
                    "monitored_seasons": [s["seasonNumber"] for s in on.get("seasons") or []
                                          if s.get("monitored")] if kind == "tv" else None,
                    "episodes_on_disk": stats.get("episodeFileCount") if kind == "tv" else None,
                }
            results.append(row)
    return {"query": query, "results": results}


async def request(type: str, tmdb_id: int, seasons: list[int] | None = None) -> dict[str, Any]:
    if type not in ("movie", "tv"):
        raise ToolError("type must be 'movie' or 'tv'")
    tmdb_id = int(tmdb_id)
    if type == "tv" and not seasons:
        raise ToolError(config.personalise("TV requests must name seasons. Ask {owner} which seasons they want."))
    recent = await requests_store.count_since(timedelta(hours=1))
    if recent >= config.REQUESTS_PER_HOUR:
        raise ToolError(f"{recent} requests in the last hour (limit {config.REQUESTS_PER_HOUR}). "
                        "Try again later.")
    free = await stack.disk_free_gb()

    if type == "movie":
        movie = next((m for m in await stack.movies() if m.get("tmdbId") == tmdb_id), None)
        if movie and movie.get("hasFile"):
            return {"ok": True, "status": "already_have", "title": movie.get("title")}
        if not movie:
            lk = await stack.lookup_movie_tmdb(tmdb_id)
            if not lk:
                raise ToolError("Radarr could not find that TMDB id")
        else:
            lk = movie
        est = _estimate_gb("movie", lk.get("runtime"))
        _disk_guard(free, est)
        if movie:
            if not movie.get("monitored"):
                movie["monitored"] = True
                await stack.arr("radarr", "PUT", f"movie/{movie['id']}", json=movie)
            await stack.command("radarr", "MoviesSearch", movieIds=[movie["id"]])
            arr_id, status_ = movie["id"], "searching"
        else:
            added = await stack.add_movie(lk, profile=await stack.profile_id("radarr", config.DEFAULT_PROFILE),
                                          root=await stack.root_folder("radarr"))
            arr_id, status_ = added["id"], "added"
        await requests_store.add({"type": "movie", "app": "radarr", "arr_id": arr_id,
                                  "tmdb_id": tmdb_id, "title": lk.get("title"), "seasons": None})
        return {"ok": True, "status": status_, "title": lk.get("title"), "year": lk.get("year"),
                "estimated_gb": est, "disk_free_gb": free}

    # TV
    wanted = sorted({int(s) for s in seasons or []})
    series = next((s for s in await stack.series_list() if s.get("tmdbId") == tmdb_id), None)
    lk = series or await stack.lookup_series_tmdb(tmdb_id)
    if not lk:
        raise ToolError("Sonarr could not find that TMDB id")
    known = {s.get("seasonNumber") for s in lk.get("seasons") or []}
    bad = [s for s in wanted if s not in known]
    if bad:
        raise ToolError(f"season(s) {bad} do not exist; the show has {sorted(x for x in known if x)}")
    eps_per = {s.get("seasonNumber"): (s.get("statistics") or {}).get("totalEpisodeCount") or 10
               for s in lk.get("seasons") or []}
    est = _estimate_gb("tv", lk.get("runtime"), sum(eps_per.get(s, 10) for s in wanted))
    _disk_guard(free, est)
    if series:
        for s in series.get("seasons") or []:
            if s.get("seasonNumber") in wanted:
                s["monitored"] = True
        series["monitored"] = True
        series = await stack.put_series(series)
        status_ = "seasons_added"
    else:
        series = await stack.add_series(
            lk, seasons=wanted, profile=await stack.profile_id("sonarr", config.DEFAULT_PROFILE),
            root=await stack.root_folder("sonarr"))
        status_ = "added"
        await asyncio.sleep(2)  # Sonarr creates episode rows right after add
    eps = await stack.episodes(series["id"])
    await stack.monitor_episodes([e["id"] for e in eps if e.get("seasonNumber") in wanted])
    for s in wanted:
        await stack.command("sonarr", "SeasonSearch", seriesId=series["id"], seasonNumber=s)
    await requests_store.add({"type": "tv", "app": "sonarr", "arr_id": series["id"],
                              "tmdb_id": tmdb_id, "title": lk.get("title"), "seasons": wanted})
    return {"ok": True, "status": status_, "title": lk.get("title"), "year": lk.get("year"),
            "seasons": wanted, "estimated_gb": est, "disk_free_gb": free}


def _disk_guard(free: float | None, est: float) -> None:
    if free is None:
        raise ToolError("could not read free disk space; not adding anything")
    if free - est < config.DISK_GUARD_GB:
        raise ToolError(
            f"not enough disk: {free} GB free, this needs ~{est} GB and "
            f"{config.DISK_GUARD_GB:.0f} GB must stay free. Run cleanup_candidates first.")


# --------------------------------------------------------------------------
# releases / search / grab
# --------------------------------------------------------------------------

async def _target_context(t: ids.Target) -> dict[str, Any]:
    """Search parameters, runtime and current-download facts for a target."""
    ctx: dict[str, Any] = {"app": t.app, "current_title": None, "current_resolution": None,
                           "current_seeders": 0, "current_dead": True, "episode_count": 1,
                           "prefer_full_season": False, "runtime": None, "search": {}, "owner": None}
    if t.kind == "q":
        recs = await _find_download(t)
        r0 = recs[0]
        ctx["current_title"] = r0.get("title")
        q = (r0.get("quality") or {}).get("quality") or {}
        ctx["current_resolution"] = q.get("resolution")
        swarm = (await _swarm()).get(t.key.lower()) or {}
        ctx["current_seeders"] = int(swarm.get("num_seeds") or 0)
        ctx["current_dead"] = ctx["current_seeders"] <= 0
        ctx["queue_ids"] = [r["id"] for r in recs]
        ctx["owner"] = r0.get("movie") or r0.get("series")
        if t.app == "radarr":
            ctx["search"] = {"movie_id": r0.get("movieId")}
            ctx["runtime"] = ((r0.get("movie") or {}).get("runtime"))
        else:
            ctx["runtime"] = ((r0.get("series") or {}).get("runtime"))
            if len(recs) > 1:
                ctx["search"] = {"series_id": r0.get("seriesId"),
                                 "season": (r0.get("episode") or {}).get("seasonNumber")}
                ctx["episode_count"] = len(recs)
                ctx["prefer_full_season"] = True
            else:
                ctx["search"] = {"episode_id": r0.get("episodeId")}
    elif t.kind == "m":
        m = await stack.get_movie(t.int_key)
        if not m:
            raise ToolError("that movie is no longer in Radarr")
        ctx["search"] = {"movie_id": m["id"]}
        ctx["runtime"] = m.get("runtime")
        ctx["owner"] = m
    elif t.kind == "s":
        s = await stack.get_series(t.int_key)
        if not s:
            raise ToolError("that show is no longer in Sonarr")
        season = int(t.extra)
        ctx["search"] = {"series_id": s["id"], "season": season}
        ctx["runtime"] = s.get("runtime")
        ctx["owner"] = s
        ctx["episode_count"] = next(((x.get("statistics") or {}).get("totalEpisodeCount") or 1
                                     for x in s.get("seasons") or [] if x.get("seasonNumber") == season), 1)
        ctx["prefer_full_season"] = True
    elif t.kind == "e":
        ep = await stack.arr("sonarr", "GET", f"episode/{t.int_key}")
        if not ep:
            raise ToolError("that episode is no longer in Sonarr")
        s = await stack.get_series(ep["seriesId"])
        ctx["search"] = {"episode_id": ep["id"]}
        ctx["runtime"] = (s or {}).get("runtime")
        ctx["owner"] = s
    else:
        raise ToolError("releases work on downloads and missing items only")
    return ctx


def _present(ranked: list[dict[str, Any]], target_id: str, source: str,
             extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out = []
    for item in ranked:
        rel = item["release"]
        entry = {"target_id": target_id, "source": source, "title": rel.get("title"),
                 "size": rel.get("size")}
        if source == "arr":
            entry.update(guid=rel.get("guid"), indexer_id=rel.get("indexerId"))
        else:
            entry.update(download_url=rel.get("magnetUrl") or rel.get("downloadUrl"),
                         protocol=rel.get("protocol") or "torrent",
                         indexer=rel.get("indexer"), publish_date=rel.get("publishDate"))
        entry.update(extra or {})
        out.append({
            "release_key": _mint_release(entry),
            "score": round(item["score"], 1), "indexer_track_record": round(item["track"], 2),
            "seeders": rel.get("seeders"), "leechers": rel.get("leechers"),
            "resolution": item["resolution"], "size_gb": item["size_gb"],
            "full_season": item["full_season"], "indexer": clean(rel.get("indexer"), 40),
            "untrusted": untrusted(rel.get("title")),
        })
    return out


async def _history_counts(app: str) -> dict[str, tuple[int, int]]:
    try:
        return await stack.indexer_track_record(app)
    except Exception:
        return {}


async def releases(target_id: str) -> dict[str, Any]:
    t = _decode(target_id, kinds={"q", "m", "s", "e"})
    ctx = await _target_context(t)
    raw = await stack.release_search(t.app, **ctx["search"])
    ranked = health.rank_releases(
        raw, history_counts=await _history_counts(t.app),
        current_resolution=ctx["current_resolution"], current_seeders=ctx["current_seeders"],
        current_dead=ctx["current_dead"], current_title=ctx["current_title"],
        runtime_minutes=ctx["runtime"], episode_count=ctx["episode_count"],
        allow_foreign=health.foreign_ok(ctx["owner"]),
        prefer_full_season=ctx["prefer_full_season"], limit=8)
    return {"target_id": target_id, "found": len(raw), "passed_filters": len(ranked),
            "releases": _present(ranked[:8], target_id, "arr")}


async def search(target_id: str, query: str) -> dict[str, Any]:
    t = _decode(target_id, kinds={"q", "m", "s", "e"})
    query = clean(query, 100)
    if not query:
        raise ToolError("empty query")
    ctx = await _target_context(t)
    cats = [2000] if t.app == "radarr" else [5000]
    raw = await stack.prowlarr_search(query, categories=cats)
    for r in raw:  # Prowlarr results have no parsed quality; give health a name to read
        r.setdefault("quality", {"quality": {"name": r.get("title") or ""}})
        r.setdefault("rejected", False)
    ranked = health.rank_releases(
        raw, history_counts=await _history_counts(t.app),
        current_resolution=ctx["current_resolution"], current_seeders=ctx["current_seeders"],
        current_dead=ctx["current_dead"], current_title=ctx["current_title"],
        runtime_minutes=ctx["runtime"], episode_count=ctx["episode_count"],
        allow_foreign=health.foreign_ok(ctx["owner"]),
        prefer_full_season=False, limit=8)
    return {"target_id": target_id, "query": query, "found": len(raw), "passed_filters": len(ranked),
            "note": "Sonarr/Radarr must still recognise the release name when it is grabbed.",
            "releases": _present(ranked[:8], target_id, "prowlarr",
                                 {"queue_ids": ctx.get("queue_ids")})}


async def grab(release_key: str) -> dict[str, Any]:
    entry = _RELEASES.get(release_key)
    if not entry or time.monotonic() - entry["at"] > _RELEASE_TTL:
        raise ToolError("unknown or expired release_key — call releases() or search() again")
    target_id = entry["target_id"]
    t = ids.decode(target_id)
    used = await requests_store.agent_grabs(target_id)
    if used >= config.AGENT_GRABS_PER_TARGET:
        raise ToolError(f"this item already had {used} agent grabs (limit "
                        f"{config.AGENT_GRABS_PER_TARGET}). Ask " + config.owner() + ", or use remove_blocklist.")
    size_gb = _gb(entry.get("size")) or 0.0
    _disk_guard(await stack.disk_free_gb(), size_gb)
    if t.kind == "q":  # replacing a download: drop and blocklist the old one first
        recs = await _find_download(t)
        await stack.queue_remove_bulk(t.app, [r["id"] for r in recs], blocklist=True, search_again=False)
    if entry["source"] == "arr":
        await stack.grab(t.app, entry["guid"], int(entry["indexer_id"]))
        result = {"ok": True, "grabbed": True}
    else:
        decisions = await stack.push_release(
            t.app, title=entry["title"], download_url=entry["download_url"],
            protocol=entry["protocol"], indexer=entry.get("indexer"),
            publish_date=entry.get("publish_date"))
        approved = any(d.get("approved") for d in decisions)
        rej = [x for d in decisions for x in (d.get("rejections") or [])]
        result = {"ok": approved, "grabbed": approved,
                  "untrusted": untrusted(entry["title"], "; ".join(map(str, rej))) if rej else None}
        if not approved:
            raise ToolError("Sonarr/Radarr did not accept that release: "
                            + clean("; ".join(map(str, rej)) or "no reason given", 300))
    await requests_store.add_agent_grab(target_id)
    _RELEASES.pop(release_key, None)
    services.invalidate_status_cache()
    return {**result, "size_gb": size_gb,
            "agent_grabs_left": config.AGENT_GRABS_PER_TARGET - used - 1}


# --------------------------------------------------------------------------
# taste / history
# --------------------------------------------------------------------------

async def taste_view(section: str | None = None) -> dict[str, Any]:
    ledger = await _taste_ledger()
    names = [section] if section else list(ledger.sections)
    return {"last_poll": ledger.last_poll,
            "sections": {n: [taste.to_dict(r) for r in ledger.sections.get(n, [])] for n in names},
            "recent_rating_changes": _read_json_log("rating_changes.json")[-10:]}


async def taste_record(tmdb_id: int, type: str, title: str, verdict: str, year: int | None = None,
                       why: str | None = None, finished: str | None = None,
                       watched: str | None = None) -> dict[str, Any]:
    if verdict not in ("loved", "liked", "meh", "disliked", "unseen", "skip", "not_mine"):
        raise ToolError("verdict must be loved, liked, meh, disliked, unseen, skip or not_mine")
    if type not in ("movie", "tv"):
        raise ToolError("type must be 'movie' or 'tv'")
    today = datetime.now().strftime("%Y-%m-%d")

    def fn(ledger):
        res = taste.record(ledger, tmdb_id=int(tmdb_id), type=type, title=clean(title, 120),
                           year=year, verdict=verdict, why=clean(why, 300) or None,
                           finished=finished, watched=watched, today=today)
        return res, res.changed

    res = await taste.update(config.TASTE_PATH, fn)
    if res.status == "needs_confirm":
        prev = res.previous[1] if res.previous else None
        return {"ok": False, "status": "unchanged",
                "message": (f"Already rated '{prev.verdict if prev else '?'}'. The agent never "
                            + config.personalise("changes an existing rating; {Owner} edits taste.md by hand for that."))}
    return {"ok": True, "status": res.status, "section": res.section, "row": taste.to_dict(res.row)}


SET_RATING_VERDICTS = ("loved", "liked", "meh", "disliked", "skip", "not_mine")


def _norm_title(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


async def set_rating(tmdb_id: int, title: str, verdict: str, why: str | None = None) -> dict[str, Any]:
    """Change the rating of a title that is already in the taste ledger, and log the change.

    Unlike taste_record this overwrites. The server cannot tell whether the owner named the
    title themselves; the tool description, the skill and the bot prompt carry that rule.
    The title must match the ledger row, which catches a wrong tmdb_id.
    """
    if verdict not in SET_RATING_VERDICTS:
        raise ToolError("verdict must be loved, liked, meh, disliked, skip or not_mine")
    tmdb_id = int(tmdb_id)
    wanted = _norm_title(title)
    if not wanted:
        raise ToolError("name the title you are re-rating")
    why = clean(why, 300) or None
    today = datetime.now().strftime("%Y-%m-%d")
    refusal: list[str] = []

    def fn(ledger):
        hit = taste.find(ledger, tmdb_id)
        if not hit:
            refusal.append("That title is not in the taste ledger. Use taste_record to rate it for the first time.")
            return None, False
        section, row = hit
        have = _norm_title(row.title)
        if wanted not in have and have not in wanted:
            refusal.append(f"tmdb:{tmdb_id} is '{clean(row.title, 80)}' in the ledger, not '{clean(title, 80)}'. "
                           "Check the id with taste() or find().")
            return None, False
        before = {"section": section, "verdict": row.verdict, "why": row.why}
        last_poll = ledger.last_poll
        res = taste.record(ledger, tmdb_id=tmdb_id, type=row.type, title=row.title, year=row.year,
                           verdict=verdict, why=why, today=today, force=True)
        ledger.last_poll = last_poll  # a re-rating is not a poll
        if res.status != "unchanged" and why is None:
            res.row.why = None  # the old reason belongs to the old rating
        after = {"section": res.section, "verdict": res.row.verdict, "why": res.row.why}
        return (res, before, after, row.title, row.type), res.changed

    out = await taste.update(config.TASTE_PATH, fn)
    if refusal:
        raise ToolError(refusal[0])
    res, before, after, name, kind = out
    if not res.changed:
        return {"ok": True, "status": "unchanged", "message": f"{clean(name, 80)} is already {verdict}."}
    _log_json("rating_changes.json", {
        "at": _now().isoformat(), "tmdb_id": tmdb_id, "type": kind, "title": clean(name, 120),
        "from": before, "to": after}, keep=200)
    log.info("rating changed tmdb:%s %s -> %s", tmdb_id, before.get("verdict") or before.get("section"),
             after.get("verdict") or after.get("section"))
    return {"ok": True, "status": "changed", "title": clean(name, 120), "from": before, "to": after,
            "logged": True,
            "note": "The previous rating is in the change log (taste() shows recent_rating_changes)."}


async def history(unrated_only: bool = True, limit: int = 30) -> dict[str, Any]:
    """The owner's account plays, collapsed to shows/movies, newest first."""
    ledger = await _taste_ledger()
    secs = await stack.plex_sections()
    guid_by_key: dict[str, dict[str, Any]] = {}
    for sec in secs:
        typ = 1 if sec.get("type") == "movie" else 2 if sec.get("type") == "show" else None
        if typ is None:
            continue
        for item in await stack.plex_library(sec["key"], typ):
            guid_by_key[str(item["ratingKey"])] = item
    seen: dict[str, dict[str, Any]] = {}
    for h in await stack.plex_history():
        if str(h.get("accountID")) != "1":
            continue
        is_ep = h.get("type") == "episode"
        key = str(h.get("grandparentKey", "").rsplit("/", 1)[-1] if is_ep else h.get("ratingKey") or "")
        name = h.get("grandparentTitle") if is_ep else h.get("title")
        if not name or key in seen:
            continue
        item = guid_by_key.get(key) or {}
        g = stack.plex_guids(item)
        seen[key] = {
            "type": "tv" if is_ep else "movie", "title": clean(name, 120),
            "year": item.get("year"), "tmdb_id": int(g["tmdb"]) if g.get("tmdb") else None,
            "last_viewed": datetime.fromtimestamp(h.get("viewedAt") or 0, timezone.utc).strftime("%Y-%m-%d"),
            "finished": bool(item) and (
                (item.get("leafCount") or 0) > 0 and item.get("viewedLeafCount") == item.get("leafCount")
                if is_ep else (item.get("viewCount") or 0) >= 1),
            "in_library": bool(item),
        }
    # Library items viewed on this account that have no history row (older plays).
    for key, item in guid_by_key.items():
        if key in seen or not (item.get("viewCount") or item.get("viewedLeafCount")):
            continue
        g = stack.plex_guids(item)
        is_show = item.get("type") == "show"
        seen[key] = {"type": "tv" if is_show else "movie", "title": clean(item.get("title"), 120),
                     "year": item.get("year"), "tmdb_id": int(g["tmdb"]) if g.get("tmdb") else None,
                     "last_viewed": datetime.fromtimestamp(item.get("lastViewedAt") or 0, timezone.utc).strftime("%Y-%m-%d"),
                     "finished": (item.get("leafCount") or 0) > 0 and item.get("viewedLeafCount") == item.get("leafCount")
                     if is_show else True, "in_library": True}
    rows = sorted(seen.values(), key=lambda r: r["last_viewed"], reverse=True)
    if unrated_only:
        rows = [r for r in rows if not (r["tmdb_id"] and taste.find(ledger, r["tmdb_id"]))]
    return {"note": config.personalise("Plays on {owner}'s account may be someone else's; offer 'Not mine'."),
            "titles": rows[:limit]}


# --------------------------------------------------------------------------
# library: everything on the stack and in Plex
# --------------------------------------------------------------------------

_STALLED_STATES = {"stalledDL", "metaDL", "error", "missingFiles"}


def _watch_state(viewed: int, total: int) -> str:
    if total <= 0:
        return "none"
    if viewed >= total:
        return "all"
    return "partial" if viewed > 0 else "none"


def _taste_of(ledger: taste.Ledger, tmdb_id: Any) -> dict[str, Any] | None:
    hit = taste.find(ledger, int(tmdb_id)) if tmdb_id else None
    if not hit:
        return None
    section, row = hit
    return {"section": section, "rating": row.verdict if section == "Rated" else None,
            "status": row.verdict if section != "Rated" else None}


def _pct(offset: Any, duration: Any) -> int | None:
    return round(100 * offset / duration) if offset and duration else None


def _movie_row(item: dict | None, m: dict | None, ledger: taste.Ledger,
               progress: dict | None) -> dict[str, Any]:
    base = item or m or {}
    g = stack.plex_guids(item) if item else {}
    tmdb = int(g["tmdb"]) if g.get("tmdb") else (m or {}).get("tmdbId")
    views = (item or {}).get("viewCount") or 0
    return {
        "type": "movie", "title": clean(base.get("title"), 120), "year": base.get("year"), "tmdb_id": tmdb,
        "item_id": ids.make("t", "radarr", m["id"]) if m else None,
        "in_plex": item is not None, "on_stack": m is not None,
        "has_file": m.get("hasFile") if m else None, "monitored": m.get("monitored") if m else None,
        "size_gb": _gb((m or {}).get("sizeOnDisk")) or 0.0,
        "watched": "all" if views >= 1 else "partial" if progress else "none",
        "in_progress": bool(progress),
        "progress_percent": _pct((progress or {}).get("viewOffset"), (progress or {}).get("duration")),
        "last_viewed": _date((item or {}).get("lastViewedAt")),
        "taste": _taste_of(ledger, tmdb),
    }


def _show_row(item: dict | None, s: dict | None, ledger: taste.Ledger, kids: list[dict] | None,
              episodes: list[dict]) -> dict[str, Any]:
    base = item or s or {}
    g = stack.plex_guids(item) if item else {}
    tmdb = int(g["tmdb"]) if g.get("tmdb") else (s or {}).get("tmdbId")
    arr_seasons = {x.get("seasonNumber"): x for x in (s or {}).get("seasons") or []}
    plex_seasons = {k.get("index"): k for k in kids or [] if k.get("index") is not None}
    live = {e.get("parentIndex") for e in episodes}
    seasons = []
    for n in sorted(set(arr_seasons) | set(plex_seasons)):
        a, p = arr_seasons.get(n), plex_seasons.get(n)
        st = (a or {}).get("statistics") or {}
        seasons.append({
            "season": n, "on_stack": a is not None, "in_plex": p is not None,
            "monitored": a.get("monitored") if a else None,
            "episodes_on_disk": st.get("episodeFileCount") if a else None,
            "episodes_total": st.get("totalEpisodeCount") if a else None,
            "size_gb": _gb(st.get("sizeOnDisk")) or 0.0 if a else None,
            "watched": _watch_state(p.get("viewedLeafCount") or 0, p.get("leafCount") or 0) if p else None,
            "watched_episodes": (p.get("viewedLeafCount") or 0) if p else None,
            "in_progress": n in live,
        })
    leaf, viewed = (item or {}).get("leafCount") or 0, (item or {}).get("viewedLeafCount") or 0
    size = ((s or {}).get("statistics") or {}).get("sizeOnDisk")
    return {
        "type": "tv", "title": clean(base.get("title"), 120), "year": base.get("year"), "tmdb_id": tmdb,
        "item_id": ids.make("t", "sonarr", s["id"]) if s else None,
        "in_plex": item is not None, "on_stack": s is not None,
        "monitored": s.get("monitored") if s else None,
        "status": s.get("status") if s else None,
        "size_gb": _gb(size) or 0.0,
        "watched": _watch_state(viewed, leaf), "watched_episodes": viewed, "plex_episodes": leaf,
        "in_progress": bool(episodes),
        "in_progress_at": [{"season": e.get("parentIndex"), "episode": e.get("index"),
                            "percent": _pct(e.get("viewOffset"), e.get("duration"))} for e in episodes[:6]],
        "last_viewed": _date((item or {}).get("lastViewedAt")),
        "seasons": seasons,
        "taste": _taste_of(ledger, tmdb),
    }


async def library(type: str | None = None) -> dict[str, Any]:
    """Every movie and show on the stack and in Plex, joined, plus downloads and stalls."""
    if type not in (None, "movie", "tv"):
        raise ToolError("type must be 'movie' or 'tv' (or leave it out for both)")
    want_movies, want_tv = type in (None, "movie"), type in (None, "tv")
    ledger = await _taste_ledger()
    movies = await stack.movies() if want_movies else []
    series = await stack.series_list() if want_tv else []

    plex_error = None
    in_progress_known = True
    plex_movies: list[dict] = []
    plex_shows: list[dict] = []
    prog_movies: dict[str, dict] = {}
    prog_eps: dict[str, list[dict]] = {}
    try:
        for sec in await stack.plex_sections():
            if sec.get("type") == "movie" and want_movies:
                plex_movies += await stack.plex_library(sec["key"], 1)
                try:
                    prog_movies.update({str(x["ratingKey"]): x for x in await stack.plex_in_progress(sec["key"], 1)})
                except Exception:
                    in_progress_known = False
            elif sec.get("type") == "show" and want_tv:
                plex_shows += await stack.plex_library(sec["key"], 2)
                try:
                    for e in await stack.plex_in_progress(sec["key"], 4):
                        prog_eps.setdefault(str(e.get("grandparentRatingKey")), []).append(e)
                except Exception:
                    in_progress_known = False
    except Exception as exc:
        plex_error = clean(exc, 200)
        in_progress_known = False

    sem = asyncio.Semaphore(4)

    async def children(key: str):
        async with sem:
            try:
                return key, await stack.plex_children(key)
            except Exception:
                return key, None
    kids = dict(await asyncio.gather(*(children(str(s["ratingKey"])) for s in plex_shows)))

    movies_by = {str(m.get("tmdbId")): m for m in movies}
    series_by: dict[tuple[str, str], dict] = {}
    for s in series:
        series_by[("tvdb", str(s.get("tvdbId")))] = s
        if s.get("tmdbId"):
            series_by[("tmdb", str(s.get("tmdbId")))] = s

    rows: list[dict[str, Any]] = []
    seen_movies: set[Any] = set()
    for item in plex_movies:
        m = movies_by.get(stack.plex_guids(item).get("tmdb", ""))
        seen_movies.add(m["id"] if m else None)
        rows.append(_movie_row(item, m, ledger, prog_movies.get(str(item["ratingKey"]))))
    rows += [_movie_row(None, m, ledger, None) for m in movies if m["id"] not in seen_movies]
    seen_series: set[Any] = set()
    for item in plex_shows:
        g = stack.plex_guids(item)
        s = series_by.get(("tvdb", g.get("tvdb", ""))) or series_by.get(("tmdb", g.get("tmdb", "")))
        seen_series.add(s["id"] if s else None)
        key = str(item["ratingKey"])
        rows.append(_show_row(item, s, ledger, kids.get(key), prog_eps.get(key, [])))
    rows += [_show_row(None, s, ledger, None, []) for s in series if s["id"] not in seen_series]
    rows.sort(key=lambda r: (r["type"], str(r["title"]).lower()))

    # Downloads (release names stay fenced) and the ones that are stalled.
    downloads: list[dict[str, Any]] = []
    try:
        from . import watch  # late import, as in status()
        swarm = await _swarm()
        for app in ("sonarr", "radarr"):
            for key, recs in watch.group_queue(await stack.queue(app), app).items():
                r0 = recs[0]
                dl = key.split(":", 1)[1]
                t = swarm.get(dl)
                size = sum(int(r.get("size") or 0) for r in recs)
                left = sum(int(r.get("sizeleft") or 0) for r in recs)
                percent = round(float(t["progress"]) * 100, 1) if t and t.get("progress") is not None else (
                    round((size - left) / size * 100, 1) if size else None)
                stalled = None
                if t is not None:
                    stalled = bool((percent or 0) < 100 and (
                        t.get("state") in _STALLED_STATES
                        or ((t.get("dlspeed") or 0) <= 0 and (t.get("num_seeds") or 0) <= 0)))
                downloads.append({
                    "target_id": ids.make("q", app, dl) if r0.get("downloadId") else None,
                    "for": clean(watch.display_title(r0) if len(recs) == 1 else
                                 f"{(r0.get('series') or {}).get('title') or 'a show'} season "
                                 f"{(r0.get('episode') or {}).get('seasonNumber', '?')}", 120),
                    "percent": percent, "eta": r0.get("timeleft"), "size_gb": _gb(size), "stalled": stalled,
                    "swarm_state": (t or {}).get("state"), "connected_seeds": (t or {}).get("num_seeds"),
                    "untrusted": untrusted(r0.get("title")),
                })
    except Exception as exc:
        downloads = [{"error": f"could not read the download queue: {clean(exc, 200)}"}]

    watching = [{"type": r["type"], "title": r["title"], "item_id": r["item_id"],
                 "where": (r["in_progress_at"] if r["type"] == "tv" else r["progress_percent"])}
                for r in rows if r["in_progress"]]
    try:
        free = await stack.disk_free_gb()
    except Exception:
        free = None
    return {
        "disk_free_gb": free, "disk_guard_gb": config.DISK_GUARD_GB,
        "counts": {"movies": sum(1 for r in rows if r["type"] == "movie"),
                   "shows": sum(1 for r in rows if r["type"] == "tv"),
                   "on_stack_not_in_plex": sum(1 for r in rows if r["on_stack"] and not r["in_plex"]),
                   "in_plex_not_on_stack": sum(1 for r in rows if r["in_plex"] and not r["on_stack"]),
                   "in_progress": len(watching),
                   "stalled_downloads": sum(1 for d in downloads if d.get("stalled")),
                   "total_gb": round(sum(r["size_gb"] or 0 for r in rows), 1)},
        "titles": rows,
        "watching_in_progress": watching,
        "downloads": downloads,
        "stalled": [d for d in downloads if d.get("stalled")],
        "in_progress_known": in_progress_known, "plex_error": plex_error,
        "note": ("item_id is what delete_any takes; titles in Plex but not on the stack have none and "
                 "cannot be deleted through MediaHub. " +
                 config.personalise("Plays on {owner}'s account may be someone else's.")),
    }


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

async def _plex_shared_plays_recent(days: int) -> set[str]:
    """grandparent/rating keys played by any account other than 1 recently."""
    cutoff = time.time() - days * 86400
    out = set()
    try:
        for h in await stack.plex_history():
            if str(h.get("accountID")) != "1" and (h.get("viewedAt") or 0) >= cutoff:
                out.add(str((h.get("grandparentKey") or h.get("key") or "").rsplit("/", 1)[-1]))
    except Exception:
        pass
    return out


async def _in_progress(rating_key: str) -> bool:
    try:
        return any((leaf.get("viewOffset") or 0) > 0 for leaf in await stack.plex_leaves(rating_key))
    except Exception:
        return True  # unknown → treat as in progress (never delete on doubt)


async def cleanup_candidates(free_gb: float | None = None) -> dict[str, Any]:
    days = config.CLEANUP_MIN_DAYS
    cutoff = time.time() - days * 86400
    ledger = await _taste_ledger()
    playing = {str(s.get("grandparentRatingKey") or s.get("ratingKey")) for s in await stack.plex_sessions()}
    shared = await _plex_shared_plays_recent(days)
    series_by = {}
    for s in await stack.series_list():
        series_by[("tvdb", str(s.get("tvdbId")))] = s
        if s.get("tmdbId"):
            series_by[("tmdb", str(s.get("tmdbId")))] = s
    movies_by = {str(m.get("tmdbId")): m for m in await stack.movies()}
    cands: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []

    def verdict_of(tmdb):
        hit = taste.find(ledger, int(tmdb)) if tmdb else None
        return hit[1].verdict if hit and hit[0] == "Rated" else None

    for sec in await stack.plex_sections():
        if sec.get("type") not in ("movie", "show"):
            continue
        is_show = sec["type"] == "show"
        for item in await stack.plex_library(sec["key"], 2 if is_show else 1):
            g = stack.plex_guids(item)
            key = str(item["ratingKey"])
            title = clean(item.get("title"), 120)
            v = verdict_of(g.get("tmdb"))
            arr_item = (series_by.get(("tvdb", g.get("tvdb", ""))) or series_by.get(("tmdb", g.get("tmdb", ""))) if is_show
                        else movies_by.get(g.get("tmdb", "")))
            if not arr_item:
                continue
            why_not = None
            if key in playing:
                why_not = "playing now"
            elif key in shared:
                why_not = "another account watched it recently"
            elif v == "loved":
                why_not = "rated loved"
            if why_not:
                blocked.append({"title": title, "why": why_not})
                continue
            if not is_show:
                if (item.get("viewCount") or 0) < 1 or (item.get("lastViewedAt") or 0) > cutoff:
                    continue
                size = arr_item.get("sizeOnDisk") or 0
                if size <= 0 or await _in_progress(key):
                    continue
                cands.append({"candidate_id": ids.make("t", "radarr", arr_item["id"]),
                              "scope": "delete_title", "type": "movie", "title": title,
                              "size_gb": _gb(size), "verdict": v,
                              "last_viewed": _date(item.get("lastViewedAt"))})
                continue
            size_total = (arr_item.get("statistics") or {}).get("sizeOnDisk") or 0
            if size_total <= 0:
                continue
            whole = ((item.get("leafCount") or 0) > 0 and item.get("viewedLeafCount") == item.get("leafCount")
                     and (item.get("lastViewedAt") or 0) <= cutoff)
            if whole and not await _in_progress(key):
                cands.append({"candidate_id": ids.make("t", "sonarr", arr_item["id"]),
                              "scope": "delete_title", "type": "tv", "title": title,
                              "size_gb": _gb(size_total), "verdict": v,
                              "last_viewed": _date(item.get("lastViewedAt"))})
                continue
            # Fully watched seasons of a show still in progress.
            season_size = {s.get("seasonNumber"): (s.get("statistics") or {}).get("sizeOnDisk") or 0
                           for s in arr_item.get("seasons") or []}
            done = []
            for season in await stack.plex_children(key):
                n = season.get("index")
                if (n and (season.get("leafCount") or 0) > 0
                        and season.get("viewedLeafCount") == season.get("leafCount")
                        and (season.get("lastViewedAt") or 0) <= cutoff
                        and season_size.get(n, 0) > 0
                        and not await _in_progress(str(season["ratingKey"]))):
                    done.append(n)
            if done:
                cands.append({"candidate_id": ids.make("w", "sonarr", arr_item["id"], ",".join(map(str, done))),
                              "scope": "delete_watched_seasons", "type": "tv", "title": title,
                              "seasons": done, "size_gb": _gb(sum(season_size.get(n, 0) for n in done)),
                              "verdict": v, "last_viewed": _date(item.get("lastViewedAt"))})
    rank = {"disliked": 0, "meh": 1, None: 2, "liked": 3}
    cands.sort(key=lambda c: (rank.get(c["verdict"], 2), -(c["size_gb"] or 0)))
    if free_gb:
        picked, total = [], 0.0
        for c in cands:
            if total >= free_gb:
                break
            picked.append(c)
            total += c["size_gb"] or 0
        cands = picked
    return {"disk_free_gb": await stack.disk_free_gb(), "candidates": cands,
            "total_gb": round(sum(c["size_gb"] or 0 for c in cands), 1),
            "blocked": blocked[:20],
            "note": config.personalise("Watched on {owner}'s account — may be someone else in the house. "
                                       "Each delete needs the code sent to {owner}'s phone.")}


def _date(ts: Any) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


# --------------------------------------------------------------------------
# destructive: approved by the owner through the client (elicitation)
# --------------------------------------------------------------------------

Approver = Callable[[str], Awaitable[bool]]

ACTIONS = ("remove_blocklist", "delete_title", "delete_watched_seasons")

# Bounded mode (no human form): limits per rolling 24 hours.
BOUNDED_CAPS = {"delete": 3, "remove_blocklist": 5}
RECYCLE_DAYS = 7


def _json_log_path(name: str):
    return config.REQUESTS_PATH.with_name(name)


def _read_json_log(name: str) -> list[dict[str, Any]]:
    import json
    try:
        data = json.loads(_json_log_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_json_log(name: str, rows: list[dict[str, Any]]) -> None:
    import json
    p = _json_log_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(p)


def _log_json(name: str, row: dict[str, Any], *, keep: int) -> None:
    """Append one row, keeping the newest `keep`."""
    _write_json_log(name, (_read_json_log(name) + [row])[-keep:])


def _read_deletions() -> list[dict[str, Any]]:
    return _read_json_log("deletions.json")


def _new_delete_id() -> str:
    return "d_" + secrets.token_hex(3)


def _log_deletion(row: dict[str, Any]) -> None:
    keep = [r for r in _read_deletions()
            if _now() - datetime.fromisoformat(r["at"]) < timedelta(days=30)]
    _write_json_log("deletions.json", keep + [row])


def _used_24h(*, kind: str, mode: str) -> int:
    return sum(1 for r in _read_deletions()
               if r.get("mode") == mode and r.get("kind") == kind
               and _now() - datetime.fromisoformat(r["at"]) < timedelta(hours=24))


def _bounded_used(kind: str) -> int:
    return _used_24h(kind=kind, mode="bounded")


def _phone_used() -> int:
    return _used_24h(kind="delete", mode="delete_any")


# Names of the library folders under /media when nothing was recorded (setup's own defaults).
DEFAULT_LIBRARY_DIRS = {"sonarr": "TV", "radarr": "Movies"}


def _host_path(*parts: str) -> str:
    """MEDIA_ROOT (the host's path to /media) joined with parts, in the host's own path style:
    backslashes for a Windows path (a drive letter such as C: followed by a backslash or slash), else slashes."""
    root = config.MEDIA_ROOT_HOST
    if re.match(r"^[A-Za-z]:([\\/]|$)", root):
        return "\\".join([re.sub(r"[\\/]+", lambda _m: "\\", root).rstrip("\\"), *[p for p in parts if p]])
    return "/".join([root.rstrip("/"), *[p for p in parts if p]])


def _library_dir_of(path: str | None) -> str | None:
    """'Shows' from /media/Shows/Title[/...]: the library folder a recorded path sits in."""
    parts = [p for p in str(path or "").split("/") if p]
    return parts[1] if len(parts) >= 3 and parts[0] == "media" else None


def _library_dir(app: str, row: dict[str, Any] | None = None) -> str:
    """The movie or TV folder name under the media root: from this delete's recorded paths, else
    from the newest other delete of the same app that recorded one, else the setup default."""
    def of(r: dict[str, Any]) -> str | None:
        for p in [r.get("folder"), *[f.get("path") for f in (r.get("files") or [])[:1]]]:
            d = _library_dir_of(p)
            if d:
                return d
        return None
    if row and (d := of(row)):
        return d
    for r in sorted(_read_deletions(), key=lambda r: r.get("at") or "", reverse=True):
        if (r.get("app") or "sonarr") == app and (d := of(r)):
            return d
    return DEFAULT_LIBRARY_DIRS[app]


def _undo_howto() -> str:
    return (f"Until the undo date the files are in {_host_path('.recyclebin', '<title>')}. Move that folder "
            f"back into {_host_path(_library_dir('sonarr'))} (or {_host_path(_library_dir('radarr'))}), then "
            "request the title again with the same seasons; Sonarr/Radarr pick up the files already on disk.")


def _undo_steps(r: dict[str, Any], undoable: bool, until: str) -> list[str]:
    """Plain steps to bring one delete back, built from what the log recorded."""
    title = r.get("title_name") or r.get("title") or "the title"
    if r.get("restored_at"):
        return [f"Already restored on {str(r['restored_at'])[:10]}."]
    if undoable and r.get("id") and r.get("files"):
        return [f"Call undo_delete with undo_id {r['id']}: it moves the files back out of the recycle bin "
                "and puts the title back in Sonarr/Radarr (monitored seasons as before). Nothing to do by hand.",
                "Seeding torrents removed by the delete are not brought back; the files themselves are."]
    if not undoable:
        return [f"The 7-day window closed on {until}: the files are gone from the recycle bin.",
                f"To get {title} again, request it (find, then request"
                + (f" with seasons {r['seasons']}" if r.get("seasons") else "") + "); it downloads from scratch."]
    seasons = r.get("seasons") or []
    app = "radarr" if r.get("app") == "radarr" else "sonarr"
    lib = _host_path(_library_dir(app, r))
    sep = "\\" if "\\" in lib else "/"
    if app == "radarr":
        home = lib + sep + "<the movie's folder>"
    elif seasons:
        home = (lib + sep + title + sep + "Season NN (one folder per season: "
                + ", ".join(f"{n:02d}" for n in seasons) + ")")
    else:
        home = lib + sep + title
    steps = [f"1. Before {until}, find {'the season files' if seasons else 'the folder'} for {title} in "
             f"{_host_path('.recyclebin')}.",
             f"2. Move {'them' if seasons else 'it'} back to {home}."]
    kind = "movie" if r.get("app") == "radarr" else "tv"
    ask = f"3. Then ask for it to be requested again: find '{title}'"
    if r.get("tmdb_id"):
        ask += f" (tmdb:{r['tmdb_id']})"
    ask += f", request type {kind}" + (f", seasons {seasons}" if kind == "tv" else "")
    if not seasons and kind == "tv":
        ask += " (name the seasons you want back)"
    steps.append(ask + ". Sonarr/Radarr find the files already on disk and import them.")
    steps.append("Note: the seeding torrent copy was removed with the delete, so a re-download is only "
                 "needed if the recycle-bin files are missing.")
    return steps


async def deleted() -> dict[str, Any]:
    """Every delete in the log (30 days), newest first, each with whether it can still be undone
    and the steps to undo it."""
    out = []
    for r in sorted(_read_deletions(), key=lambda r: r["at"], reverse=True):
        if r.get("kind") != "delete":
            continue
        at = datetime.fromisoformat(r["at"])
        undoable = _now() < at + timedelta(days=RECYCLE_DAYS) and not r.get("restored_at")
        until = (at + timedelta(days=RECYCLE_DAYS)).strftime("%Y-%m-%d")
        out.append({"undo_id": r.get("id") if r.get("files") else None, "restored": bool(r.get("restored_at")),
                    "title": r.get("title_name") or r.get("title"), "summary": r.get("summary"),
                    "mode": r.get("mode"),
                    "reason": r.get("reason"), "size_gb": r.get("size_gb"), "seasons": r.get("seasons"),
                    "deleted": at.strftime("%Y-%m-%d %H:%M"), "undo_until": until, "undoable": undoable,
                    "undo_steps": _undo_steps(r, undoable, until)})
    return {"count": len(out), "deletes": out[:50], "undo": _undo_howto(),
            "limits_today": {"bounded_deletes_left": max(0, BOUNDED_CAPS["delete"] - _bounded_used("delete")),
                             "delete_any_left": max(0, config.DELETE_ANY_PER_DAY - _phone_used())}}


async def _plan(target_id: str, action: str) -> tuple[str, dict[str, Any]]:
    """Validate a destructive target against live state; return (summary, payload)."""
    if action == "remove_blocklist":
        t = _decode(target_id, kinds={"q"})
        recs = await _find_download(t)
        r0 = recs[0]
        name = (r0.get("series") or r0.get("movie") or {}).get("title") or "a download"
        return (f"Remove and blocklist the download for {clean(name, 80)} ({len(recs)} item(s), "
                f"{_gb(r0.get('size')) or '?'} GB); {t.app} will search for another release",
                {"app": t.app, "queue_ids": [r["id"] for r in recs]})
    if action == "delete_title":
        t = _decode(target_id, kinds={"t"})
        item = await (stack.get_movie(t.int_key) if t.app == "radarr" else stack.get_series(t.int_key))
        if not item:
            raise ToolError("that title is no longer in Sonarr/Radarr")
        await _still_candidate(target_id)
        size = item.get("sizeOnDisk") or (item.get("statistics") or {}).get("sizeOnDisk")
        return (f"Delete {clean(item.get('title'), 80)} entirely ({_gb(size)} GB). "
                "Files and the Sonarr/Radarr entry are removed.",
                {"app": t.app, "id": t.int_key, "tmdb_id": item.get("tmdbId"),
                 "name": clean(item.get("title"), 80), "size_gb": _gb(size)})
    if action == "delete_watched_seasons":
        t = _decode(target_id, kinds={"w"})
        item = await stack.get_series(t.int_key)
        if not item:
            raise ToolError("that show is no longer in Sonarr")
        await _still_candidate(target_id)
        size = sum((s.get("statistics") or {}).get("sizeOnDisk") or 0
                   for s in item.get("seasons") or [] if s.get("seasonNumber") in t.seasons)
        return (f"Delete {clean(item.get('title'), 80)} season(s) {', '.join(map(str, t.seasons))} "
                f"({_gb(size)} GB). The show stays; those seasons are unmonitored.",
                {"app": "sonarr", "id": t.int_key, "seasons": t.seasons, "tmdb_id": item.get("tmdbId"),
                 "name": clean(item.get("title"), 80), "size_gb": _gb(size)})
    raise ToolError(f"action must be one of {', '.join(ACTIONS)}")


async def _record_files(pl: dict[str, Any]) -> None:
    """Note which files (and folder) a delete is about to remove, so undo_delete can put them back.
    Best effort: if it can't be read the delete still goes ahead and undo falls back to manual steps."""
    try:
        if pl["app"] == "radarr":
            m = await stack.get_movie(pl["id"]) or {}
            f = m.get("movieFile") or {}
            pl["folder"] = m.get("path")
            pl["files"] = [{"path": f["path"], "size": f.get("size")}] if f.get("path") else []
            pl["season_numbers"] = []
        else:
            s = await stack.get_series(pl["id"]) or {}
            keep = pl.get("seasons")
            files = [f for f in await stack.episode_files(pl["id"])
                     if f.get("path") and (not keep or f.get("seasonNumber") in keep)]
            pl["folder"] = s.get("path")
            pl["files"] = [{"path": f["path"], "size": f.get("size")} for f in files][:2000]
            pl["season_numbers"] = sorted({f.get("seasonNumber") for f in files
                                           if f.get("seasonNumber") is not None})
    except Exception as exc:
        log.warning("could not record files before delete: %s", exc.__class__.__name__)
        pl.setdefault("files", [])


async def _do_delete(pl: dict[str, Any]) -> str:
    """Delete a title, or the named seasons of a show, and the seeding copy that holds the same
    blocks. The caller has already checked who may do this. Returns the result text."""
    seasons = pl.get("seasons")
    await _record_files(pl)
    # Which torrents imported these files — read before the *arr forgets them.
    if pl["app"] == "radarr":
        hashes = await stack.history_download_ids("radarr", movie_id=pl["id"])
    else:
        hashes = await stack.history_download_ids("sonarr", series_id=pl["id"], seasons=seasons)
    if not seasons:
        await (stack.delete_movie(pl["id"]) if pl["app"] == "radarr" else stack.delete_series(pl["id"]))
        done = "Deleted."
    else:
        series = await stack.get_series(pl["id"])
        for s in series.get("seasons") or []:
            if s.get("seasonNumber") in seasons:
                s["monitored"] = False
        await stack.put_series(series)
        files = [f["id"] for f in await stack.episode_files(pl["id"])
                 if f.get("seasonNumber") in seasons]
        await stack.delete_episode_files(files)
        done = f"Deleted {len(files)} episode file(s) and unmonitored those seasons."
    # The seeding copy in /media/Downloads shares disk blocks with the
    # library file (hardlink); without removing the torrent no space frees.
    busy = set()
    for app in ("sonarr", "radarr"):
        busy |= {(r.get("downloadId") or "").lower() for r in await stack.queue(app)}
    live = {(t.get("hash") or "").lower() for t in await stack.qbit_torrents()}
    drop = sorted((hashes & live) - busy)
    await stack.qbit_delete(drop, delete_files=True)
    return done + (f" Removed {len(drop)} seeding torrent(s) holding the same files. The files stay in the "
                   f"recycle bin for {RECYCLE_DAYS} days (undo window); the space returns after that.")


async def _still_candidate(target_id: str) -> None:
    current = await cleanup_candidates()
    if not any(c["candidate_id"] == target_id for c in current["candidates"]):
        raise ToolError("that is no longer a cleanup candidate (watched recently, in progress, "
                        "or rated loved)")


async def destructive(target_id: str, action: str, decision: str) -> dict[str, Any]:
    """Check, then act according to how it was confirmed.

    decision: "approved" (the owner ticked the form), "declined" (the owner said no), or
    "bounded" (no human form: the media bot, headless runs). Deletes are always
    limited to cleanup candidates; _plan enforces that for every decision.
    """
    summary, pl = await _plan(target_id, action)
    if decision == "declined":
        return {"ok": False, "status": "declined", "summary": summary,
                "message": config.personalise("{Owner} declined. Nothing was changed.")}
    kind = "remove_blocklist" if action == "remove_blocklist" else "delete"
    if decision != "approved":
        decision = "bounded"
        used = _bounded_used(kind)
        if used >= BOUNDED_CAPS[kind]:
            raise ToolError(f"Limit reached: {used} {kind.replace('_', '+')}(s) without a confirm form in "
                            f"the last 24 hours (max {BOUNDED_CAPS[kind]}). Try tomorrow"
                            + (", or use delete_any with a code." if kind == "delete" else "."))
    if action == "remove_blocklist":
        await stack.queue_remove_bulk(pl["app"], pl["queue_ids"], blocklist=True, search_again=True)
        done = "Removed and blocklisted; a new search is running."
    else:
        done = await _do_delete(pl)
    services.invalidate_status_cache()
    title = summary.split(" (")[0]
    _log_deletion({"at": _now().isoformat(), "kind": kind, "action": action, "mode": decision,
                   "title": title, "summary": summary,
                   **({"id": _new_delete_id(), "title_name": pl["name"], "app": pl["app"],
                       "tmdb_id": pl.get("tmdb_id"), "seasons": pl.get("seasons"), "size_gb": pl.get("size_gb"),
                       "files": pl.get("files"), "folder": pl.get("folder"),
                       "season_numbers": pl.get("season_numbers")}
                      if kind == "delete" and pl.get("name") else {})})
    log.info("destructive %s done (%s): %s", action, decision, summary)
    if decision == "bounded":
        what = "Removed and blocklisted" if kind == "remove_blocklist" else "Deleted"
        await notify.send([notify.Alert(
            "bot_action", title,
            f"{what} by an agent without a confirm form."
            + (f" Undo possible for {RECYCLE_DAYS} days." if kind == "delete" else ""))])
    return {"ok": True, "status": "done", "mode": decision, "summary": summary, "result": done,
            "disk_free_gb": await stack.disk_free_gb()}


# --------------------------------------------------------------------------
# delete_any: any title or seasons the owner names
# --------------------------------------------------------------------------
# Not limited to cleanup candidates. Needs a stated reason, is capped per day, refused
# while playing, and goes to the 7-day recycle bin (undo_delete). Optional gate
# (DELETE_ANY_REQUIRE_CODE, on by default): a six-digit code the server
# sends to the owner through the alert channel; the owner types it into their own message. The
# code is bound to one target, single-use, valid 10 minutes, three tries; it is never
# returned by a tool, logged, or kept in clear. With the code required and no alert channel,
# delete_any is disabled. A stated reason is required and
# logged; at most DELETE_ANY_PER_DAY a rolling day. Same recycle bin and undo.

DELETE_ANY_DISABLED = (
    "delete_any is disabled because there is no alert channel to send the confirmation code to. "
    "Set NOTIFIER=telegram (with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) or a webhook, or switch to "
    "no-code mode (DELETE_ANY_REQUIRE_CODE=false, which removes the confirmation step and its "
    "protection against a wrong or tricked delete). Bounded delete of cleanup candidates still works.")


def delete_any_mode() -> str:
    """"code" (confirmation code required), "no_code" (owner opted out) or "disabled" (code required, no channel)."""
    if not config.DELETE_ANY_REQUIRE_CODE:
        return "no_code"
    return "code" if notify.enabled() else "disabled"

PHONE_CODE_MINUTES = 10
PHONE_CODE_TRIES = 3
PHONE_CODES_PER_WINDOW = 3           # new codes per 10 minutes

_phone: dict[str, Any] = {}          # the one live code: key, salt, digest, label, expires, tries
_phone_issued: list[datetime] = []
_phone_locked_until: datetime | None = None


def _phone_digest(salt: str, digits: str) -> str:
    return hashlib.sha256(f"{salt}:{digits}".encode()).hexdigest()


def _phone_key(item_id: str, seasons: list[int] | None) -> str:
    return f"{item_id}|{','.join(map(str, seasons or []))}"


async def _plan_delete_any(item_id: str, seasons: list[int] | None) -> tuple[str, dict[str, Any]]:
    t = _decode(item_id, kinds={"t"})
    item = await (stack.get_movie(t.int_key) if t.app == "radarr" else stack.get_series(t.int_key))
    if not item:
        raise ToolError("that title is no longer in Sonarr/Radarr (library() lists what can be deleted)")
    name = clean(item.get("title"), 80)
    if t.app == "radarr":
        if seasons:
            raise ToolError("a movie has no seasons; leave seasons out")
        size = item.get("sizeOnDisk") or (item.get("statistics") or {}).get("sizeOnDisk")
        return (f"Delete {name} entirely ({_gb(size) or 0} GB)",
                {"app": "radarr", "id": t.int_key, "tmdb_id": item.get("tmdbId"), "name": name,
                 "size_gb": _gb(size)})
    by_num = {s.get("seasonNumber"): s for s in item.get("seasons") or []}
    if not seasons:
        size = (item.get("statistics") or {}).get("sizeOnDisk")
        return (f"Delete {name} entirely ({_gb(size) or 0} GB)",
                {"app": "sonarr", "id": t.int_key, "tmdb_id": item.get("tmdbId"), "name": name,
                 "size_gb": _gb(size)})
    missing = [n for n in seasons if n not in by_num]
    if missing:
        raise ToolError(f"{name} has no season(s) {missing}; it has {sorted(by_num)}")
    empty = [n for n in seasons if not (by_num[n].get("statistics") or {}).get("sizeOnDisk")]
    if empty:
        raise ToolError(f"season(s) {empty} of {name} have no files on disk; nothing to delete there")
    size = sum((by_num[n].get("statistics") or {}).get("sizeOnDisk") or 0 for n in seasons)
    return (f"Delete {name} season(s) {', '.join(map(str, seasons))} ({_gb(size) or 0} GB)",
            {"app": "sonarr", "id": t.int_key, "tmdb_id": item.get("tmdbId"), "name": name,
             "size_gb": _gb(size), "seasons": seasons})


async def _check_not_playing(name: str) -> None:
    try:
        sessions = await stack.plex_sessions()
    except Exception:
        raise ToolError("could not check whether it is playing right now; not deleting") from None
    if _norm_title(name) in {_norm_title(s.get("grandparentTitle") or s.get("title")) for s in sessions}:
        raise ToolError(f"{name} is playing right now; try again when it has stopped")


def _phone_locked() -> None:
    if _phone_locked_until and _now() < _phone_locked_until:
        raise ToolError("Too many wrong codes. New codes are blocked until "
                        f"{_phone_locked_until.astimezone().strftime('%H:%M')}.")


async def _issue_code(key: str, summary: str) -> dict[str, Any]:
    _phone_locked()
    now = _now()
    recent = [t for t in _phone_issued if now - t < timedelta(minutes=10)]
    if len(recent) >= PHONE_CODES_PER_WINDOW:
        raise ToolError(f"{len(recent)} codes were already sent in the last 10 minutes. Wait a little.")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    label = secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") + str(secrets.randbelow(10))
    salt = secrets.token_hex(8)
    _phone.clear()
    _phone.update(key=key, salt=salt, digest=_phone_digest(salt, code), label=label, tries=0,
                  expires=now + timedelta(minutes=PHONE_CODE_MINUTES))
    if not await notify.send_code(label, summary, code, PHONE_CODE_MINUTES):
        _phone.clear()
        raise ToolError(config.personalise("Could not send the code to {owner} (the alert channel failed or is over "
                                           "its hourly limit). Nothing was deleted."))
    _phone_issued[:] = recent + [now]
    return {"ok": False, "status": "code_sent", "label": f"#{label}", "summary": summary,
            "valid_minutes": PHONE_CODE_MINUTES,
            "message": (f"A one-time code for '{summary}' was sent to " + config.owner() + f" [#{label}]. Ask them for it. "
                        "Do not look for it anywhere else. When they give it, call delete_any again with the "
                        "same item_id, seasons and reason, plus code. It works once, for 10 minutes, 3 tries.")}


def _check_code(key: str, code: str) -> None:
    global _phone_locked_until
    _phone_locked()
    now = _now()
    if not _phone or now >= _phone["expires"]:
        _phone.clear()
        raise ToolError("There is no live code for this delete (they last 10 minutes). Call delete_any "
                        "without a code to send " + config.owner() + " a new one.")
    if _phone["key"] != key:
        raise ToolError("The live code is for a different delete. Use the same item_id and seasons you "
                        "asked the code for, or call without a code to start over.")
    digits = re.sub(r"\D", "", str(code))
    if not hmac.compare_digest(_phone_digest(_phone["salt"], digits), _phone["digest"]):
        _phone["tries"] += 1
        if _phone["tries"] >= PHONE_CODE_TRIES:
            _phone.clear()
            _phone_locked_until = now + timedelta(minutes=10)
            raise ToolError("Wrong code three times. That code is cancelled and new codes are blocked "
                            "for 10 minutes. Nothing was deleted.")
        raise ToolError(f"Wrong code ({PHONE_CODE_TRIES - _phone['tries']} tries left). Nothing was deleted.")
    _phone.clear()  # single use


async def delete_any(item_id: str, reason: str, seasons: list[int] | None = None,
                     code: str | None = None) -> dict[str, Any]:
    if delete_any_mode() == "disabled":
        raise ToolError(DELETE_ANY_DISABLED)
    reason = clean(reason, 300)
    if len(reason.split()) < 3:
        raise ToolError(config.personalise("A stated reason is required: what {owner} said about why (a few words)."))
    seasons = sorted({int(n) for n in seasons}) if seasons else None
    summary, pl = await _plan_delete_any(item_id, seasons)
    await _check_not_playing(pl["name"])
    used = _phone_used()
    if used >= config.DELETE_ANY_PER_DAY:
        raise ToolError(f"Limit reached: {used} delete_any(s) in the last 24 hours (max "
                        f"{config.DELETE_ANY_PER_DAY}). Try tomorrow.")
    if config.DELETE_ANY_REQUIRE_CODE:
        key = _phone_key(item_id, seasons)
        if not code:
            return await _issue_code(key, summary)
        _check_code(key, code)

    row = {"id": _new_delete_id(), "at": _now().isoformat(), "kind": "delete", "action": "delete_any",
           "mode": "delete_any", "title": summary.split(" (")[0], "summary": summary, "reason": reason,
           "title_name": pl["name"], "app": pl["app"], "tmdb_id": pl.get("tmdb_id"),
           "seasons": pl.get("seasons"), "size_gb": pl.get("size_gb")}

    def with_files() -> dict[str, Any]:
        return {**row, "files": pl.get("files"), "folder": pl.get("folder"),
                "season_numbers": pl.get("season_numbers")}
    try:
        result = await _do_delete(pl)
    except Exception as exc:
        # Part of it may have happened. Log it (it counts toward the cap) and say so.
        _log_deletion({**with_files(), "summary": summary + " [FAILED part-way]"})
        services.invalidate_status_cache()
        log.error("delete_any failed part-way: %s", exc.__class__.__name__)
        raise ToolError("The delete failed part-way (" + clean(exc, 200) + "). Some files may already be in "
                        "the recycle bin; see deleted() and library().") from None
    services.invalidate_status_cache()
    row = with_files()
    _log_deletion(row)
    log.info("delete_any done: %s", summary)
    scope = f"seasons {', '.join(map(str, pl['seasons']))}" if pl.get("seasons") else "whole title"
    await notify.send([notify.Alert(
        "bot_action", pl["name"],
        config.personalise(f"Deleted ({scope}) by the media bot at {{owner}}'s request. "
                           f"Undo possible for {RECYCLE_DAYS} days."))])
    undo = _undo_steps(row, True, (_now() + timedelta(days=RECYCLE_DAYS)).strftime("%Y-%m-%d"))
    return {"ok": True, "status": "done", "mode": "delete_any", "summary": summary, "result": result,
            "undo_steps": undo, "delete_any_left_today": max(0, config.DELETE_ANY_PER_DAY - _phone_used()),
            "disk_free_gb": await stack.disk_free_gb()}

# --------------------------------------------------------------------------
# undo_delete: put a delete back from the recycle bin
# --------------------------------------------------------------------------

def _restore_files(bin_dir: Path, files: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Move recorded files from the recycle bin back to where they were. Runs in a thread.

    Only paths under the media root (the recycle bin's parent) are written, nothing is
    overwritten, and a file is matched by name (and size when known), newest first."""
    import os
    import shutil
    bin_real = os.path.realpath(bin_dir)
    root = os.path.realpath(bin_dir.parent)
    index: dict[str, list[str]] = {}
    for dirpath, _dirs, names in os.walk(bin_dir):
        for n in names:
            index.setdefault(n, []).append(os.path.join(dirpath, n))
    out: dict[str, list[str]] = {"restored": [], "missing": [], "already_there": [], "refused": []}
    for f in files:
        dest = os.path.realpath(str(f.get("path") or ""))
        name = os.path.basename(dest)
        try:
            inside = bool(f.get("path")) and os.path.commonpath([root, dest]) == root
        except ValueError:
            inside = False
        if not inside or dest.startswith(bin_real + os.sep):
            out["refused"].append(name)
            continue
        if os.path.exists(dest):
            out["already_there"].append(name)
            continue
        cands = index.get(name, [])
        if f.get("size"):
            cands = [c for c in cands if os.path.getsize(c) == f["size"]]
        if not cands:
            out["missing"].append(name)
            continue
        src = max(cands, key=os.path.getmtime)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        index[name].remove(src)
        out["restored"].append(name)
    return out


def _mark_restored(undo_id: str) -> None:
    rows = _read_deletions()
    for r in rows:
        if r.get("id") == undo_id:
            r["restored_at"] = _now().isoformat()
    _write_json_log("deletions.json", rows)


async def undo_delete(undo_id: str) -> dict[str, Any]:
    """Bring back a delete made in the last 7 days: files out of the recycle bin, title back in the *arr."""
    row = next((r for r in _read_deletions() if r.get("id") == undo_id and r.get("kind") == "delete"), None)
    if not row:
        raise ToolError("unknown undo_id. deleted() lists the deletes that can be undone, each with its undo_id")
    if row.get("restored_at"):
        raise ToolError("that delete was already undone")
    if _now() >= datetime.fromisoformat(row["at"]) + timedelta(days=RECYCLE_DAYS):
        raise ToolError(f"the {RECYCLE_DAYS}-day window for that delete has closed; the files are gone. "
                        "Request the title again to download it.")
    if not row.get("files"):
        raise ToolError("that delete did not record its files, so it cannot be undone automatically; "
                        "deleted() has the manual steps")
    app = row.get("app") or "sonarr"
    bin_path = await stack.recycle_bin(app)
    if not bin_path or not Path(bin_path).is_dir():
        raise ToolError("the recycle bin is not reachable from MediaHub; nothing was changed")
    res = await asyncio.to_thread(_restore_files, Path(bin_path), row["files"])
    if not res["restored"] and not res["already_there"]:
        raise ToolError(f"none of the {len(row['files'])} file(s) are in the recycle bin any more "
                        f"(missing: {len(res['missing'])}, refused: {len(res['refused'])}). Nothing was changed.")

    tmdb, name, folder = row.get("tmdb_id"), row.get("title_name") or "the title", row.get("folder")
    if app == "radarr":
        have = next((m for m in await stack.movies() if m.get("tmdbId") == tmdb), None)
        what = "movie still in Radarr, rescanned"
        if not have:
            what = "movie added back and rescanned"
            lk = await stack.lookup_movie_tmdb(int(tmdb)) if tmdb else None
            if not lk or not folder:
                raise ToolError("files are back, but Radarr could not find the movie to add it again; "
                                "request it by hand and it will pick the files up")
            have = await stack.add_movie(lk, profile=await stack.profile_id("radarr", config.DEFAULT_PROFILE),
                                         root=await stack.root_folder("radarr"), path=folder, search=False)
        await stack.command("radarr", "RescanMovie", movieIds=[have["id"]])
    else:
        seasons = row.get("seasons") or row.get("season_numbers") or []
        have = next((s for s in await stack.series_list() if s.get("tmdbId") == tmdb), None)
        if not have:
            lk = await stack.lookup_series_tmdb(int(tmdb)) if tmdb else None
            if not lk or not folder:
                raise ToolError("files are back, but Sonarr could not find the show to add it again; "
                                "request it by hand and it will pick the files up")
            have = await stack.add_series(lk, seasons=seasons,
                                          profile=await stack.profile_id("sonarr", config.DEFAULT_PROFILE),
                                          root=await stack.root_folder("sonarr"), path=folder)
            await asyncio.sleep(2)  # Sonarr creates episode rows right after add
            what = "show added back and rescanned"
        else:
            for s in have.get("seasons") or []:
                if s.get("seasonNumber") in seasons:
                    s["monitored"] = True
            have = await stack.put_series(have) or have
            what = "seasons re-monitored and rescanned"
        await stack.command("sonarr", "RescanSeries", seriesId=have["id"])
        eps = await stack.episodes(have["id"])
        await stack.monitor_episodes([e["id"] for e in eps if e.get("seasonNumber") in seasons])
    _mark_restored(undo_id)
    services.invalidate_status_cache()
    await notify.send([notify.Alert("bot_action", name, f"Delete undone: {len(res['restored'])} file(s) restored.")])
    log.info("undo_delete %s: %s restored, %s missing", undo_id, len(res["restored"]), len(res["missing"]))
    return {"ok": True, "status": "restored", "title": name, "restored_files": len(res["restored"]),
            "missing_files": len(res["missing"]), "already_there": len(res["already_there"]),
            "result": what + ". Plex picks it up on its next scan (a minute or two).",
            "note": ("Some files were no longer in the recycle bin; the rest are back." if res["missing"] else None)}
