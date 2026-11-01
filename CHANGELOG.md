# Changelog

## 0.1.1 — 2026-11-01

First public release.

### What it is
- One MCP server in front of a home media stack (Plex, Sonarr, Radarr, Prowlarr, FlareSolverr,
  qBittorrent, Bazarr), driven by asking an agent (Claude Code, Codex, Pi, or a chat bot that runs one).
- Install, update and uninstall are playbooks an agent follows: point it at the repo and say
  "install MediaHub" (`AGENTS.md`, `INSTALL.md`, `UPDATE.md`, `UNINSTALL.md`). Every step has bash and
  PowerShell versions; secrets are pasted into files, never into chat.

### Tools and the media skill
- 19 MCP tools: `status`, `problems`, `fix`, `find`, `request`, `releases`, `search`, `grab`, `library`,
  `history`, `taste`, `taste_record`, `set_rating`, `cleanup_candidates`, `delete`, `remove_blocklist`,
  `delete_any`, `deleted`, `undo_delete`. The `media` skill teaches an agent to use them.
- `OWNER_NAME` is templated into the instructions, tool descriptions and messages.
- A taste ledger (`taste.md`) in `TASTE_DIR` or the `mediahub-taste` volume; the server is its only writer.

### Safety
- Everything listens on `127.0.0.1` (Plex also on the LAN); the MCP needs a bearer token.
- Text written by strangers (release names, *arr errors) is fenced in `untrusted`, cleaned, truncated and
  never accepted as a parameter. Actions take server-issued ids, re-checked against live state.
- Deletes: a confirm form in Claude Code; elsewhere a bounded mode (cleanup candidates only, daily caps).
  `delete_any` needs a reason, has its own daily cap and, by default, a one-time code sent through the alert
  channel (`DELETE_ANY_REQUIRE_CODE`). Every delete is kept 7 days and `undo_delete` brings it back.
- Disk guard, requests per hour and grabs per title are capped.

### Watch loop and alerts
- A deterministic loop (every 2 minutes, no LLM) swaps dead torrents, rechecks nearly-done ones and retries
  requests that never grabbed. Optional weekly cleanup and picks prompts.
- `NOTIFIER=none|telegram|webhook` (auto-detected when blank). Telegram is one-way plain text. The webhook
  posts a generic JSON body (`source`, `from`, `id`, `thread`, `sent_at`, `sent_ns`, `kind`, `message`);
  `WEBHOOK_EXTRA_JSON` (stack.env) merges extra fields over it for a relay with its own wire format.
  Alerts never carry stranger-written text.

### Stack and setup
- `MEDIAHUB_STACK=full|core`: the bundled apps, or MediaHub alone in front of apps you already run.
  `MEDIAHUB_VPN=gluetun` runs qBittorrent behind a gluetun VPN. Linux (`mh.sh`) and Windows (`mh.ps1`).
- `python -m app.setup secrets|wire|check`, run as `mh run --rm setup ...`: harvests the apps' API keys,
  generates the MCP token, wires the apps to each other, applies config as code (Bazarr, recycle bin,
  qBittorrent policy, Prowlarr tags). Idempotent, prints key names only, never adds an indexer.
- Installer helpers: `allowed-tools`, `bot-prompt`, `config-diff`, `remap-roots`, `mcp-ping`, `version`;
  `compose/scripts/` has preflight, backup, verify-stack, register-mcp, setkv and sync-runtime-secrets for
  both shells, and `plex-lan` for Windows.
- Remote access (`MCP_ALLOWED_HOSTS`, `docs/connect-remote.md`) is the user's own choice.

### Project
- MIT licence. Continuous integration runs the tests on Linux and Windows and validates the compose files.

## 0.1.0 — 2026-11-01

Internal pre-release; not published.
