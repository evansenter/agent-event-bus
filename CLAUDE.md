# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Overview

MCP server providing an event bus for cross-session Claude Code communication. Sessions can register, publish events, and poll for updates.

## Commands

```bash
# Install in development mode
pip install -e .

# Run the server
python -m event_bus.server
# or
event-bus

# Run tests
pytest
```

## Architecture

- `src/event_bus/server.py` - Main MCP server with all tools
- `src/event_bus/storage.py` - SQLite storage backend
- FastMCP framework for MCP protocol handling
- Uvicorn for HTTP transport

## MCP Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, machine?, cwd?, pid?)` | Register session, get session_id |
| `list_sessions()` | List all active sessions |
| `publish_event(type, payload, session_id?, channel?)` | Publish event to channel |
| `get_events(since_id?, limit?, session_id?)` | Poll for events (filtered by subscriptions) |
| `unregister_session(session_id)` | Clean up session on exit |
| `notify(title, message, sound?)` | Send system notification |

## Channel-Based Messaging

Events can be targeted to specific channels:

| Channel | Who receives |
|---------|--------------|
| `all` | Everyone (default, broadcast) |
| `session:{id}` | Direct message to one session |
| `repo:{name}` | All sessions in that repo |
| `machine:{name}` | All sessions on that machine |

Sessions are auto-subscribed based on their attributes - no explicit subscribe needed.

```python
# Broadcast (default)
publish_event("status", "Done!", channel="all")

# Direct message
publish_event("help", "Review auth.ts?", channel="session:abc123")

# Repo-scoped
publish_event("api_ready", "API merged", channel="repo:my-project")
```

## Design Decisions

- **Polling over push**: MCP is request/response, so sessions poll with `get_events(since_id)`
- **Session cleanup**: 7-day heartbeat timeout + PID liveness checks for local sessions
- **Auto-heartbeat**: `publish_event` and `get_events` auto-refresh heartbeat
- **SQLite persistence**: State persists across restarts in `~/.claude/event-bus.db`
- **Event retention**: Keeps last 1000 events, auto-cleans on write
- **Localhost binding**: Binds to 127.0.0.1 by default for security
- **Implicit subscriptions**: No explicit subscribe - sessions auto-subscribed to relevant channels

## Configuration

```bash
# Override database path (default: ~/.claude/event-bus.db)
EVENT_BUS_DB=/path/to/db.sqlite event-bus

# Enable request/response logging (for dev mode)
DEV_MODE=1 event-bus
```

## Future Work

- Tailscale support for multi-machine
- File locking tools for conflict detection
- SSE streaming for lower latency
