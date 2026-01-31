# Agent Event Bus

MCP server for cross-session Claude Code communication and coordination.

## Overview

When running multiple Claude Code sessions (in separate terminals or worktrees), each session is isolated. This MCP server provides an event bus for sessions to:

- **Announce presence** - Know what other sessions are active
- **Broadcast status** - Share progress updates and task completion
- **Coordinate work** - Signal dependencies and handoffs
- **Send notifications** - System notifications with custom icon support

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  CC Session 1   │  │  CC Session 2   │  │  CC Session 3   │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         └────────────────────┼────────────────────┘
                              ▼
              ┌───────────────────────────────────┐
              │   agent-event-bus (localhost)    │
              └───────────────────────────────────┘
```

## Installation

### Server

Run the event bus server on this machine:

```bash
git clone https://github.com/evansenter/agent-event-bus.git
cd agent-event-bus
make install-server
```

Installs: venv, LaunchAgent (auto-start), CLI (`~/.local/bin/agent-event-bus-cli`), MCP server pointing to localhost.

### Client

Connect to an event bus running on another machine (e.g., via Tailscale):

```bash
git clone https://github.com/evansenter/agent-event-bus.git
cd agent-event-bus
make install-client REMOTE_URL=https://your-server.tailnet.ts.net/agent-event-bus/mcp
```

Installs CLI and configures MCP to point to the remote server. Add the URL to your shell profile:

```bash
# In your shell profile (~/.extra, ~/.zshrc, etc.)
export AGENT_EVENT_BUS_URL="https://your-server.tailnet.ts.net/agent-event-bus/mcp"
```

### PATH

Ensure `~/.local/bin` is in PATH: `export PATH="$HOME/.local/bin:$PATH"`

## MCP Tools

| Tool | Description |
|------|-------------|
| `register_session` | Register session, get session_id + cursor |
| `list_sessions` | List active sessions |
| `list_channels` | List channels with subscriber counts |
| `publish_event` | Publish event to channel |
| `get_events` | Poll for events (use `resume=True` for incremental) |
| `unregister_session` | Clean up on exit |
| `notify` | System notification |
| `register_webhook` | Register HTTP endpoint for push notifications |
| `list_webhooks` | List registered webhooks |
| `unregister_webhook` | Remove a webhook |

## Channels

Events include channel metadata for context. **All sessions see all events** (broadcast model).

| Channel | Context |
|---------|---------|
| `all` | General broadcast (default) |
| `session:{id}` | Direct message (triggers notification) |
| `repo:{name}` | Repository-specific |
| `machine:{name}` | Machine-specific |

## CLI

For shell scripts and hooks:

```bash
# Register (returns session_id)
SESSION_ID=$(agent-event-bus-cli register --name "my-feature" --client-id "$$" --json | jq -r .session_id)

# Publish and notify
agent-event-bus-cli publish --type "done" --payload "Finished" --channel "repo:my-project"
agent-event-bus-cli notify --title "Build" --message "Complete" --sound

# Poll for events (incremental)
agent-event-bus-cli events --session-id "$SESSION_ID" --resume --order asc

# Cleanup
agent-event-bus-cli unregister --session-id "$SESSION_ID"
```

## Webhooks

Push events to HTTP endpoints instead of polling. Webhooks are called asynchronously when events are published.

### MCP

```python
# Register a webhook for all events
register_webhook(url="https://your-server.com/events")

# Filter by channel (prefix matching supported)
register_webhook(url="https://...", channel="session:")

# Filter by event type
register_webhook(url="https://...", event_types=["task_completed", "help_needed"])

# With HMAC signature verification
register_webhook(url="https://...", secret="your-shared-secret")

# List and manage
list_webhooks()
unregister_webhook(webhook_id=1)
```

### CLI

```bash
# Register
agent-event-bus-cli webhook register --url https://your-server.com/events
agent-event-bus-cli webhook register --url https://... --channel "session:" --secret "my-secret"

# List
agent-event-bus-cli webhook list

# Remove
agent-event-bus-cli webhook unregister 1
```

### Payload

Webhooks receive a POST with JSON body:

```json
{
  "event_id": 123,
  "event_type": "task_completed",
  "payload": "Finished building feature X",
  "session_id": "abc-123",
  "timestamp": "2026-01-31T08:00:00",
  "channel": "repo:my-project"
}
```

If a `secret` is configured, requests include an `X-Event-Bus-Signature` header:

```
X-Event-Bus-Signature: sha256=<hmac-sha256-hex>
```

Verify by computing HMAC-SHA256 of the raw request body with your secret.

## Multi-Machine Setup

Run one server, connect from multiple machines via Tailscale (or any VPN).

**Server machine:**

```bash
make install-server
```

Edit `~/Library/LaunchAgents/com.evansenter.agent-event-bus.plist`, add to `EnvironmentVariables`:
```xml
<key>HOST</key>
<string>0.0.0.0</string>
```

Then restart: `make restart`

> **Note:** `make install-server` overwrites the plist. To persist this setting, also edit `scripts/com.evansenter.agent-event-bus.plist` (uncomment the HOST lines).

**Client machines:**

```bash
make install-client REMOTE_URL=https://your-server.tailnet.ts.net/agent-event-bus/mcp
```

Then add to your shell profile:
```bash
export AGENT_EVENT_BUS_URL="https://your-server.tailnet.ts.net/agent-event-bus/mcp"
```

New Claude Code sessions will have full `mcp__agent-event-bus__*` tool access to the central server.

## Development

```bash
make dev          # Install with dev dependencies
./scripts/dev.sh  # Run in foreground with auto-reload
make check        # Format + lint + test
```

## Notifications

Requires `terminal-notifier` for custom icon: `brew install terminal-notifier`

## Data

All data in `~/.claude/contrib/agent-event-bus/`: `data.db`, `agent-event-bus.log`, `agent-event-bus.err`

## Related

- [claude-session-analytics](https://github.com/evansenter/claude-session-analytics) - Historical session analysis
- [dotfiles](https://github.com/evansenter/dotfiles) - `/parallel-work` command and hooks
