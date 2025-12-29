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
| `register_session(name, machine?, cwd?)` | Register session, get session_id |
| `list_sessions()` | List all active sessions |
| `publish_event(type, payload, session_id?)` | Broadcast event |
| `get_events(since_id?, limit?)` | Poll for events |
| `heartbeat(session_id)` | Keep session alive |

## Design Decisions

- **Polling over push**: MCP is request/response, so sessions poll with `get_events(since_id)`
- **Heartbeat-based cleanup**: Sessions timeout after 2 minutes without heartbeat
- **SQLite persistence**: State persists across restarts in `~/.claude/event-bus.db`
- **Event retention**: Keeps last 1000 events, auto-cleans on write
- **Localhost binding**: Binds to 127.0.0.1 by default for security

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
