"""Pure release-health rules for the download watcher.

The watcher force-grabs releases, which bypasses Sonarr/Radarr profiles, so
every filter that matters has to live here. No I/O, no network: everything
takes plain dicts/numbers and returns plain values.

Contents:
- the original pure helpers from ``watch.py`` (same names, same behaviour);
- ``reject_reason``: hard rejects (protocol, junk, executables/archives,
  foreign language, hardcoded subs, implausible size);
- ``track_record`` / ``score`` / ``rank_releases`` / ``pick_scored``: ranking
  that discounts indexers whose grabs tend to fail;
- ``swarm_dead``: "nobody is seeding this" detector for a qBittorrent torrent.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

GRACE = timedelta(minutes=30)
STALL = timedelta(minutes=40)
ALMOST_DONE = 80.0
MIN_SEEDERS = 3

QUALITY_LADDER = [2160, 1080, 720, 480]

# CAM / telesync / screener and friends. \b so "SCR" does not match "DESCRIBE".
JUNK_RE = re.compile(
    r"\b(CAM|CAMRIP|TS|TC|TELESYNC|TELECINE|SCR|SCREENER|R5|WP|WORKPRINT)\b",
    re.I,
)

# qBittorrent states that mean "not a stall, leave it".
DONE_STATES = {
    "uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP",
}
WAIT_STATES = {
    "queuedDL", "checkingDL", "checkingResumeData", "metaDL", "allocating", "moving",
}
PAUSE_STATES = {"pausedDL", "stoppedDL"}
ERROR_STATES = {"error", "missingFiles"}
IMPORTING_STATES = {"importPending", "importing", "imported"}


# --------------------------------------------------------------------------
# original pure helpers (copied from watch.py, behaviour unchanged)
# --------------------------------------------------------------------------

def is_junk(title: str) -> bool:
    return bool(JUNK_RE.search(title or ""))


def resolution_of(quality: Any) -> int | None:
    """Read a pixel-height from an *arr quality object or a name like 'HDTV-1080p'."""
    if isinstance(quality, dict):
        inner = quality.get("quality") if isinstance(quality.get("quality"), dict) else quality
        res = inner.get("resolution") if isinstance(inner, dict) else None
        if res:
            try:
                return int(res)
            except (TypeError, ValueError):
                pass
        name = (inner.get("name") if isinstance(inner, dict) else None) or quality.get("name") or ""
    else:
        name = str(quality or "")
    for r in QUALITY_LADDER:
        if str(r) in name:
            return r
    return None


def quality_allowed(current: int | None, candidate: int | None) -> bool:
    """Same or better always. One ladder step down is ok. Two steps is not."""
    if candidate is None:
        return False
    if current is None or candidate >= current:
        return True
    cur = _rung(current)
    can = _rung(candidate)
    if cur is None or can is None:
        return False
    return can - cur == 1


def _rung(resolution: int) -> int | None:
    """Index on QUALITY_LADDER, snapping to the nearest rung."""
    if resolution in QUALITY_LADDER:
        return QUALITY_LADDER.index(resolution)
    nearest = min(QUALITY_LADDER, key=lambda r: abs(r - resolution))
    return QUALITY_LADDER.index(nearest)


def cutoff_only(rejections: list[Any]) -> bool:
    """True if *arr's only complaint is the quality cutoff (so we may still grab)."""
    if not rejections:
        return True
    for raw in rejections:
        text = str(raw).lower()
        if "cutoff" in text:
            continue
        return False
    return True


def health_score(seeders: int, leechers: int) -> int:
    return int(seeders) * 10 + int(leechers)


def is_healthier(current_seeders: int, candidate_seeders: int, current_dead: bool) -> bool:
    if candidate_seeders < MIN_SEEDERS:
        return False
    if current_dead or current_seeders <= 0:
        return True
    return candidate_seeders >= current_seeders * 2


def season_numbers(seasons: list[Any]) -> list[int] | None:
    """Request-app seasons are objects with seasonNumber, or plain ints. None = all."""
    if not seasons:
        return None
    out: list[int] = []
    for s in seasons:
        if isinstance(s, int):
            out.append(s)
        elif isinstance(s, dict) and s.get("seasonNumber") is not None:
            out.append(int(s["seasonNumber"]))
    return out or None


def pick_release(
    releases: list[dict[str, Any]],
    *,
    current_resolution: int | None,
    current_seeders: int,
    current_dead: bool,
    current_title: str | None = None,
) -> dict[str, Any] | None:
    """Highest health score that passes junk / quality / seeder rules."""
    best: dict[str, Any] | None = None
    best_score = -1
    current_title_l = (current_title or "").lower()
    for rel in releases:
        if (rel.get("protocol") or "").lower() != "torrent":
            continue
        title = rel.get("title") or ""
        if current_title_l and title.lower() == current_title_l:
            continue
        if is_junk(title):
            continue
        seeders = int(rel.get("seeders") or 0)
        leechers = int(rel.get("leechers") or 0)
        if seeders <= 0:
            continue
        res = resolution_of(rel.get("quality"))
        if not quality_allowed(current_resolution, res):
            continue
        if rel.get("rejected") and not cutoff_only(rel.get("rejections") or []):
            continue
        if not is_healthier(current_seeders, seeders, current_dead):
            continue
        score = health_score(seeders, leechers)
        if score > best_score:
            best = rel
            best_score = score
    return best


def first_missing_episode(
    episodes: list[dict[str, Any]],
    *,
    seasons: list[int] | None,
    queued_ids: set[int],
    now: datetime,
) -> dict[str, Any] | None:
    wanted = set(seasons) if seasons else None
    found: list[dict[str, Any]] = []
    for ep in episodes:
        if not ep.get("monitored") or ep.get("hasFile"):
            continue
        eid = ep.get("id")
        if eid in queued_ids:
            continue
        season = ep.get("seasonNumber")
        if wanted is not None and season not in wanted:
            continue
        if season == 0 and (wanted is None or 0 not in wanted):
            continue
        air = ep.get("airDateUtc") or ep.get("airDate")
        if air:
            try:
                when = datetime.fromisoformat(str(air).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when > now:
                    continue
            except ValueError:
                pass
        found.append(ep)
    found.sort(key=lambda e: (e.get("seasonNumber") or 0, e.get("episodeNumber") or 0))
    return found[0] if found else None


def classify_download(
    *,
    qbit_state: str | None,
    tracked_status: str | None,
    tracked_state: str | None,
    dlspeed: int,
    percent: float,
    added_at: datetime | None,
    last_progress_at: datetime | None,
    now: datetime,
) -> str:
    """One of: importing, done, paused, waiting, grace, error, stalled,
    almost_done_stalled, progressing."""
    if (tracked_state or "") in IMPORTING_STATES:
        return "importing"
    state = qbit_state or ""
    if state in DONE_STATES:
        return "done"
    if state in PAUSE_STATES:
        return "paused"
    if state in ERROR_STATES or (tracked_status or "").lower() == "error":
        return "error"
    if state in WAIT_STATES:
        return "waiting"

    start = added_at or last_progress_at or now
    if now - start < GRACE:
        return "grace"

    last = last_progress_at or start
    no_speed = dlspeed <= 0 or state == "stalledDL"
    if no_speed and now - last >= STALL:
        if percent >= ALMOST_DONE:
            return "almost_done_stalled"
        return "stalled"
    return "progressing"


# --------------------------------------------------------------------------
# hard rejects
# --------------------------------------------------------------------------

# Whole tokens only: a token is bounded by start/end or a non-alphanumeric
# character (".", "-", "_", " ", brackets...). So "Rarity" is not "rar" and
# "Disclosure" is not "scr".
_DANGEROUS_RE = re.compile(
    r"(?<![A-Za-z0-9])(exe|scr|lnk|bat|cmd|msi|zip|rar|iso)(?![A-Za-z0-9])",
    re.I,
)

# Case-sensitive on purpose: scene tags are upper-case (or the listed mixed
# forms), while ordinary title words are not — "The.French.Dispatch",
# "The Spanish Prisoner", "The Good German" must not be rejected.
_FOREIGN_TOKENS = (
    "MULTi", "MULTI", "iTA", "ITA", "FRENCH", "VFF", "VOSTFR", "GERMAN",
    "SPANISH", "LATINO", "RUS", "HINDI", "DUBBED", "DUAL",
)
_FOREIGN_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(_FOREIGN_TOKENS) + r")(?![A-Za-z0-9])",
)

# Case-insensitive: "korsub"/"HC"/"HardSub" all appear in the wild, and none of
# these is an ordinary English word. Whole tokens, so "HDR" is not "HC".
_HARDSUB_RE = re.compile(
    r"(?<![A-Za-z0-9])(HC|HARDSUB|HARDSUBS|KORSUB|HCSUB)(?![A-Za-z0-9])",
    re.I,
)

# MB per minute of runtime, inclusive bounds, keyed by ladder rung.
SIZE_BOUNDS_MB_PER_MIN: dict[int, tuple[float, float]] = {
    2160: (15.0, 400.0),
    1080: (4.0, 150.0),
    720: (2.0, 60.0),
    480: (0.8, 30.0),
}


def _language_names(release: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for lang in release.get("languages") or []:
        if isinstance(lang, dict):
            name = lang.get("name")
        else:
            name = lang
        if name:
            out.append(str(name))
    return out


# "ITA ENG", "MULTi English": several audio tracks including English — fine.
_ENGLISH_RE = re.compile(r"(?<![A-Za-z0-9])(ENG|ENGLISH|English)(?![A-Za-z0-9])")


def _foreign_by_languages(release: dict[str, Any]) -> bool:
    """A parsed language list that is present, not just Unknown, and has no English."""
    names = [n for n in _language_names(release) if n.strip().lower() != "unknown"]
    if not names:
        return False
    return not any("english" in n.lower() for n in names)


def _size_bounds(resolution: int | None) -> tuple[float, float]:
    res = resolution if resolution else 1080
    rung = QUALITY_LADDER[_rung(res)]
    return SIZE_BOUNDS_MB_PER_MIN[rung]


def size_plausible(
    size_bytes: Any,
    *,
    resolution: int | None,
    runtime_minutes: int | None,
    episode_count: int = 1,
) -> bool:
    """True when size or runtime is unknown, or MB/min is inside the bounds."""
    try:
        size = float(size_bytes or 0)
    except (TypeError, ValueError):
        return True
    if size <= 0 or not runtime_minutes or runtime_minutes <= 0:
        return True
    per_episode = size / max(1, int(episode_count or 1))
    mb_per_min = per_episode / 1e6 / float(runtime_minutes)
    lo, hi = _size_bounds(resolution)
    return lo <= mb_per_min <= hi


def reject_reason(
    release: dict[str, Any],
    *,
    runtime_minutes: int | None,
    episode_count: int = 1,
    allow_foreign: bool = False,
) -> str | None:
    """First reason this release must never be force-grabbed, else None."""
    if (release.get("protocol") or "").lower() != "torrent":
        return "not a torrent"
    title = release.get("title") or ""
    if is_junk(title):
        return "cam/telesync/screener"
    if _DANGEROUS_RE.search(title):
        return "archive or executable"
    if not allow_foreign:
        tagged = bool(_FOREIGN_RE.search(title)) and not _ENGLISH_RE.search(title)
        if tagged or _foreign_by_languages(release):
            return "foreign language"
        if _HARDSUB_RE.search(title):
            return "hardcoded subtitles"
    if not size_plausible(
        release.get("size"),
        resolution=resolution_of(release.get("quality")),
        runtime_minutes=runtime_minutes,
        episode_count=episode_count,
    ):
        return "size implausible for runtime"
    return None


def foreign_ok(item: dict[str, Any] | None) -> bool:
    """True when a Sonarr series / Radarr movie was not originally in English.

    Then releases tagged ITA, MULTi etc. are the normal ones (Ember Coast, Kobe Nights,
    Northern Line) and must not be filtered out as foreign.
    """
    lang = ((item or {}).get("originalLanguage") or {}).get("name") or ""
    return bool(lang) and lang.strip().lower() not in ("english", "unknown")


# --------------------------------------------------------------------------
# indexer track record and ranking
# --------------------------------------------------------------------------

_PROWLARR_SUFFIX = " (prowlarr)"

# score = health * track ** TRACK_EXPONENT. Squared because a bad indexer hurts
# twice: its grabs fail more often AND its seeder counts are untrustworthy, so
# the advertised health number itself needs discounting. With exponent 1 a
# release from a poor indexer (6 ok / 19 failed) at 200 seeders still beats
# one from a good indexer (135/7) at 40 seeders; with 2 it does not.
TRACK_EXPONENT = 2


def normalise_indexer(name: str | None) -> str:
    """'Example (Prowlarr)' and 'example' compare equal."""
    text = (name or "").strip().casefold()
    if text.endswith(_PROWLARR_SUFFIX):
        text = text[: -len(_PROWLARR_SUFFIX)].rstrip()
    return text


def track_record(
    history_counts: dict[str, tuple[int, int]],
    indexer: str | None,
    *,
    prior: float = 0.8,
    prior_weight: float = 5.0,
    min_grabs: int = 5,
) -> float:
    """Smoothed import success rate for an indexer; ``prior`` when too little data."""
    key = normalise_indexer(indexer)
    if not key:
        return prior
    imported = failed = 0
    found = False
    for name, counts in (history_counts or {}).items():
        if normalise_indexer(name) != key:
            continue
        found = True
        imported += int(counts[0] or 0)
        failed += int(counts[1] or 0)
    if not found or imported + failed < min_grabs:
        return prior
    return (imported + prior * prior_weight) / (imported + failed + prior_weight)


def score(release: dict[str, Any], track: float) -> float:
    seeders = int(release.get("seeders") or 0)
    leechers = int(release.get("leechers") or 0)
    return float(health_score(seeders, leechers)) * (float(track) ** TRACK_EXPONENT)


def rank_releases(
    releases: list[dict[str, Any]],
    *,
    history_counts: dict[str, tuple[int, int]],
    current_resolution: int | None,
    current_seeders: int,
    current_dead: bool,
    current_title: str | None = None,
    runtime_minutes: int | None = None,
    episode_count: int = 1,
    prefer_full_season: bool = False,
    allow_foreign: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every grab-worthy release, best first, wrapped with its score."""
    current_title_l = (current_title or "").lower()
    ranked: list[dict[str, Any]] = []
    for rel in releases:
        title = rel.get("title") or ""
        if current_title_l and title.lower() == current_title_l:
            continue
        if reject_reason(
            rel,
            runtime_minutes=runtime_minutes,
            episode_count=episode_count,
            allow_foreign=allow_foreign,
        ):
            continue
        seeders = int(rel.get("seeders") or 0)
        if seeders <= 0:
            continue
        res = resolution_of(rel.get("quality"))
        if not quality_allowed(current_resolution, res):
            continue
        if rel.get("rejected") and not cutoff_only(rel.get("rejections") or []):
            continue
        if not is_healthier(current_seeders, seeders, current_dead):
            continue
        track = track_record(history_counts, rel.get("indexer"))
        size = rel.get("size")
        try:
            size_gb = round(float(size) / 1e9, 2) if size else None
        except (TypeError, ValueError):
            size_gb = None
        ranked.append({
            "release": rel,
            "score": score(rel, track),
            "track": track,
            "resolution": res,
            "size_gb": size_gb,
            "full_season": bool(rel.get("fullSeason")),
        })

    # Stable sort: equal keys keep search order, like pick_release's first-wins.
    if prefer_full_season:
        ranked.sort(key=lambda r: (not r["full_season"], -r["score"]))
    else:
        ranked.sort(key=lambda r: -r["score"])
    if limit is not None:
        ranked = ranked[: max(0, int(limit))]
    return ranked


def pick_scored(releases: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any] | None:
    """Top of ``rank_releases`` (same keyword arguments), or None."""
    ranked = rank_releases(releases, **kwargs)
    return ranked[0]["release"] if ranked else None


# --------------------------------------------------------------------------
# swarm death
# --------------------------------------------------------------------------

# States where "no seeds connected" says nothing about the swarm. metaDL is
# deliberately NOT here: stuck fetching metadata with nobody around is dead.
_NOT_JUDGED_STATES = (
    DONE_STATES | PAUSE_STATES | IMPORTING_STATES | ERROR_STATES
    | (WAIT_STATES - {"metaDL"})
)


def swarm_dead(
    torrent: dict[str, Any],
    *,
    now_ts: float,
    first_seen_ts: float,
    dead_since_ts: float | None,
    grace_s: int = 600,
    window_s: int = 600,
) -> tuple[bool, float | None]:
    """(is_dead, new_dead_since_ts) for a qBittorrent torrent dict.

    Dead-looking = availability known and < 1 (no complete copy reachable) and
    zero connected seeds. It must look dead continuously for ``window_s``; any
    sign of life returns (False, None), which resets the clock.
    """
    # Grace counts from whichever is later, so a watcher restart (fresh
    # first_seen) never makes an old torrent instantly judgeable — and vice versa.
    try:
        added_on = float(torrent.get("added_on") or 0)
    except (TypeError, ValueError):
        added_on = 0.0
    start = max(float(first_seen_ts or 0), added_on)
    if now_ts - start < grace_s:
        return False, None

    if (torrent.get("state") or "") in _NOT_JUDGED_STATES:
        return False, None
    try:
        if float(torrent.get("progress") or 0) >= 1.0:
            return False, None
    except (TypeError, ValueError):
        pass

    try:
        availability = float(torrent.get("availability"))
    except (TypeError, ValueError):
        availability = -1.0
    seeds = torrent.get("num_seeds")
    if availability < 0 or seeds is None:
        return False, None  # unknown is not evidence of death

    if availability < 1 and int(seeds) == 0:
        since = dead_since_ts if dead_since_ts is not None else now_ts
        return (now_ts - since >= window_s), since
    return False, None
