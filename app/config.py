"""Reads settings once at startup. Nothing here is ever logged or sent anywhere.

In Docker, compose passes <MEDIAHUB_HOME>/stack.env and secrets.env (default
~/.mediahub) as the environment. A local dev run reads the same file (or MEDIAHUB_SECRETS).
Secrets never live inside the repo or the agent workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
SECRETS_FILE = Path(os.getenv("MEDIAHUB_SECRETS") or Path.home() / ".mediahub" / "secrets.env")
if SECRETS_FILE.is_file():
    load_dotenv(SECRETS_FILE)

# Who the agent-facing text talks about ("the user" until OWNER_NAME is set).
OWNER_NAME = (os.getenv("OWNER_NAME") or "").strip() or "the user"


def owner(capital: bool = False) -> str:
    """The owner's name for prose; capital=True upper-cases the first letter ("The user")."""
    return OWNER_NAME[:1].upper() + OWNER_NAME[1:] if capital else OWNER_NAME


def personalise(text: str) -> str:
    """Fill {owner} / {Owner} in user-facing text. Plain replace, so other braces are safe;
    never .format() a string before or after this."""
    return text.replace("{Owner}", owner(True)).replace("{owner}", owner())


def _url(name: str, default: str) -> str:
    """Normalise a base URL — strip the trailing slash so we can just append paths."""
    return (os.getenv(name) or default).rstrip("/")


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SONARR_URL = _url("SONARR_URL", "http://localhost:8989")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")

RADARR_URL = _url("RADARR_URL", "http://localhost:7878")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")

# Prowlarr feeds indexers to Sonarr and Radarr. When one of them complains that
# an indexer is failing, Prowlarr is where you check it.
PROWLARR_URL = _url("PROWLARR_URL", "http://localhost:9696")
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")


def _names(name: str) -> list[str]:
    """A comma-separated list setting; blanks dropped."""
    return [n.strip() for n in (os.getenv(name) or "").split(",") if n.strip()]


# Config as code (app/setup.py), set in stack.env. Indexers to remove from
# Prowlarr, and the ones that need FlareSolverr (the 'flare' tag). Empty (the default)
# leaves indexers and their flare tags alone. The package never names an indexer.
PROWLARR_DROP_INDEXERS = _names("PROWLARR_DROP_INDEXERS")
PROWLARR_FLARE_INDEXERS = _names("PROWLARR_FLARE_INDEXERS")

PLEX_URL = _url("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")

QBIT_URL = _url("QBIT_URL", "http://localhost:8090")
QBIT_USER = os.getenv("QBIT_USER", "")
QBIT_PASS = os.getenv("QBIT_PASS", "")
# qBittorrent policy, checked by status(deep) and applied by `setup wire`. KiB/s, 0 = unlimited.
QBIT_DL_LIMIT_KIB = int(os.getenv("QBIT_DL_LIMIT_KIB") or "2000")
QBIT_UP_LIMIT_KIB = int(os.getenv("QBIT_UP_LIMIT_KIB") or "250")
# Used only when seeding limits are not enabled yet; limits already enabled are never changed.
QBIT_MAX_RATIO = float(os.getenv("QBIT_MAX_RATIO") or "2.0")
QBIT_MAX_SEEDING_MINUTES = int(os.getenv("QBIT_MAX_SEEDING_MINUTES") or "10080")

# Bazarr downloads English subtitles next to each file. Key lives in
# the Bazarr volume (sync-runtime-secrets.ps1 copies it into secrets.env).
BAZARR_URL = _url("BAZARR_URL", "http://localhost:6767")
BAZARR_API_KEY = os.getenv("BAZARR_API_KEY", "")

# Seconds to wait on any single service before calling it down.
TIMEOUT = float(os.getenv("TIMEOUT", "8"))

# Pinned stacks (Docker image pins) cannot self-update; hide UpdateCheck noise.
PINNED_STACK = _flag("MEDIAHUB_PINNED_STACK", False)

# Watch loop. Blank means on. Set to false to pause it without stopping the server.
WATCH_ENABLED = _flag("WATCH_ENABLED", True)



# --- LLM-native layer (MCP server, alerts, taste) -------------------------

# Bearer token every MCP client must send. Empty disables /mcp entirely.
MCP_TOKEN = os.getenv("MCP_TOKEN", "")

# Alert channel: none | telegram | webhook. Blank = auto (webhook if WEBHOOK_URL and WEBHOOK_KEY
# are set, else telegram if its token and chat id are set, else none); app/notify.py resolves it.
NOTIFIER = (os.getenv("NOTIFIER") or "").strip().lower()
# telegram: one-way messages from your own bot (token from @BotFather; chat id of your chat with it).
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
# webhook: a JSON POST with a bearer key, for an agent relay.
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or ""
WEBHOOK_KEY = os.getenv("WEBHOOK_KEY") or ""
# Not secret (stack.env). A JSON object merged over the webhook body, for a relay with its own wire
# format. Invalid JSON is logged (without its content) and ignored.
WEBHOOK_EXTRA_JSON = os.getenv("WEBHOOK_EXTRA_JSON") or ""
# The webhook's relay forwards alerts to this bot (the owner runs MediaHub from it).
ALERT_FORWARD_TO = os.getenv("ALERT_FORWARD_TO") or "media bot"

# Destructive tools ask the human through MCP elicitation. Only clients known
# to show that form to a person (not answer it themselves) may use them.
ELICIT_CLIENTS = {c.strip().lower() for c in (os.getenv("ELICIT_CLIENTS") or "claude-code").split(",") if c.strip()}

# The media folder on the HOST (MEDIA_ROOT in stack.env, passed in through env_file). Only used to
# word the by-hand undo steps; the container itself always sees it as /media.
MEDIA_ROOT_HOST = (os.getenv("MEDIA_ROOT") or "").strip() or "<your media folder>"

# Extra Host / Origin patterns the MCP endpoint accepts besides localhost (a reverse proxy,
# Tailscale name, ...), e.g. "mediahub.example.net,mediahub.example.net:*". Comma-separated.
MCP_ALLOWED_HOSTS = _names("MCP_ALLOWED_HOSTS")

# Taste ledger. /taste is a bind-mounted folder in Docker.
TASTE_PATH = Path(os.getenv("TASTE_PATH") or "/taste/taste.md")

# Requests made through the MCP; read by the watch loop's ungrabbed rescue.
REQUESTS_PATH = ROOT / "data" / "requests.json"

# request()/grab() refuse when free space on the media disk would fall below this.
DISK_GUARD_GB = float(os.getenv("DISK_GUARD_GB") or "50")
REQUESTS_PER_HOUR = int(os.getenv("REQUESTS_PER_HOUR") or "5")
AGENT_GRABS_PER_TARGET = int(os.getenv("AGENT_GRABS_PER_TARGET") or "2")

# delete_any: deletes of titles that are not cleanup candidates.
# On by default: each delete_any needs a one-time code sent to the owner through the alert channel
# (with no channel, delete_any is disabled). The installer offers the looser no-code mode
# (DELETE_ANY_REQUIRE_CODE=false), which trusts the agent's reading of what the owner asked for.
DELETE_ANY_REQUIRE_CODE = _flag("DELETE_ANY_REQUIRE_CODE", True)

# Defaults for new titles (names, resolved to ids at call time).
# New requests default to 720p to save space. Existing titles keep theirs.
DEFAULT_PROFILE = os.getenv("DEFAULT_PROFILE") or "HD-720p"

# Weekly messages to the media bot (local time, container TZ): "DAY HH:MM".
WEEKLY_CLEANUP = os.getenv("WEEKLY_CLEANUP") or "SUN 10:00"
WEEKLY_PICKS = os.getenv("WEEKLY_PICKS") or "FRI 18:00"

# Cleanup: never propose anything watched more recently than this.
CLEANUP_MIN_DAYS = int(os.getenv("CLEANUP_MIN_DAYS") or "14")
