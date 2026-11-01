#!/usr/bin/env bash
# Copy app-generated API keys into <MEDIAHUB_HOME>/secrets.env (default ~/.mediahub), then
# recreate the dashboard container so it uses them. Run after a fresh app volume (Bazarr,
# Sonarr, ...). A wrapper for `mh run --rm setup secrets` (app/setup.py), which prints key
# names only. It also fills an empty MCP_TOKEN, and an empty QBIT_PASS if qBittorrent is
# stopped (see README, First run).
#
#   ./sync-runtime-secrets.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mh="$here/../mh.sh"
secrets="${MEDIAHUB_HOME:-$HOME/.mediahub}/secrets.env"
[ -f "$secrets" ] || { echo "missing $secrets (copy compose/secrets.env.example there first)" >&2; exit 1; }

# MEDIAHUB_STACK=core has no bundled apps and no setup service.
if ! "$mh" config --services 2>/dev/null | grep -qx sonarr; then
    echo "No bundled apps (MEDIAHUB_STACK=core?): nothing to sync."
    exit 0
fi

rc=0
"$mh" run --rm setup secrets || rc=$?
"$mh" up -d mediahub >/dev/null
echo "Dashboard container recreated with the new keys."
exit "$rc"
