"""taste.md: the personal taste ledger.

Plain markdown, one row per title, fields separated by `` | ``::

    # plex-recs taste
    Last poll: 2026-10-11

    ## Rated
    - 2026-08-26 | tv | tmdb:910003 | The Long Harbour (2001) | loved | why: Great finale

    ## Unseen
    - 2026-08-26 | tv | tmdb:910004 | Night Shift Nine | asked

The MCP server is the only writer. Every write goes through `update()`, which
serialises load -> modify -> save under `LOCK` and saves atomically.

Rows that were parsed and not modified are re-emitted byte-for-byte (`Row.raw`),
so hand-written field orders and unknown fields survive a rewrite.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

SECTIONS = ("Rated", "Unseen", "Skipped", "Not mine")
LEGACY_SECTIONS = ("Requested",)

DEFAULT_HEADER = "# plex-recs taste"
SEP = " | "

VERDICTS = ("loved", "liked", "meh", "disliked")
# record() verdict -> (section, status word written in the verdict slot)
STATUS_VERDICTS = {
    "unseen": ("Unseen", "asked"),
    "skip": ("Skipped", "later"),
    "not_mine": ("Not mine", "not mine"),
}
STATUS_WORDS = tuple(word for _, word in STATUS_VERDICTS.values())
TYPES = ("tv", "movie")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TMDB_RE = re.compile(r"^tmdb:(\d+)$")
_YEAR_RE = re.compile(r"^(.*\S) \((\d{4})\)$")
_FINISHED_RE = re.compile(r"^finished (\d{4}-\d{2}-\d{2})$")
_WATCHED_RE = re.compile(r"^watched (\d{4}-\d{2}-\d{2})$")
_WHY_PREFIX = "why:"
_LAST_POLL = "Last poll:"

LOCK = asyncio.Lock()


class TasteFormatError(ValueError):
    """taste.md contains a line this module cannot represent.

    Raised instead of silently dropping the line, because the next save would
    otherwise delete it from the file.
    """


@dataclass
class Row:
    date: str
    type: str
    tmdb_id: int
    title: str
    year: int | None = None
    verdict: str | None = None
    finished: str | None = None
    watched: str | None = None
    why: str | None = None
    extra: list[str] = field(default_factory=list)
    # The original line, re-emitted verbatim while the row still matches it.
    raw: str | None = field(default=None, compare=False, repr=False)


@dataclass
class Ledger:
    header: str = DEFAULT_HEADER
    last_poll: str | None = None
    sections: dict[str, list[Row]] = field(default_factory=dict)


@dataclass
class RecordResult:
    status: str  # added | moved | updated | unchanged | needs_confirm
    section: str
    row: Row
    previous: tuple[str, Row] | None
    changed: bool = False  # True when the ledger was modified (save needed)


# --------------------------------------------------------------------------- parse


def _parse_row(line: str) -> Row:
    """Parse one ``- a | b | ...`` line (with the leading ``- ``)."""
    body = line[2:]
    parts = body.split(SEP)
    if len(parts) < 4:
        raise TasteFormatError(f"row needs at least 4 fields: {line!r}")
    date, type_, tmdb, title_field = parts[0], parts[1], parts[2], parts[3]
    if not _DATE_RE.match(date):
        raise TasteFormatError(f"bad date {date!r} in row: {line!r}")
    m = _TMDB_RE.match(tmdb)
    if not m:
        raise TasteFormatError(f"bad tmdb field {tmdb!r} in row: {line!r}")
    tmdb_id = int(m.group(1))

    ym = _YEAR_RE.match(title_field)
    if ym:
        title, year = ym.group(1), int(ym.group(2))
    else:
        title, year = title_field, None

    row = Row(date=date, type=type_, tmdb_id=tmdb_id, title=title, year=year, raw=line)
    rest = parts[4:]
    for i, f in enumerate(rest):
        if f.startswith(_WHY_PREFIX) and row.why is None:
            # why is free text and may itself contain " | ": it owns the rest of the line.
            row.why = SEP.join(rest[i:])[len(_WHY_PREFIX):].removeprefix(" ")
            break
        if row.verdict is None and (f in VERDICTS or f in STATUS_WORDS):
            row.verdict = f
            continue
        if row.finished is None and (fm := _FINISHED_RE.match(f)):
            row.finished = fm.group(1)
            continue
        if row.watched is None and (wm := _WATCHED_RE.match(f)):
            row.watched = wm.group(1)
            continue
        row.extra.append(f)
    return row


def parse(text: str) -> Ledger:
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    ledger = Ledger(header="", sections={})
    current: list[Row] | None = None
    for lineno, line in enumerate(text.split("\n"), start=1):
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            name = stripped[3:].strip()
            current = ledger.sections.setdefault(name, [])
            continue
        if current is None:
            if stripped.startswith("# ") and not ledger.header:
                ledger.header = stripped
                continue
            if stripped.startswith(_LAST_POLL) and ledger.last_poll is None:
                ledger.last_poll = stripped[len(_LAST_POLL):].strip() or None
                continue
            raise TasteFormatError(f"line {lineno}: unexpected text before first section: {line!r}")
        if line.startswith("- "):
            try:
                current.append(_parse_row(line.rstrip()))
            except TasteFormatError as exc:
                raise TasteFormatError(f"line {lineno}: {exc}") from None
            continue
        raise TasteFormatError(f"line {lineno}: unexpected text in section: {line!r}")
    if not ledger.header:
        ledger.header = DEFAULT_HEADER
    return ledger


# -------------------------------------------------------------------------- render


def _title_field(row: Row) -> str:
    return f"{row.title} ({row.year})" if row.year is not None else row.title


def render_row(row: Row) -> str:
    if row.raw is not None:
        try:
            if _parse_row(row.raw) == row:
                return row.raw
        except TasteFormatError:
            pass
    parts = [row.date, row.type, f"tmdb:{row.tmdb_id}", _title_field(row)]
    if row.verdict:
        parts.append(row.verdict)
    if row.finished:
        parts.append(f"finished {row.finished}")
    if row.watched:
        parts.append(f"watched {row.watched}")
    parts.extend(row.extra)
    # why goes last: it is free text and owns the rest of the line when parsed.
    if row.why:
        parts.append(f"why: {row.why}")
    return "- " + SEP.join(parts)


def render(ledger: Ledger) -> str:
    lines = [ledger.header or DEFAULT_HEADER]
    if ledger.last_poll:
        lines.append(f"{_LAST_POLL} {ledger.last_poll}")
    for name, rows in ledger.sections.items():
        lines.append("")
        lines.append(f"## {name}")
        lines.extend(render_row(r) for r in rows)
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------- file io


def empty_ledger() -> Ledger:
    return Ledger(header=DEFAULT_HEADER, last_poll=None, sections={s: [] for s in SECTIONS})


def load(path: Path) -> Ledger:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return empty_ledger()
    return parse(text.replace("\r\n", "\n"))


def ensure_sections(ledger: Ledger) -> None:
    """Add any missing SECTIONS, each placed after its nearest present predecessor."""
    if all(s in ledger.sections for s in SECTIONS):
        return
    names = list(ledger.sections)
    for i, s in enumerate(SECTIONS):
        if s in names:
            continue
        before = [p for p in SECTIONS[:i] if p in names]
        after = [p for p in SECTIONS[i + 1:] if p in names]
        if before:
            pos = names.index(before[-1]) + 1
        elif after:
            pos = names.index(after[0])
        else:
            pos = len(names)
        names.insert(pos, s)
    ledger.sections = {n: ledger.sections.get(n, []) for n in names}


def save(path: Path, ledger: Ledger) -> None:
    path = Path(path)
    ensure_sections(ledger)
    data = render(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ------------------------------------------------------------------------- queries


def _search_order(ledger: Ledger) -> list[str]:
    return [s for s in SECTIONS if s in ledger.sections] + [
        s for s in ledger.sections if s not in SECTIONS
    ]


def find(ledger: Ledger, tmdb_id: int) -> tuple[str, Row] | None:
    for name in _search_order(ledger):
        for row in ledger.sections[name]:
            if row.tmdb_id == tmdb_id:
                return name, row
    return None


def duplicates(ledger: Ledger) -> dict[int, list[str]]:
    seen: dict[int, list[str]] = {}
    for name, rows in ledger.sections.items():
        for row in rows:
            where = seen.setdefault(row.tmdb_id, [])
            if name not in where:
                where.append(name)
    return {tid: names for tid, names in seen.items() if len(names) > 1}


def drop_section(ledger: Ledger, name: str) -> list[Row]:
    return ledger.sections.pop(name, [])


def to_dict(row: Row) -> dict:
    out = {f.name: getattr(row, f.name) for f in fields(row) if f.name != "raw"}
    out["extra"] = list(row.extra)
    return out


# -------------------------------------------------------------------------- record


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())  # no newlines, collapse whitespace
    return text or None


def _clean_title(title: str) -> str:
    title = " ".join(title.split())
    # " | " is the field separator; keep it out of titles.
    return title.replace(" | ", " / ").replace("|", "/")


def _remove_everywhere(ledger: Ledger, tmdb_id: int) -> list[tuple[str, int]]:
    """Remove the id from all four SECTIONS. Returns (section, index) of removed rows."""
    removed = []
    for name in SECTIONS:
        rows = ledger.sections.get(name)
        if not rows:
            continue
        keep = []
        for idx, r in enumerate(rows):
            if r.tmdb_id == tmdb_id:
                removed.append((name, idx))
            else:
                keep.append(r)
        ledger.sections[name] = keep
    return removed


def record(
    ledger: Ledger,
    *,
    tmdb_id: int,
    type: str,
    title: str,
    year: int | None,
    verdict: str,
    why: str | None = None,
    finished: str | None = None,
    watched: str | None = None,
    today: str,
    force: bool = False,
) -> RecordResult:
    if type not in TYPES:
        raise ValueError(f"type must be one of {TYPES}, got {type!r}")
    if verdict in VERDICTS:
        target, word = "Rated", verdict
    elif verdict in STATUS_VERDICTS:
        target, word = STATUS_VERDICTS[verdict]
    else:
        raise ValueError(f"unknown verdict {verdict!r}")
    for label, value in (("today", today), ("finished", finished), ("watched", watched)):
        if value is not None and not _DATE_RE.match(value):
            raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    tmdb_id = int(tmdb_id)
    why = _clean(why)
    title = _clean_title(title)

    # previous = first hit among the four SECTIONS only (Rated first).
    previous: tuple[str, Row] | None = None
    for name in SECTIONS:
        hit = next((r for r in ledger.sections.get(name, []) if r.tmdb_id == tmdb_id), None)
        if hit is not None:
            previous = (name, hit)
            break
    prev_section, prev_row = previous if previous else (None, None)

    proposed = Row(
        date=prev_row.date if prev_row else today,
        type=type,
        tmdb_id=tmdb_id,
        title=title,
        year=year,
        verdict=word,
        finished=finished if finished is not None else (prev_row.finished if prev_row else None),
        watched=watched if watched is not None else (prev_row.watched if prev_row else None),
        why=why if why is not None else (prev_row.why if prev_row else None),
        extra=list(prev_row.extra) if prev_row else [],
    )

    # Rules 2-3: a Rated verdict is never overwritten without confirmation.
    if prev_section == "Rated" and not force:
        if target != "Rated" or prev_row.verdict != word:
            return RecordResult("needs_confirm", target, proposed, previous, changed=False)

    # Same section, same verdict/status: merge details only.
    if prev_section == target and prev_row.verdict == word:
        before = (prev_row.why, prev_row.finished, prev_row.watched)
        if why is not None:
            prev_row.why = why
        if finished is not None:
            prev_row.finished = finished
        if watched is not None:
            prev_row.watched = watched
        changed = (prev_row.why, prev_row.finished, prev_row.watched) != before
        # Clean up any stray copies in the other SECTIONS (or same section).
        for name in SECTIONS:
            rows = ledger.sections.get(name, [])
            kept = [r for r in rows if r is prev_row or r.tmdb_id != tmdb_id]
            if len(kept) != len(rows):
                ledger.sections[name] = kept
                changed = True
        if changed:
            ledger.last_poll = today
        return RecordResult("unchanged", target, prev_row, previous, changed=changed)

    removed = _remove_everywhere(ledger, tmdb_id)
    ensure_sections(ledger)
    rows = ledger.sections[target]
    same_section_idx = next((i for s, i in removed if s == target), None)
    if same_section_idx is not None:
        rows.insert(min(same_section_idx, len(rows)), proposed)
    else:
        rows.append(proposed)
    ledger.last_poll = today
    if previous is None:
        status = "added"
    elif prev_section == target:
        status = "updated"  # forced verdict change within Rated
    else:
        status = "moved"
    return RecordResult(status, target, proposed, previous, changed=True)


# -------------------------------------------------------------------------- update


async def update(path: Path, fn: Callable[[Ledger], tuple[Any, bool]]) -> Any:
    """Serialised read-modify-write. ``fn(ledger)`` returns ``(result, changed)``.

    The ledger is saved only when ``changed`` is truthy; ``result`` is returned.
    """
    async with LOCK:
        ledger = await asyncio.to_thread(load, path)
        result, changed = fn(ledger)
        if changed:
            await asyncio.to_thread(save, path, ledger)
        return result
