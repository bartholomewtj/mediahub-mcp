# MediaHub

A home media stack you run by talking to an agent. One MCP server is the only
interface: request a show, see what's downloading, fix a stuck torrent, get
recommendations, free disk space, all by asking Claude Code, Codex, or a chat bot
that drives one. A deterministic loop keeps downloads moving in the background, and
an optional alert channel (Telegram or a webhook) tells you the few things it can't fix.

```
Agent (Claude Code / Codex / Pi / a chat bot) ── media skill
        │  MCP over HTTP (bearer token), 127.0.0.1:8000/mcp
        ▼
mediahub container ── watch loop (every 2 min) ── alerts (optional)
        ▼
Plex · Sonarr · Radarr · Prowlarr · FlareSolverr · qBittorrent · Bazarr
```

## Install

Point your coding agent at this repo (<https://github.com/bartholomewtj/mediahub-mcp>) and say **"install MediaHub"**. It follows
[`AGENTS.md`](AGENTS.md) and then [`INSTALL.md`](INSTALL.md): checks your machine, asks you a
handful of questions in one go (media folder, timezone, VPN, alerts, delete mode, limits),
starts the stack, wires the apps together, connects your agents and verifies the result.
Secrets (Telegram token, VPN login) you paste into a file yourself, never into the chat.

Later: **"update MediaHub"** ([`UPDATE.md`](UPDATE.md): backup, new release, settings check,
verify) and **"uninstall MediaHub"** ([`UNINSTALL.md`](UNINSTALL.md): keeps your media and, unless
you say otherwise, your data).

### Requirements

- Linux with Docker Engine and Docker Compose 2.24.4 or newer, or Windows 10/11 with Docker
  Desktop (WSL 2 backend). No macOS in v0.1.
- A disk for media with plenty of room: requests stop when free space would fall below 50 GB
  (`DISK_GUARD_GB`).
- An MCP client: Claude Code, Codex, or any client that speaks MCP over HTTP with a bearer
  header. Optionally a chat bot that runs headless Claude Code on the same machine.
- Your own indexers (you add them in Prowlarr; MediaHub ships none and names none).
- Optional: a Plex account, a VPN provider supported by [gluetun](https://github.com/qdm12/gluetun),
  a Telegram bot for alerts.

### What you get

- **Full mode** (default): MediaHub plus its own Sonarr, Radarr, Prowlarr, FlareSolverr,
  qBittorrent (optionally behind a gluetun VPN), Plex and Bazarr, wired to each other by
  `setup wire`. Or **core mode**: MediaHub alone in front of apps you already run. An existing
  stack can be imported instead (the installer's REPLACE path keeps your library, history and
  Plex watch history, and stops but never deletes the old apps).
- 19 MCP tools (below) and the `media` skill that teaches an agent to use them.
- The watch loop: swaps dead torrents, rechecks nearly-done ones, retries requests that never
  grabbed, and alerts you about the rest.
- A read-only status page, health checks, backup and verify scripts for both OSes.

### Safety model

- **Local only.** Everything listens on `127.0.0.1` (Plex also on your LAN, port 32400), and the
  MCP needs a bearer token. Remote access is your decision, never the installer's:
  [`docs/connect-remote.md`](docs/connect-remote.md).
- **Secrets stay in files** in `MEDIAHUB_HOME` (default `~/.mediahub`), outside the repo. Setup
  generates and harvests them and prints key names only.
- **Stranger-written text is fenced.** Release names and error text are data, never instructions
  or parameters.
- **Deletes are gated.** A confirm form in Claude Code; elsewhere a bounded mode (cleanup
  candidates only, daily caps). Deleting anything else (`delete_any`) needs a one-time code sent to
  your alert channel unless you opt out. Every delete can be undone for 7 days.
- **Bounded actions.** Disk guard, requests per hour, grabs per title.
- **VPN is optional.** Without it your internet provider can see the torrent traffic; the
  installer says so plainly.

### Docs

| | |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Entry point and hard rules for any agent |
| [`INSTALL.md`](INSTALL.md), [`UPDATE.md`](UPDATE.md), [`UNINSTALL.md`](UNINSTALL.md) | The playbooks agents follow |
| [`docs/connect-remote.md`](docs/connect-remote.md) | Reaching MediaHub from a phone or another machine (your choice, your risk) |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each version |

## Design

- **MCP is the only interface.** Agents act on your media through the tools the server registers;
  nothing else (and no direct *arr, Plex or qBittorrent API call) is part of the contract.
- **The watch loop is deterministic.** Every 2 minutes, no LLM: it retries, swaps and flags stuck
  downloads by fixed rules, and alerts you only for what it cannot fix.
- **Untrusted text is fenced.** Release names and *arr error text arrive inside an `untrusted`
  object, cleaned and truncated, and never go into alerts or tool parameters.
- **Ids are server-issued.** Actions take ids the server handed out (like `t:radarr:17`), re-checked
  against live state on every call.
- **Destructive actions are bounded.** Elicitation form in Claude Code; elsewhere cleanup
  candidates only, with daily caps and a 7-day recycle bin you can undo from.
- **Safe defaults.** Everything binds to `127.0.0.1`, secrets stay in files outside the repo, and
  guards (disk, request and grab limits, confirmation codes) are on unless you turn them off.

## Using it

Ask the agent. The `media` skill (`skill/SKILL.md`; the installer links it into your agent's
skills folder) tells it how. Examples:

- "Get me season 1 of Night Shift Nine."
- "What's downloading? Anything stuck?"
- "Recommend something — poll me on what I've watched first."
- "I need 100 GB back."

### The tools

| Tool | What it does | Gate |
|---|---|---|
| `status(deep?)` | Services, media disk free, downloads with swarm health, playing now, watch-loop actions | — |
| `problems()` | What needs attention, with the actions that apply | — |
| `fix(action, target_id?)` | `refresh_all`, `prowlarr_test`, `queue_refresh`, `qbit_recheck`, `qbit_resume` | — |
| `find(query, type?)` | Look up by title or `tmdb:`/`tvdb:`; on stack? in Plex? taste? | — |
| `request(type, tmdb_id, seasons?)` | Add to Radarr/Sonarr and search. TV needs explicit seasons | disk guard, 5/hour |
| `releases(target_id)` / `search(target_id, query)` | Filtered, scored candidates for a stuck item | — |
| `grab(release_key)` | Download one of them (replacing the stuck one) | 2 per item, disk guard |
| `library(type?)` | Everything on the stack and in Plex: size, watched state, taste rating, seasons, part-way items, downloads, stalled ones; `item_id` per title | — |
| `history()` / `taste()` / `taste_record(...)` | Watch history, the taste ledger (+ recent rating changes), record a poll answer | `taste_record` never changes a rating |
| `set_rating(tmdb_id, title, verdict, why?)` | Change a rating (`loved liked meh disliked skip not_mine`) of a title already in the ledger | only a title the owner names; every change logged in `data/rating_changes.json` |
| `cleanup_candidates(free_gb?)` | What could go: watched, not in 14 days, not loved, nothing part-way | — |
| `delete(candidate_id)` / `remove_blocklist(target_id)` | Destructive | Claude Code: the owner confirms in a form. Elsewhere: bounded (below) |
| `delete_any(item_id, reason, seasons?)` | Delete any title or named seasons, candidate or not | a reason, 3 a day, not while playing, a one-time code by default (see below) |
| `deleted()` | Every delete in 30 days, each with its `undo_id`, whether it can still be undone, and the steps | — |
| `undo_delete(undo_id)` | Put a delete back: files out of the recycle bin, title back in Sonarr/Radarr, rescan | within 7 days, once |

### Trust rules (enforced in the server)

- **Stranger-written text is fenced.** Release names and *arr error text come
  back inside `untrusted`, cleaned and truncated, and are never accepted as
  parameters.
- **Server-issued ids only.** Actions take ids like `q:sonarr:<hash>` or
  `t:radarr:17`, re-checked against live state on every call.
- **Destructive actions.** In interactive Claude Code, `delete` and
  `remove_blocklist` use MCP elicitation: the owner ticks a Confirm form the model
  can't answer. Everywhere else — a chat bot running headless Claude Code, Codex,
  Pi — they run in **bounded mode**: deletes only for
  cleanup candidates (watched, 14 days, not loved, nothing part-way), at most
  3 deletes and 5 remove+blocklists per 24 hours, deleted files kept 7 days in
  the recycle bin (`deleted()` explains undo), and an alert for each. A form
  the owner declines always stops it.
- **`delete_any` (anything).** The way to delete a non-candidate (and any title).
  `item_id` from `library()`, a `reason` of at least 3 words (logged), 3 a day on its
  own budget (`DELETE_ANY_PER_DAY`), refused while the title is playing or if Plex
  can't be read, same recycle bin and `undo_delete`. It has three states, shown by
  `status()` as `alerts.delete_any`:
  - `code` (the default, `DELETE_ANY_REQUIRE_CODE=true`): the first call sends the owner
    a six-digit code through the alert channel and returns `code_sent`; the second call
    includes the code (single-use, bound to the target, 10 minutes, 3 tries then a
    10-minute lockout, stored only as a salted hash).
  - `disabled`: a code is required but there is no alert channel, so `delete_any` refuses
    up front. Bounded `delete` of cleanup candidates still works. Set up a channel, or
    choose no-code mode.
  - `no_code` (`DELETE_ANY_REQUIRE_CODE=false`, an explicit opt-in): one call, and works
    with no channel. The only guards against a wrong delete are the prompt rule "only a
    title the owner named", the daily cap, and the 7-day undo.
- **`undo_delete`.** Every delete records the files it removes. Undo moves them
  from `/media/.recyclebin` back to their old paths (never outside `/media`, never
  over an existing file), re-adds the title at its old folder if it was removed
  (no search), re-monitors the seasons and rescans. The mediahub container
  therefore mounts `/media` read-write. Seeding torrents are not brought back.
- **`set_rating`.** Overwrites a rating, logs old and new in
  `data/rating_changes.json` (last 200; `taste()` shows the latest 10), drops
  the old `why` unless a new one is given. The title text must match the ledger
  row for that `tmdb_id`. "Only a title the owner named" is enforced by the skill and
  the bot prompt, not the server.
- **Bounded safe actions.** Requests stop below 50 GB free (`DISK_GUARD_GB`)
  and after 5 an hour; agent grabs stop after 2 per item.
- **Alerts carry no stranger text.** Every channel gets fixed wording and titles from
  Sonarr/Radarr metadata only. See Alerts below.

### Connect a client

The token is `MCP_TOKEN` in `<MEDIAHUB_HOME>/secrets.env` (default `~/.mediahub`). One script per
OS registers the server with a client without ever printing the token
(`compose/scripts/register-mcp.ps1` on Windows, `register-mcp.sh` on Linux; `--url` / `-Url`
changes the default `http://127.0.0.1:8000/mcp`):

| Client | Command | What it does |
|---|---|---|
| Claude Code | `register-mcp.ps1 claude` | `claude mcp remove --scope user mediahub` (failure ignored), then `claude mcp add --transport http --scope user mediahub <url> --header "Authorization: Bearer <token>"` |
| Codex CLI | `register-mcp.ps1 codex` | adds `[mcp_servers.mediahub]` with `url` and `bearer_token_env_var = "MEDIAHUB_MCP_TOKEN"` to `~/.codex/config.toml` (or `$CODEX_HOME`), replacing an older entry; sets the user env var `MEDIAHUB_MCP_TOKEN` (Linux: a line in `~/.profile` that reads it from secrets.env). Restart Codex afterwards |
| Anything else | `register-mcp.ps1 json` | writes `<MEDIAHUB_HOME>/mcp-client.json` (`{"mcpServers":{"mediahub":{"type":"http","url":...,"headers":{...}}}}`, mode 600 on Linux) and prints only its path |

```powershell
.\compose\scripts\register-mcp.ps1 claude
```

### Installer helpers

`python -m app.setup <command>`, run in the stack as `mh run --rm setup <command>`:

| Command | What it does |
|---|---|
| `allowed-tools` | The `--allowedTools` string: `mcp__mediahub__<tool>` for every tool the server registers, comma-separated, in registration order |
| `bot-prompt --repo-dir P --shell powershell\|bash` | `templates/bot-prompt.md` rendered from the config (`OWNER_NAME`, `DISK_GUARD_GB`, the alert channel, the `delete_any` mode) for a headless-agent bot. `P` is the host folder that holds `compose/`. |
| `config-diff` | After an update: `NEW` / `OPTIONAL` for keys in the shipped `stack.env.example` / `secrets.env.example` that your files lack, `UNKNOWN` for keys they do not know. Names only, never values. Exit 0 |
| `remap-roots --app sonarr\|radarr --from OLD --to NEW [--apply]` | For an existing Sonarr/Radarr whose series/movies use another container path (for example `/tv` and you now mount `/media/TV`): lists what would change (count, up to 10 titles); with `--apply` adds the new root folder if missing and re-points the titles through the bulk editor (`moveFiles: false`, files are not moved), one batch per root folder, checking the resulting paths |
| `version` | Prints `VERSION` |

## Alerts

Alerts and `delete_any` codes go out over one optional channel, chosen by `NOTIFIER`
in secrets.env (`TELEGRAM_*` and `WEBHOOK_*` in `compose/secrets.env.example`):

| `NOTIFIER` | What happens |
|---|---|
| `none` | Nothing is sent. `delete_any` is disabled unless `DELETE_ANY_REQUIRE_CODE=false`. |
| `telegram` | One-way messages from your own bot (`TELEGRAM_BOT_TOKEN` from @BotFather, `TELEGRAM_CHAT_ID`), written for you to read. No markup is interpreted; the token is never logged. |
| `webhook` | A JSON POST with a bearer key (`WEBHOOK_URL`, `WEBHOOK_KEY`) to an agent relay that forwards the note to the media bot. `WEBHOOK_EXTRA_JSON` in stack.env (not secret) merges extra fields over the JSON body for a relay with its own wire format; a default field set to `null` is dropped. |

Left blank, `NOTIFIER` is chosen from what is set: webhook, else telegram, else none.

## The watch loop

Every 2 minutes, without an LLM:

- Groups the Sonarr/Radarr queues by torrent (a season pack is one torrent).
- A download that hasn't moved for 40 minutes (30 min grace), or whose swarm
  is dead (no availability, no seeds for 10 minutes), is swapped for a
  healthier release — a season search for packs. At most 2 swaps.
  Nearly-done stalls get one resume + recheck first.
- Requests made through the MCP that never grabbed anything are searched
  again with backoff (1h, 6h, 24h) per movie / season / episode.
- Releases are filtered (no CAM/TS, no archives or executables, size sane for
  the runtime, English) and scored by seeders × the indexer's import track
  record squared, from 90 days of Sonarr/Radarr history.
- What it can't fix becomes a problem for the agent and one alert on the configured channel.
- If qBittorrent or a queue can't be read, it judges nothing that pass.

Pause it with `WATCH_ENABLED=false` in stack.env (then `mh up -d mediahub`).

It also sends two weekly messages through the alert channel (local time, set in
stack.env): `WEEKLY_CLEANUP=SUN 10:00` lists cleanup candidates for the owner to
pick from, and `WEEKLY_PICKS=FRI 18:00` asks for 3 recommendations. With no channel
nothing is sent.

New requests use the `HD-720p` profile (`DEFAULT_PROFILE`) to save space;
titles already on the stack keep their profile.

## Read-only page

`http://localhost:8000` — services, media disk, downloads, problems, watch
loop, playing now. No buttons; actions go through the agent.

## Production (Docker Desktop, or Linux)

`compose/docker-compose.yml` (the core), project name `mediahub`, always run through
`compose\mh.ps1` (or `compose/mh.sh` on Linux, WSL, Git Bash), which adds
`--env-file <MEDIAHUB_HOME>\stack.env` and picks the compose files from it:

| `stack.env` | Files | You get |
|---|---|---|
| `MEDIAHUB_STACK=full` (default) | `docker-compose.yml` + `stack.yml` | MediaHub plus bundled Sonarr, Radarr, Prowlarr, FlareSolverr, qBittorrent, Plex, Bazarr |
| `MEDIAHUB_STACK=core` | `docker-compose.yml` | MediaHub only, on `127.0.0.1:8000`. Set `SONARR_URL`, `RADARR_URL`, ... in secrets.env (e.g. `http://host.docker.internal:8989`). No other port is published, so your own apps keep theirs |
| `MEDIAHUB_STACK=full` + `MEDIAHUB_VPN=gluetun` | + `vpn.yml` | qBittorrent runs inside a [gluetun](https://github.com/qdm12/gluetun) VPN container. Provider: `stack.env.example`; credentials: `vpn.env.example` (gluetun reads only that file). Sonarr and Radarr reach qBittorrent at host `gluetun`, port `8090` |

Dashboard links (`/api/config`) show the URLs MediaHub itself uses, so with core
URLs or the VPN they may not open from a browser.

Media in `MEDIA_ROOT` (`/media` in every container). App state in named volumes. Every app shares
the `net` container's network namespace, so services reach each other on
`127.0.0.1`. Ports publish on `127.0.0.1` only, except Plex (`32400`, LAN).

| | |
|---|---|
| Settings | `%USERPROFILE%\.mediahub\stack.env` (template `compose\stack.env.example`). Not secret. |
| Secrets | `%USERPROFILE%\.mediahub\secrets.env` (template `compose\secrets.env.example`; `setup secrets` fills it, below). Never in the repo or the agent workspace. |
| Backups | `%USERPROFILE%\.mediahub\backups\` — `compose\scripts\backup.ps1` / `backup.sh`, keeps 4. Schedule it weekly (the installer offers to) |
| Health | `compose\scripts\verify-stack.ps1` (pings + `GET /api/checks`); Linux: `compose/scripts/verify-stack.sh` |
| Config as code | `mh run --rm setup check` / `wire` inside the stack (first-run wiring + the steps below), (Bazarr links, no *arr ntfy, recycle bin 7 days, Prowlarr indexers in `PROWLARR_DROP_INDEXERS` / `PROWLARR_FLARE_INDEXERS` from stack.env). Same code: `app/setup.py` |
| Linux scripts | `backup.sh` (`--no-stop`, `--keep N`), `verify-stack.sh`, `sync-runtime-secrets.sh` next to the `.ps1` files, same behaviour. `plex-lan` is Windows-only |
| Plex on the LAN | `compose\scripts\plex-lan.ps1 -HostIp <host-lan-ip>` (Docker Desktop has no bridged networking) |

```powershell
cd <repo>\compose
.\mh.ps1 ps
.\mh.ps1 up -d --build mediahub      # after changing app/
.\mh.ps1 logs -f --tail 50 mediahub
.\scripts\verify-stack.ps1                  # -AllowLowDisk while the disk is below the guard; -Fresh on a new install
                                            # (unclaimed Plex, no indexers: PENDING instead of FAIL)
.\mh.ps1 run --rm -T setup mcp-ping         # checks /mcp with no client; never prints the token
```

Upgrade an app: bump its tag in `stack.yml`, `.\mh.ps1 up -d
<service>`. Never lower a *arr or Plex tag (their databases migrate forward
only). After a fresh Bazarr volume: `.\scripts\sync-runtime-secrets.ps1` (runs
`mh run --rm setup secrets`, then recreates the dashboard container).

### First run (`MEDIAHUB_STACK=full`)

A new install is wired by commands alone: no Python on the host, and no key or
password passes through the person or agent running them (`setup` prints key
names only). `setup` is a compose service in the `setup` profile, so `up` never
starts it; it runs the mediahub image as `PUID:PGID` with the app config volumes
mounted (read-only, except qBittorrent's) and `MEDIAHUB_HOME` at `/mh-home`.

1. Create `<MEDIAHUB_HOME>/stack.env` from `compose/stack.env.example` and an
   empty `secrets.env` next to it. On Linux, that folder, both files and
   `MEDIA_ROOT` must be writable by `PUID` (`sudo chown -R $PUID:$PGID ~/.mediahub <MEDIA_ROOT>`).
   Optional: `PLEX_CLAIM=` from https://plex.tv/claim (valid 4 minutes, so do
   step 2 straight away; see `compose/plex-claim.yml`).
2. Run the stack; first start writes each app's config and API key:

```
.\mh.ps1 up -d                                 # ./mh.sh up -d
.\mh.ps1 stop qbittorrent                      # ./mh.sh stop qbittorrent
.\mh.ps1 run --rm setup secrets                # ./mh.sh run --rm setup secrets
.\mh.ps1 start qbittorrent                     # ./mh.sh start qbittorrent
.\mh.ps1 up -d --force-recreate mediahub       # ./mh.sh up -d --force-recreate mediahub
.\mh.ps1 run --rm setup wire                   # ./mh.sh run --rm setup wire
.\mh.ps1 run --rm setup check                  # ./mh.sh run --rm setup check
```

- `setup secrets` copies the Sonarr, Radarr, Prowlarr and Bazarr API keys into
  secrets.env (the running app's key wins), `PLEX_TOKEN` once Plex is claimed (a
  token you set yourself is kept), and generates `MCP_TOKEN` and the qBittorrent
  login (`QBIT_USER=admin`, a random `QBIT_PASS`, its PBKDF2 hash written to
  qBittorrent.conf) only when they are empty. qBittorrent rewrites its config on
  exit, so it must be stopped while its password is set; `secrets` refuses with a
  `STOP` line if it is running. It waits up to 180 s for the apps' keys
  (`--wait N`). Every other line of secrets.env is kept as it was. Exit 1 if
  something is missing.
- `setup wire` only adds what is missing, and never an indexer: a root folder
  (`/media/TV`, `/media/Movies`) if the app has none, a qBittorrent download
  client in Sonarr (`tv-sonarr`) and Radarr (`radarr`) if there is none (host
  `localhost`, or `gluetun` with `MEDIAHUB_VPN=gluetun`), qBittorrent's save path
  `/media/Downloads` if it is outside `/media`, Prowlarr's Sonarr and Radarr apps
  (full sync), a FlareSolverr proxy for the `flare` tag, Plex libraries "Movies"
  and "TV Shows" if Plex has no library of that type (needs `PLEX_TOKEN`), plus
  the config-as-code steps. Anything that exists in another form is left alone.
  It prints `FIXED`/`OK`/`NOTE`/`FAIL` lines; run it again any time.
- `setup check` is `wire` without writes: `DRIFT` lines, exit 1 on drift or failure.
- Plex unclaimed: `wire` prints a `NOTE`. Claim it (`PLEX_CLAIM`, or on Linux Plex's web page on
  port 32400 from a browser on the same network), then `setup secrets`, `up -d --force-recreate
  mediahub`, `setup wire`.
- Then add indexers in Prowlarr (http://localhost:9696); it pushes them to Sonarr
  and Radarr.

### Backup / restore

```powershell
.\scripts\backup.ps1            # stops the stack ~1 min, tars every volume, restarts
.\scripts\backup.ps1 -NoStop    # hot copy
```

Restore one volume:

```powershell
./mh.ps1 stop sonarr
docker run --rm -v mediahub_sonarr-config:/dst -v "$HOME\.mediahub\backups\<stamp>:/src" alpine:3.20 `
  sh -c "rm -rf /dst/* && tar -xzf /src/mediahub_sonarr-config.tgz -C /dst"
./mh.ps1 start sonarr
```

### Rebuild from nothing

1. Docker (Windows: Docker Desktop with WSL 2; `~/.wslconfig` with
   `[experimental] autoMemoryReclaim=gradual` keeps its memory in check).
2. Clone this repo (<https://github.com/bartholomewtj/mediahub-mcp>); put your saved `stack.env` and `secrets.env` back in `MEDIAHUB_HOME`
   (or create them from the templates).
3. `mh up --no-start` (creates the volumes), restore each volume from the backup (above), then
   `mh up -d`, `scripts/sync-runtime-secrets` (runs `setup secrets`, recreates MediaHub),
   `mh run --rm setup wire`, `scripts/verify-stack`. Windows only: `scripts\plex-lan.ps1`.

## Development

```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt pytest pytest-asyncio pyyaml
.venv\Scripts\python -m pytest -q
```

`pyyaml` is dev-only: `tests/test_compose.py` uses it to check the compose
files (skipped when it is missing).

A local run (`python -m uvicorn app.main:app --port 8001`) reads the same
secrets file. qBittorrent only answers from inside the stack, so the loop and
`status` swarm data need the container.

| Path | What |
|---|---|
| `app/mcp_server.py` | MCP tools (descriptions are the agent's manual) |
| `app/tools.py` | What each tool does; trust rules |
| `app/watch.py` | The loop |
| `app/health.py` | Release filters, scoring, dead-swarm check |
| `app/stack.py` | API wrappers (*arr, Prowlarr, qBittorrent, Plex) |
| `app/services.py` | The cached status snapshot |
| `app/taste.py` | The taste ledger (`/taste/taste.md`: `TASTE_DIR` or the `mediahub-taste` volume) |
| `data/` (volume) | `requests.json`, `deletions.json` (30 days), `rating_changes.json` |
| `app/notify.py` | Alerts and codes: telegram, webhook or none |
| `app/checks.py` | Deep health checks |
| `app/setup.py` | First-run setup and config as code (`python -m app.setup secrets\|wire\|check`, run by the `setup` service) |
| `app/setup_tools.py` | Installer helpers: `allowed-tools`, `bot-prompt`, `config-diff`, `remap-roots`, `version` |
| `templates/` | `bot-prompt.md`, the generic media-bot prompt (copied into the image) |
| `VERSION`, `CHANGELOG.md`, `LICENSE` | Release metadata (MIT) |
| `skill/` | The `media` skill |
| `compose/` | Production stack |
| `AGENTS.md`, `INSTALL.md`, `UPDATE.md`, `UNINSTALL.md` | The agent playbooks (`tests/test_playbooks.py` checks the commands, paths and keys they name) |
