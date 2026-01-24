# Tailscale Setup for Event Bus

This guide covers running event-bus across multiple machines using Tailscale for secure authentication.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Your Tailnet                            │
│                                                             │
│  ┌──────────────┐         ┌──────────────────────────────┐  │
│  │ Mac (client) │         │ Server (e.g., speck-vm)      │  │
│  │              │         │                              │  │
│  │ Claude Code  │ ──────► │ tailscale serve (:443)       │  │
│  │     ↓        │  HTTPS  │       ↓                      │  │
│  │ MCP client   │         │ event-bus (:8080 localhost)  │  │
│  └──────────────┘         └──────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**How it works:**
1. Event-bus runs on localhost:8080 (not exposed to network)
2. `tailscale serve` proxies HTTPS requests to localhost:8080
3. Tailscale injects identity headers (`Tailscale-User-Login`) into requests
4. Event-bus middleware rejects requests without identity headers
5. Only devices on your Tailnet can connect

---

## Server Setup

Run these steps on the machine hosting event-bus (e.g., a VM, homelab server).

### 1. Install event-bus

```bash
git clone https://github.com/evansenter/claude-event-bus.git
cd claude-event-bus
make install
```

This installs the service (LaunchAgent on macOS, systemd on Linux) bound to `localhost:8080`.

### 2. Set up Tailscale serve

```bash
# Proxy HTTPS traffic to localhost:8080
tailscale serve --bg 8080
```

Verify it's running:
```bash
tailscale serve status
# Should show: https://HOSTNAME.TAILNET.ts.net -> localhost:8080
```

Note your server's Tailscale URL (e.g., `https://speck-vm.tailac7b3c.ts.net`).

### 3. Verify the setup

```bash
# From the server itself (should work - has identity headers via loopback)
curl https://$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

---

## Client Setup (Remote Machines)

Run these steps on each machine that will connect to the event-bus (e.g., your laptop).

### Prerequisites

- Tailscale installed and connected to the same Tailnet as the server
- Claude Code installed

### 1. Install CLI only (no local server)

```bash
git clone https://github.com/evansenter/claude-event-bus.git
cd claude-event-bus
make install-cli
```

### 2. Configure Claude Code MCP

```bash
# Remove any existing event-bus config
claude mcp remove event-bus 2>/dev/null

# Add the remote server (replace URL with your server's Tailscale URL)
claude mcp add --transport http --scope user event-bus https://YOUR-SERVER.TAILNET.ts.net/mcp
```

### 3. Set environment variable

Add to your shell profile (`~/.zshrc`, `~/.bashrc`, or `~/.extra`):

```bash
export EVENT_BUS_URL="https://YOUR-SERVER.TAILNET.ts.net/mcp"
```

Or add to Claude Code settings (`~/.claude/settings.json`):

```json
{
  "env": {
    "EVENT_BUS_URL": "https://YOUR-SERVER.TAILNET.ts.net/mcp"
  }
}
```

### 4. Restart Claude Code

```bash
# Exit and restart Claude Code to pick up the new MCP config
```

### 5. Verify connection

```bash
# Test via CLI
event-bus-cli sessions

# Or in Claude Code, the event-bus MCP tools should now work
```

---

## Local-Only Setup

If you only need event-bus on a single machine (no remote access):

```bash
git clone https://github.com/evansenter/claude-event-bus.git
cd claude-event-bus
make install
```

This binds to `localhost:8080` only. No Tailscale setup needed.

To disable authentication for local-only use, set in your service config:
```bash
EVENT_BUS_AUTH_DISABLED=1
```

---

## Troubleshooting

### "Unauthorized" errors

The server requires Tailscale identity headers. This means:
- Requests must go through `tailscale serve` (not direct to localhost:8080)
- Your device must be on the same Tailnet

Check your URL is using the Tailscale hostname, not an IP address.

### Connection refused

1. Verify tailscale serve is running: `tailscale serve status`
2. Verify event-bus is running: `systemctl --user status claude-event-bus` (Linux) or check LaunchAgent (macOS)
3. Verify Tailscale connectivity: `tailscale ping YOUR-SERVER`

### MCP tools not appearing in Claude Code

1. Check MCP config: `claude mcp list`
2. Restart Claude Code after config changes
3. Check for errors in Claude Code's MCP connection status

### Testing authentication

```bash
# This should fail (no identity headers)
curl http://localhost:8080/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# Expected: {"error": "Unauthorized", "message": "Tailscale identity required"}

# This should work (through tailscale serve)
curl https://YOUR-SERVER.TAILNET.ts.net/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# Expected: tools list response
```

---

## Applying to Other MCP Servers

This pattern works for any MCP server (e.g., session-analytics):

1. **Server side:** Run `tailscale serve --bg <port>` to proxy to your server
2. **Add middleware:** Check for `Tailscale-User-Login` header, reject if missing
3. **Client side:** Update MCP config to use `https://HOSTNAME.TAILNET.ts.net/path`

The key insight: `tailscale serve` acts as a reverse proxy that handles TLS and injects identity headers, so your server code stays simple.
