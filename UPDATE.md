# Update MediaHub (playbook for agents)

The user asked you to update MediaHub. Read `AGENTS.md` first. The conventions of `INSTALL.md`
("How to read this file") apply here too: run from the repo root, one block per command, bash on
Linux, PowerShell on Windows, `mh` is `compose/mh.sh` / `.\compose\mh.ps1`, **ASK THE USER** lines
wait for an answer, never print a secret value, filter raw container logs (not `setup` output). Every bash
block is single-quote-free, so it can go through a `bash -lc '...'` wrapper.

Precondition: `~/.mediahub/stack.env` (Windows `$HOME\.mediahub\stack.env`) exists and you are in
the MediaHub checkout the stack runs from. If not, this is not an update: see `INSTALL.md`.

Find out the mode once; several steps differ for core:

```bash
grep -E "^MEDIAHUB_(STACK|VPN)=" ~/.mediahub/stack.env || echo "MEDIAHUB_STACK=full (default)"
```

```powershell
Select-String -Path "$HOME\.mediahub\stack.env" -Pattern '^MEDIAHUB_(STACK|VPN)='; 'no line above = full'
```

`setup <command>` below means `mh run --rm setup <command>`, in full and in core mode (see
`INSTALL.md` 5.0).

## 1. Back up first

The backup stops the stack for about a minute and saves every volume (app databases, MediaHub's
data and taste ledger) to `MEDIAHUB_HOME/backups/<stamp>/`. It keeps the newest 4.

```bash
compose/scripts/backup.sh
```

```powershell
.\compose\scripts\backup.ps1
```

Check: one line per `mediahub_...` volume with a size, then `Backup in <path>`. Note `<path>`
(the rollback needs it). On failure: do not update. Report the error.

## 2. Current and target version

```bash
git status --porcelain
git fetch --tags --quiet
git remote get-url origin
echo "checkout: $(cat VERSION) at $(git describe --tags --always)"
git tag --sort=-v:refname | grep -v -- - | head -n5
```

```powershell
git status --porcelain
git fetch --tags --quiet
git remote get-url origin
"checkout: $(Get-Content VERSION) at $(git describe --tags --always)"
git tag --sort=-v:refname | Where-Object { $_ -notmatch '-' } | Select-Object -First 5
```

The running image's version: `setup version`.

`git remote get-url origin` must be the real repository URL: the fetch above gets the new releases from
it. A local path, a bundle or a temporary folder (how some installs were first cloned) never gets
new tags: **ASK THE USER** for the real repository URL and run `git remote set-url origin <the real
repo URL>` (`git remote add origin <the real repo URL>` when there is none), then fetch again.

- `git status --porcelain` printed lines: local changes. **ASK THE USER** before going on; never
  reset or stash them on your own.
- Target: the newest tag, unless the user named one. A target older than the current version is
  not an update; that is a rollback (below).
- Already at the target and `setup version` agrees: tell the user MediaHub is up to date. Stop.

## 3. Read what changed

```bash
git show <target>:CHANGELOG.md
git diff <current-tag> <target> -- compose/stack.yml compose/vpn.yml compose/docker-compose.yml | grep -E "^[-+].*image:"
```

```powershell
git show <target>:CHANGELOG.md
git diff <current-tag> <target> -- compose/stack.yml compose/vpn.yml compose/docker-compose.yml | Select-String -Pattern '^[-+].*image:'
```

Read every section newer than the current version. Note:
- **Upgrade notes**: steps this release needs. Do them at the point they say. If one conflicts
  with this playbook, **ASK THE USER**.
- Tool changes (new, renamed or removed MCP tools): the bot prompt needs re-rendering (step 9).
- A changed MCP URL or token handling: re-register clients (step 9).
- Image tags: every `+` tag must be higher than the `-` tag it replaces. If any app's tag goes
  down, stop: never lower a Sonarr, Radarr, Prowlarr, Bazarr or Plex tag.

**ASK THE USER**: summarise what is new and anything they will have to do, and ask to go ahead.
Headless: go ahead unless an upgrade note needs the user.

## 4. Check out the target

```bash
git checkout --quiet <target> && { [ -x compose/mh.sh ] || chmod +x compose/mh.sh compose/scripts/*.sh; } && cat VERSION
```

```powershell
git checkout --quiet <target>; Get-Content VERSION
```

Check: prints the target version.

## 5. Rebuild MediaHub's image

The settings check in step 6 reads the new templates from the new image.

```bash
compose/mh.sh build mediahub
```

```powershell
.\compose\mh.ps1 build mediahub
```

If this (or any `mh` command) stops with `set <KEY> in stack.env`, the release added a required
setting: find it in `compose/stack.env.example` and the upgrade notes, add it (step 6), and rerun.

## 6. New settings

```bash
compose/mh.sh run --rm -T setup config-diff
```

```powershell
.\compose\mh.ps1 run --rm -T setup config-diff
```

It prints key names only, never values:

| Line | What to do |
|---|---|
| `OK   stack.env` / `OK   secrets.env` | Nothing. |
| `NEW stack.env <KEY>` | Read its comment: `grep -n -B4 "^<KEY>=" compose/stack.env.example`. **ASK THE USER** for a value, or use the example's value (headless: the example's value). Write it with `compose/scripts/setkv.sh stack.env <KEY> "<value>"` (PowerShell: `.\compose\scripts\setkv.ps1 stack.env <KEY> '<value>'`; one line, nothing to paste; it prints `OK setkv <KEY>`, and a `FAIL setkv` line means the key was not set). |
| `NEW secrets.env <KEY>` | Never write a value yourself. Add the empty line (`compose/scripts/setkv.sh secrets.env <KEY> ""`, or `setkv.ps1` on Windows). If its comment in `compose/secrets.env.example` says `setup secrets` fills it, step 8 does. If it needs a value only the user has, tell them the file and line to paste it into, and wait. |
| `OPTIONAL ... <KEY>` | A commented-out option in the example. Nothing to do unless the user wants it. |
| `UNKNOWN ... <KEY>` | The new templates no longer know this key (renamed or dropped). Check the upgrade notes. Leave it unless they say otherwise; it is harmless. |

Never change an existing value in `secrets.env`.

## 7. Start the new version

`--build` rebuilds MediaHub; image tags that changed are pulled (they only ever go up).

```bash
compose/mh.sh up -d --build --quiet-pull > "${TMPDIR:-/tmp}/mh-up.log" 2>&1; rc=$?; tail -n 40 "${TMPDIR:-/tmp}/mh-up.log"; echo "up exit code: $rc"
compose/mh.sh ps --format "table {{.Service}}\t{{.State}}\t{{.Status}}"
[ "$rc" = 0 ]
```

```powershell
.\compose\mh.ps1 up -d --build --quiet-pull 2>&1 | Select-Object -Last 40; "up exit code: $LASTEXITCODE"; .\compose\mh.ps1 ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
```

The block shows only the last 40 lines of `up` and no pull progress, so an agent channel is not
flooded. Judge by the `up exit code` line (0) and the `ps` table, not by the tail text; a non-zero
code is a failure whatever the tail shows (the full output is in the log file the block names).

Check: every service `running` within 3 minutes (gluetun `healthy`). An app that migrates its
database may take longer on its first start. With the VPN, if qBittorrent has no network after
gluetun was recreated: `mh up -d --force-recreate gluetun qbittorrent`.

On failure: read the failing service's raw logs (`mh logs`, filtered as in `INSTALL.md`; `setup` output needs no filter). A database migration error: go to Rollback.

## 8. Re-run setup (full mode)

```bash
compose/mh.sh run --rm setup secrets
compose/mh.sh run --rm setup wire
compose/mh.sh run --rm setup check
```

```powershell
.\compose\mh.ps1 run --rm setup secrets
.\compose\mh.ps1 run --rm setup wire
.\compose\mh.ps1 run --rm setup check
```

Expected: `secrets` prints `updated: nothing` (exit 0). `wire` prints `OK` lines (a new release
may add `FIXED` lines for new wiring). `check` prints only `OK` lines.

- `secrets` prints `updated: <names>`: run `mh up -d --force-recreate mediahub` so MediaHub uses them.
- `secrets` prints `STOP qBittorrent is running`: only happens when `QBIT_PASS` is empty. Run
  `mh stop qbittorrent`, `setup secrets`, `mh start qbittorrent`, then
  `mh up -d --force-recreate mediahub`. Otherwise never stop qBittorrent for an update.
- Other lines: see the tables in `INSTALL.md` 5.4.

Core mode: skip this step.

## 9. Verify and refresh the agents

1. `compose/scripts/verify-stack.sh` / `.\compose\scripts\verify-stack.ps1` (without `--fresh`),
   and the client-less MCP check `mh run --rm -T setup mcp-ping` (it must print `OK mcp status: ...`
   and `OK mcp rejects calls without the token`; the token is never shown), exactly as `INSTALL.md`
   Phase 8. Everything that passed before the update must pass now.
2. The skill: if `~/.claude/skills/media` (or the other agents' copy) is a link to `skill/`, it is
   already current. If it is a copy, copy `skill/` over it again.
3. Headless bots: if step 3 found tool changes, render the prompt again (`INSTALL.md` 7.3) and
   tell the user to replace their bot's instructions with it. Compare `setup allowed-tools` with
   the string in the old prompt if unsure.
4. MCP registration: only if the changelog says the URL or token handling changed, rerun
   `compose/scripts/register-mcp.sh` / `.\compose\scripts\register-mcp.ps1` for each client.

## 10. Tell the user

Version before and after, what changed for them, anything they still have to do, and where the
pre-update backup is.

---

## Rollback

Only when the new version does not work and the fix is not clear. **ASK THE USER** first.

1. Check out the version that worked: `git checkout --quiet <previous-tag>`.
2. Find apps whose image tag would go down:

```bash
git diff <target> <previous-tag> -- compose/stack.yml compose/vpn.yml | grep -E "^[-+].*image:"
```

```powershell
git diff <target> <previous-tag> -- compose/stack.yml compose/vpn.yml | Select-String -Pattern '^[-+].*image:'
```

   For each such app the new version has already migrated its database forward, and the old
   image cannot read it. Lowering its tag is allowed only together with restoring its volume
   from the step 1 backup. **ASK THE USER**: the restore loses everything that app recorded
   since the backup (new titles, history).

3. Restore each such volume (example: Sonarr; `<stamp>` is the folder from step 1):

```bash
compose/mh.sh stop sonarr
docker run --rm -v mediahub_sonarr-config:/dst -v "$HOME/.mediahub/backups/<stamp>:/src:ro" alpine:3.20 sh -c "rm -rf /dst/* && tar -xzf /src/mediahub_sonarr-config.tgz -C /dst"
```

```powershell
.\compose\mh.ps1 stop sonarr
docker run --rm -v mediahub_sonarr-config:/dst -v "$HOME\.mediahub\backups\<stamp>:/src:ro" alpine:3.20 sh -c 'rm -rf /dst/* && tar -xzf /src/mediahub_sonarr-config.tgz -C /dst'
```

   Restore `mediahub_mediahub-data` the same way if the changelog says MediaHub's own data
   format changed.

4. Start and check: `mh up -d --build`, then steps 8 and 9.
5. Tell the user what was rolled back, what was restored from the backup, and what was lost.
