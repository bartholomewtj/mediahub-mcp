# Uninstall MediaHub (playbook for agents)

The user asked you to remove MediaHub. Read `AGENTS.md` first. The conventions of `INSTALL.md`
("How to read this file") apply: run from the repo root, bash on Linux, PowerShell on Windows,
`mh` is `compose/mh.sh` / `.\compose\mh.ps1`, **ASK THE USER** lines wait for an answer.

What these steps never do: delete anything under MEDIA_ROOT (movies, TV, downloads and the
recycle bin stay where they are), or touch apps the user ran before MediaHub.

Default: remove the containers and the agent connections, keep the data. Each further removal
needs the user's explicit yes.

## 1. Ask what to remove

**ASK THE USER** (one message):

```text
I will stop and remove MediaHub's containers and disconnect your agents from it. Your media
files are not touched. Also remove:
a) the apps' data (Docker volumes): Sonarr's and Radarr's databases and history, Prowlarr's
   indexers, Plex's libraries and watch history, qBittorrent's torrents, MediaHub's taste ledger
   and logs? This cannot be undone except from a backup. [no]
b) the settings, secrets and backups folder (<MEDIAHUB_HOME>)? [no]
c) the MediaHub code folder (<REPO>)? [no]
<if a REPLACE import was done:> d) restart your old apps that MediaHub replaced? [yes]
```

Headless: the defaults (no, no, no, and yes to d).

## 2. Stop and remove the containers

`mh` needs `stack.env`, so this runs before anything in MEDIAHUB_HOME is removed.

```bash
compose/mh.sh down
```

```powershell
.\compose\mh.ps1 down
```

Check: `docker ps -a --filter label=com.docker.compose.project=mediahub --format '{{.Names}}'`
prints nothing. Volumes are still there: `docker volume ls -q --filter name=mediahub_`.

### 2a. Only if the user said yes to (a)

Confirm twice. First, in plain words: "This deletes Sonarr's and Radarr's databases, Prowlarr's
indexers, Plex's libraries and watch history, qBittorrent's torrent list and MediaHub's taste
ledger. Your media files stay. Shall I delete them?" On yes, offer a last backup
(`compose/scripts/backup.sh --no-stop` / `.\compose\scripts\backup.ps1 -NoStop`; it writes to
MEDIAHUB_HOME/backups, so it is lost too if (b) is yes). Then ask once more: "Last check: delete
the MediaHub volumes now?" Only on a second yes:

```bash
compose/mh.sh down -v && docker volume ls -q --filter name=mediahub_
```

```powershell
.\compose\mh.ps1 down -v; docker volume ls -q --filter name=mediahub_
```

Check: no `mediahub_` volume is listed. If the user set `TASTE_DIR`, the taste ledger is in that
host folder and stays.

The image can go too (it is rebuilt on the next install): `docker image rm mediahub-dashboard:local`.

## 3. Disconnect the agents

Do each that applies; a missing entry is fine.

Claude Code:

```bash
claude mcp remove --scope user mediahub
```

```powershell
claude mcp remove --scope user mediahub
```

Codex: remove the `[mcp_servers.mediahub]` table (the header line and the `url` and
`bearer_token_env_var` lines under it) from `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`).
Leave everything else in the file as it is. Then remove the token variable:

```bash
sed -i "/added by mediahub register-mcp/d" ~/.profile
```

```powershell
[Environment]::SetEnvironmentVariable('MEDIAHUB_MCP_TOKEN', $null, 'User')
```

Other clients: delete `MEDIAHUB_HOME/mcp-client.json` (step 6 does if the folder goes), and
tell the user to remove the `mediahub` entry they pasted into their client's MCP config.

The skill: remove the link or copy. A link must be removed as a link, never recursively (that
would delete the repo's `skill/` folder).

```bash
if [ -L ~/.claude/skills/media ]; then rm ~/.claude/skills/media; else rm -r ~/.claude/skills/media; fi
```

```powershell
$p = "$HOME\.claude\skills\media"
if ((Get-Item $p -Force).LinkType) { (Get-Item $p -Force).Delete() } else { Remove-Item $p -Recurse -Force }
```

Do the same for any `.agents/skills/media` you created at install. Headless bots: tell the user
to delete the MediaHub instructions from their bot.

Scheduled backups, if they were set up:

```bash
crontab -l 2>/dev/null | grep -v "compose/scripts/backup.sh" | crontab -
```

```powershell
Unregister-ScheduledTask -TaskName 'MediaHub backup' -Confirm:$false -ErrorAction SilentlyContinue
```

Check: `claude mcp list` no longer shows `mediahub`; the skill path no longer exists; the repo's
`skill/SKILL.md` still exists.

## 4. Restart the old apps (only after a REPLACE, if the user said yes to d)

The install recorded the old containers' restart policies in
`MEDIAHUB_HOME/backups/import-<date>/restart-policies.txt` (one `<name> <policy>` per line).
MediaHub's containers are gone (step 2), so the ports are free. For each line:

```bash
docker update --restart=<policy> <name> && docker start <name>
```

```powershell
docker update --restart=<policy> <name>; docker start <name>
```

Native services: `sudo systemctl enable --now <unit>` (Linux) or, as admin,
`Set-Service <name> -StartupType Automatic; Start-Service <name>` (Windows). The old apps still
have their old settings. Anything changed in MediaHub's copies since the import is not in them.

## 5. Plex account

If MediaHub's Plex was claimed and (a) removed its data, tell the user the server still appears
in their Plex account until they remove it: https://app.plex.tv, Settings, Authorized Devices.
A Telegram bot made for alerts can be deleted with @BotFather (`/deletebot`).

## 6. MEDIAHUB_HOME (only if the user said yes to b)

It holds `secrets.env`, `vpn.env`, the bot prompt and every backup. Say so, and ask once more if
backups exist (`ls ~/.mediahub/backups` / `Get-ChildItem $HOME\.mediahub\backups`). Then:

```bash
rm -rf ~/.mediahub
```

```powershell
Remove-Item "$HOME\.mediahub" -Recurse -Force
```

Do not read or print anything in it before deleting.

## 7. The code (only if the user said yes to c)

Leave the repo folder first (`cd ~` / `Set-Location $HOME`), then delete `<REPO>`. Never delete
MEDIA_ROOT.

## 8. Tell the user

What was removed, what was kept (media at MEDIA_ROOT; volumes and MEDIAHUB_HOME unless removed),
and how to come back: point an agent at the repo and say "install MediaHub". With the volumes
kept, a reinstall picks up the old app data.
