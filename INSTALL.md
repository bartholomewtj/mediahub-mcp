# Install MediaHub (playbook for agents)

You are installing MediaHub for a user who asked you to. Read `AGENTS.md` first: its hard rules
apply to every step here. Go phase by phase. Run each phase's check before moving on. On a
failure, do what the phase says; if that does not fix it, stop and tell the user exactly what
failed (the command and its last lines of output, with nothing secret in them).

## How to read this file

- Each step has a **bash** block (Linux) and a **PowerShell** block (Windows). Run only the
  one for the host's OS. On Windows prefer PowerShell 7 (`pwsh`); Windows PowerShell 5.1 works too.
- Every block of the bash track is safe to send through a `bash -lc '...'` wrapper: none contains a
  single quote (strings use double quotes; anything longer lives in a script under `compose/scripts/`).
  Keep it that way when you adapt a command: write your own values in double quotes too.
- Run every command **from the repository root** (the folder with `AGENTS.md`). If your shell
  does not keep the working directory between commands, start each one with `cd <repo> &&`
  (bash) or `Set-Location <repo>;` (PowerShell).
- Shell variables and functions do not survive between separate tool calls in most agents.
  Each block below is self-contained: run a block as one command.
- `<ANGLE_BRACKETS>` are values you fill in from the interview (Phase 1). Keep the quotes.
- **ASK THE USER** marks a decision or an action only the user can take. Ask, wait for the
  answer, then go on. If the user's request already answered it, use that answer and do not
  ask again. In a headless run with no way to ask, use the default named there; if none is
  named, stop and report what you need.
- `mh` means the compose wrapper: `compose/mh.sh` on Linux, `.\compose\mh.ps1` on Windows.
  It reads `MEDIAHUB_HOME/stack.env` and picks the compose files from it. Never run plain
  `docker compose` for this stack: it stops with a `MEDIA_ROOT` / `MEDIAHUB_HOME` error.
- `MEDIAHUB_HOME` is `~/.mediahub` on Linux and `$HOME\.mediahub` on Windows. This playbook
  uses that default. It holds `stack.env` (settings), `secrets.env` (secrets) and `backups/`.
- Secret values are never printed. Where a step must check a secret, it checks presence,
  length or shape only. Do not "just look" at `secrets.env` or `vpn.env`.
- Raw container logs can hold secrets: qBittorrent prints a temporary password on its first start;
  never copy it anywhere. When you show **container logs** (`mh logs`, `docker logs`), filter out
  secret-looking lines. The output of `setup` and of `mh` commands other than `logs` needs **no**
  filtering: `setup` prints names, never values (its `updated: ...` line lists key names, so a filter
  for `key` or `token` would only hide it). Do not filter it.

  ```bash
  compose/mh.sh logs --tail 80 <service> 2>&1 | grep -viE "pass|token|key|secret"
  ```

  ```powershell
  .\compose\mh.ps1 logs --tail 80 <service> 2>&1 | Select-String -NotMatch -Pattern 'pass|token|key|secret'
  ```

---

## Phase 0: Preflight

Collect facts. Change nothing yet. One call gathers everything this phase and Phase 1 need:

```bash
compose/scripts/preflight.sh
```

```powershell
.\compose\scripts\preflight.ps1
```

It prints labelled sections (`== OS ==`, `== docker ==`, and so on), changes nothing, and always
exits 0: it reports, you decide. A missing tool is a line saying so, not an error. Keep the output;
you quote parts of it in the Phase 1 message and the hand-over. Once the user has named the media
folder (Phase 1 question 2), run it again with the folder so the disk section looks at the right
disk: `compose/scripts/preflight.sh <MEDIA_ROOT>` / `.\compose\scripts\preflight.ps1 <MEDIA_ROOT>`
(a folder that does not exist yet is fine: it shows the nearest existing parent). Run it again after
Phase 4.3 too, to see that the old apps stopped and their ports are free.

How to read each section (the numbers are what the rest of this playbook refers to):

### 0.1 Operating system (`== OS ==`)

- `supported: yes` (Linux x86_64 or aarch64, or Windows 10/11): go on.
- `supported: no` (macOS or anything else): stop. MediaHub v0.1 supports Linux + Docker and
  Windows + Docker Desktop only. Tell the user.
- Windows with `execution policy: Restricted` or `AllSigned`: the repo's `.ps1` scripts will not
  run. **ASK THE USER** to allow them with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`,
  or run each script as `powershell -NoProfile -ExecutionPolicy Bypass -File <script> <args>`.

### 0.2 Tools: git, Docker, Compose v2 (`== tools ==`, `== docker ==`)

MediaHub needs Docker Compose **2.24.4 or newer**: `compose/vpn.yml` uses the `!override` merge
tag, which Compose added in 2.24.4. One minimum for every mode keeps the playbook simple.

Check: `git: git version ...`, `curl: present` (Linux), `docker: Docker version ...` and
`compose: <version> (minimum 2.24.4: ok)`. `absent` or `TOO OLD` is a failure.

On failure: **ASK THE USER** to install or upgrade. Linux: Docker Engine with the compose
plugin (https://docs.docker.com/engine/install/) and `curl`. Windows: Docker Desktop
(https://docs.docker.com/desktop/) with the WSL 2 backend, and Git for Windows. Do not install
system packages without the user's yes.

### 0.3 Docker is running (`== docker running ==`)

Check: `running: yes (server and OS type: <version> linux)`.

On failure (`running: no (...)`; the text in the brackets is Docker's own message):
- Linux `permission denied ... docker.sock`: **ASK THE USER** to add themselves to the
  `docker` group (`sudo usermod -aG docker $USER`, then log out and in) or to tell you to use
  `sudo`. With `sudo`, `$HOME` changes: run `sudo env MEDIAHUB_HOME=$HOME/.mediahub compose/mh.sh ...`.
- Linux `Cannot connect to the Docker daemon`: **ASK THE USER**, then start it and run the preflight again:

  ```bash
  sudo systemctl start docker
  ```

- Windows: start Docker Desktop and wait up to 2 minutes:

  ```powershell
  Start-Process "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
  1..24 | ForEach-Object { if (docker info --format '{{.OSType}}' 2>$null) { 'up'; break }; Start-Sleep 5 }
  ```

- The OS type is `windows` (the preflight prints a `NOTE`): Docker Desktop is in Windows-containers
  mode. **ASK THE USER** to switch to Linux containers (tray icon, "Switch to Linux containers").

### 0.4 Is MediaHub already installed? (`== mediahub ==`)

Check: `settings file: none`, `containers: none`, `volumes: none`.

If anything shows up (or says `unknown` because Docker was not usable, which is not a pass), MediaHub
(or part of it) may already be here. Stop. **ASK THE USER** whether they want to update (`UPDATE.md`),
repair, or remove it first (`UNINSTALL.md`). Never install over it.

### 0.5 Existing media stack (`== existing stack ==`)

Three lists: `-- containers` (any state, matched by name or image), `-- services` and
`-- processes`, each for Sonarr, Radarr, Prowlarr, Plex, qBittorrent, Bazarr, Jellyfin, Jellyseerr,
Overseerr, Transmission, Deluge, SABnzbd and NZBGet.

`(none found)` under a heading means that check ran and matched nothing. A line such as
`(docker not usable: this check did not run)` or an error message under a heading means that check
did not run: it is not a pass. Fix it or **ASK THE USER** before you call this a fresh install. (On
Linux without systemd the process list stands in for the services list.)

Nothing found: a fresh install. Something found: an existing stack. For each container found,
record its image, restart policy and mounts (you need them in Phase 4):

```bash
docker inspect -f "{{.Name}} {{.Config.Image}} restart={{.HostConfig.RestartPolicy.Name}} {{.Config.Labels.build_version}}{{range .Mounts}}{{println}}  {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}}{{end}}" <container>
```

```powershell
docker inspect -f '{{.Name}} {{.Config.Image}} restart={{.HostConfig.RestartPolicy.Name}} {{.Config.Labels.build_version}}{{range .Mounts}}{{println}}  {{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}} -> {{.Destination}}{{end}}' <container>
```

### 0.6 Ports (`== ports ==`)

The full stack publishes 8000, 8989, 7878, 9696, 8090, 6767, 8191 (all on 127.0.0.1) and
32400 (Plex, LAN). Core mode publishes only 8000.

The script tries `ss`, then `netstat`, then reads `/proc/net/tcp` and `tcp6` (listening sockets,
hex ports), so it works on Alpine/BusyBox too; `checked with:` names the one it used. Check:
`ports free`, or only ports held by the media apps found in 0.5 (REPLACE stops those; CONNECT
needs only 8000). `could not check ports` is not a pass: **ASK THE USER** to confirm nothing else
uses those ports, and say so in the hand-over. `in use: ...` lists the busy ports; the containers
list (0.5) shows whether a container holds them. On Windows the line is `in use: <address>:<port>
<process>`.

A port held by something else: **ASK THE USER** to stop that program. Only port 8000 can move
(`MEDIAHUB_PORT_BIND=127.0.0.1:<port>` in stack.env, then pass `--url` to `register-mcp`;
`verify-stack.sh` and `verify-stack.ps1` read the port, and `MEDIAHUB_PREFIX`, from stack.env). The
other ports are fixed in v0.1.

### 0.7 Disks (`== disk ==`)

The section shows `df -h` (Linux; Windows: the drive's free GiB) for the media folder you passed,
else for `/` (the current drive on Windows). Note the free space (on a bare `/` ignore `tmpfs`,
`devtmpfs`, `overlay` and `shm` rows). `df -h` and Windows count in GiB (1024 cubed), MediaHub's
numbers (`DISK_GUARD_GB`, `disk_free_gb`, `verify-stack`) in GB (1000 cubed): the same disk reads
about 7% higher in MediaHub (100 GiB is 107 GB). That is not a fault. Requests are refused below `DISK_GUARD_GB`
(default 50) free on the media disk, so the disk for MEDIA_ROOT needs well over 50 GB free. Docker
itself needs about 5 GB for the images. You check the chosen folder again in Phase 3.

### 0.8 Linux user ids (`== uid/gid ==`)

The apps run as PUID:PGID, and so do MediaHub's own container and the `setup` helper (so
`undo_delete` can move files the apps own). Default: the current user's ids (`uid:` and `gid:`
in the section). If the user is `root` (uid 0; the script prints a `NOTE`), tell them a normal
user is better and **ASK THE USER** whether to go on as root. Windows prints no ids: PUID and PGID
stay 1000.

---

## Phase 1: Interview

Send the user **one** message with every question below, filled with what you detected and the
defaults. Skip any question their request already answered. Wait for the reply. Questions that
come back unanswered take the default. Two have no default: MEDIA_ROOT, and REPLACE or CONNECT
when an existing stack was found. Headless with no answer to those: stop and report.

The preflight output (Phase 0) already holds what you need to fill in the questions:

- `== timezone ==` is the IANA name to offer in question 3. `timezone: unknown` means ask the user
  (suggest `Etc/UTC`); on Windows without a mapping it prints the Windows name and asks for the IANA one.
- `== uid/gid ==` gives the Linux ids for question 11.
- `== claude cli ==` says whether the agent CLI is installed here (`claude: present (<path>)` or
  `claude: absent`). The bot in question 10 drives `claude -p`.

**ASK THE USER** (one message; adapt the wording, keep every point):

```text
To install MediaHub I need a few answers. Defaults are in [brackets]; reply "defaults" to accept them.

1. Your name, used when MediaHub's tools talk about you. [none: "the user"]
2. Media folder (MEDIA_ROOT): where movies and TV will live. It gets Movies, TV and Downloads
   subfolders. It needs plenty of free space (requests stop below 50 GB free). [no default]
   <Windows: write it like D:/Media>
3. Timezone: I detected <TZ>. Right? [yes]   <if "unknown": ask for it and suggest Etc/UTC>
4. What to run:
   a) full: MediaHub plus its own Sonarr, Radarr, Prowlarr, qBittorrent, Plex, Bazarr [default]
   b) core: MediaHub only, connected to apps you already run
   <If an existing stack was found, replace 4 with:>
   I found an existing media setup: <list>. Choose:
   a) REPLACE (recommended): I back up their settings, import Sonarr, Radarr, Prowlarr and Plex
      (settings, history, watch history) into MediaHub's stack, keep your media files where they
      are, and stop (not delete) the old ones.
   b) CONNECT: keep your apps as they are and run MediaHub alone in front of them (core mode).
5. VPN for qBittorrent (through gluetun)? [no]
   If yes: your VPN provider's name, and wireguard or openvpn [wireguard], and optionally which
   countries. You will paste the login into a file yourself; never into this chat.
   If no: your internet provider can see your torrent traffic. I will carry on without it.
6. Alerts: MediaHub can message you when something needs you. [none]
   Or Telegram: you create your own bot with @BotFather and paste its token and your chat id
   into a file yourself.
7. Deleting titles that are not cleanup candidates (delete_any):
   a) with a one-time code sent to your alert channel [default; with no alert channel this
      kind of delete is switched off, ordinary cleanup still works]
   b) no code: one step, but a wrong or tricked delete goes through, limited to 3 a day and
      undoable for 7 days.
8. Settings (accept all, or name the ones to change):
   new titles at quality profile HD-720p | requests stop below 50 GB free | 5 requests per hour |
   2 agent grabs per title | 3 delete_any per day | weekly cleanup suggestions SUN 10:00 |
   weekly picks FRI 18:00 | never suggest deleting anything watched in the last 14 days |
   qBittorrent caps 2000 KiB/s down, 250 KiB/s up | seeding stops at ratio 2.0 or after 7 days
9. Plex: claim it for your Plex account now (you get a code at https://plex.tv/claim, valid
   4 minutes, and paste it into a file when I say), or later. [later]  <full/REPLACE only>
10. Which agents should get MediaHub: Claude Code, Codex, another MCP client (I make a JSON
    snippet), and/or a chat bot that runs headless Claude Code? [<the client you are running in>]
    If a bot: which machine runs the bot's agent? [this one]
    <If the bot is to run on this machine and `claude` is not installed here:> Claude Code is not
    installed on this machine, and the bot needs it (or another agent CLI). Options: install Claude
    Code here (official install docs: https://docs.claude.com/en/docs/claude-code/setup; I will not
    install it unless you ask), or run the bot on a machine that has it, with remote access to
    MediaHub's MCP (your choice and risk, docs/connect-remote.md), or skip the bot for now.
<Linux only:> 11. Run the apps as <user> (uid <PUID>, gid <PGID>)? [yes]
```

Map the answers to settings (Phase 3 writes them):

| Answer | Setting (file) |
|---|---|
| 1 name | `OWNER_NAME` (stack.env); empty for none |
| 2 folder | `MEDIA_ROOT` (stack.env); forward slashes on Windows |
| 3 timezone | `TZ` (stack.env), IANA name such as `Europe/London` |
| 4 full / REPLACE | `MEDIAHUB_STACK=full` (stack.env) |
| 4 core / CONNECT | `MEDIAHUB_STACK=core` (stack.env); app URLs and keys in secrets.env (Phase 5.7) |
| 5 VPN yes | `MEDIAHUB_VPN=gluetun`, `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `VPN_SERVER_COUNTRIES` (stack.env); login in vpn.env. Needs full. |
| 5 VPN no | `MEDIAHUB_VPN=none` (stack.env) |
| 6 Telegram | `NOTIFIER=telegram` (secrets.env); user pastes `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| 6 none | leave `NOTIFIER` empty |
| 7 code / no code | `DELETE_ANY_REQUIRE_CODE=true` / `false` (stack.env) |
| 8 settings | `DEFAULT_PROFILE`, `DISK_GUARD_GB`, `REQUESTS_PER_HOUR`, `AGENT_GRABS_PER_TARGET`, `DELETE_ANY_PER_DAY`, `WEEKLY_CLEANUP`, `WEEKLY_PICKS`, `CLEANUP_MIN_DAYS`, `QBIT_DL_LIMIT_KIB`, `QBIT_UP_LIMIT_KIB`, `QBIT_MAX_RATIO`, `QBIT_MAX_SEEDING_MINUTES` (stack.env) |
| 9 Plex now | `PLEX_CLAIM` (stack.env), pasted by the user in Phase 5.1 |
| 10 bot machine | this machine with an agent CLI installed: 7.3 renders the prompt. This machine without one, or another machine: flag it (see the rules below) |
| 11 ids | `PUID`, `PGID` (stack.env); Windows keeps 1000 |

Rules for the answers:
- No-code mode (7b) only if the user chose it after reading the risk sentence. Otherwise `true`.
- Code mode with no alerts means `delete_any` is disabled. Say so once in your summary.
- `WEEKLY_*` are `DAY HH:MM` with a three-letter day (`MON`...`SUN`), 24-hour time.
- A headless bot whose agent runs on another machine cannot reach MediaHub: the MCP listens on
  127.0.0.1 only. Tell the user the MCP must be reachable from that machine (`docs/connect-remote.md`;
  their choice and their risk), do not render the bot prompt silently (7.3), and list it under "Still to
  do" in the hand-over.
- A bot on this machine with no agent CLI installed here (`claude` not found above): say so in the
  Phase 1 message, with the three options. Never install Claude Code unasked. Whatever the user picks,
  7.3 applies.
- VPN with core mode is not possible: the VPN only routes the bundled qBittorrent. Point it out.
- REPLACE needs MEDIA_ROOT to be the folder that already holds their media folders (the common
  parent of their TV and movie folders). If those are on different disks, **ASK THE USER**:
  v0.1 mounts one media folder, so pick CONNECT or move one folder first.
- Windows-native Sonarr/Radarr (not in Docker) cannot be imported in v0.1 (their database holds
  Windows paths). Offer CONNECT, or a fresh full stack where the user re-adds titles with Sonarr's
  and Radarr's own Library Import.

Check: you have a value (or default) for every row. Repeat the final choices to the user in a
short list before Phase 3. In an interactive run, wait for "go" on that list. In a headless run (every
answer was given in the request and the user is not there to reply), do not wait: put the list in the
hand-over (Phase 9) as "Choices I made", mark which ones were defaults, and continue. Say in the
hand-over that you did not wait for a confirmation.

---

## Phase 2: Get the code

If you are already inside a checkout of this repo (it has `AGENTS.md` and `compose/mh.sh`),
use it. Otherwise **ASK THE USER** for the repository URL (if their request did not give it; the public
repository is `https://github.com/bartholomewtj/mediahub-mcp`) and clone it to a lasting place, such as `~/mediahub` or `$HOME\mediahub`. Not a temporary
folder: the bot prompt and backups point at this path.

Use the newest release tag. With no tags yet, use the remote's default branch (whatever
`origin/HEAD` points at: `main`, `master`, a renamed branch). With neither (a fork or a bundle with no
`origin/HEAD`), stay on the branch you are on. A missing `main` is never a failure. Pre-release tags
(with a `-`) are skipped.

```bash
git status --porcelain
git fetch --tags --quiet
tag="$(git tag --sort=-v:refname | grep -v -- - | head -n1)"
if [ -n "$tag" ]; then
  git checkout --quiet "$tag"
else
  br="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"; br="${br#origin/}"
  if [ -n "$br" ]; then
    git checkout --quiet "$br" && { git pull --ff-only --quiet || echo "WARN git pull failed: staying at $(git rev-parse --short HEAD)"; }
  else
    echo "no release tag and no remote default branch: staying on $(git branch --show-current)"
  fi
fi
[ -x compose/mh.sh ] || chmod +x compose/mh.sh compose/scripts/*.sh   # only for checkouts that lost the modes
echo "version $(cat VERSION) at $(git describe --tags --always)"; pwd
```

```powershell
git status --porcelain
git fetch --tags --quiet
$tag = git tag --sort=-v:refname | Where-Object { $_ -notmatch '-' } | Select-Object -First 1
if ($tag) { git checkout --quiet $tag }
else {
    $br = "$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null)" -replace '^origin/', ''
    if ($br) { git checkout --quiet $br; git pull --ff-only --quiet; if ($LASTEXITCODE -ne 0) { "WARN git pull failed: staying at $(git rev-parse --short HEAD)" } }
    else { "no release tag and no remote default branch: staying on $(git branch --show-current)" }
}
"version $(Get-Content VERSION) at $(git describe --tags --always)"; (Get-Location).Path
```

Check: `git status --porcelain` printed nothing, and the last lines show a version and the
absolute repo path. Remember the path (`<REPO>`).

Then look at where `origin` points, because `UPDATE.md` fetches new releases from it:

```bash
git remote get-url origin
```

```powershell
git remote get-url origin
```

If it is a local path (`/tmp/...`, `C:\...`, `file://...`), a bundle, or a temporary folder, or there
is no `origin` at all, warn the user: updates will not work (or will update from a copy that never
changes). **ASK THE USER** for the real repository URL (normally `https://github.com/bartholomewtj/mediahub-mcp.git`)
and run `git remote set-url origin <the real
repo URL>` (`git remote add origin <the real repo URL>` when there was none). Headless without a URL:
leave it, and list this under "Still to do" in the hand-over.

On failure: local changes (porcelain printed lines): **ASK THE USER** before touching them.
Never `git reset` or `git stash` their work on your own.

---

## Phase 3: Write MEDIAHUB_HOME

### 3.1 Folder and templates

Existing files are never overwritten.

```bash
mkdir -p ~/.mediahub && chmod 700 ~/.mediahub
[ -e ~/.mediahub/stack.env ] || cp compose/stack.env.example ~/.mediahub/stack.env
[ -e ~/.mediahub/secrets.env ] || { cp compose/secrets.env.example ~/.mediahub/secrets.env; chmod 600 ~/.mediahub/secrets.env; }
ls -l ~/.mediahub
```

```powershell
New-Item -ItemType Directory -Force "$HOME\.mediahub" | Out-Null
if (-not (Test-Path "$HOME\.mediahub\stack.env")) { Copy-Item compose\stack.env.example "$HOME\.mediahub\stack.env" }
if (-not (Test-Path "$HOME\.mediahub\secrets.env")) { Copy-Item compose\secrets.env.example "$HOME\.mediahub\secrets.env" }
Get-ChildItem "$HOME\.mediahub"
```

Check: `stack.env` and `secrets.env` are listed.

### 3.2 Settings

One block of one-line calls. Write every key from Phase 1; for tunables write only the ones the
user changed. Leave out the VPN lines without a VPN, and
`VPN_SERVER_COUNTRIES` if none were named.

`compose/scripts/setkv.sh <file> <KEY> <value>` (POSIX sh and BusyBox awk only; `<file>` is a name
under `~/.mediahub`) replaces the first `KEY=` line, or else the first commented `# KEY=` line, or
else appends `KEY=value`; it drops any further `KEY=` duplicates, writes a temporary file and moves
it over the original (keeping its permissions), then checks that exactly one `KEY=` line exists with
the new value. Values may hold `/`, `&`, `\` and spaces, not a newline. A `FAIL setkv: ...` line and
a non-zero exit mean the key was **not** set: stop and fix that before going on (a missing file means
3.1 did not finish). `compose/scripts/setkv.ps1` does the same in PowerShell. Nothing is pasted or
defined: each call is one line, so it also works through wrappers such as `bash -lc '...'` (write the
value in double quotes; inside them, a `$`, backtick, `"` or `\` in a value needs a backslash before it).
On success each call prints `OK setkv <KEY>` (the key name, never the value).

```bash
compose/scripts/setkv.sh stack.env MEDIA_ROOT "<MEDIA_ROOT>"
compose/scripts/setkv.sh stack.env TZ "<TZ>"
compose/scripts/setkv.sh stack.env PUID "<PUID>"
compose/scripts/setkv.sh stack.env PGID "<PGID>"
compose/scripts/setkv.sh stack.env OWNER_NAME "<OWNER_NAME>"
compose/scripts/setkv.sh stack.env MEDIAHUB_STACK "<full|core>"
compose/scripts/setkv.sh stack.env MEDIAHUB_VPN "<none|gluetun>"
compose/scripts/setkv.sh stack.env VPN_SERVICE_PROVIDER "<provider>"
compose/scripts/setkv.sh stack.env VPN_TYPE "<wireguard|openvpn>"
compose/scripts/setkv.sh stack.env VPN_SERVER_COUNTRIES "<Country1,Country2>"
compose/scripts/setkv.sh stack.env DELETE_ANY_REQUIRE_CODE "<true|false>"
compose/scripts/setkv.sh stack.env DISK_GUARD_GB "<only if changed>"
grep -vE "^[[:space:]]*(#|\$)" ~/.mediahub/stack.env
```

```powershell
.\compose\scripts\setkv.ps1 stack.env MEDIA_ROOT '<MEDIA_ROOT>'
.\compose\scripts\setkv.ps1 stack.env TZ '<TZ>'
.\compose\scripts\setkv.ps1 stack.env OWNER_NAME '<OWNER_NAME>'
.\compose\scripts\setkv.ps1 stack.env MEDIAHUB_STACK '<full|core>'
.\compose\scripts\setkv.ps1 stack.env MEDIAHUB_VPN '<none|gluetun>'
.\compose\scripts\setkv.ps1 stack.env VPN_SERVICE_PROVIDER '<provider>'
.\compose\scripts\setkv.ps1 stack.env VPN_TYPE '<wireguard|openvpn>'
.\compose\scripts\setkv.ps1 stack.env VPN_SERVER_COUNTRIES '<Country1,Country2>'
.\compose\scripts\setkv.ps1 stack.env DELETE_ANY_REQUIRE_CODE '<true|false>'
.\compose\scripts\setkv.ps1 stack.env DISK_GUARD_GB '<only if changed>'
Get-Content "$HOME\.mediahub\stack.env" | Where-Object { $_ -notmatch '^\s*(#|$)' }
```

Other tunables use the same call: `compose/scripts/setkv.sh stack.env WEEKLY_CLEANUP "SAT 09:00"`,
and so on with the keys in the Phase 1 table. `stack.env` holds no secrets, so printing it is fine.

Check: one `OK setkv <KEY>` per call and no `FAIL` line, and every key appears once in the printed
lines with the intended value.

### 3.3 Alerts (Telegram only)

Set the channel (not a secret):

```bash
compose/scripts/setkv.sh secrets.env NOTIFIER telegram
```

```powershell
.\compose\scripts\setkv.ps1 secrets.env NOTIFIER telegram
```

**ASK THE USER** to do this themselves and reply "done":

```text
1. In Telegram, message @BotFather, send /newbot, follow the prompts, and copy the token it gives you.
2. Send your new bot any message (for example "hi").
3. In a browser, open https://api.telegram.org/bot<TOKEN>/getUpdates (your token in place of
   <TOKEN>) and copy the number at "chat":{"id": ... }. Do not share that URL; it contains the token.
4. Open <MEDIAHUB_HOME>/secrets.env in a text editor and fill in these two lines (no quotes, no spaces):
   TELEGRAM_BOT_TOKEN=<the token>
   TELEGRAM_CHAT_ID=<the number>
   Save. Do not paste either value into this chat.
```

Then check the shape without printing anything:

```bash
grep -cE "^TELEGRAM_BOT_TOKEN=[0-9]+:[A-Za-z0-9_-]{30,}[[:space:]]*\$" ~/.mediahub/secrets.env
grep -cE "^TELEGRAM_CHAT_ID=-?[0-9]+[[:space:]]*\$" ~/.mediahub/secrets.env
```

```powershell
Select-String -Path "$HOME\.mediahub\secrets.env" -Pattern '^TELEGRAM_BOT_TOKEN=\d+:[\w-]{30,}\s*$' -Quiet
Select-String -Path "$HOME\.mediahub\secrets.env" -Pattern '^TELEGRAM_CHAT_ID=-?\d+\s*$' -Quiet
```

Check: `1` and `1` (bash) or `True` and `True`. On `0`/`False`: tell the user which line does not
look right (a token is digits, a colon, then letters; a chat id is a number) and wait again.
Headless and empty: go on with alerts off and list this step in the hand-over (Phase 9).

### 3.4 VPN login (VPN only)

```bash
[ -e ~/.mediahub/vpn.env ] || { cp compose/vpn.env.example ~/.mediahub/vpn.env; chmod 600 ~/.mediahub/vpn.env; }
```

```powershell
if (-not (Test-Path "$HOME\.mediahub\vpn.env")) { Copy-Item compose\vpn.env.example "$HOME\.mediahub\vpn.env" }
```

**ASK THE USER** to open `<MEDIAHUB_HOME>/vpn.env` in a text editor, fill in what their provider
needs for the chosen VPN type (gluetun's page for the provider lists it:
https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers), save, and reply "done".
Openvpn: `OPENVPN_USER`, `OPENVPN_PASSWORD`. Wireguard: `WIREGUARD_PRIVATE_KEY`, and for some
providers `WIREGUARD_ADDRESSES`. Never into the chat.

Check lengths only:

```bash
for k in OPENVPN_USER OPENVPN_PASSWORD WIREGUARD_PRIVATE_KEY WIREGUARD_ADDRESSES; do v="$(sed -n "s/^$k=//p" ~/.mediahub/vpn.env | tail -n1 | tr -d "\r")"; echo "$k: ${#v} chars"; done; unset v
```

```powershell
foreach ($k in 'OPENVPN_USER','OPENVPN_PASSWORD','WIREGUARD_PRIVATE_KEY','WIREGUARD_ADDRESSES') { $v = "$((Select-String -Path "$HOME\.mediahub\vpn.env" -Pattern "^$k=(.*)$" | Select-Object -Last 1).Matches.Groups[1].Value)".Trim(); '{0}: {1} chars' -f $k, $v.Length }; Remove-Variable v
```

Check: the keys the VPN type needs are not `0 chars`. Headless and empty: stop before Phase 5
and report; qBittorrent cannot start without a working VPN.

### 3.5 Media folders and permissions

Fresh install: create the three folders and the recycle bin. REPLACE: create only `Downloads`
and `.recyclebin` if missing; never rename, move or create look-alikes of the user's folders.

```bash
mkdir -p "<MEDIA_ROOT>/Movies" "<MEDIA_ROOT>/TV" "<MEDIA_ROOT>/Downloads" "<MEDIA_ROOT>/.recyclebin"
stat -c "%u:%g %n" "<MEDIA_ROOT>" "<MEDIA_ROOT>/Downloads" ~/.mediahub ~/.mediahub/secrets.env
df -h "<MEDIA_ROOT>"
```

```powershell
'Movies','TV','Downloads','.recyclebin' | ForEach-Object { New-Item -ItemType Directory -Force (Join-Path '<MEDIA_ROOT>' $_) | Out-Null }
Get-ChildItem -Force '<MEDIA_ROOT>'
Get-PSDrive ('<MEDIA_ROOT>'.Substring(0,1)) | Format-Table Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB)}}
```

Check (Linux): every path shows `<PUID>:<PGID>`. The `setup` helper runs as PUID:PGID and must
write `MEDIAHUB_HOME`; the apps must write MEDIA_ROOT. Check (both): free space is above
`DISK_GUARD_GB`; if not, tell the user requests will be refused until space is freed.

On failure (Linux): `mkdir` permission denied, or wrong owner: **ASK THE USER**, then
`sudo mkdir -p ...` and `sudo chown -R <PUID>:<PGID> '<MEDIA_ROOT>' ~/.mediahub`. For a large
existing library (REPLACE), ask before a recursive `chown`; if the old apps ran as the same ids
it is not needed.

### 3.6 Validate

```bash
compose/mh.sh config --services
```

```powershell
.\compose\mh.ps1 config --services
```

Check: full prints `net`, `mediahub`, `sonarr`, `radarr`, `prowlarr`, `qbittorrent`, `plex`,
`bazarr`, `flaresolverr` (any order), plus `gluetun` with the VPN. Core prints `net` and `mediahub`.

On failure: the message names the problem. `set MEDIA_ROOT in stack.env` / `set
VPN_SERVICE_PROVIDER in stack.env`: fix that key (3.2). `MEDIAHUB_STACK=... use full or core`:
typo. `vpn.env: no such file`: do 3.4. `MEDIAHUB_VPN=gluetun needs MEDIAHUB_STACK=full`: back to
the interview.

---

## Phase 4: REPLACE only (before the first start)

Skip this phase unless the user chose REPLACE.

### 4.1 What is imported

| App | Imported? | What comes along |
|---|---|---|
| Sonarr, Radarr | yes | database (series, movies, history, quality profiles), settings, API key, app login |
| Prowlarr | yes | indexers, their settings, API key |
| Plex | yes | the whole config folder: libraries, watch history, accounts, claimed state |
| Bazarr | no | `setup wire` configures a fresh one; subtitle files next to the media stay |
| qBittorrent | no (optional) | let the old downloads finish first; importing carries over in-flight torrents but they keep the old save paths |

Not migrated automatically: Jellyfin, Overseerr, Jellyseerr and any other app (they keep running;
the user re-points them at the new apps afterwards), custom scripts, Sonarr/Radarr notification
connections of type ntfy (`setup wire` removes those; MediaHub sends alerts itself), anything that
used the old apps' hostnames. Mixing (bundled Sonarr with the user's own Plex, say) is not
supported in v0.1.

### 4.2 Versions

Bundled versions:

```bash
grep -E "image:" compose/stack.yml
```

```powershell
Select-String -Path compose\stack.yml -Pattern 'image:'
```

Compare with each old app's version (the `build_version` label from 0.5, the image tag, or
**ASK THE USER** to read it in the app: System, Status).
- Old app newer than the bundled one: do not import it (a database cannot go back). Stop and
  **ASK THE USER**.
- Sonarr older than v4 or Radarr older than v5: the bundled app will migrate the database
  forward on first start. Supported for Sonarr v4 and Radarr v5 and later; for older ones
  **ASK THE USER** whether to try (the backup in 4.4 makes it reversible).

### 4.3 Stop the old apps (never remove them)

Stop first so their databases are consistent for the backup. For containers, also stop them
from coming back after a reboot, and record the old restart policy so `UNINSTALL.md` can restore it.

```bash
bk=~/.mediahub/backups/import-$(date +%Y%m%d); mkdir -p "$bk"
for c in <old containers>; do echo "$c $(docker inspect -f "{{.HostConfig.RestartPolicy.Name}}" "$c")" >> "$bk/restart-policies.txt"; done
docker stop <old containers>
docker update --restart=no <old containers>
```

```powershell
$bk = "$HOME\.mediahub\backups\import-$(Get-Date -Format yyyyMMdd)"; New-Item -ItemType Directory -Force $bk | Out-Null
foreach ($c in '<old1>','<old2>') { "$c $(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' $c)" | Add-Content "$bk\restart-policies.txt" }
docker stop <old containers>
docker update --restart=no <old containers>
```

If the user runs those containers from their own compose file, tell them not to `up` it again.

Native services: **ASK THE USER**, then stop and keep them from starting at boot (reversible):
Linux `sudo systemctl stop <unit> && sudo systemctl disable <unit>`; Windows (admin)
`Stop-Service <name>; Set-Service <name> -StartupType Manual`. Plex on Windows usually runs as
a tray app: ask the user to quit it and turn off its start-at-login option.

Check: run the preflight again (Phase 0): 0.5's container list shows them `Exited`, and 0.6 shows their ports free.

### 4.4 Back up the old configs

The old config folder is the mount whose destination is `/config` in 0.5's inspect output
(true for linuxserver, hotio and the official Plex image). For a native install, **ASK THE
USER** to confirm the folder (often `/var/lib/sonarr`, `/var/lib/radarr`, `/var/lib/prowlarr`,
`/var/lib/plexmediaserver/Library/Application Support/Plex Media Server`); it holds
`config.xml` (or Plex's `Preferences.xml`).

From a container (one line per app):

```bash
bk=~/.mediahub/backups/import-$(date +%Y%m%d)
docker run --rm --volumes-from "<old>:ro" -v "$bk:/bk" alpine:3.20 sh -c "tar -czf /bk/<app>-config.tgz -C /config . && chown $(id -u):$(id -g) /bk/<app>-config.tgz"
```

```powershell
$bk = "$HOME\.mediahub\backups\import-$(Get-Date -Format yyyyMMdd)"
docker run --rm --volumes-from '<old>:ro' -v "${bk}:/bk" alpine:3.20 tar -czf /bk/<app>-config.tgz -C /config .
```

From a host folder: replace `--volumes-from "<old>:ro"` with `-v "<folder>:/src:ro"` and `-C /config` with `-C /src`.

Check each archive holds the database:

```bash
docker run --rm -v ~/.mediahub/backups/import-$(date +%Y%m%d):/bk:ro alpine:3.20 tar -tzf /bk/<app>-config.tgz | grep -cE "config\.xml|Preferences\.xml|\.db\$"
```

```powershell
docker run --rm -v "$HOME\.mediahub\backups\import-$(Get-Date -Format yyyyMMdd):/bk:ro" alpine:3.20 tar -tzf /bk/<app>-config.tgz | Select-String -Pattern 'config\.xml|Preferences\.xml|\.db$' | Measure-Object | ForEach-Object Count
```

Check: each archive counts at least 1. Windows PowerShell 5.1 mangles double quotes inside
arguments to programs such as `docker`; that is why these blocks use none. A Plex folder can be many GB (metadata); check disk space first.

### 4.5 Create the bundled stack without starting it

`up --no-start` creates the containers and empty volumes (`mediahub_sonarr-config` and so on)
without starting any app, so no fresh database can clash with the imported one. It also pulls
the images (a few GB; this takes a while).

```bash
compose/mh.sh up --no-start --quiet-pull > "${TMPDIR:-/tmp}/mh-up.log" 2>&1; rc=$?; tail -n 20 "${TMPDIR:-/tmp}/mh-up.log"; echo "up exit code: $rc"; [ "$rc" = 0 ] && docker volume ls -q --filter name=mediahub_
```

```powershell
.\compose\mh.ps1 up --no-start --quiet-pull 2>&1 | Select-Object -Last 20; "up exit code: $LASTEXITCODE"; docker volume ls -q --filter name=mediahub_
```

Check: `mediahub_sonarr-config`, `mediahub_radarr-config`, `mediahub_prowlarr-config`,
`mediahub_plex-config` (and the others) are listed.

### 4.6 Copy each old config into its volume

Volumes: `mediahub_sonarr-config`, `mediahub_radarr-config`, `mediahub_prowlarr-config`,
`mediahub_plex-config` (and, only if the user opted in, `mediahub_qbittorrent-config`). One line per app:

```bash
docker run --rm --volumes-from "<old>:ro" -v mediahub_<app>-config:/dst alpine:3.20 sh -c "cp -a /config/. /dst/ && chown -R <PUID>:<PGID> /dst && ls -ln /dst | head"
```

```powershell
docker run --rm --volumes-from '<old>:ro' -v mediahub_<app>-config:/dst alpine:3.20 sh -c 'cp -a /config/. /dst/ && chown -R 1000:1000 /dst && ls -ln /dst | head'
```

From a host folder: `-v "<folder>:/src:ro"` and `cp -a /src/. /dst/`. Native Plex on Linux: the
folder is the `Plex Media Server` folder itself, so copy it to
`/dst/Library/Application Support/Plex Media Server/` (`mkdir -p` that path first).

Check: the listing shows `config.xml` and the app's `.db` (Plex: `Library`), owned by PUID:PGID.

Never start the old Plex again once the new one runs: both would claim the same server identity.
Then go on to Phase 5. Phase 5.8 finishes the import (paths, download client, Prowlarr links).

---

## Phase 5: Start

### 5.0 Running `setup` commands

`setup` is MediaHub's helper (`python -m app.setup`). How to run it depends on the mode:

| Mode | Command |
|---|---|
| full or core | `compose/mh.sh run --rm setup <command>` / `.\compose\mh.ps1 run --rm setup <command>` |

Core mode has no bundled apps, so `wire`, `check` and `remap-roots` do not apply there (they would
change the user's own apps). `secrets` only generates `MCP_TOKEN` there (it prints a `NOTE` and
skips the app keys), and `allowed-tools`, `bot-prompt`, `config-diff` and `version` work as in full mode.
Add `-T` after `--rm` whenever you capture the output into a file.

### 5.1 Plex claim now (only if the user chose it in Phase 1)

The code lasts 4 minutes, so pull the images first and claim right before the start.

```bash
ok=0; for n in 1 2 3; do compose/mh.sh pull --quiet --ignore-buildable && { ok=1; break; }; echo "pull failed (try $n of 3)"; [ "$n" -lt 3 ] && sleep $((n * 30)); done
if [ "$ok" = 1 ]; then compose/mh.sh build --quiet; else echo "pull failed 3 times: see 5.2 On failure"; fi
compose/scripts/setkv.sh stack.env PLEX_CLAIM ""
```

```powershell
$ok = $false
foreach ($n in 1..3) { .\compose\mh.ps1 pull --quiet --ignore-buildable; if ($LASTEXITCODE -eq 0) { $ok = $true; break }; "pull failed (try $n of 3)"; if ($n -lt 3) { Start-Sleep (30 * $n) } }
if ($ok) { .\compose\mh.ps1 build --quiet } else { 'pull failed 3 times: see 5.2 On failure' }
.\compose\scripts\setkv.ps1 stack.env PLEX_CLAIM ''
```

**ASK THE USER**: "Sign in at https://plex.tv/claim, copy the code (it starts with `claim-`),
paste it after `PLEX_CLAIM=` in `<MEDIAHUB_HOME>/stack.env`, save, and reply done. It expires in
4 minutes." As soon as they reply, check and start (5.2; the images are already pulled, so it is quick) without delay:

```bash
grep -c "^PLEX_CLAIM=claim-" ~/.mediahub/stack.env
```

```powershell
Select-String -Path "$HOME\.mediahub\stack.env" -Pattern '^PLEX_CLAIM=claim-' -Quiet
```

### 5.2 Start the stack

Pull the images first, on their own, with a retry: a plain `up -d` pulls everything at once and
hits the registry's rate limit more easily. Then start and build MediaHub's own image.

```bash
ok=0; for n in 1 2 3; do compose/mh.sh pull --quiet --ignore-buildable && { ok=1; break; }; echo "pull failed (try $n of 3)"; [ "$n" -lt 3 ] && sleep $((n * 30)); done
[ "$ok" = 1 ] || { echo "pull failed 3 times: see On failure"; false; }
compose/mh.sh up -d --build --quiet-pull > "${TMPDIR:-/tmp}/mh-up.log" 2>&1; rc=$?; tail -n 40 "${TMPDIR:-/tmp}/mh-up.log"; echo "up exit code: $rc"; [ "$rc" = 0 ]
```

```powershell
$ok = $false
foreach ($n in 1..3) { .\compose\mh.ps1 pull --quiet --ignore-buildable; if ($LASTEXITCODE -eq 0) { $ok = $true; break }; "pull failed (try $n of 3)"; if ($n -lt 3) { Start-Sleep (30 * $n) } }
if ($ok) { .\compose\mh.ps1 up -d --build --quiet-pull 2>&1 | Select-Object -Last 40; "up exit code: $LASTEXITCODE" } else { 'pull failed 3 times: see On failure' }
```

The first start pulls about 3 GB of images and builds MediaHub's image; allow 10 minutes.

The block keeps your channel quiet on purpose: `pull --quiet` and `--quiet-pull` drop the per-layer
progress, and only the last 40 lines of `up` are shown (the full output is in the log file the block
names). Do not judge by those 40 lines alone. Check: the `up exit code: 0` line (and `pull` printing
nothing), then 5.3's `mh ps` for the real state of every service. A non-zero exit code is a failure
whatever the tail shows: read more of the log (`tail -n 200 "${TMPDIR:-/tmp}/mh-up.log"`, or run the
failing step again on its own). In the tail, a healthy `up` ends with `Started` (or `Running`/`Healthy`)
for every container and no `Error` line.
(`mediahub-dashboard:local ... pull access denied` is not an error: that image is built locally,
never pulled. `--ignore-buildable` keeps it away; if you still see that line, ignore it.)

On failure:
- A pull fails with `toomanyrequests`, `rate limit`, `retry-after` (for example `allowed:
  44000/minute`), `TLS handshake timeout` or `unexpected EOF`: the registry is throttling or the
  network dropped. Images already pulled are kept, so retry later (wait a few minutes and run the
  block again), or pull the services one at a time: `mh config --services`, then `mh pull <service>`
  for each (`mh pull sonarr`), repeating any that fail. Then run the start line again
  (`mh up -d --build`). Logging in to the registry (`docker login`) raises the limit: **ASK THE
  USER**, never use credentials you were not given.
- `port is already allocated` / `bind: address already in use`: back to 0.6.
- `dependency failed to start: container gluetun is unhealthy`: the VPN did not connect. Read
  `mh logs gluetun` (filtered, see top). Usually a wrong provider name, VPN type or login in
  vpn.env: **ASK THE USER** to check vpn.env, then `mh up -d --force-recreate gluetun qbittorrent`.
- `invalid mount config` or a path error on Windows: MEDIA_ROOT must be an existing folder on
  a local drive, written like `D:/Media`.

### 5.3 Wait until everything runs

```bash
compose/mh.sh ps --format "table {{.Service}}\t{{.State}}\t{{.Status}}"
```

```powershell
.\compose\mh.ps1 ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
```

Check: every service is `running` (gluetun `Up ... (healthy)`). Repeat every 15 seconds for up
to 3 minutes. A service `restarting` or `exited`: read its logs (filtered) and fix what they
say; Linux permission errors on `/config` or `/media` mean PUID/PGID or folder owners are wrong (3.5).

### 5.4 First run (full mode)

Run these in order. qBittorrent must be stopped while `setup secrets` sets its password:
it rewrites its own config when it exits.

```bash
compose/mh.sh stop qbittorrent
compose/mh.sh run --rm setup secrets
compose/mh.sh start qbittorrent
compose/mh.sh up -d --force-recreate mediahub
compose/mh.sh run --rm setup wire
compose/mh.sh run --rm setup check
```

```powershell
.\compose\mh.ps1 stop qbittorrent
.\compose\mh.ps1 run --rm setup secrets
.\compose\mh.ps1 start qbittorrent
.\compose\mh.ps1 up -d --force-recreate mediahub
.\compose\mh.ps1 run --rm setup wire
.\compose\mh.ps1 run --rm setup check
```

No manual sleep is needed anywhere in this sequence:

- `setup secrets` waits by itself, up to 180 seconds (`--wait <seconds>`, default 180), for Sonarr,
  Radarr, Prowlarr and Bazarr to write their API keys, so run it right after 5.3. It does not wait for
  Plex: an unclaimed Plex never gets a token.
- `setup wire` and `setup check` talk to the apps (Sonarr, Radarr, Prowlarr, qBittorrent, Bazarr, Plex),
  never to MediaHub, and read `secrets.env` themselves. So after `up -d --force-recreate mediahub` (which
  only gives MediaHub its new keys) they need no wait either.

All output names keys, never values. Expected:

**`setup secrets`** (exit 0):

```text
NOTE PLEX_TOKEN: Plex is not claimed yet (claim it, then run `setup secrets` again)
qBittorrent: WebUI login set for user admin
updated: BAZARR_API_KEY, MCP_TOKEN, PROWLARR_API_KEY, QBIT_PASS, QBIT_USER, RADARR_API_KEY, SONARR_API_KEY
```

The `NOTE PLEX_TOKEN: Plex is not claimed yet ...` line appears on every run of a full-mode stack that
has no Plex token yet, whether Plex has not written its `Preferences.xml` yet or wrote it without a
token (on a fresh install: the first run). It is information, not a failure (exit stays 0). Once Plex
is claimed (5.6) and `setup secrets` has run again, the line is gone and `PLEX_TOKEN` is in the
`updated` list. Other lines:

| Line | Meaning and fix |
|---|---|
| `MISSING SONARR_API_KEY: sonarr has not written its key yet` | That app is not up yet. Wait, check 5.3, then rerun `setup secrets` (add `--wait 300` on slow machines). |
| `STOP qBittorrent is running` | Run `mh stop qbittorrent`, then `setup secrets` again, then `mh start qbittorrent`. |
| `MISSING qBittorrent config` | qBittorrent never started. `mh start qbittorrent`, wait 30 s, `mh stop qbittorrent`, rerun. |
| `NOTE qBittorrent already has a password` | An imported qBittorrent (REPLACE). **ASK THE USER** to put its username and password in secrets.env lines `QBIT_USER=` and `QBIT_PASS=`. |
| `NOTE PLEX_TOKEN: ...` right after a claim | Plex needs a minute to finish claiming and to write its token. Wait, rerun `setup secrets`. |
| `FAIL cannot write ...` | Linux: MEDIAHUB_HOME or secrets.env is not writable by PUID. Fix owners (3.5). |

**`setup wire`** (exit 0). Each step prints one or more lines:

| Line | Meaning |
|---|---|
| `FIXED <what>` | It changed that (a root folder, a download client, a Prowlarr app, the FlareSolverr proxy, a qBittorrent setting, Bazarr's settings, a Plex library...). Normal on the first run. |
| `OK   <step>` | Already right. |
| `NOTE plex libraries: Plex not claimed: ...` | Plex needs claiming (5.6). Everything else is wired. |
| `NOTE <step>: skipped: the qBittorrent login failed above` | Fix the FAIL above it first. |
| `FAIL <step>: <reason>` | That step failed; the rest still ran. Exit 1. See below. |

`setup wire` only adds what is missing and never adds an indexer. Run it as often as you like.

Common `FAIL` reasons:
- `HTTP 401`: the helper has an old key. Rerun `setup secrets`, then `setup wire`.
- Connection refused / timed out: the app is still starting. Wait a minute, rerun `setup wire`.
- `qBittorrent login refused`: QBIT_USER/QBIT_PASS do not match qBittorrent. On a fresh install
  this means qBittorrent was running while its password was written. **ASK THE USER** to clear
  the value after `QBIT_PASS=` in secrets.env (leave the line), then run:
  `mh stop qbittorrent`, then
  `docker run --rm -v mediahub_qbittorrent-config:/c alpine:3.20 sed -i "/Password_PBKDF2/d" /c/qBittorrent/qBittorrent.conf`,
  then the 5.4 sequence again from `setup secrets`.

**`setup check`** (exit 0): one `OK` line per step, and the Plex `NOTE` if unclaimed. A `DRIFT`
line means something differs from what `wire` sets: run `setup wire` again, then `setup check`.
Still drifting: report the line to the user.

### 5.5 Recycle bin (full mode)

MediaHub's 7-day undo needs Sonarr's and Radarr's recycle bin. `setup wire` (5.4) sets it: when the
app's recycle bin folder is empty (as it is on a new install) it becomes `/media/.recyclebin`, and
files stay in it 7 days. A folder the app already has is kept as it is. Nothing to do by hand.
Verify with the same read-only check as 5.4:

```bash
compose/mh.sh run --rm -T setup check | grep recycle
```

```powershell
.\compose\mh.ps1 run --rm -T setup check | Select-String recycle
```

Check: `OK   sonarr recycle` and `OK   radarr recycle`. A `DRIFT ... no recycle bin folder` line
means `setup wire` has not run (or failed) for that app: run it again. Without the folder deletes
are permanent and `undo_delete` refuses.

### 5.6 Plex claimed later

Any time after 5.4. Two ways to claim; use either.

- **Web page (Linux).** The user opens Plex's own page from a browser on the same network, at
  `http://<server-lan-ip>:32400/web` (find the address as described below), signs in with their Plex account and accepts the server setup. Plex treats a browser
  on the LAN as local, so it lets them claim. On Docker Desktop (Windows, macOS) Plex sees the
  browser as remote and refuses: use `PLEX_CLAIM` there.
- **`PLEX_CLAIM`** (works everywhere): the code from https://plex.tv/claim, valid 4 minutes.

Finding `<server-lan-ip>` (Linux): run `ip -4 -o addr show scope global`. Ignore the Docker bridges:
`docker0`, `br-*`, `veth*`, and addresses in 172.17.0.0 to 172.31.255.255 on them. If exactly one
address is left, that is the LAN address. If none or several are left, **ASK THE USER** for the
server's LAN IP; do not guess. The same rule applies to the Plex address in the hand-over (Phase 9).

**After ANY claim, however it was done, you must run `setup secrets`, then recreate `mediahub`, then
`setup wire`, in that order.** Until you do, MediaHub has no `PLEX_TOKEN` and `wire` creates no
Plex libraries.

Web page path: **ASK THE USER** to claim in the browser and reply "done", then:

```bash
compose/mh.sh run --rm setup secrets
compose/mh.sh up -d --force-recreate mediahub
compose/mh.sh run --rm setup wire
```

```powershell
.\compose\mh.ps1 run --rm setup secrets
.\compose\mh.ps1 up -d --force-recreate mediahub
.\compose\mh.ps1 run --rm setup wire
```

`PLEX_CLAIM` path:

1. Do 5.1 without the pull/build line: make the `PLEX_CLAIM=` line, **ASK THE USER** to paste
   the code, check it.
2. Then at once:

```bash
compose/mh.sh up -d
sleep 60
compose/mh.sh run --rm setup secrets
compose/mh.sh up -d --force-recreate mediahub
compose/mh.sh run --rm setup wire
sed -i "/^PLEX_CLAIM=/d" ~/.mediahub/stack.env && compose/mh.sh up -d   # plain delete: works on GNU and BusyBox sed
```

```powershell
.\compose\mh.ps1 up -d
Start-Sleep 60
.\compose\mh.ps1 run --rm setup secrets
.\compose\mh.ps1 up -d --force-recreate mediahub
.\compose\mh.ps1 run --rm setup wire
$f = "$HOME\.mediahub\stack.env"; [IO.File]::WriteAllLines($f, [string[]](Get-Content $f | Where-Object { $_ -notmatch '^PLEX_CLAIM=' })); .\compose\mh.ps1 up -d
```

Check (both ways): `setup secrets` lists `PLEX_TOKEN` under `updated`; `setup wire` prints
`FIXED plex has no movie library, want 'Movies' at /media/Movies` and the same for `show` (or
`OK   plex libraries` when libraries exist, as after a REPLACE). The last `up -d` recreates Plex
without the claim code; it stays claimed.

### 5.7 Core mode (CONNECT)

There is nothing to wire: the user's apps keep their own settings. MediaHub needs their
addresses and keys, and its own MCP token.

1. Addresses (not secret; you write them). From inside MediaHub's container, an app on the same
   machine is `http://host.docker.internal:<port>`; on another machine use its LAN address.
   `localhost` does not work. Leave out apps they do not run.

```bash
compose/scripts/setkv.sh secrets.env SONARR_URL "http://host.docker.internal:8989"
compose/scripts/setkv.sh secrets.env RADARR_URL "http://host.docker.internal:7878"
compose/scripts/setkv.sh secrets.env PROWLARR_URL "http://host.docker.internal:9696"
compose/scripts/setkv.sh secrets.env PLEX_URL "http://host.docker.internal:32400"
compose/scripts/setkv.sh secrets.env QBIT_URL "http://host.docker.internal:<qbittorrent web ui port>"
compose/scripts/setkv.sh secrets.env BAZARR_URL "http://host.docker.internal:6767"
```

```powershell
$urls = @{ SONARR_URL = 'http://host.docker.internal:8989'; RADARR_URL = 'http://host.docker.internal:7878'; PROWLARR_URL = 'http://host.docker.internal:9696'; PLEX_URL = 'http://host.docker.internal:32400'; QBIT_URL = 'http://host.docker.internal:<qbittorrent web ui port>'; BAZARR_URL = 'http://host.docker.internal:6767' }
foreach ($k in $urls.Keys) { .\compose\scripts\setkv.ps1 secrets.env $k $urls[$k] }
```

2. **ASK THE USER** to paste, into `<MEDIAHUB_HOME>/secrets.env`, the values for:
   `SONARR_API_KEY`, `RADARR_API_KEY`, `PROWLARR_API_KEY`, `BAZARR_API_KEY` (each app:
   Settings, General, API Key), `PLEX_TOKEN` (Plex's support article "Finding an
   authentication token"), `QBIT_USER` and `QBIT_PASS` (their qBittorrent web login). Then check
   lengths (none of the apps they run should be `0 chars`):

```bash
for k in SONARR_API_KEY RADARR_API_KEY PROWLARR_API_KEY BAZARR_API_KEY PLEX_TOKEN QBIT_USER QBIT_PASS MCP_TOKEN; do v="$(sed -n "s/^$k=//p" ~/.mediahub/secrets.env | tail -n1 | tr -d "\r")"; echo "$k: ${#v} chars"; done; unset v
```

```powershell
foreach ($k in 'SONARR_API_KEY','RADARR_API_KEY','PROWLARR_API_KEY','BAZARR_API_KEY','PLEX_TOKEN','QBIT_USER','QBIT_PASS','MCP_TOKEN') { $v = "$((Select-String -Path "$HOME\.mediahub\secrets.env" -Pattern "^$k=(.*)$" | Select-Object -Last 1).Matches.Groups[1].Value)".Trim(); '{0}: {1} chars' -f $k, $v.Length }; Remove-Variable v
```

3. Start: the 5.2 block, then 5.3. Only `net` and `mediahub` run. MediaHub cannot serve `/mcp`
   yet: `MCP_TOKEN` is still empty. Generate it with the same helper as in full mode, then restart
   MediaHub so it reads it:

```bash
compose/mh.sh run --rm setup secrets
compose/mh.sh up -d --force-recreate mediahub
```

```powershell
.\compose\mh.ps1 run --rm setup secrets
.\compose\mh.ps1 up -d --force-recreate mediahub
```

   Expected (exit 0): `NOTE bundled apps: no app config volumes are mounted (MEDIAHUB_STACK=core) ...`
   and `updated: MCP_TOKEN` (`updated: nothing` when the token was already set). Your own apps'
   keys, `PLEX_TOKEN` and the qBittorrent login are not touched: they stay as you pasted them in step 2.

Limits of core mode in v0.1: MediaHub mounts MEDIA_ROOT at `/media`. Undo of deletes and the
"Downloads" checks assume the user's apps see the media under the same `/media/...` paths and
use a recycle bin under `/media`. If their apps use other paths (`/tv`, `/data/media`), tell the
user that `undo_delete` will not work and deletes are only as reversible as their own apps make them.

### 5.8 REPLACE only: finish the import

1. **Paths.** For each old media mount from 0.5 (for example `/srv/media/tv -> /tv`), the files
   are now at `/media/<folder under MEDIA_ROOT>` (here `/media/tv`). Dry run first, show the user:

```bash
compose/mh.sh run --rm setup remap-roots --app sonarr --from /tv --to /media/tv
```

```powershell
.\compose\mh.ps1 run --rm setup remap-roots --app sonarr --from /tv --to /media/tv
```

   Expected: `sonarr: N series under /tv (would become /media/tv)`, up to 10 example lines,
   `would add root folder /media/tv`, `Dry run: nothing changed. ...`. **ASK THE USER** to
   confirm, then run the same command with `--apply`. Expected: `FIXED added root folder ...`,
   `FIXED N series -> ...`, `NOTE files were not moved. ...`. A `FAIL ... did not get the
   expected path` line stops the run with nothing else changed: report it. Repeat for
   `--app radarr` (for example `--from /movies --to /media/movies`).

2. **ASK THE USER** to do these in the web pages (they hold the old hostnames):
   - Sonarr and Radarr, Settings, Media Management, Root Folders: remove the old ones that now
     show as missing (for example `/tv`).
   - Sonarr and Radarr, Settings, Download Clients: delete the old qBittorrent entry (it points at
     the old host). `setup wire` adds a correct one.
   - Prowlarr (http://127.0.0.1:9696), Settings, Apps: edit Sonarr and Radarr: Prowlarr Server
     `http://localhost:9696`, Sonarr Server `http://localhost:8989`, Radarr Server
     `http://localhost:7878`. Keep the API keys. Test, Save.
   - Prowlarr, Settings, Indexers, Indexer Proxies: delete an old FlareSolverr entry if it points
     at another host. `setup wire` adds one at `http://localhost:8191/`.
   - Plex (http://127.0.0.1:32400/web), each library, Edit, Folders: add the `/media/...` folder
     and remove the old one. Plex rescans and keeps the watch history.
   - Other apps (Overseerr, Jellyseerr...): point them at the new Sonarr/Radarr addresses.
3. Then:

```bash
compose/mh.sh run --rm setup wire && compose/mh.sh run --rm setup check
```

```powershell
.\compose\mh.ps1 run --rm setup wire; .\compose\mh.ps1 run --rm setup check
```

Check: no `FAIL`, no `DRIFT`.

---

## Phase 6: Indexers

MediaHub ships Prowlarr with no indexers and never names one. **ASK THE USER**:

```text
Open Prowlarr at http://127.0.0.1:9696 (set its login on the first visit, as with Sonarr).
Indexers > Add Indexer, and add the indexers you want to use; they are your choice.
Prowlarr sends them to Sonarr and Radarr by itself.
If an indexer is behind Cloudflare and fails its test, give it the tag "flare" (MediaHub set up
FlareSolverr for that tag). Reply "done" when you have added at least one.
```

Never suggest, name or add an indexer yourself, even if asked for "good ones". REPLACE: their
imported indexers are already there; this step is only a check.

Optional: if the user wants MediaHub to manage the `flare` tag (or to remove some indexers),
they list the names in stack.env (`PROWLARR_FLARE_INDEXERS`, `PROWLARR_DROP_INDEXERS`) and you run
`setup wire`. You never fill those in yourself.

Check: Phase 8's `sonarr indexers` and `radarr indexers` lines turn `OK` (Prowlarr syncs within a
minute or two). Headless: list this step in the hand-over.

---

## Phase 7: Connect agents

### 7.1 Register the MCP server

Run one line per client the user named in Phase 1. The script reads `MCP_TOKEN` itself and
never prints it.

```bash
compose/scripts/register-mcp.sh claude
compose/scripts/register-mcp.sh codex
compose/scripts/register-mcp.sh json
```

```powershell
.\compose\scripts\register-mcp.ps1 claude
.\compose\scripts\register-mcp.ps1 codex
.\compose\scripts\register-mcp.ps1 json
```

| Client | Expected | Then |
|---|---|---|
| `claude` | `Registered 'mediahub' with Claude Code (user scope) at http://127.0.0.1:8000/mcp. ...` | `claude mcp list` shows `mediahub` as connected. New Claude sessions have the tools. |
| `codex` | `Codex config ... [mcp_servers.mediahub] written ...` and a line about `MEDIAHUB_MCP_TOKEN` | Restart Codex. Linux: open a new login shell first (the token comes from `~/.profile`). |
| `json` | only a path, `<MEDIAHUB_HOME>/mcp-client.json` | Tell the user to merge that file's `mcpServers` entry into their client's MCP config (Cursor and others: see the client's docs). The file contains the token: do not print it or paste it into chat. |

On failure: `MCP_TOKEN is empty`: `setup secrets` has not run (5.4, or step 3 of 5.7 in core
mode). `claude is not on PATH`: Claude Code is not installed for this user.

### 7.2 Install the `media` skill

The skill (`skill/SKILL.md`) teaches the agent how to use the tools. It belongs on the machine
where the agent runs, which may not be this one. **If no agent runs on this machine, skip the
commands below** and put `skill/SKILL.md` (the whole `skill/` folder) in the hand-over (Phase 9)
for the user to install where their agent runs: `~/.claude/skills/media/` for Claude Code, or
`.agents/skills/media/` for `AGENTS.md`-style agents (Codex, Cursor, Pi and others). If an agent
does run here, link the folder so updates come with `git checkout`; if a `media` skill folder
already exists there, **ASK THE USER** first.

Claude Code:

```bash
mkdir -p ~/.claude/skills && ln -sfn "$PWD/skill" ~/.claude/skills/media && ls ~/.claude/skills/media/SKILL.md
```

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills" | Out-Null
New-Item -ItemType Junction -Path "$HOME\.claude\skills\media" -Target (Resolve-Path skill).Path | Out-Null
Test-Path "$HOME\.claude\skills\media\SKILL.md"
```

Other agents that read `AGENTS.md`-style skills (Codex, Cursor, Pi and others): put the same
folder at `.agents/skills/media/` in the user's workspace or home, or wherever that client's docs
say skills live. A client without skills: paste the body of `skill/SKILL.md` into its instructions.

Check: `SKILL.md` exists at the target (`True`), or you skipped it and the hand-over names `skill/SKILL.md`.

### 7.3 Headless chat bots

For a bot that drives headless Claude Code (`claude -p`) from a chat app: render its prompt.
`<REPO>` is the absolute repo path from Phase 2.

First check Phase 1's answer: which machine runs the bot's agent? **If it is this machine**, go on.
**If it is another machine**, the bot cannot reach MediaHub (the MCP listens on 127.0.0.1 only):
do not render the prompt as if all were well. Tell the user the MCP must be reachable from that
machine (`docs/connect-remote.md`, their choice and their risk), list it under "Still to do" in the
hand-over, and render the prompt only after that is settled, with the repo path on the bot's machine.

**If it is this machine but no agent CLI is installed here** (`command -v claude` / `Get-Command claude`
found nothing, and Phase 1 said so): render the prompt anyway only if the user still wants it after
Phase 1's options. The file is just text and harmless. Then list the missing prerequisites in the
hand-over under "Still to do": install Claude Code (or the agent the bot uses) where the bot runs;
register the MCP there (7.1: `register-mcp` fails with `claude is not on PATH` until then); install
the skill (7.2); and, if the bot runs elsewhere, make the MCP reachable. If the user does not want it,
skip the render and say so.

```bash
compose/mh.sh run --rm -T setup allowed-tools
compose/mh.sh run --rm -T setup bot-prompt --repo-dir "<REPO>" --shell bash > ~/.mediahub/bot-prompt.txt && wc -l ~/.mediahub/bot-prompt.txt
```

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
.\compose\mh.ps1 run --rm -T setup allowed-tools
.\compose\mh.ps1 run --rm -T setup bot-prompt --repo-dir '<REPO>' --shell powershell | Set-Content -Encoding utf8 "$HOME\.mediahub\bot-prompt.txt"; (Get-Content "$HOME\.mediahub\bot-prompt.txt").Count
```

Check: `allowed-tools` prints one line of 19 comma-separated `mcp__mediahub__...` names; the prompt
file has well over 100 lines and no `{{`. The prompt is not secret. Tell the user where the file
is and to paste it into their bot's instructions. The bot needs Claude Code (or another agent that
can reach the MCP) on the machine that runs it, with the MCP registered (7.1) and the skill (7.2)
there, and must always pass `--permission-mode dontAsk`.

---

## Phase 8: Verify

### 8.1 Stack health

```bash
compose/scripts/verify-stack.sh --fresh
```

```powershell
.\compose\scripts\verify-stack.ps1 -Fresh
```

Expected: `OK` lines for every service ping, `OK   /media bind mount (read + write)`,
`OK   /mcp requires the token`, then one line per deep check, and a `RESULT:` line that begins
`OK` (exit 0).

`--fresh` / `-Fresh` is for a brand-new install: the checks that cannot pass yet (the indexer rows
and the Plex rows below) print `PENDING` and do not change the exit code, so an automated run does
not read the install as failed. Every other check still prints `FAIL` and exits 1. List each
`PENDING` line under "Still to do" in the hand-over. Run the script without the flag after the user
has added indexers and claimed Plex (and on every later check, such as after an update): then
nothing may be pending, and the result must be `RESULT: all OK`.

On a brand-new install these are `PENDING` with the flag (`FAIL` without it), and clear once done:

| Line | Clears when |
|---|---|
| `FAIL sonarr indexers - 0 synced from Prowlarr` (and radarr) | the user added indexers (Phase 6) and Prowlarr synced |
| `FAIL prowlarr indexers - 0 indexers: add your own in Prowlarr` | the same |
| `FAIL plex - ...` or `FAIL plex claimed` | Plex is claimed (5.6) |
| `FAIL media disk free - N GB (guard 50 GB)` | there is more free space than `DISK_GUARD_GB`. This one is a real `FAIL` even with `--fresh`; rerun with `--allow-low-disk` / `-AllowLowDisk` as well to see the rest |

`media disk free` counts in GB (1000 cubed), while `df -h` and Windows' Explorer count in GiB (1024 cubed),
so the same disk reads about 7% higher here than in 0.7 (100 GiB is 107 GB); that is not a fault.

`OK   Alerts: none - delete_any is disabled ...` is information, not a failure. Any other `FAIL`
or `DOWN` is real: read that service's logs (filtered) and fix it before going on. Core mode
checks only MediaHub and the deep checks against the user's apps.

### 8.2 The MCP answers

No MCP client on this machine? You also must never read the token yourself, so use the built-in
check. It runs inside the `setup` container (`python -m app.setup mcp-ping`), reads `MCP_TOKEN` from
`secrets.env` itself, and talks to `MEDIAHUB_MCP_URL` (default `http://127.0.0.1:8000/mcp`): it
initializes a session, calls the `status` tool, and then calls `/mcp` without the token. It prints
only the lines below, never the token or any header:

```bash
compose/mh.sh run --rm -T setup mcp-ping
```

```powershell
.\compose\mh.ps1 run --rm -T setup mcp-ping
```

Expected (exit 0):

```text
OK mcp status: services up N/M (<names down>), alerts <channel>, delete_any <mode>
OK mcp rejects calls without the token
```

`services up N/M`: M counts the apps MediaHub talks to (Sonarr, Radarr, Prowlarr, Plex, qBittorrent
and Bazarr, so 6 on a full stack), not every container: `net`, FlareSolverr and `mediahub` itself are
not counted, and in core mode only the apps you connected are. `N` below `M` lists the names that are
down. Plex is down until claimed. `alerts` and `delete_any` must match the interview (table below).
Any `FAIL mcp ...` line exits 1: `HTTP 401` or `503` means MediaHub was not recreated after `setup
secrets` (`mh up -d --force-recreate mediahub`); `MCP_TOKEN is empty` means `setup secrets` has not
run; `cannot reach` means the stack is not up (5.3).

With a client, also do the real end-to-end test: start a **new** session of the connected agent and
ask it to call the mediahub `status` tool once. With Claude Code, headless:

```bash
claude -p "Call the mediahub status tool once. Report: which services are up, disk_free_gb, alerts.channel, alerts.delete_any." --permission-mode dontAsk --allowedTools mcp__mediahub__status
```

```powershell
claude -p "Call the mediahub status tool once. Report: which services are up, disk_free_gb, alerts.channel, alerts.delete_any." --permission-mode dontAsk --allowedTools mcp__mediahub__status
```

Expected: every service up (Plex down until claimed), a disk free number, and the alert
settings matching the interview (the same values `mcp-ping` printed):

| Interview | `alerts.channel` | `alerts.delete_any` |
|---|---|---|
| Telegram + code | `telegram` | `code` |
| no alerts + code | `none` | `disabled` (expected; say so) |
| no code (either) | `none` or `telegram` | `no_code` |

On failure: tools missing: the session started before 7.1; start a new one. `401`: the client has
an old token; rerun 7.1. `MCP disabled: no MCP_TOKEN`: MediaHub was not recreated after `setup
secrets`; run `mh up -d --force-recreate mediahub`.

---

## Phase 9: Hand over

Send the user one message. Fill it in; drop lines that do not apply. No secret values.

```text
MediaHub <version> is installed and running.

Running: MediaHub<, Sonarr, Radarr, Prowlarr, qBittorrent<, behind your VPN>, Plex, Bazarr, FlareSolverr>.
Web pages (this machine only):
  MediaHub status  http://127.0.0.1:8000
  Sonarr           http://127.0.0.1:8989
  Radarr           http://127.0.0.1:7878
  Prowlarr         http://127.0.0.1:9696
  qBittorrent      http://127.0.0.1:8090  (login: QBIT_USER / QBIT_PASS in secrets.env)
  Bazarr           http://127.0.0.1:6767
  Plex             http://<this machine's LAN address, see 5.6>:32400/web  (also on your home network)
Media: <MEDIA_ROOT> (Movies, TV, Downloads). Deleted titles sit in <MEDIA_ROOT>/.recyclebin for 7 days.
Settings: <MEDIAHUB_HOME>/stack.env. Secrets: <MEDIAHUB_HOME>/secrets.env (keep it private).
Connected: <Claude Code / Codex / JSON file at ...>. Bot prompt: <MEDIAHUB_HOME>/bot-prompt.txt.

Choices I made (headless run only): <the Phase 1 list, defaults marked; I did not wait for a confirmation>

Still to do: <add indexers in Prowlarr | claim Plex | Telegram token | install skill/SKILL.md where your
  agent runs (none runs here) | install Claude Code where the bot runs (not installed here) | make the
  MCP reachable from the bot's machine | check ports (could not) | set the real git origin so updates work (Phase 2) | none>

Ask your agent things like "get me season 1 of <show>", "what's downloading?", "I need 50 GB back".

Backups: <REPO>/compose/scripts/<backup.sh|backup.ps1> stops the stack for about a minute, saves
every app's data to <MEDIAHUB_HOME>/backups/, and keeps the last 4. I suggest running it weekly;
I can schedule that if you want.
Updates: ask your agent to "update MediaHub" (it follows UPDATE.md).
Remote access (phone, another computer): off. It is your choice and your risk; see docs/connect-remote.md.
```

If the user says yes to scheduled backups, create it then, and only then:

```bash
( crontab -l 2>/dev/null; echo "0 4 * * 0 <REPO>/compose/scripts/backup.sh >> $HOME/.mediahub/backup.log 2>&1" ) | crontab -
```

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"<REPO>\compose\scripts\backup.ps1`""
Register-ScheduledTask -TaskName 'MediaHub backup' -Action $a -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 4am)
```

Done. Do not keep any secret in your notes or memory.
