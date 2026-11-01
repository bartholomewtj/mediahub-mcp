# Reaching MediaHub from another device

MediaHub's MCP server listens on `http://127.0.0.1:8000/mcp`, on the machine that runs it and
nowhere else, and every request needs the bearer token (`MCP_TOKEN` in
`MEDIAHUB_HOME/secrets.env`). That is deliberate: whoever reaches it with the token can request,
grab and delete media.

**The package never opens it up for you.** An installer or agent must not do any of the
following on its own. They are your choice, made with the risks below in mind.

## Before you choose

- **What sits on port 8000.** The same port serves `/mcp` (token required) and the read-only
  status page and its JSON (`/`, `/api/...`), which have **no login**. They show titles, downloads,
  disk space and service health, never keys. Anything that exposes port 8000 exposes those pages
  to the same audience unless you limit it to `/mcp`.
- **The Host check.** The MCP endpoint only accepts requests whose `Host` header is `127.0.0.1` or
  `localhost`, plus the names you list in `MCP_ALLOWED_HOSTS` (protection against DNS rebinding).
  A request that arrives with another host name is refused with HTTP 421 "Invalid Host header",
  even with the right token. Either list your proxy's or Tailscale name (next section) or have the
  proxy send `Host: 127.0.0.1:8000` upstream. A browser `Origin` header is checked the same way.
- **`MCP_ALLOWED_HOSTS`.** In `MEDIAHUB_HOME/stack.env`, a comma-separated list of extra host names
  (optionally with a port, or `:*` for any port) that may appear in the `Host` header, added to the
  localhost names. For example `MCP_ALLOWED_HOSTS=mediahub.example.lan,mediahub.example.lan:*,mymachine.tailnet-name.ts.net`.
  List a name both bare (the default HTTPS port, as in `https://mediahub.example.lan/mcp`) and with
  `:*` if clients also use an explicit port. Each name is also accepted as an `http://` or `https://`
  `Origin`. Then `mh up -d --force-recreate mediahub` (the setting is read at start). It only decides
  which names the check accepts: it opens no port, and the token is still required.
- **The token is the only lock on `/mcp`.** Treat it like a password. If it leaks, empty `MCP_TOKEN=`
  in secrets.env, run `mh run --rm setup secrets` (a new one is generated),
  `mh up -d --force-recreate mediahub`, and register your clients again.
- **Never** change `MEDIAHUB_PORT_BIND` to `0.0.0.0` or a public address, or forward port 8000 on
  your router. That puts the status pages on the network with no login and the MCP behind a single
  token over plain HTTP.

## Option 1: SSH tunnel (simplest, nothing stays open)

What it exposes: nothing new. A device with SSH access to the MediaHub machine forwards its own
local port to MediaHub for as long as the tunnel runs.

```bash
ssh -N -L 8000:127.0.0.1:8000 you@mediahub-machine
```

Then point the MCP client on that device at `http://127.0.0.1:8000/mcp` with the same token. The
Host header is `127.0.0.1:8000`, so the Host check passes.

Risk: anyone who can SSH into the machine can reach MediaHub, which is already true of the whole
machine. Only works where you can run SSH (laptops; not most phone apps or cloud services).

## Option 2: Tailscale (your devices only)

What it exposes: MediaHub, over HTTPS, to the devices on your tailnet. Not the internet.

```bash
tailscale serve --bg http://127.0.0.1:8000
```

This gives `https://<machine>.<tailnet>.ts.net/` (see `tailscale serve status`); the MCP URL is
that plus `/mcp`. The command syntax differs between Tailscale versions: check `tailscale serve --help`.

**Host check:** `tailscale serve` passes the `.ts.net` host name through, so add it to
`MCP_ALLOWED_HOSTS` in stack.env (for example `MCP_ALLOWED_HOSTS=<machine>.<tailnet>.ts.net`; see
above) and recreate the `mediahub` container. No proxy that rewrites `Host` is needed. Test with
your client before relying on it.

Risk: every device and user on your tailnet (and anyone you share the machine with) reaches the
status pages and, with the token, the tools. Use Tailscale ACLs to limit it.

**Never use `tailscale funnel`** for MediaHub unless you understand it makes the URL public on the
internet: then anyone can read the status pages and try tokens against `/mcp`.

## Option 3: Reverse proxy with TLS and its own login

What it exposes: whatever the proxy allows, to whoever can reach the proxy. Put it on your LAN or
tailnet, not the internet, unless you know exactly what you are doing.

Caddy example: only `/mcp` passes, with HTTPS and a second password in front of the token. Either
put `mediahub.example.lan` in `MCP_ALLOWED_HOSTS` (and drop the `header_up` line), or keep the
`header_up` line, which rewrites the Host header to one MediaHub already accepts.

```caddyfile
mediahub.example.lan {
    handle /mcp* {
        basic_auth {
            # caddy hash-password --plaintext '<a long password>'
            agent <bcrypt hash>
        }
        reverse_proxy 127.0.0.1:8000 {
            header_up Host 127.0.0.1:8000
            flush_interval -1
        }
    }
    handle {
        respond 404
    }
}
```

Notes:
- MCP over HTTP streams responses (server-sent events), so the proxy must not buffer them
  (`flush_interval -1` in Caddy, `proxy_buffering off;` in nginx) and must allow long requests
  (indexer searches can take two minutes).
- Many MCP clients can send only one `Authorization` header, and it already carries MediaHub's
  token. A second login in front then has to be something else your client supports (client
  certificates, a VPN, an IP allow list). Check before you add one.
- Risk: a mistake in the proxy config exposes the status pages or `/mcp` more widely than you
  meant. Test from outside before you rely on it.

## Phones and cloud services

MCP clients on a phone or in the cloud need a URL they can reach, so they need one of the options
above (in practice Tailscale on the phone, or a proxy reachable from where the client runs).

claude.ai custom connectors (and other hosted connectors) require OAuth. MediaHub v0.1 supports only
a bearer token, so it cannot be added there. A chat bot that runs headless Claude Code on the
MediaHub machine (`INSTALL.md` 7.3) needs none of this: it talks to `127.0.0.1` directly.
