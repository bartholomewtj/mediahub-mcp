"""Background loop that keeps downloads and requests moving. Deterministic; never calls an LLM.

Every two minutes it:

1. Groups the Sonarr/Radarr queues by download (a season pack is one torrent
   but many queue records) and checks each against qBittorrent.
2. Rechecks a nearly-finished stall once; swaps a stalled or dead download for
   a healthier release (season search for packs), at most MAX_SWAPS times.
3. Rescues requests made through the MCP (data/requests.json) that never
   grabbed anything: one search per pass, attempts per movie/season/episode
   with backoff, then escalation.
4. Escalates what it cannot fix: a problem the agent can see, and one alert on the configured channel.

Release choice uses health.py: filters (junk, size, archives, language) and a
score weighted by each indexer's import/fail track record.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, health, ids, notify, requests_store, stack
from .health import (  # noqa: F401  (re-exported for tests and callers)
    ALMOST_DONE, GRACE, MIN_SEEDERS, QUALITY_LADDER, STALL, classify_download, cutoff_only,
    health_score, is_healthier, is_junk, pick_release, quality_allowed, resolution_of,
)

log = logging.getLogger("mediahub.watch")

POLL_SECONDS = 120
UNGRABBED = timedelta(minutes=60)        # let *arr's own search on add run first
EMPTY_SEARCH_WAIT = timedelta(hours=6)   # after a swap search found nothing better
SEARCH_COOLDOWN = timedelta(seconds=60)
MAX_SWAPS = 2
MAX_ACTIONS = 20
BACKOFF = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)]
MAX_ATTEMPTS = len(BACKOFF)

STATE_PATH = config.ROOT / "data" / "watch-state.json"


# --------------------------------------------------------------------------
# pure helpers (unit-tested)
# --------------------------------------------------------------------------

def season_numbers(seasons: list[Any]) -> list[int] | None:
    """Season objects with seasonNumber, or plain ints. None = all."""
    if not seasons:
        return None
    out: list[int] = []
    for s in seasons:
        if isinstance(s, int):
            out.append(s)
        elif isinstance(s, dict) and s.get("seasonNumber") is not None:
            out.append(int(s["seasonNumber"]))
    return out or None


def _aired(ep: dict[str, Any], now: datetime) -> bool:
    air = ep.get("airDateUtc") or ep.get("airDate")
    if not air:
        return False
    try:
        when = datetime.fromisoformat(str(air).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def first_missing_episode(episodes: list[dict[str, Any]], *, seasons: list[int] | None,
                          queued_ids: set[int], now: datetime) -> dict[str, Any] | None:
    found = missing_episodes(episodes, seasons=seasons, queued_ids=queued_ids, now=now)
    return found[0] if found else None


def missing_episodes(episodes: list[dict[str, Any]], *, seasons: list[int] | None,
                     queued_ids: set[int], now: datetime) -> list[dict[str, Any]]:
    wanted = set(seasons) if seasons else None
    found = []
    for ep in episodes:
        if not ep.get("monitored") or ep.get("hasFile") or ep.get("id") in queued_ids:
            continue
        season = ep.get("seasonNumber")
        if wanted is not None and season not in wanted:
            continue
        if season == 0 and (wanted is None or 0 not in wanted):
            continue
        if not _aired(ep, now):
            continue
        found.append(ep)
    found.sort(key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
    return found


def rescue_targets(request: dict[str, Any], *, movie: dict | None, episodes: list[dict],
                   queued_movies: set[int], queued_episodes: set[int],
                   now: datetime) -> list[str]:
    """Target ids still needing a grab for one request, in search order.

    A season with nothing on disk and nothing queued is one season target
    (pack search); otherwise each missing aired episode is its own target.
    """
    if request.get("type") == "movie":
        if not movie or movie.get("hasFile") or movie.get("id") in queued_movies:
            return []
        available = movie.get("isAvailable") or movie.get("status") in ("inCinemas", "released")
        return [ids.make("m", "radarr", movie["id"])] if available else []
    out: list[str] = []
    sid = request.get("arr_id")
    for season in request.get("seasons") or []:
        in_season = [e for e in episodes if e.get("seasonNumber") == season]
        missing = missing_episodes(in_season, seasons=[season], queued_ids=queued_episodes, now=now)
        if not missing:
            continue
        untouched = all(not e.get("hasFile") and e.get("id") not in queued_episodes
                        for e in in_season if _aired(e, now))
        if untouched and len(missing) > 1:
            out.append(ids.make("s", "sonarr", sid, season))
        else:
            out.extend(ids.make("e", "sonarr", e["id"]) for e in missing)
    return out


def attempt_due(att: dict[str, Any] | None, now: datetime) -> str:
    """'go', 'wait' or 'exhausted' for one rescue target."""
    if not att:
        return "go"
    if int(att.get("count") or 0) >= MAX_ATTEMPTS:
        return "exhausted"
    nxt = _parse_iso(att.get("next_at"))
    return "wait" if nxt and nxt > now else "go"


def record_attempt(att: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """An empty search: count it and schedule the next try."""
    att = dict(att or {})
    n = int(att.get("count") or 0) + 1
    att["count"] = n
    att["last_at"] = _iso(now)
    if n < MAX_ATTEMPTS:
        att["next_at"] = _iso(now + BACKOFF[n - 1])
    else:
        att["next_at"] = None
    return att


def group_queue(records: list[dict[str, Any]], app: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        dl = (r.get("downloadId") or f"rec{r.get('id')}").lower()
        groups.setdefault(f"{app}:{dl}", []).append(r)
    return groups


# --------------------------------------------------------------------------
# state (written only by the loop)
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("items", {})
            data.setdefault("actions", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": {}, "last_search_at": None, "actions": [], "last_run": None, "last_error": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _log_action(state: dict[str, Any], title: str, action: str, detail: str) -> None:
    state.setdefault("actions", []).insert(0, {"at": _iso(_now()), "title": title,
                                                "action": action, "detail": detail})
    del state["actions"][MAX_ACTIONS:]
    log.info("%s %s — %s", action, title, detail)


def _problem(target_id: str | None, title: str, detail: str, severity: str = "warning") -> dict[str, Any]:
    return {"id": f"watch:{target_id or title}", "service": "Watch", "kind": "watch",
            "severity": severity, "title": title, "detail": detail, "link": None,
            "actions": [], "target_id": target_id}


def _prune_items(state: dict[str, Any], seen: set[str], now: datetime) -> None:
    keep = {}
    for key, row in (state.get("items") or {}).items():
        last = _parse_iso(row.get("last_seen")) or _parse_iso(row.get("first_seen")) or now
        if key in seen or now - last < timedelta(days=3):
            keep[key] = row
    state["items"] = keep


# --------------------------------------------------------------------------
# snapshot for the page and the MCP
# --------------------------------------------------------------------------

_lock = asyncio.Lock()          # guards one pass and its state file
_public: dict[str, Any] = {"enabled": False, "last_run": None, "last_error": None,
                           "actions": [], "counts": {}, "problems": []}
_public_lock = asyncio.Lock()   # short; never held across network I/O


async def _publish(state: dict[str, Any], counts: dict[str, int], problems: list[dict[str, Any]]) -> None:
    async with _public_lock:
        _public.update({"enabled": config.WATCH_ENABLED, "last_run": state.get("last_run"),
                        "last_error": state.get("last_error"),
                        "actions": list(state.get("actions") or []),
                        "counts": counts, "problems": problems})


async def snapshot() -> dict[str, Any]:
    async with _public_lock:
        out = deepcopy(_public)
    out["enabled"] = config.WATCH_ENABLED
    return out


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

async def tick(now: datetime | None = None) -> None:
    now = now or _now()
    async with _lock:
        state = load_state()
        counts: dict[str, int] = {}
        problems: list[dict[str, Any]] = []
        try:
            counts, problems = await _run_pass(state, now)
            state["last_error"] = None
        except Exception as exc:
            log.exception("watch pass failed")
            state["last_error"] = str(exc) or exc.__class__.__name__
        state["last_run"] = _iso(now)
        save_state(state)
        await _publish(state, counts, problems)


class _Slot:
    """One indexer search per pass, and not more often than SEARCH_COOLDOWN."""

    def __init__(self, state: dict[str, Any], now: datetime):
        last = _parse_iso(state.get("last_search_at"))
        self.free = not (last and now - last < SEARCH_COOLDOWN)
        self.state, self.now = state, now

    def take(self) -> bool:
        if not self.free:
            return False
        self.free = False
        self.state["last_search_at"] = _iso(self.now)
        return True


async def _run_pass(state: dict[str, Any], now: datetime) -> tuple[dict[str, int], list[dict[str, Any]]]:
    counts = {"progressing": 0, "stalled": 0, "waiting": 0, "grace": 0, "paused": 0,
              "error": 0, "importing": 0, "done": 0, "other": 0}
    problems: list[dict[str, Any]] = []
    queues: dict[str, list[dict[str, Any]]] = {}
    for app in ("sonarr", "radarr"):
        try:
            queues[app] = await stack.queue(app)
        except Exception as exc:
            # Unknown queue: neither judge its downloads nor rescue its
            # requests (we cannot tell what is already downloading).
            log.warning("%s queue unreadable: %s", app, exc)
    try:
        torrents = {(t.get("hash") or "").lower(): t for t in await stack.qbit_torrents()}
    except Exception as exc:
        # Without qBittorrent every download would look lost. Judge nothing.
        log.warning("qBittorrent unreadable, skipping downloads this pass: %s", exc)
        problems.append(_problem(None, "qBittorrent", f"Unreadable ({exc.__class__.__name__}); "
                                 "downloads not checked this pass."))
        torrents = None
    tracks: dict[str, dict] = {}
    for app in ("sonarr", "radarr"):
        try:
            tracks[app] = await stack.indexer_track_record(app)
        except Exception:
            tracks[app] = {}

    slot = _Slot(state, now)
    seen: set[str] = set(state.get("items") or {}) if torrents is None else set()
    for app, records in (queues.items() if torrents is not None else ()):  # only readable apps
        for key, group in group_queue(records, app).items():
            seen.add(key)
            status = await _handle_download(state, app, key, group, torrents, tracks[app],
                                            now, problems, slot)
            counts[status if status in counts else "other"] += 1
    _prune_items(state, seen, now)

    await _rescue_requests(state, queues, tracks, now, problems, slot)
    await _weekly(state, now)
    if _ALERTS:
        busy = sum(v for k, v in counts.items() if k not in ("done",))
        try:
            free = await stack.disk_free_gb()
        except Exception:
            free = None
        line = f"{busy} download(s) active, {len(problems)} problem(s), disk free {free} GB"
        await notify.send(list(_ALERTS), line)
        _ALERTS.clear()
    return counts, problems


_ALERTS: list[notify.Alert] = []

_DAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def weekly_due(spec: str, local_now: datetime, last_fired: str | None) -> str | None:
    """'SUN 10:00' → the date string to record if it should fire now, else None.

    Fires once per week, at or after the given local time on that weekday (so a
    pass that runs late, or after a restart, still fires the same day).
    """
    try:
        day, hhmm = spec.split()
        hh, mm = (int(x) for x in hhmm.split(":"))
        wd = _DAYS[day.upper()[:3]]
    except (ValueError, KeyError):
        return None
    if local_now.weekday() != wd or (local_now.hour, local_now.minute) < (hh, mm):
        return None
    today = local_now.strftime("%Y-%m-%d")
    return None if last_fired == today else today


async def _weekly(state: dict[str, Any], now: datetime) -> None:
    """Sunday cleanup suggestion and Friday picks, as alerts to the media bot."""
    local = now.astimezone()
    fired = state.setdefault("weekly", {})
    due = weekly_due(config.WEEKLY_CLEANUP, local, fired.get("cleanup"))
    if due:
        fired["cleanup"] = due
        try:
            from . import tools
            out = await tools.cleanup_candidates()
            cands = out.get("candidates") or []
            if cands:
                top = ", ".join(f"{notify.safe_title(c['title'])}"
                                + (f" S{','.join(map(str, c['seasons']))}" if c.get("seasons") else "")
                                + f" ({c['size_gb']} GB)" for c in cands[:6])
                _ALERTS.append(notify.Alert(
                    "weekly_cleanup", "Weekly cleanup",
                    f"Could free {out.get('total_gb')} GB (disk free {out.get('disk_free_gb')} GB): {top}."))
        except Exception as exc:
            log.warning("weekly cleanup suggestion failed: %s", exc)
    due = weekly_due(config.WEEKLY_PICKS, local, fired.get("picks"))
    if due:
        fired["picks"] = due
        _ALERTS.append(notify.Alert("weekly_picks", "Weekly picks",
                                    config.personalise("Time for {owner}'s 3 weekly picks.")))


def display_title(rec: dict[str, Any]) -> str:
    """A title from *arr metadata for alerts — never the release name."""
    if rec.get("movie"):
        m = rec["movie"]
        return f"{m.get('title')} ({m.get('year')})" if m.get("year") else str(m.get("title"))
    series = (rec.get("series") or {}).get("title")
    ep = rec.get("episode") or {}
    if series and ep:
        return f"{series} S{int(ep.get('seasonNumber') or 0):02d}E{int(ep.get('episodeNumber') or 0):02d}"
    return series or "a download"


async def _handle_download(state, app, key, group, torrents, track, now, problems, slot) -> str:
    r0 = group[0]
    title = r0.get("title") or "(untitled)"   # release name: problems/MCP only (fenced there)
    shown = display_title(r0) if len(group) == 1 else (
        f"{(r0.get('series') or {}).get('title') or 'a show'} season "
        f"{(r0.get('episode') or {}).get('seasonNumber', '?')}")
    dl = (r0.get("downloadId") or "").lower()
    target = ids.make("q", app, dl) if dl else None
    item = state["items"].setdefault(key, {"first_seen": _iso(now), "last_bytes": 0,
                                           "last_progress_at": _iso(now), "swap_count": 0,
                                           "recheck_tried": False, "dead_since": None,
                                           "escalated_at": None, "import_alerted": False})
    item["last_seen"] = _iso(now)
    if ((r0.get("trackedDownloadStatus") or "").lower() in ("warning", "error")
            and (r0.get("trackedDownloadState") or "") in ("importPending", "importBlocked", "importFailed", "failedPending")
            and not item.get("import_alerted")):
        item["import_alerted"] = True
        _ALERTS.append(notify.Alert("import_failed", shown,
                                    f"{'Sonarr' if app == 'sonarr' else 'Radarr'} could not import it."))
    torrent = torrents.get(dl)
    size = sum(int(r.get("size") or 0) for r in group[:1])
    left = sum(int(r.get("sizeleft") or 0) for r in group[:1])
    downloaded = int(torrent.get("downloaded") or 0) if torrent else max(0, size - left)
    if downloaded > int(item.get("last_bytes") or 0):
        item["last_progress_at"] = _iso(now)
    item["last_bytes"] = downloaded

    added_at = _parse_iso(r0.get("added"))
    qstate, dlspeed, seeds = None, 0, 0
    percent = round((size - left) / size * 100, 1) if size else 0.0
    if torrent:
        qstate = torrent.get("state")
        dlspeed = int(torrent.get("dlspeed") or 0)
        seeds = int(torrent.get("num_seeds") or 0)
        percent = round(float(torrent.get("progress") or 0) * 100, 1)
        if torrent.get("added_on"):
            added_at = datetime.fromtimestamp(int(torrent["added_on"]), tz=timezone.utc)
    elif dl:
        start = added_at or _parse_iso(item.get("first_seen")) or now
        if now - start >= GRACE:
            qstate = "error"  # lost from qBittorrent

    status = classify_download(
        qbit_state=qstate,
        tracked_status="error" if (r0.get("status") or "").lower() == "failed" else r0.get("trackedDownloadStatus"),
        tracked_state=r0.get("trackedDownloadState"), dlspeed=dlspeed, percent=percent,
        added_at=added_at, last_progress_at=_parse_iso(item.get("last_progress_at")), now=now)

    if torrent and status not in ("stalled", "error", "almost_done_stalled"):
        dead, since = health.swarm_dead(
            torrent, now_ts=now.timestamp(),
            first_seen_ts=(added_at or _parse_iso(item["first_seen"]) or now).timestamp(),
            dead_since_ts=item.get("dead_since"))
        item["dead_since"] = since
        if dead:
            status = "almost_done_stalled" if percent >= ALMOST_DONE else "stalled"

    if status == "almost_done_stalled" and not item.get("recheck_tried") and torrent:
        # One try only, whether or not it works: a failing call must not repeat every pass.
        item["recheck_tried"] = True
        item["last_progress_at"] = _iso(now)
        item["dead_since"] = None
        try:
            await _qbit("resume", torrent["hash"])
            await _qbit("recheck", torrent["hash"])
            _log_action(state, title, "recheck", f"{percent:.0f}% done, stalled — resume + force recheck")
        except Exception as exc:
            log.warning("recheck failed for %s: %s", title, exc)
            _log_action(state, title, "recheck-failed", exc.__class__.__name__)
        return "stalled"

    if status not in ("stalled", "error", "almost_done_stalled"):
        return status

    if int(item.get("swap_count") or 0) >= MAX_SWAPS:
        await _escalate(item, target, title, shown, f"Still stuck after {MAX_SWAPS} swaps.", problems, now)
        return "stalled"
    empty_at = _parse_iso(item.get("empty_search_at"))
    if empty_at and now - empty_at < EMPTY_SEARCH_WAIT:
        problems.append(_problem(target, title, "Stalled, and no healthier release passed the filters. "
                                 "Needs a decision.", "error"))
        return "stalled"
    if not slot.take():
        return "stalled"
    await _swap(state, item, app, target, group, title, shown, seeds, status == "error" or seeds <= 0,
                track, now, problems)
    return "stalled"


async def _qbit(path: str, torrent_hash: str) -> None:
    from . import services
    if path == "resume":
        await services.qbit_resume(torrent_hash)
    else:
        await services.qbit_recheck(torrent_hash)


async def _swap(state, item, app, target, group, title, shown, seeds, dead, track, now, problems) -> None:
    r0 = group[0]
    q = (r0.get("quality") or {}).get("quality") or {}
    pack = app == "sonarr" and len(group) > 1
    try:
        if app == "radarr":
            search = {"movie_id": r0.get("movieId")}
            runtime = (r0.get("movie") or {}).get("runtime")
        elif pack:
            search = {"series_id": r0.get("seriesId"), "season": (r0.get("episode") or {}).get("seasonNumber")}
            runtime = (r0.get("series") or {}).get("runtime")
        else:
            search = {"episode_id": r0.get("episodeId")}
            runtime = (r0.get("series") or {}).get("runtime")
        releases = await stack.release_search(app, **search)
    except Exception as exc:
        problems.append(_problem(target, title, f"Stalled; search failed: {exc}"))
        return
    ranked = health.rank_releases(
        releases, history_counts=track, current_resolution=q.get("resolution"),
        current_seeders=seeds, current_dead=dead, current_title=title,
        runtime_minutes=runtime, episode_count=len(group), prefer_full_season=pack,
        allow_foreign=health.foreign_ok(r0.get("movie") or r0.get("series")))
    if not ranked:
        item["empty_search_at"] = _iso(now)  # don't search this again for EMPTY_SEARCH_WAIT
        await _escalate(item, target, title, shown,
                        "Stalled, and no healthier release passed the filters.", problems, now)
        return
    winner = ranked[0]["release"]
    try:
        await stack.queue_remove_bulk(app, [r["id"] for r in group], blocklist=True, search_again=False)
        await stack.grab(app, winner["guid"], int(winner["indexerId"]))
    except Exception as exc:
        problems.append(_problem(target, title, f"Swap failed: {exc}", "error"))
        return
    item["swap_count"] = int(item.get("swap_count") or 0) + 1
    _log_action(state, title, "swapped",
                f"{seeds} seeds → {winner.get('title')} ({winner.get('seeders') or 0} seeders, "
                f"score {ranked[0]['score']:.0f}, indexer record {ranked[0]['track']:.2f})")


async def _escalate(item: dict[str, Any], target: str | None, title: str, shown: str, detail: str,
                    problems: list[dict[str, Any]], now: datetime) -> None:
    problems.append(_problem(target, title, detail + " Needs a decision.", "error"))
    if item.get("escalated_at"):
        return
    item["escalated_at"] = _iso(now)
    _ALERTS.append(notify.Alert("stuck_download", shown, detail))


async def _rescue_requests(state, queues, tracks, now, problems, slot) -> None:
    rows = await requests_store.snapshot(now)
    if not rows:
        return
    queued_movies = {r.get("movieId") for r in queues.get("radarr") or [] if r.get("movieId")}
    queued_eps = {r.get("episodeId") for r in queues.get("sonarr") or [] if r.get("episodeId")}
    for req in rows:
        if req.get("app") not in queues:  # that app's queue is unreadable this pass
            continue
        requested = _parse_iso(req.get("requested_at")) or now
        if now - requested < UNGRABBED:
            continue
        try:
            if req["type"] == "movie":
                movie = await stack.get_movie(int(req["arr_id"]))
                targets = rescue_targets(req, movie=movie, episodes=[], queued_movies=queued_movies,
                                         queued_episodes=queued_eps, now=now)
            else:
                eps = await stack.episodes(int(req["arr_id"]))
                targets = rescue_targets(req, movie=None, episodes=eps, queued_movies=queued_movies,
                                         queued_episodes=queued_eps, now=now)
        except Exception as exc:
            log.warning("rescue read failed for %s: %s", req.get("title"), exc)
            continue
        attempts = req.get("attempts") or {}
        for t in targets:
            due = attempt_due(attempts.get(t), now)
            if due == "exhausted":
                att = attempts.get(t) or {}
                problems.append(_problem(t, req.get("title") or "request",
                                         f"No healthy release after {MAX_ATTEMPTS} searches. Needs a decision.",
                                         "error"))
                if not att.get("escalated_at"):
                    await _mark(req, t, {**att, "escalated_at": _iso(now)}, now)
                    _ALERTS.append(notify.Alert(
                        "no_release", _request_title(req, t),
                        f"No healthy release found after {MAX_ATTEMPTS} searches."))
                continue
            if due == "wait" or not slot.take():
                continue
            grabbed = await _rescue_one(state, t, req, tracks)
            if not grabbed:
                await _mark(req, t, record_attempt(attempts.get(t), now), now)


def _request_title(req: dict[str, Any], target: str) -> str:
    """Title from our own request record (set from *arr metadata at request time)."""
    base = str(req.get("title") or "a request")
    t = ids.decode(target)
    if t.kind == "s":
        return f"{base} season {t.extra}"
    if t.kind == "e":
        return f"{base} (an episode)"
    return base


async def _mark(req: dict[str, Any], target: str, att: dict[str, Any], now: datetime) -> None:
    key = (req.get("type"), req.get("arr_id"), req.get("requested_at"))

    def match(r):
        return (r.get("type"), r.get("arr_id"), r.get("requested_at")) == key

    def fn(r):
        r.setdefault("attempts", {})[target] = att

    await requests_store.update(match, fn, now)


async def _rescue_one(state: dict[str, Any], target_id: str, req: dict[str, Any], tracks) -> bool:
    t = ids.decode(target_id)
    try:
        if t.kind == "m":
            movie = await stack.get_movie(t.int_key)
            rel = await stack.release_search("radarr", movie_id=t.int_key)
            runtime, n, pack = (movie or {}).get("runtime"), 1, False
            owner = movie
        elif t.kind == "s":
            series = await stack.get_series(t.int_key)
            season = int(t.extra)
            rel = await stack.release_search("sonarr", series_id=t.int_key, season=season)
            runtime = (series or {}).get("runtime")
            n = next(((s.get("statistics") or {}).get("totalEpisodeCount") or 1
                      for s in (series or {}).get("seasons") or [] if s.get("seasonNumber") == season), 1)
            pack = True
            owner = series
        else:
            ep = await stack.arr("sonarr", "GET", f"episode/{t.int_key}")
            series = await stack.get_series(ep["seriesId"]) if ep else None
            rel = await stack.release_search("sonarr", episode_id=t.int_key)
            runtime, n, pack = (series or {}).get("runtime"), 1, False
            owner = series
    except Exception as exc:
        log.warning("rescue search failed for %s: %s", req.get("title"), exc)
        return False
    ranked = health.rank_releases(
        rel, history_counts=tracks.get(t.app) or {}, current_resolution=None, current_seeders=0,
        current_dead=True, runtime_minutes=runtime, episode_count=n, prefer_full_season=pack,
        allow_foreign=health.foreign_ok(owner))
    if pack:
        ranked = [r for r in ranked if r["full_season"]] or []
    if not ranked:
        return False
    winner = ranked[0]["release"]
    try:
        await stack.grab(t.app, winner["guid"], int(winner["indexerId"]))
    except Exception as exc:
        log.warning("rescue grab failed for %s: %s", req.get("title"), exc)
        return False
    _log_action(state, req.get("title") or "request", "grabbed",
                f"never grabbed — {winner.get('title')} ({winner.get('seeders') or 0} seeders)")
    return True


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------

async def run(stop: asyncio.Event) -> None:
    await _publish(load_state(), {}, [])
    if not config.WATCH_ENABLED:
        log.info("watch is off (WATCH_ENABLED=false)")
        await stop.wait()
        return
    log.info("watch started (stall %s, grace %s, rescue after %s)", STALL, GRACE, UNGRABBED)
    try:
        await asyncio.wait_for(stop.wait(), timeout=15)
        return
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            await tick()
        except Exception:
            log.exception("watch tick crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
