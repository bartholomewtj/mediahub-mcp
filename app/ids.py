"""Opaque, deterministic ids handed to the agent.

The agent never passes raw Sonarr/Radarr ids, hashes or guids into an action.
It passes one of these strings, which the server parses and then re-checks
against live state before acting. They are deterministic (no server memory),
so they survive a container restart.

    q:sonarr:<downloadId>     a download in the Sonarr queue (all its episodes)
    q:radarr:<downloadId>     a download in the Radarr queue
    m:radarr:<movieId>        a missing movie (ungrabbed request)
    s:sonarr:<seriesId>:<n>   a missing season
    e:sonarr:<episodeId>      a missing episode
    t:radarr:<movieId>        a whole movie (cleanup)
    t:sonarr:<seriesId>       a whole show (cleanup)
    w:sonarr:<seriesId>:<n,m> watched seasons of a show (cleanup)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KINDS = {"q", "m", "s", "e", "t", "w"}
_APPS = {"sonarr", "radarr"}
_SAFE = re.compile(r"^[A-Za-z0-9,]+$")


class BadId(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    kind: str
    app: str
    key: str
    extra: str | None = None

    def __str__(self) -> str:
        return encode(self)

    @property
    def int_key(self) -> int:
        return int(self.key)

    @property
    def seasons(self) -> list[int]:
        if not self.extra:
            return []
        return [int(x) for x in self.extra.split(",") if x]


def encode(t: Target) -> str:
    parts = [t.kind, t.app, t.key]
    if t.extra is not None:
        parts.append(t.extra)
    return ":".join(parts)


def make(kind: str, app: str, key: object, extra: object | None = None) -> str:
    return encode(Target(kind, app, str(key), None if extra is None else str(extra)))


def decode(raw: str) -> Target:
    if not isinstance(raw, str) or len(raw) > 120:
        raise BadId("not a target id")
    parts = raw.split(":")
    if len(parts) not in (3, 4):
        raise BadId("not a target id")
    kind, app, key = parts[0], parts[1], parts[2]
    extra = parts[3] if len(parts) == 4 else None
    if kind not in _KINDS or app not in _APPS:
        raise BadId("not a target id")
    if not _SAFE.match(key) or (extra is not None and not _SAFE.match(extra)):
        raise BadId("not a target id")
    if kind in ("m", "e", "t") and not key.isdigit():
        raise BadId("not a target id")
    if kind in ("s", "w") and (not key.isdigit() or not extra):
        raise BadId("not a target id")
    if kind == "s" and not extra.isdigit():
        raise BadId("not a target id")
    return Target(kind, app, key, extra)
