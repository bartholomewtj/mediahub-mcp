"""First-run setup and config as code for the bundled stack (MEDIAHUB_STACK=full).

Runs in the `setup` compose service (profile "setup", never started by `up`; also present in
core mode, where `secrets` only generates MCP_TOKEN and the helper commands below work as usual):

    mh run --rm setup secrets   # app keys -> secrets.env; MCP_TOKEN and the qBittorrent login if empty
    mh run --rm setup wire      # connect the apps to each other; idempotent
    mh run --rm setup check     # what `wire` would change; exit 1 on drift

and the installer's helpers (app/setup_tools.py): `allowed-tools`, `bot-prompt --repo-dir P
--shell powershell|bash`, `config-diff`, `remap-roots --app sonarr|radarr --from OLD --to NEW
[--apply]`, `version`, `mcp-ping` (checks /mcp with no client; prints OK/FAIL only).

`secrets` reads the apps' own config files (config volumes mounted under /cfg) and
writes <MEDIAHUB_HOME>/secrets.env (mounted at /mh-home). It prints key names only.
qBittorrent must be stopped while `secrets` sets its password (`mh stop qbittorrent`).

`wire` only ever adds what is missing: a root folder, download client, Prowlarr app or
proxy, Plex library that already exists in any form is left alone and reported OK. It
never adds an indexer. It also runs the config-as-code steps (Bazarr, no *arr ntfy, recycle bin folder and days, Prowlarr indexer tags). The qBittorrent
policy (Subfolder layout, speed caps, seeding limits from QBIT_* in stack.env) is applied the
same way: seeding limits that are already enabled keep their values.

Nothing secret is ever printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets as pysecrets
import socket
import sys
import time
import weakref
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import dotenv_values

from app import config

# ================================================================ config as code

# ---------------------------------------------------------------- Bazarr

PROVIDERS = ["gestdown", "tvsubtitles", "yifysubtitles", "embeddedsubtitles"]
ENGLISH_PROFILE = {
    "profileId": 1, "name": "English", "cutoff": None,
    "items": [{"id": 1, "language": "en", "audio_exclude": "False",
               "audio_only_include": "False", "hi": "False", "forced": "False"}],
    "mustContain": [], "mustNotContain": [], "originalFormat": None, "tag": None,
}
ARR_HOST = "127.0.0.1"  # every container shares the `net` namespace


def bazarr_form() -> list[tuple[str, str]]:
    fields = [
        ("languages-enabled", "en"),
        ("languages-profiles", json.dumps([ENGLISH_PROFILE])),
        ("settings-general-use_sonarr", "true"),
        ("settings-general-use_radarr", "true"),
        ("settings-sonarr-ip", ARR_HOST), ("settings-sonarr-port", "8989"),
        ("settings-sonarr-apikey", config.SONARR_API_KEY), ("settings-sonarr-ssl", "false"),
        ("settings-radarr-ip", ARR_HOST), ("settings-radarr-port", "7878"),
        ("settings-radarr-apikey", config.RADARR_API_KEY), ("settings-radarr-ssl", "false"),
        ("settings-general-path_mappings", "/media/Shows,/media/Shows"),
        ("settings-general-path_mappings_movie", "/media/Movies,/media/Movies"),
        ("settings-general-single_language", "true"),
        ("settings-general-subfolder", "current"),
        ("settings-general-serie_default_enabled", "true"),
        ("settings-general-serie_default_profile", "1"),
        ("settings-general-movie_default_enabled", "true"),
        ("settings-general-movie_default_profile", "1"),
        ("settings-general-default_und_audio_lang", "en"),
        ("settings-general-embedded_subs_show_desired", "false"),
    ]
    fields += [("settings-general-enabled_providers", p) for p in PROVIDERS]
    return fields


def bazarr(c: httpx.Client, check: bool) -> list[str]:
    h = {"X-API-KEY": config.BAZARR_API_KEY}
    s = c.get(f"{config.BAZARR_URL}/api/system/settings", headers=h).json()
    drift = []
    for app in ("sonarr", "radarr"):
        if (s.get(app) or {}).get("ip") != ARR_HOST:
            drift.append(f"bazarr {app} host is {(s.get(app) or {}).get('ip')!r}, want {ARR_HOST}")
    enabled = set((s.get("general") or {}).get("enabled_providers") or [])
    if not set(PROVIDERS) <= enabled:
        drift.append(f"bazarr providers missing {sorted(set(PROVIDERS) - enabled)}")
    if drift and not check:
        r = c.post(f"{config.BAZARR_URL}/api/system/settings",
                   headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
                   content=urlencode(bazarr_form(), doseq=True).encode())
        if r.status_code not in (200, 204):
            raise RuntimeError(f"bazarr settings save HTTP {r.status_code}")
    return drift


# ---------------------------------------------------------------- *arr notifications

# Alerts go out through the NOTIFIER channel from the watch loop (fixed wording, no release
# names). The *arr apps' own ntfy connections are removed.


def _arr(app: str) -> tuple[str, dict[str, str]]:
    if app == "sonarr":
        return config.SONARR_URL, {"X-Api-Key": config.SONARR_API_KEY}
    return config.RADARR_URL, {"X-Api-Key": config.RADARR_API_KEY}


def arr_no_ntfy(c: httpx.Client, app: str, check: bool) -> list[str]:
    base, h = _arr(app)
    drift = []
    for n in c.get(f"{base}/api/v3/notification", headers=h).json():
        if n.get("implementation") == "Ntfy":
            drift.append(f"{app} has an ntfy connection ({n.get('name')})")
            if not check:
                c.delete(f"{base}/api/v3/notification/{n['id']}", headers=h).raise_for_status()
    return drift


# ---------------------------------------------------------------- recycle bin

# Deletes go to /media/.recyclebin. A week keeps an undo window for deletes made
# without a confirm form (the media bot's bounded mode). Sonarr and Radarr ship with no
# recycle bin folder (deletes are permanent and undo_delete refuses), so an empty one is
# set here; a folder the owner already chose is left alone.
RECYCLE_DAYS = 7
RECYCLE_BIN = "/media/.recyclebin"
MEDIA_MOUNT = Path("/media")  # where the setup container mounts the media folder


def _media_mounted() -> bool:
    """True inside the setup container. On a host run (outside the container) /media is not the media
    folder, and on Windows it would resolve to a folder on the system drive."""
    return os.name == "posix" and MEDIA_MOUNT.is_dir()


def arr_recycle(c: httpx.Client, app: str, check: bool) -> list[str]:
    base, h = _arr(app)
    cfg = c.get(f"{base}/api/v3/config/mediamanagement", headers=h).json()
    drift = []
    change: dict = {}
    if not (cfg.get("recycleBin") or "").strip():
        drift.append(f"{app} has no recycle bin folder (deletes are permanent), want {RECYCLE_BIN}")
        change["recycleBin"] = RECYCLE_BIN
    if cfg.get("recycleBinCleanupDays") != RECYCLE_DAYS:
        drift.append(f"{app} recycle bin keeps {cfg.get('recycleBinCleanupDays')} day(s), want {RECYCLE_DAYS}")
        change["recycleBinCleanupDays"] = RECYCLE_DAYS
    if change and not check:
        if "recycleBin" in change and _media_mounted():  # only where /media is mounted (not a host run)
            try:
                _mkdir(RECYCLE_BIN)  # the app also creates it on the first delete
            except OSError:
                pass
        c.put(f"{base}/api/v3/config/mediamanagement", headers=h, json={**cfg, **change}).raise_for_status()
    return drift


# ---------------------------------------------------------------- Prowlarr indexer tags

# Which indexers to remove and which need FlareSolverr are the owner's choice, set in
# stack.env (PROWLARR_DROP_INDEXERS, PROWLARR_FLARE_INDEXERS). The package names none.
# With PROWLARR_FLARE_INDEXERS empty the 'flare' tags are not managed at all.


def prowlarr(c: httpx.Client, check: bool) -> list[str]:
    base, h = f"{config.PROWLARR_URL}/api/v1", {"X-Api-Key": config.PROWLARR_API_KEY}
    dropped, needs_flare = set(config.PROWLARR_DROP_INDEXERS), set(config.PROWLARR_FLARE_INDEXERS)
    tags = {t["label"]: t["id"] for t in c.get(f"{base}/tag", headers=h).json()}
    flare = tags.get("flare")
    drift = []
    for i in c.get(f"{base}/indexer", headers=h).json():
        if i["name"] in dropped:
            drift.append(f"prowlarr indexer {i['name']} should be removed")
            if not check:
                c.delete(f"{base}/indexer/{i['id']}", headers=h).raise_for_status()
            continue
        if not needs_flare:
            continue
        has = flare in (i.get("tags") or [])
        want = i["name"] in needs_flare
        if flare is not None and has != want and i.get("enable"):
            drift.append(f"prowlarr {i['name']} flare tag {'on' if has else 'off'}, want {'on' if want else 'off'}")
            if not check:
                i["tags"] = [t for t in i.get("tags") or [] if t != flare] + ([flare] if want else [])
                c.put(f"{base}/indexer/{i['id']}", headers=h, json=i).raise_for_status()
    return drift


# ================================================================ wiring (fresh installs)

TV_ROOT = "/media/TV"
MOVIES_ROOT = "/media/Movies"
DOWNLOADS = "/media/Downloads"
QBIT_PORT = 8090
QBIT_CATEGORY = {"sonarr": ("tvCategory", "tv-sonarr"), "radarr": ("movieCategory", "radarr")}
ARR_PORT = {"sonarr": 8989, "radarr": 7878}
FLARESOLVERR_URL = "http://localhost:8191/"


class Note(Exception):
    """Not drift and not a failure: something only the user can do (printed as NOTE)."""


def _fill(schema: dict, values: dict) -> dict:
    """A copy of an *arr schema entry with the named field values set."""
    body = json.loads(json.dumps(schema))
    for f in body.get("fields") or []:
        if f.get("name") in values:
            f["value"] = values[f["name"]]
    return body


def _schema(c: httpx.Client, url: str, h: dict, implementation: str) -> dict:
    r = c.get(url, headers=h)
    r.raise_for_status()
    for s in r.json():
        if s.get("implementation") == implementation:
            return s
    raise RuntimeError(f"no {implementation} in {url.rsplit('/api/', 1)[-1]}")


def _post(c: httpx.Client, url: str, h: dict, body: dict) -> None:
    r = c.post(url, headers=h, json=body)
    if r.status_code >= 400:
        # *arr validation errors name the field and the reason; they hold no secrets.
        try:
            msgs = "; ".join(e.get("errorMessage", "") for e in r.json())
        except Exception:
            msgs = ""
        raise RuntimeError(f"HTTP {r.status_code} {msgs}".strip()[:300])


def _mkdir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def arr_root_folder(c: httpx.Client, app: str, check: bool) -> list[str]:
    """A root folder if the app has none. An existing one, whatever its path, is kept."""
    base, h = _arr(app)
    folders = c.get(f"{base}/api/v3/rootfolder", headers=h).json()
    if folders:
        return []
    want = TV_ROOT if app == "sonarr" else MOVIES_ROOT
    if not check:
        _mkdir(want)
        _post(c, f"{base}/api/v3/rootfolder", h, {"path": want})
    return [f"{app} has no root folder, want {want}"]


def qbit_host() -> str:
    """How Sonarr/Radarr reach qBittorrent: through gluetun with the VPN, else the shared namespace."""
    return "gluetun" if (os.getenv("MEDIAHUB_VPN") or "").strip().lower() == "gluetun" else "localhost"


def arr_download_client(c: httpx.Client, app: str, check: bool) -> list[str]:
    """A qBittorrent download client if the app has none. An existing one is never changed."""
    base, h = _arr(app)
    clients = c.get(f"{base}/api/v3/downloadclient", headers=h).json()
    if any(d.get("implementation") == "QBittorrent" for d in clients):
        return []
    drift = [f"{app} has no qBittorrent download client"]
    if not check:
        if not config.QBIT_PASS:
            _qbit_login(c)  # empty credentials are fine only if qBittorrent skips auth for localhost
        field, category = QBIT_CATEGORY[app]
        body = _fill(_schema(c, f"{base}/api/v3/downloadclient/schema", h, "QBittorrent"), {
            "host": qbit_host(), "port": QBIT_PORT, "useSsl": False, "urlBase": "",
            "username": config.QBIT_USER, "password": config.QBIT_PASS,
            field: category, "contentLayout": 2,  # 2 = Subfolder (checks.py expects it)
        })
        body.update(name="qBittorrent", enable=True, priority=1)
        _post(c, f"{base}/api/v3/downloadclient", h, body)
    return drift


_QBIT_LOGIN_OK: "weakref.WeakKeyDictionary[httpx.Client, bool]" = weakref.WeakKeyDictionary()


def _qbit_login(c: httpx.Client) -> None:
    """Log in once per client. After a refused login later qBittorrent steps are skipped (a NOTE),
    because qBittorrent bans an address after repeated failed logins."""
    if c in _QBIT_LOGIN_OK:
        if _QBIT_LOGIN_OK[c]:
            return
        raise Note("skipped: the qBittorrent login failed above")
    if not config.QBIT_PASS:
        # No password: qBittorrent may skip auth for localhost (WebUI\LocalHostAuth=false), which is
        # how MediaHub and the *arrs reach it over the shared network namespace. Same as
        # services.qbit_snapshot: try the API without a login.
        r = c.get(f"{config.QBIT_URL}/api/v2/app/version")
        _QBIT_LOGIN_OK[c] = r.status_code == 200
        if r.status_code in (401, 403):
            raise RuntimeError("qBittorrent needs a login: QBIT_PASS is empty — run `setup secrets` "
                               "(fresh install) or set QBIT_USER/QBIT_PASS")
        if r.status_code != 200:
            raise RuntimeError(f"qBittorrent answered HTTP {r.status_code} without a login")
        return
    r = c.post(f"{config.QBIT_URL}/api/v2/auth/login",
               data={"username": config.QBIT_USER, "password": config.QBIT_PASS},
               headers={"Referer": config.QBIT_URL})
    # Success: 204 (qBittorrent 5.2+) or 200 "Ok." (older). Failure: 401/403, or 200 "Fails.".
    ok = r.status_code == 204 or (r.status_code == 200 and r.text.strip() == "Ok.")
    _QBIT_LOGIN_OK[c] = ok
    if not ok:
        raise RuntimeError(f"qBittorrent login refused (HTTP {r.status_code}): QBIT_USER/QBIT_PASS do not match")


def qbit_save_path(c: httpx.Client, check: bool) -> list[str]:
    """Downloads go under /media (the default /downloads is not on the media mount)."""
    _qbit_login(c)
    prefs = c.get(f"{config.QBIT_URL}/api/v2/app/preferences").json()
    current = str(prefs.get("save_path") or "")
    if current.startswith("/media/"):
        return []
    if not check:
        _mkdir(DOWNLOADS)
        c.post(f"{config.QBIT_URL}/api/v2/app/setPreferences",
               data={"json": json.dumps({"save_path": DOWNLOADS})},
               headers={"Referer": config.QBIT_URL}).raise_for_status()
    return [f"qbittorrent save path is {current!r}, want {DOWNLOADS}"]


def qbit_policy(c: httpx.Client, check: bool) -> list[str]:
    """Content layout, speed caps and seeding limits from stack.env (QBIT_*). Seeding limits that
    are already enabled keep their values; only a disabled one is switched on with the config value."""
    _qbit_login(c)
    prefs = c.get(f"{config.QBIT_URL}/api/v2/app/preferences").json()
    drift: list[str] = []
    want: dict = {}
    if prefs.get("torrent_content_layout") != "Subfolder":
        drift.append(f"qbittorrent content layout is {prefs.get('torrent_content_layout')!r}, want 'Subfolder'")
        want["torrent_content_layout"] = "Subfolder"
    for key, kib, label in (("dl_limit", config.QBIT_DL_LIMIT_KIB, "download"),
                            ("up_limit", config.QBIT_UP_LIMIT_KIB, "upload")):
        if prefs.get(key) != kib * 1024:
            drift.append(f"qbittorrent {label} limit is {(prefs.get(key) or 0) // 1024} KiB/s, want {kib}")
            want[key] = kib * 1024
    if not prefs.get("max_ratio_enabled"):
        drift.append(f"qbittorrent seeding ratio limit is off, want {config.QBIT_MAX_RATIO}")
        want.update(max_ratio_enabled=True, max_ratio=config.QBIT_MAX_RATIO)
    if not prefs.get("max_seeding_time_enabled"):
        drift.append(f"qbittorrent seeding time limit is off, want {config.QBIT_MAX_SEEDING_MINUTES} min")
        want.update(max_seeding_time_enabled=True, max_seeding_time=config.QBIT_MAX_SEEDING_MINUTES)
    if want and not check:
        c.post(f"{config.QBIT_URL}/api/v2/app/setPreferences",
               data={"json": json.dumps(want)},
               headers={"Referer": config.QBIT_URL}).raise_for_status()
    return drift


def _prowlarr_h() -> tuple[str, dict[str, str]]:
    return f"{config.PROWLARR_URL}/api/v1", {"X-Api-Key": config.PROWLARR_API_KEY}


def prowlarr_app(c: httpx.Client, app: str, check: bool) -> list[str]:
    """Prowlarr pushes its indexers to Sonarr/Radarr (full sync). An existing app entry is kept."""
    base, h = _prowlarr_h()
    impl = app.capitalize()
    if any(a.get("implementation") == impl for a in c.get(f"{base}/applications", headers=h).json()):
        return []
    if not check:
        key = config.SONARR_API_KEY if app == "sonarr" else config.RADARR_API_KEY
        if not key:
            raise RuntimeError(f"{app.upper()}_API_KEY is empty in secrets.env: run `setup secrets` first")
        body = _fill(_schema(c, f"{base}/applications/schema", h, impl), {
            "prowlarrUrl": "http://localhost:9696",
            "baseUrl": f"http://localhost:{ARR_PORT[app]}",
            "apiKey": key,
        })
        body.update(name=impl, syncLevel="fullSync")
        _post(c, f"{base}/applications", h, body)
    return [f"prowlarr has no {impl} application"]


def prowlarr_flaresolverr(c: httpx.Client, check: bool) -> list[str]:
    """A FlareSolverr proxy for indexers tagged 'flare'. Any existing FlareSolverr proxy is kept."""
    base, h = _prowlarr_h()
    if any(p.get("implementation") == "FlareSolverr" for p in c.get(f"{base}/indexerproxy", headers=h).json()):
        return []
    if not check:
        tags = {t["label"]: t["id"] for t in c.get(f"{base}/tag", headers=h).json()}
        if "flare" not in tags:
            r = c.post(f"{base}/tag", headers=h, json={"label": "flare"})
            r.raise_for_status()
            tags["flare"] = r.json()["id"]
        body = _fill(_schema(c, f"{base}/indexerproxy/schema", h, "FlareSolverr"),
                     {"host": FLARESOLVERR_URL, "requestTimeout": 60})
        body.update(name="FlareSolverr", tags=[tags["flare"]])
        _post(c, f"{base}/indexerproxy", h, body)
    return ["prowlarr has no FlareSolverr proxy (tag 'flare')"]


PLEX_LIBRARIES = [
    # (Plex type, name, agent, scanner, folder)
    ("movie", "Movies", "tv.plex.agents.movie", "Plex Movie", MOVIES_ROOT),
    ("show", "TV Shows", "tv.plex.agents.series", "Plex TV Series", TV_ROOT),
]


def plex_libraries(c: httpx.Client, check: bool) -> list[str]:
    """A Movies and a TV library if Plex has none of that type. Needs PLEX_TOKEN (a claimed server)."""
    if not config.PLEX_TOKEN:
        raise Note("Plex not claimed: open Plex's web page on port 32400 of this server (from a browser on "
                   "the same network), or set PLEX_CLAIM in stack.env, then run `setup secrets` and "
                   "`setup wire` again")
    h = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
    r = c.get(f"{config.PLEX_URL}/library/sections", headers=h)
    r.raise_for_status()
    have = {d.get("type") for d in (r.json().get("MediaContainer") or {}).get("Directory") or []}
    drift = []
    for kind, name, agent, scanner, folder in PLEX_LIBRARIES:
        if kind in have:
            continue
        drift.append(f"plex has no {kind} library, want {name!r} at {folder}")
        if not check:
            _mkdir(folder)
            q = urlencode({"name": name, "type": kind, "agent": agent, "scanner": scanner,
                           "language": "en-US", "location": folder})
            c.post(f"{config.PLEX_URL}/library/sections?{q}", headers=h).raise_for_status()
    return drift


# ---------------------------------------------------------------- running the steps

Step = tuple[str, Callable[[httpx.Client, bool], list[str]]]


def config_steps(c: httpx.Client) -> list[tuple[str, Callable[[bool], list[str]]]]:
    """The config-as-code steps (Bazarr, no ntfy, recycle bin, Prowlarr tags)."""
    return [("bazarr", lambda ck: bazarr(c, ck)),
            ("sonarr", lambda ck: arr_no_ntfy(c, "sonarr", ck)),
            ("radarr", lambda ck: arr_no_ntfy(c, "radarr", ck)),
            ("sonarr recycle", lambda ck: arr_recycle(c, "sonarr", ck)),
            ("radarr recycle", lambda ck: arr_recycle(c, "radarr", ck)),
            ("prowlarr", lambda ck: prowlarr(c, ck))]


def wire_steps(c: httpx.Client) -> list[tuple[str, Callable[[bool], list[str]]]]:
    """Fresh-install wiring first (later steps need it), then the config-as-code steps."""
    return [("sonarr root folder", lambda ck: arr_root_folder(c, "sonarr", ck)),
            ("radarr root folder", lambda ck: arr_root_folder(c, "radarr", ck)),
            ("qbittorrent save path", lambda ck: qbit_save_path(c, ck)),
            ("qbit policy", lambda ck: qbit_policy(c, ck)),
            ("sonarr download client", lambda ck: arr_download_client(c, "sonarr", ck)),
            ("radarr download client", lambda ck: arr_download_client(c, "radarr", ck)),
            ("prowlarr sonarr app", lambda ck: prowlarr_app(c, "sonarr", ck)),
            ("prowlarr radarr app", lambda ck: prowlarr_app(c, "radarr", ck)),
            ("prowlarr flaresolverr", lambda ck: prowlarr_flaresolverr(c, ck)),
            *config_steps(c),
            ("plex libraries", lambda ck: plex_libraries(c, ck))]


def run_steps(steps, check: bool, out=print) -> tuple[list[str], list[str]]:
    """Run each step, print DRIFT/FIXED/OK/NOTE/FAIL lines. Returns (drift, failed step names)."""
    drift_all, failed = [], []
    for name, fn in steps:
        try:
            drift = fn(check)
        except Note as note:
            out(f"NOTE {name}: {note}")
            continue
        except Exception as exc:  # report and carry on with the rest
            out(f"FAIL {name}: {exc}")
            failed.append(name)
            continue
        for d in drift:
            out(("DRIFT " if check else "FIXED ") + d)
        if not drift:
            out(f"OK   {name}")
        drift_all += drift
    return drift_all, failed


def wire(check: bool, client: httpx.Client | None = None) -> int:
    c = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        drift, failed = run_steps(wire_steps(c), check)
    finally:
        if client is None:
            c.close()
    if failed:
        return 1
    return 1 if (check and drift) else 0


# ================================================================ secrets

CFG = Path(os.getenv("MEDIAHUB_SETUP_CONFIGS") or "/cfg")  # config volumes, mounted by compose
PLEX_PREFS = "plex/Library/Application Support/Plex Media Server/Preferences.xml"
QBIT_CONF = "qbittorrent/qBittorrent/qBittorrent.conf"
QBIT_ADMIN = "admin"
PLEX_UNCLAIMED_NOTE = "NOTE PLEX_TOKEN: Plex is not claimed yet (claim it, then run `setup secrets` again)"

_QBIT_HASH_LINE = re.compile(r"(?m)^\s*WebUI\\Password_PBKDF2\s*=\s*\S")
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def arr_api_key(xml_text: str) -> str | None:
    """<ApiKey> from a Sonarr/Radarr/Prowlarr config.xml."""
    try:
        node = ET.fromstring(xml_text).find("ApiKey")
    except ET.ParseError:
        return None
    key = (node.text or "").strip() if node is not None else ""
    return key or None


def bazarr_api_key(yaml_text: str) -> str | None:
    """auth.apikey from Bazarr's config.yaml (no YAML library in the image; the block is simple)."""
    m = re.search(r"(?ms)^auth:[ \t]*\n((?:^[ \t]+.*(?:\n|$))*)", yaml_text)
    m2 = re.search(r"(?m)^[ \t]+apikey:[ \t]*(\S+)", m.group(1)) if m else None
    if m2 and m2.group(1).strip("'\"").lower() not in ("", "null", "none"):
        return m2.group(1).strip("'\"")
    return None


def plex_token(xml_text: str) -> str | None:
    """PlexOnlineToken from Plex's Preferences.xml; only a claimed server has one."""
    try:
        return (ET.fromstring(xml_text).get("PlexOnlineToken") or "").strip() or None
    except ET.ParseError:
        return None


def env_values(raw: bytes) -> dict[str, str]:
    """What compose/dotenv would read from a secrets.env (quotes and comments handled)."""
    text = raw.decode("utf-8-sig", errors="replace")
    return {k: (v or "") for k, v in dotenv_values(stream=io.StringIO(text)).items()}


def env_with(raw: bytes, updates: dict[str, str]) -> bytes:
    """secrets.env with `updates` applied. Every other byte (comments, order, line endings,
    BOM, other values) is kept. A key that is present gets its value replaced in place (every
    occurrence); a missing key is appended. Values must be plain tokens (no quoting needed)."""
    for k, v in updates.items():
        if not re.fullmatch(r"[A-Za-z0-9_.~+/=:@-]*", v):
            raise ValueError(f"{k}: value needs quoting")  # names only, never the value
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    text = raw[len(bom):].decode("utf-8", errors="surrogateescape")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    seen = set()
    for i, line in enumerate(lines):
        m = _KEY_LINE.match(line)
        if not m or m.group(1) not in updates:
            continue
        key = m.group(1)
        ending = line[len(line.rstrip("\r\n")):]
        lines[i] = line[: m.end()] + updates[key] + ending
        seen.add(key)
    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += eol
        lines += [f"{k}={updates[k]}{eol}" for k in missing]
    return bom + "".join(lines).encode("utf-8", errors="surrogateescape")


def write_atomic(path: Path, data: bytes) -> None:
    """Write next to the target and rename over it, keeping the file mode."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass  # some bind mounts (Docker Desktop) ignore modes
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def qbit_pbkdf2(password: str, salt: bytes | None = None) -> str:
    """qBittorrent's WebUI password hash: base64(salt):base64(PBKDF2-HMAC-SHA512, 100000
    iterations, 16-byte random salt, 64-byte key). Same as Utils::Password::PBKDF2::generate
    in qBittorrent's src/base/utils/password.cpp."""
    salt = os.urandom(16) if salt is None else salt
    key = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100_000, dklen=64)
    return base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode()


def qbit_conf_with_login(text: str, user: str, pbkdf2: str) -> str:
    """qBittorrent.conf with WebUI\\Username and WebUI\\Password_PBKDF2 set in [Preferences].
    Every other line is kept."""
    want = {"WebUI\\Username": user, "WebUI\\Password_PBKDF2": f'"@ByteArray({pbkdf2})"'}
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += eol
    start = next((i for i, l in enumerate(lines) if l.strip() == "[Preferences]"), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append(eol)
        return "".join(lines + ["[Preferences]" + eol] + [f"{k}={v}{eol}" for k, v in want.items()])
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    done = set()
    for i in range(start + 1, end):
        key = lines[i].split("=", 1)[0].strip()
        if key in want:
            lines[i] = f"{key}={want[key]}" + lines[i][len(lines[i].rstrip("\r\n")):]
            done.add(key)
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # before the blank line(s) that end the section
    new = [f"{k}={v}{eol}" for k, v in want.items() if k not in done]
    return "".join(lines[:insert_at] + new + lines[insert_at:])


def _port_open(url: str) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 80), timeout=2):
            return True
    except OSError:
        return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


APP_KEYS = [
    # (secrets.env key, config file under /cfg, parser)
    ("SONARR_API_KEY", "sonarr/config.xml", arr_api_key),
    ("RADARR_API_KEY", "radarr/config.xml", arr_api_key),
    ("PROWLARR_API_KEY", "prowlarr/config.xml", arr_api_key),
    ("BAZARR_API_KEY", "bazarr/config/config.yaml", bazarr_api_key),
]


def harvest(cfg: Path, wait: float, out=print) -> dict[str, str]:
    """API keys the apps generated at first start. Waits up to `wait` seconds for them."""
    deadline = time.monotonic() + wait
    found: dict[str, str] = {}
    while True:
        for name, rel, parse in APP_KEYS:
            if name not in found:
                text = _read(cfg / rel)
                key = parse(text) if text else None
                if key:
                    found[name] = key
        if len(found) == len(APP_KEYS) or time.monotonic() >= deadline:
            break
        time.sleep(3)
    for name, rel, _ in APP_KEYS:
        if name not in found:
            out(f"MISSING {name}: {rel.split('/')[0]} has not written its key yet (is the stack up? `mh up -d`)")
    return found


def bundled_apps_mounted(cfg: Path) -> bool:
    """True when the bundled apps' config volumes are mounted under /cfg (MEDIAHUB_STACK=full)."""
    return any((cfg / rel.split("/")[0]).is_dir() for _, rel, _ in APP_KEYS)


def _harvest_bundled(have: dict[str, str], updates: dict[str, str], cfg: Path, wait: float, out) -> int:
    """The bundled apps' keys, the Plex token and the qBittorrent login. Returns the problem count."""
    problems = 0
    # App keys: the running app's key is the truth.
    found = harvest(cfg, wait, out)
    problems += len(APP_KEYS) - len(found)
    for name, key in found.items():
        if have.get(name) != key:
            updates[name] = key

    # Plex: any valid token works, so one that is set is kept.
    if not have.get("PLEX_TOKEN"):
        text = _read(cfg / PLEX_PREFS)
        tok = plex_token(text) if text else None
        if tok:
            updates["PLEX_TOKEN"] = tok
        else:
            out(PLEX_UNCLAIMED_NOTE)   # same line whether Preferences.xml is missing, unreadable or has no token

    # qBittorrent: generate a login only if there is none anywhere (QBIT_PASS empty AND no
    # WebUI\Password_PBKDF2 in its config), and only while qBittorrent is stopped (it rewrites its
    # config file on exit, which would undo the change). An existing hash is the owner's own.
    if not have.get("QBIT_PASS"):
        conf = cfg / QBIT_CONF
        conf_text = _read(conf)
        if conf_text is not None and _QBIT_HASH_LINE.search(conf_text):
            out("NOTE qBittorrent already has a password; put it in QBIT_USER/QBIT_PASS, "
                "or keep localhost auth bypass")
        elif _port_open(config.QBIT_URL):
            out("STOP qBittorrent is running. Stop it, run `setup secrets` again, then start it: "
                "`mh stop qbittorrent` ... `mh start qbittorrent`")
            problems += 1
        elif conf_text is None:
            out("MISSING qBittorrent config: start qBittorrent once (`mh up -d`), stop it, run again")
            problems += 1
        else:
            password = pysecrets.token_urlsafe(24)
            text = conf.read_text(encoding="utf-8", errors="surrogateescape")
            new = qbit_conf_with_login(text, QBIT_ADMIN, qbit_pbkdf2(password))
            st = conf.stat()
            write_atomic(conf, new.encode("utf-8", errors="surrogateescape"))
            if hasattr(os, "chown"):  # keep qBittorrent's owner (no chown on Windows)
                try:
                    os.chown(conf, st.st_uid, st.st_gid)
                except OSError:
                    pass
            updates["QBIT_USER"] = QBIT_ADMIN
            updates["QBIT_PASS"] = password
            out(f"qBittorrent: WebUI login set for user {QBIT_ADMIN}")
    return problems


def secrets_phase(secrets_file: Path, cfg: Path = CFG, wait: float = 180, out=print) -> int:
    raw = secrets_file.read_bytes() if secrets_file.exists() else b""
    have = env_values(raw)
    updates: dict[str, str] = {}
    problems = 0

    if bundled_apps_mounted(cfg):
        problems += _harvest_bundled(have, updates, cfg, wait, out)
    else:
        # Core mode mounts no app config volumes: there is nothing to harvest. The user's own apps'
        # keys, Plex token and qBittorrent login go into secrets.env by hand.
        out("NOTE bundled apps: no app config volumes are mounted (MEDIAHUB_STACK=core), so their keys, "
            "PLEX_TOKEN and the qBittorrent login are not harvested; put your own apps' values in secrets.env")

    if not have.get("MCP_TOKEN"):
        updates["MCP_TOKEN"] = pysecrets.token_urlsafe(32)

    if updates:
        write_atomic(secrets_file, env_with(raw, updates))
    out("updated: " + (", ".join(sorted(updates)) or "nothing"))
    return 1 if problems else 0


# ================================================================ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.setup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="phase", required=True, metavar="command")
    p = sub.add_parser("secrets", help="app keys -> secrets.env; MCP_TOKEN and the qBittorrent login if empty")
    p.add_argument("--wait", type=float, default=180,
                   help="seconds to wait for the apps to write their keys (default 180)")
    sub.add_parser("wire", help="connect the apps to each other; idempotent")
    sub.add_parser("check", help="what `wire` would change; exit 1 on drift")
    sub.add_parser("allowed-tools", help="print the --allowedTools string (every MCP tool, comma-separated)")
    p = sub.add_parser("bot-prompt", help="print templates/bot-prompt.md rendered from the config")
    p.add_argument("--repo-dir", required=True, help="host path of the repo (where compose/ lives)")
    p.add_argument("--shell", required=True, choices=["powershell", "bash"])
    p = sub.add_parser("config-diff", help="keys the shipped examples have that stack.env/secrets.env lack, and back")
    p.add_argument("--home", type=Path, help="folder with stack.env and secrets.env (default /mh-home)")
    p.add_argument("--examples", type=Path, help="folder with stack.env.example and secrets.env.example")
    p = sub.add_parser("remap-roots", help="re-point Sonarr/Radarr paths from one container prefix to another")
    p.add_argument("--app", required=True, choices=["sonarr", "radarr"])
    p.add_argument("--from", dest="old", required=True, help="old path prefix, e.g. /tv")
    p.add_argument("--to", dest="new", required=True, help="new path prefix, e.g. /media/TV")
    p.add_argument("--apply", action="store_true", help="do it (without this, only list what would change)")
    sub.add_parser("version", help="print the MediaHub version")
    sub.add_parser("mcp-ping", help="call /mcp (handshake, status tool, and a call without the token); "
                                    "prints OK/FAIL only; exit 1 on failure")
    args = ap.parse_args(argv)
    if args.phase in ("allowed-tools", "bot-prompt", "config-diff", "remap-roots", "version", "mcp-ping"):
        from app import setup_tools as st
        if args.phase == "allowed-tools":
            print(st.allowed_tools())
        elif args.phase == "bot-prompt":
            print(st.bot_prompt(args.repo_dir, args.shell), end="")
        elif args.phase == "config-diff":
            return st.config_diff(args.home, args.examples)
        elif args.phase == "version":
            print(st.version())
        elif args.phase == "mcp-ping":
            return st.mcp_ping()
        else:
            try:
                return st.remap_roots(args.app, args.old, args.new, args.apply)
            except (ValueError, httpx.HTTPError) as exc:
                print(f"FAIL {exc}")
                return 1
        return 0
    if args.phase == "secrets":
        try:
            return secrets_phase(config.SECRETS_FILE, CFG, args.wait)
        except PermissionError as exc:
            print(f"FAIL cannot write {exc.filename}: setup runs as PUID:PGID from stack.env; on Linux "
                  "make MEDIAHUB_HOME (and secrets.env) writable by that user, e.g. "
                  "`sudo chown -R $PUID:$PGID ~/.mediahub`")
            return 1
    return wire(check=args.phase == "check")


if __name__ == "__main__":
    sys.exit(main())
