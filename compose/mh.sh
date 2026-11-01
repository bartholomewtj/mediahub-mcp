#!/usr/bin/env bash
# Run docker compose for this stack with the per-install settings.
#   ./mh.sh up -d      ./mh.sh ps      ./mh.sh logs -f mediahub
# MEDIAHUB_HOME defaults to ~/.mediahub and holds stack.env (settings) and secrets.env.
# stack.env picks the files: MEDIAHUB_STACK=full|core, MEDIAHUB_VPN=none|gluetun, PLEX_CLAIM.
set -euo pipefail
export MEDIAHUB_HOME="${MEDIAHUB_HOME:-$HOME/.mediahub}"
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$MEDIAHUB_HOME/stack.env" ]; then
    echo "missing $MEDIAHUB_HOME/stack.env - copy compose/stack.env.example there and edit it (or set MEDIAHUB_HOME)" >&2
    exit 1
fi
# Last KEY=value wins; comments and blank lines are ignored.
setting() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MEDIAHUB_HOME/stack.env" | tail -n1 | tr -d '\r"'"'"' ' ; }
stack="$(setting MEDIAHUB_STACK)"; stack="${stack:-full}"
vpn="$(setting MEDIAHUB_VPN)"; vpn="${vpn:-none}"
case "$stack" in full|core) ;; *) echo "MEDIAHUB_STACK=$stack in stack.env: use full or core" >&2; exit 1 ;; esac
case "$vpn" in none|gluetun) ;; *) echo "MEDIAHUB_VPN=$vpn in stack.env: use none or gluetun" >&2; exit 1 ;; esac
files=(-f "$dir/docker-compose.yml")
[ "$stack" = full ] && files+=(-f "$dir/stack.yml")
if [ "$vpn" = gluetun ]; then
    [ "$stack" = full ] || { echo "MEDIAHUB_VPN=gluetun needs MEDIAHUB_STACK=full (it routes the bundled qBittorrent)" >&2; exit 1; }
    files+=(-f "$dir/vpn.yml")
fi
# A one-time Plex claim code adds plex-claim.yml; without it the plex service is unchanged.
if [ "$stack" = full ] && [ -n "$(setting PLEX_CLAIM)" ]; then files+=(-f "$dir/plex-claim.yml"); fi
exec docker compose --env-file "$MEDIAHUB_HOME/stack.env" "${files[@]}" "$@"
