# Security policy

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Please do not open a public issue or pull
request for a security problem, and do not include real secrets in the report.

You will get an answer through that report. Fixes ship in the next release.

## Scope

In scope:

- The MCP endpoint and its bearer-token authentication, including Host and Origin checks.
- Handling of secrets: anything that logs, prints, returns or writes a token, key or password where it
  should not.
- Fencing of untrusted text: release names, torrent names and *arr error text reaching an agent as
  instructions, a tool parameter, or an alert.
- Destructive tools: `delete`, `remove_blocklist`, `delete_any`, `undo_delete`, their guards, the
  confirmation code, and the bounded mode.
- The install, update and uninstall playbooks and `compose/` scripts doing something unsafe by default.

Out of scope: vulnerabilities in the applications MediaHub runs (Plex, Sonarr, Radarr, Prowlarr,
qBittorrent, Bazarr, FlareSolverr, gluetun; report those upstream), and problems that need you to expose
the MCP port beyond `127.0.0.1` against the advice in `docs/connect-remote.md`.

## Supported versions

Only the latest release receives fixes.
