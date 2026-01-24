# Tailscale Setup for Agent Event Bus

This guide covers running agent-event-bus across multiple machines using Tailscale for secure authentication.

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
│  │ MCP client   │         │ agent-event-bus (:8080)      │  │
│  └──────────────┘         └──────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**How it works:**
1. Agent-event-bus runs on localhost:8080 (not exposed to network)
2. `tailscale serve` proxies HTTPS requests to localhost:8080
3. Tailscale injects identity headers (`Tailscale-User-Login`) into requests
4. Agent-event-bus middleware rejects requests without identity headers
5. Only devices on your Tailnet can connect

---

## Server Setup

Run these steps on the machine hosting agent-event-bus (e.g., a VM, homelab server).

### 1. Install agent-event-bus

```bash
git clone https://github.com/evansenter/agent-event-bus.git
cd agent-event-bus
make install-server
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

Run these steps on each machine that will connect to the agent-event-bus (e.g., your laptop).

### Prerequisites

- Tailscale installed and connected to the same Tailnet as the server
- Claude Code installed

### 1. Install client (CLI + MCP config)

```bash
git clone https://github.com/evansenter/agent-event-bus.git
cd agent-event-bus
make install-client REMOTE_URL=https://YOUR-SERVER.TAILNET.ts.net/mcp
```

This installs the CLI and configures Claude Code MCP to use the remote server.

### 2. Set environment variable

Add to your shell profile (`~/.zshrc`, `~/.bashrc`, or `~/.extra`):

```bash
export AGENT_EVENT_BUS_URL="https://YOUR-SERVER.TAILNET.ts.net/mcp"
```

Or add to Claude Code settings (`~/.claude/settings.json`):

```json
{
  "env": {
    "AGENT_EVENT_BUS_URL": "https://YOUR-SERVER.TAILNET.ts.net/mcp"
  }
}
```

### 3. Restart Claude Code

```bash
# Exit and restart Claude Code to pick up the new MCP config
```

### 4. Verify connection

```bash
# Test via CLI
agent-event-bus-cli sessions

# Or in Claude Code, the agent-event-bus MCP tools should now work
```

---

## Local-Only Setup

If you only need agent-event-bus on a single machine (no remote access):

```bash
git clone https://github.com/evansenter/agent-event-bus.git
cd agent-event-bus
make install-server
```

This binds to `localhost:8080` only. No Tailscale setup needed.

To disable authentication for local-only use, set in your service config:
```bash
AGENT_EVENT_BUS_AUTH_DISABLED=1
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
2. Verify agent-event-bus is running: `systemctl --user status agent-event-bus` (Linux) or check LaunchAgent (macOS)
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

### Implementation Guide

#### 1. Add the middleware class

```python
# middleware.py (or wherever your middleware lives)
import logging

logger = logging.getLogger(__name__)

class TailscaleAuthMiddleware:
    """ASGI middleware that requires Tailscale identity headers."""

    TAILSCALE_USER_HEADER = b"tailscale-user-login"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        tailscale_user = headers.get(self.TAILSCALE_USER_HEADER)

        if not tailscale_user:
            logger.warning(
                f"Rejected unauthenticated request to {scope.get('path', '/')} "
                f"from {scope.get('client', ('unknown',))[0]}"
            )
            await self._send_unauthorized(send)
            return

        await self.app(scope, receive, send)

    async def _send_unauthorized(self, send):
        body = b'{"error": "Unauthorized", "message": "Tailscale identity required"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })
```

#### 2. Integrate in create_app()

```python
# server.py
import os
from .middleware import TailscaleAuthMiddleware, RequestLoggingMiddleware

def create_app():
    app = mcp.http_app(stateless_http=True)

    # Logging middleware (innermost)
    app = RequestLoggingMiddleware(app)

    # Auth middleware (outermost) - unless disabled
    if not os.environ.get("AGENT_EVENT_BUS_AUTH_DISABLED", "").lower() in ("1", "true"):
        app = TailscaleAuthMiddleware(app)

    return app
```

#### 3. Disable auth in tests

```python
# tests/conftest.py
def pytest_configure(config):
    # ... other setup ...
    os.environ["AGENT_EVENT_BUS_AUTH_DISABLED"] = "1"
```

#### 4. Set up tailscale serve on the server

```bash
# If your server runs on port 8081
tailscale serve --bg 8081
```

#### 5. Update client configs

```bash
# MCP config
claude mcp add --transport http --scope user session-analytics https://YOUR-SERVER.TAILNET.ts.net/mcp

# Environment variable
export SESSION_ANALYTICS_URL="https://YOUR-SERVER.TAILNET.ts.net/mcp"
```
