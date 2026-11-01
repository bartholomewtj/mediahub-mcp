#!/bin/sh
# preflight.sh [media-path]: collect the facts INSTALL.md Phase 0/1 needs. Changes nothing.
#
# Prints labelled sections (`== name ==`); the agent reads them and decides. Exit 0 always, except
# a usage error (exit 2). POSIX sh and POSIX awk only (works on BusyBox). Needs nothing installed:
# a missing tool is a line saying so, never a crash.
#
#   compose/scripts/preflight.sh                  # disk of /
#   compose/scripts/preflight.sh /srv/media       # disk of that path (or its nearest existing parent)
MIN_COMPOSE=2.24.4
PORTS='8000|8989|7878|9696|8090|6767|8191|32400'
APPS='sonarr|radarr|prowlarr|plex|qbittorrent|bazarr|jellyfin|jellyseerr|overseerr|transmission|deluge|sabnzbd|nzbget'

case "${1:-}" in -h|--help) echo "usage: preflight.sh [media-path]"; exit 0 ;; esac
if [ "$#" -gt 1 ]; then echo "usage: preflight.sh [media-path]" >&2; exit 2; fi
media="${1:-/}"
home="${MEDIAHUB_HOME:-$HOME/.mediahub}"

have() { command -v "$1" >/dev/null 2>&1; }
found() { grep -iE "$APPS" || echo '(none found)'; }

echo "== OS =="
kernel="$(uname -sm 2>/dev/null)"; echo "kernel: ${kernel:-unknown}"
name="$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") )"; echo "distro: ${name:-unknown}"
case "$(uname -s 2>/dev/null)" in
  Linux) case "$(uname -m 2>/dev/null)" in x86_64|aarch64|arm64) echo "supported: yes" ;; *) echo "supported: no (only x86_64 and aarch64)" ;; esac ;;
  *) echo "supported: no (MediaHub v0.1 supports Linux and Windows with Docker Desktop only)" ;;
esac

echo "== tools =="
if have git; then echo "git: $(git --version 2>/dev/null)"; else echo "git: absent"; fi
if have curl; then echo "curl: present"; else echo "curl: absent"; fi

echo "== docker =="
if have docker; then
  echo "docker: $(docker --version 2>/dev/null)"
  cv="$(docker compose version --short 2>/dev/null)"
  if [ -n "$cv" ]; then
    ok="$(echo "$cv" | sed 's/^v//; s/-.*//' | awk -v min="$MIN_COMPOSE" '
      BEGIN { split(min, m, "."); }
      { n = split($0, a, "."); r = "ok"
        for (i = 1; i <= 3; i++) { x = a[i] + 0; y = m[i] + 0; if (x > y) break; if (x < y) { r = "old"; break } }
        print r }')"
    if [ "$ok" = ok ]; then echo "compose: $cv (minimum $MIN_COMPOSE: ok)"
    else echo "compose: $cv (minimum $MIN_COMPOSE: TOO OLD, ask the user to upgrade)"; fi
  else echo "compose: absent (the docker compose plugin is missing; minimum $MIN_COMPOSE)"; fi
else
  echo "docker: absent"; echo "compose: absent"
fi

echo "== docker running =="
if have docker; then
  info="$(docker info --format '{{.ServerVersion}} {{.OSType}}' 2>&1)"
  if [ $? -eq 0 ] && [ -n "$info" ]; then
    echo "running: yes (server and OS type: $info)"
    case "$info" in *" linux") ;; *) echo "NOTE the OS type must be linux (Docker Desktop: switch to Linux containers)" ;; esac
  else echo "running: no ($(echo "$info" | head -n1))"; fi
else echo "running: no (docker is absent)"; fi

echo "== disk =="
probe="$media"
while [ ! -d "$probe" ] && [ "$probe" != "/" ] && [ -n "$probe" ]; do probe="$(dirname "$probe")"; done
if [ "$probe" = "$media" ]; then echo "path: $media"; else echo "path: $media (does not exist yet; showing $probe)"; fi
df -h "$probe" 2>&1 | head -n 5

echo "== ports =="
if raw="$(ss -ltn 2>/dev/null)" && [ -n "$raw" ]; then
  how=ss; open="$(echo "$raw" | awk 'NR>1 {n=split($4,a,":"); print a[n]}')"
elif raw="$(netstat -ltn 2>/dev/null)" && [ -n "$raw" ]; then
  how=netstat; open="$(echo "$raw" | awk '$1 ~ /^tcp/ {n=split($4,a,":"); print a[n]}')"
elif raw="$(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null)" && [ -n "$raw" ]; then
  how=/proc/net/tcp
  open="$(echo "$raw" | awk 'function hex(s,  i,n) { n=0; s=toupper(s); for (i=1; i<=length(s); i++) n=n*16+index("0123456789ABCDEF",substr(s,i,1))-1; return n }
    $4 == "0A" { split($2,a,":"); print hex(a[2]) }')"
else how=none; fi
if [ "$how" = none ]; then
  echo "could not check ports (no ss, netstat or /proc/net/tcp)"
else
  echo "checked with: $how (wanted: $(echo "$PORTS" | tr '|' ' '))"
  busy="$(echo "$open" | grep -xE "$PORTS" | sort -un | tr '\n' ' ')"
  if [ -n "$busy" ]; then echo "in use: $busy"; else echo "ports free"; fi
fi

echo "== existing stack =="
echo "-- containers (any state, matched by name or image)"
if have docker && docker info >/dev/null 2>&1; then
  docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>&1 | found
else echo "(docker not usable: this check did not run)"; fi
echo "-- services"
if have systemctl; then systemctl list-units --type=service --all --no-legend 2>/dev/null | found
else echo "(no systemd here; the process list covers it)"; fi
echo "-- processes"
procs="$({ ps -ef 2>/dev/null || ps 2>/dev/null; } | grep -v grep | grep -v preflight | found | cut -c1-140)"
echo "$procs" | head -n 15
n="$(echo "$procs" | wc -l | tr -d ' ')"
[ "$n" -le 15 ] || echo "... ($((n - 15)) more lines)"

echo "== mediahub =="
if [ -e "$home/stack.env" ]; then echo "settings file: present ($home/stack.env)"; else echo "settings file: none"; fi
if have docker && docker info >/dev/null 2>&1; then
  c="$(docker ps -a --filter label=com.docker.compose.project=mediahub --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
  v="$(docker volume ls -q --filter name=mediahub_ 2>/dev/null | tr '\n' ' ')"
  echo "containers: ${c:-none}"; echo "volumes: ${v:-none}"
else echo "containers: unknown (docker not usable)"; echo "volumes: unknown (docker not usable)"; fi

echo "== timezone =="
tz="${TZ:-}"
[ -n "$tz" ] || tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
[ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null)"
[ -n "$tz" ] || tz="$(cat /etc/TZ 2>/dev/null)"
[ -n "$tz" ] || tz="$(readlink /etc/localtime 2>/dev/null | awk -F/zoneinfo/ 'NF > 1 {print $2}')"
tz="${tz#:}"
if [ -n "$tz" ]; then echo "timezone: $tz"
else echo "timezone: unknown"; echo "NOTE ask the user for the IANA name (suggest Etc/UTC)"; fi

echo "== uid/gid =="
echo "user: $(id -un 2>/dev/null) uid: $(id -u 2>/dev/null) gid: $(id -g 2>/dev/null)"
[ "$(id -u 2>/dev/null)" = 0 ] && echo "NOTE running as root: ask the user whether to go on as root"

echo "== claude cli =="
if p="$(command -v claude 2>/dev/null)" && [ -n "$p" ]; then echo "claude: present ($p)"; else echo "claude: absent"; fi
exit 0
