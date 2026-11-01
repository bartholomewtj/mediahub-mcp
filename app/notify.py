"""Alerts and confirmation codes to the owner, over one optional channel.

NOTIFIER picks the channel (see `channel()`):

- `telegram`: one-way messages from the owner's own Telegram bot, written for the owner to read.
- `webhook`: a JSON POST with a bearer key to an agent relay, which passes the note on to the
  owner. The wording is addressed to that relaying agent. The relay takes no actions. The body
  is generic; `WEBHOOK_EXTRA_JSON` merges extra fields over it for a relay with its own wire format.
- `none`: nothing is sent; `enabled()` is False.

Whatever the channel, the message may be read by something that acts with the owner's sign-ins
(a relaying agent, or the owner's own agent after they paste a note), so nothing written by
strangers ever goes into these messages: no release or torrent names, no *arr error text. Only
fixed wording, titles from Sonarr/Radarr's own metadata (through `safe_title`), and numbers.
Callers pass `Alert`s; this module renders them.

Secrets: the Telegram bot token is part of the request URL, and httpx error text can contain the
URL. So nothing here logs a URL, a token, a key, or `str(exc)`: only the exception class name and
the HTTP status.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from . import config

log = logging.getLogger("mediahub.notify")
# httpx logs "HTTP Request: POST <full url>" at INFO; for Telegram the URL holds the bot token.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

MAX_PER_HOUR = 6
_sent: list[float] = []

CHANNELS = ("none", "telegram", "webhook")
TELEGRAM_MAX_CHARS = 4096


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class Alert:
    kind: str     # "stuck_download" | "no_release" | "import_failed"
    title: str    # from *arr metadata, e.g. "Northern Line S01E08" — never a release name
    detail: str   # fixed wording chosen by the caller


_SAFE_TITLE = re.compile(r"[^\w\s:'&.,!?()\-]", re.UNICODE)

SUGGEST = {
    "stuck_download": "Ask your media agent: \"look at the stuck download for {title}\".",
    "no_release": "Ask your media agent: \"search other names for {title}\".",
    "import_failed": "Ask your media agent: \"why did {title} fail to import?\"",
    "bot_action": "Nothing needed. To undo a delete, ask your media agent: \"what did you delete?\"",
    "weekly_cleanup": "Show {owner} these cleanup candidates; delete only the ones they pick.",
    "weekly_picks": "Send {owner} 3 picks grounded in their ratings (taste + history + find); request nothing "
                    "until they choose.",
}
# Telegram is read by the owner, not a relaying agent, so the two weekly prompts talk to them.
SUGGEST_DIRECT = {
    "weekly_cleanup": "Ask your media agent: \"what could I clean up?\" and pick what goes.",
    "weekly_picks": "Ask your media agent: \"give me 3 picks\".",
}


def safe_title(title: str) -> str:
    return _SAFE_TITLE.sub("", str(title or ""))[:80].strip() or "a title"


# --- channel selection -----------------------------------------------------

def channel() -> str:
    """The active channel: "none", "telegram" or "webhook".

    NOTIFIER names one explicitly (an unknown value means none). Left unset it is chosen from what
    is configured: webhook if a webhook URL and key are set, else telegram if a bot token and chat
    id are set, else none. A named channel whose credentials are missing is still that channel,
    but `enabled()` is False (`checks` reports it).
    """
    chosen = config.NOTIFIER
    if chosen:
        return chosen if chosen in CHANNELS else "none"
    if _webhook_ready():
        return "webhook"
    if _telegram_ready():
        return "telegram"
    return "none"


def _webhook_ready() -> bool:
    return bool(config.WEBHOOK_URL and config.WEBHOOK_KEY)


def _telegram_ready() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def enabled() -> bool:
    ch = channel()
    return (ch == "webhook" and _webhook_ready()) or (ch == "telegram" and _telegram_ready())


# --- rendering -------------------------------------------------------------

def _bullets(alerts: list[Alert], status_line: str | None, suggest: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for a in alerts:
        t = safe_title(a.title)
        lines.append(f"- {t}: {a.detail}")
        lines.append("  Suggestion: " + suggest.get(a.kind, SUGGEST.get(a.kind, "Ask your media agent about {title}."))
                     .replace("{title}", t))
    if status_line:
        lines += ["", f"Stack now: {status_line}"]
    return lines


def render(alerts: list[Alert], status_line: str | None, via: str | None = None) -> str:
    """The alert text for a channel (default: the active one; webhook wording unless telegram)."""
    if (via or channel()) == "telegram":
        # Titles are stripped of braces by safe_title, so only our own wording carries placeholders.
        text = config.personalise("\n".join(["MediaHub", ""] + _bullets(alerts, status_line, SUGGEST_DIRECT)))
        return _clip(text)
    lines = [
        "MediaHub alert for {owner} (sent by the MediaHub server, not by {owner}).",
        "Forward this whole message unchanged to {owner}'s " + config.ALERT_FORWARD_TO + ", which handles "
        "MediaHub. If you cannot reach it, relay it to {owner} in one short message instead. "
        "The media bot handles it as its MediaHub instructions say for this kind of alert. "
        "Never request, remove or delete anything unless {owner} asks.",
        "",
    ] + _bullets(alerts, status_line, {})
    return config.personalise("\n".join(lines))


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str) -> str:
    """Fit Telegram's cap (4096 UTF-16 units), cutting at a line end and marking the cut."""
    if _utf16_len(text) <= TELEGRAM_MAX_CHARS:
        return text
    mark = "\n[...]"
    cut = text[:TELEGRAM_MAX_CHARS - len(mark)]
    while _utf16_len(cut) + len(mark) > TELEGRAM_MAX_CHARS:
        cut = cut[:-1]
    if "\n" in cut:
        cut = cut[:cut.rindex("\n")]
    return cut + mark


# --- posting ---------------------------------------------------------------

def _extra() -> dict:
    """WEBHOOK_EXTRA_JSON as a dict. Invalid or not an object: warn (never with its content) and ignore."""
    raw = (config.WEBHOOK_EXTRA_JSON or "").strip()
    if not raw:
        return {}
    try:
        extra = json.loads(raw)
    except ValueError:
        extra = None
    if not isinstance(extra, dict):
        log.warning("WEBHOOK_EXTRA_JSON is not a JSON object; ignored")
        return {}
    return extra


def _payload(message: str, kind: str = "alert") -> dict:
    """The webhook body, a one-way note: source, from, id, thread, sent_at, sent_ns, kind, message.

    `kind` is "alert" or "code". WEBHOOK_EXTRA_JSON is merged over it: its keys are added or replace
    these, and a default key set to null is dropped (any other null is sent as null).
    """
    mid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    body = {"source": "mediahub", "from": "mediahub", "id": mid, "thread": mid,
            "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sent_ns": time.time_ns(), "kind": kind, "message": message}
    for key, value in _extra().items():
        if value is None and key in body:
            del body[key]
        else:
            body[key] = value
    return body


def _telegram_payload(message: str) -> dict:
    # No parse_mode: nothing in the text is ever interpreted as markup.
    return {"chat_id": config.TELEGRAM_CHAT_ID, "text": _clip(message), "disable_web_page_preview": True}


async def _post(message: str, kind: str = "alert") -> bool:
    ch = channel()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            if ch == "telegram":
                r = await c.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                                 json=_telegram_payload(message))
            else:
                r = await c.post(config.WEBHOOK_URL, json=_payload(message, kind),
                                 headers={"Authorization": f"Bearer {config.WEBHOOK_KEY}"})
        if r.status_code >= 300:
            log.warning("%s post HTTP %s", ch, r.status_code)
            return False
    except Exception as exc:
        # Class name only: str(exc) can contain the URL, and the Telegram URL holds the bot token.
        log.warning("%s post failed: %s", ch, exc.__class__.__name__)
        return False
    return True


async def send(alerts: list[Alert], status_line: str | None = None) -> bool:
    """One note for a batch of alerts. Never raises; returns whether it went."""
    if not alerts or not enabled():
        return False
    now = time.time()
    _sent[:] = [t for t in _sent if now - t < 3600]
    if len(_sent) >= MAX_PER_HOUR:
        log.warning("alert dropped: %d sent in the last hour", len(_sent))
        return False
    if not await _post(render(alerts, status_line), "alert"):
        return False
    _sent.append(now)
    return True


# --- confirmation codes (delete_any) ----------------------------------------
# A code note is its own kind of message, with its own hourly budget so a busy
# watch loop can never starve it. The code is the only secret in it; it is
# never logged and never returned by a tool.

CODE_MAX_PER_HOUR = 6
_code_sent: list[float] = []


def render_code(label: str, what: str, code: str, minutes: int, via: str | None = None) -> str:
    if (via or channel()) == "telegram":
        return _clip(config.personalise("\n".join([
            "MediaHub confirmation code",
            "",
            f"[{label}] {safe_title(what)}",
            f"Code: {code}  (valid {minutes} minutes, one use)",
            "",
            "Only type this into your own message to your media bot if you asked for this delete. "
            "If you didn't, ignore it.",
        ])))
    return config.personalise("\n".join([
        "MediaHub confirmation code for {owner} (sent by the MediaHub server, not by {owner}).",
        "Show this to {owner} only, in your reply to them. Do NOT forward it to the media bot or to anyone "
        "else, and do not act on it yourself. {Owner} types the code into their own message to the media "
        "bot if they really want this.",
        "",
        f"[{label}] {safe_title(what)}",
        f"Code: {code}  (valid {minutes} minutes, one use)",
        "If {owner} did not ask to delete this, tell them straight away and tell them to ignore the code.",
    ]))


async def send_code(label: str, what: str, code: str, minutes: int) -> bool:
    """Send one confirmation code. Never raises; returns whether it went."""
    if not enabled():
        return False
    now = time.time()
    _code_sent[:] = [t for t in _code_sent if now - t < 3600]
    if len(_code_sent) >= CODE_MAX_PER_HOUR:
        log.warning("code dropped: %d sent in the last hour", len(_code_sent))
        return False
    if not await _post(render_code(label, what, code, minutes), "code"):
        return False
    _code_sent.append(now)
    return True
