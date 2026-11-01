#!/usr/bin/env bash
# Register the MediaHub MCP server with an MCP client. The token is read from
# <MEDIAHUB_HOME>/secrets.env (default ~/.mediahub) into a variable and goes only into the
# client's own config. It is never printed.
#
#   register-mcp.sh claude   Claude Code, user scope (replaces an existing 'mediahub')
#   register-mcp.sh codex    Codex CLI: [mcp_servers.mediahub] in ~/.codex/config.toml (or $CODEX_HOME)
#                            + MEDIAHUB_MCP_TOKEN exported from ~/.profile
#   register-mcp.sh json     writes <MEDIAHUB_HOME>/mcp-client.json (mode 600) for any other client;
#                            prints only its path
#   Options: --url http://127.0.0.1:8000/mcp   --no-persist-env (codex: leave ~/.profile alone)
set -euo pipefail

die() { echo "register-mcp: $*" >&2; exit 1; }
usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

client="${1:-}"; [ $# -gt 0 ] && shift
url="http://127.0.0.1:8000/mcp"; persist=1
while [ $# -gt 0 ]; do
    case "$1" in
        --url) [ $# -ge 2 ] || die "--url needs a value"; url="$2"; shift 2 ;;
        --url=*) url="${1#--url=}"; shift ;;
        --no-persist-env) persist=0; shift ;;
        *) usage ;;
    esac
done
case "$client" in claude|codex|json) ;; *) usage ;; esac
[[ "$url" =~ ^https?://[^[:space:]\"\\]+$ ]] || die "--url must be an http(s) URL"

home="${MEDIAHUB_HOME:-$HOME/.mediahub}"
secrets="$home/secrets.env"
[ -f "$secrets" ] || die "missing $secrets - run the first-run setup first (README, First run)"
token="$(sed -n 's/^[[:space:]]*MCP_TOKEN[[:space:]]*=[[:space:]]*//p' "$secrets" | tail -n1 | tr -d '\r"'"'"' ')"
[ -n "$token" ] || die "MCP_TOKEN is empty in $secrets - run: mh.sh run --rm setup secrets"

case "$client" in
    claude)
        command -v claude >/dev/null 2>&1 || die "claude is not on PATH"
        claude mcp remove --scope user mediahub >/dev/null 2>&1 || true   # not registered is fine
        if out="$(claude mcp add --transport http --scope user mediahub "$url" --header "Authorization: Bearer $token" 2>&1)"; then rc=0; else rc=$?; fi
        out="${out//"$token"/***}"
        [ -z "$out" ] || echo "$out"
        [ "$rc" -eq 0 ] || die "claude mcp add failed (exit $rc)"
        echo "Registered 'mediahub' with Claude Code (user scope) at $url. The token is in Claude's own config only."
        ;;
    codex)
        cfgdir="${CODEX_HOME:-$HOME/.codex}"
        cfg="$cfgdir/config.toml"
        mkdir -p "$cfgdir"
        [ -f "$cfg" ] || : > "$cfg"
        tmp="$(mktemp)"
        trap 'rm -f "$tmp"' EXIT
        # Drop any existing [mcp_servers.mediahub] table (and sub-tables), keep everything else, append ours.
        awk -v url="$url" '
            BEGIN { blank = 0; skip = 0; sep = 0; n = 0 }
            /^[[:space:]]*\[/ { skip = ($0 ~ /^[[:space:]]*\[mcp_servers\."?mediahub"?(\]|\.)/) }
            skip { sep = 1; next }
            /^[[:space:]]*$/ { blank++; next }
            {
                if (blank > 0) { for (i = 0; i < blank; i++) print "" }
                else if (sep && n > 0 && $0 ~ /^[[:space:]]*\[/) print ""
                blank = 0; sep = 0; n++; print
            }
            END {
                if (n > 0) print ""
                print "[mcp_servers.mediahub]"
                print "url = \"" url "\""
                print "bearer_token_env_var = \"MEDIAHUB_MCP_TOKEN\""
            }' "$cfg" > "$tmp"
        if cmp -s "$tmp" "$cfg"; then did="already up to date"; else cat "$tmp" > "$cfg"; did="written"; fi
        echo "Codex config $cfg : [mcp_servers.mediahub] $did (url $url, token from env var MEDIAHUB_MCP_TOKEN)."
        if [ "$persist" -eq 0 ]; then
            echo "Skipped ~/.profile (--no-persist-env): export MEDIAHUB_MCP_TOKEN yourself."
        else
            profile="$HOME/.profile"
            if grep -qs 'MEDIAHUB_MCP_TOKEN' "$profile"; then
                echo "$profile already sets MEDIAHUB_MCP_TOKEN; left as it is."
            else
                # The line reads the token from secrets.env at login, so no second copy of it is written.
                line='[ -r "SECRETS" ] && export MEDIAHUB_MCP_TOKEN="$(grep -m1 "^MCP_TOKEN=" "SECRETS" | cut -d= -f2- | tr -d "\r\"")" # added by mediahub register-mcp'
                printf '%s\n' "${line//SECRETS/$secrets}" >> "$profile"
                echo "Added an export of MEDIAHUB_MCP_TOKEN to $profile. Start a new login shell (or: . ~/.profile) before running Codex."
            fi
        fi
        ;;
    json)
        [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "MCP_TOKEN has characters this script will not write into JSON; build the snippet by hand"
        dest="$home/mcp-client.json"
        ( umask 077
          printf '{\n  "mcpServers": {\n    "mediahub": {\n      "type": "http",\n      "url": "%s",\n      "headers": {\n        "Authorization": "Bearer %s"\n      }\n    }\n  }\n}\n' "$url" "$token" > "$dest" )
        chmod 600 "$dest" 2>/dev/null || true
        echo "$dest"
        ;;
esac
