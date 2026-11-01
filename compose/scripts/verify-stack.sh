#!/usr/bin/env bash
# Health check against the compose stack (published on localhost).
# Pings every bundled service (MEDIAHUB_STACK=full), checks the /media mount,
# then runs the server's deep checks (GET /api/checks), which also cover your
# own apps in core mode. Exit 1 on any failure.
#
# Follows stack.env (or the environment): MEDIAHUB_PORT_BIND gives the dashboard address
# (default 127.0.0.1:8000) and MEDIAHUB_PREFIX the container name prefix. MEDIAHUB_URL overrides
# the address.
#
#   ./verify-stack.sh
#   ./verify-stack.sh --allow-low-disk   # don't fail on the disk-guard check
#   ./verify-stack.sh --fresh            # a brand-new install: the checks that cannot pass yet (indexers
#                                        # synced, Plex claimed, Plex libraries) print PENDING and do not
#                                        # affect the exit code; everything else still FAILs
set -uo pipefail
allow=0; fresh=0
for a in "$@"; do
    case "$a" in
        --allow-low-disk) allow=1 ;;
        --fresh) fresh=1 ;;
        *) echo "unknown option: $a (use --allow-low-disk, --fresh)" >&2; exit 2 ;;
    esac
done
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail=0
# Settings: the environment first, then <MEDIAHUB_HOME>/stack.env (last KEY=value wins, like mh.sh).
stack_env="${MEDIAHUB_HOME:-$HOME/.mediahub}/stack.env"
setting() {
    if [ -n "${!1:-}" ]; then printf '%s' "${!1}"; return; fi
    [ -f "$stack_env" ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$stack_env" | tail -n1 | tr -d '\r"'"'"' '
    return 0
}
prefix="$(setting MEDIAHUB_PREFIX)"
base="$(setting MEDIAHUB_URL)"
if [ -z "$base" ]; then   # the dashboard address from MEDIAHUB_PORT_BIND ("127.0.0.1:8100", "0.0.0.0:8100" or "8100")
    bind="$(setting MEDIAHUB_PORT_BIND)"; bind="${bind:-127.0.0.1:8000}"
    case "$bind" in *:*) bhost="${bind%:*}"; bport="${bind##*:}" ;; *) bhost=127.0.0.1; bport="$bind" ;; esac
    case "$bhost" in ''|0.0.0.0|'[::]'|::) bhost=127.0.0.1 ;; esac
    base="http://$bhost:$bport"
fi
base="${base%/}"
box="${prefix}mediahub"                         # the dashboard container

check() {   # name url
    local code
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$2" || true)"
    # 401/403 still proves the service is up
    if [ "$code" -ge 200 ] 2>/dev/null && { [ "$code" -lt 400 ] || [ "$code" = 401 ] || [ "$code" = 403 ]; }; then
        echo "OK   $1 ($2) [$code]"
    else
        echo "DOWN $1 ($2)"; fail=1
    fi
}

if "$here/../mh.sh" config --services 2>/dev/null | grep -qx sonarr; then
    # Media mount visible and writable from a container that uses it.
    probe="$(docker exec "${prefix}sonarr" sh -c 'ls /media >/dev/null && touch /media/Downloads/.write-test && rm /media/Downloads/.write-test && echo ok' 2>&1)"
    if [[ "$probe" == *ok* ]]; then echo 'OK   /media bind mount (read + write)'; else echo "FAIL /media bind mount: $probe"; fail=1; fi

    check sonarr       http://127.0.0.1:8989/ping
    check radarr       http://127.0.0.1:7878/ping
    check prowlarr     http://127.0.0.1:9696/ping
    check qbittorrent  http://127.0.0.1:8090/
    check plex         http://127.0.0.1:32400/identity
    check flaresolverr http://127.0.0.1:8191/health
    check bazarr       http://127.0.0.1:6767/api/system/ping
fi
check mediahub "$base/api/config"

# MCP answers, and refuses requests without the token.
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST "$base/mcp" || true)"
if [ "$code" = 401 ]; then echo 'OK   /mcp requires the token'; else echo "FAIL /mcp returned $code"; fail=1; fi

# Deep checks from inside the stack (authenticated reads of every service).
# The dashboard container's Python parses the JSON, so the host needs no jq/python.
py='
import json, sys
allow, fresh, bad = sys.argv[1] == "1", sys.argv[2] == "1", 0
PENDING = ("sonarr indexers", "radarr indexers", "prowlarr indexers", "plex", "plex claimed")
for r in json.load(sys.stdin)["checks"]:
    name = r["check"]
    tag = "OK  " if r["ok"] else ("WARN" if allow and name == "media disk free" else
                                  "PENDING" if fresh and (name in PENDING or name.startswith("plex library")) else "FAIL")
    print(tag, r["check"], ("- " + r["detail"]) if r.get("detail") else "")
    bad |= tag == "FAIL"
sys.exit(bad)'
if body="$(curl -sf -m 90 "$base/api/checks")"; then
    printf '%s' "$body" | docker exec -i "$box" python -c "$py" "$allow" "$fresh" || fail=1
else
    echo "FAIL /api/checks"; fail=1
fi

if [ "$fail" = 1 ]; then echo 'RESULT: FAIL'; exit 1; fi
if [ "$fresh" = 1 ]; then echo 'RESULT: OK (fresh install: PENDING items are for the user: indexers, Plex claim)'; else echo 'RESULT: all OK'; fi
exit 0
