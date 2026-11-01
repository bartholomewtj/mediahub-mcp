#!/usr/bin/env bash
# Tar every named volume into <MEDIAHUB_HOME>/backups/<stamp>/ (default ~/.mediahub).
# Stops the stack first so the SQLite files are consistent, then starts it
# again. Keeps the newest --keep backups (default 4). Run it from cron.
#
#   ./backup.sh              # stop, back up, start
#   ./backup.sh --no-stop    # hot copy (WAL files included; fine for a quick safety net)
#   ./backup.sh --keep 8
set -euo pipefail
stop=1; keep=4
while [ $# -gt 0 ]; do
    case "$1" in
        --no-stop) stop=0 ;;
        --keep) keep="${2:?--keep needs a number}"; shift ;;
        *) echo "usage: backup.sh [--no-stop] [--keep N]" >&2; exit 2 ;;
    esac
    shift
done
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mh="$here/../mh.sh"
root="${MEDIAHUB_HOME:-$HOME/.mediahub}/backups"
dest="$root/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$dest"

if [ "$stop" = 1 ]; then
    "$mh" stop >/dev/null
    trap '"$mh" start >/dev/null' EXIT
fi

for v in $(docker volume ls --format '{{.Name}}' | grep "^${COMPOSE_PROJECT_NAME:-mediahub}_" || true); do
    MSYS_NO_PATHCONV=1 docker run --rm -v "$v:/src:ro" -v "$dest:/dst" alpine:3.20 \
        sh -c "tar -czf /dst/$v.tgz -C /src . && chown $(id -u):$(id -g) /dst/$v.tgz" \
        || { echo "backup of $v failed" >&2; exit 1; }
    printf '%-32s %8s\n' "$v" "$(du -h "$dest/$v.tgz" | cut -f1)"
done
echo "Backup in $dest"

for old in $(ls -1 "$root" | grep -E '^[0-9]{8}-[0-9]{6}$' | sort -r | tail -n +"$((keep + 1))"); do
    rm -rf "${root:?}/$old"; echo "pruned $old"
done
