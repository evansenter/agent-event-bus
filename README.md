# Claude Event Bus

MCP server for cross-session Claude Code communication and coordination.

## Overview

When running multiple Claude Code sessions (via `/parallel-work` or separate terminals), each session is isolated. This MCP server provides an event bus for sessions to:

- **Announce presence** - Know what other sessions are active
- **Broadcast status** - Share progress updates and task completion
- **Coordinate work** - Signal dependencies and handoffs

## Architecture

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  CC Session 1   │  │  CC Session 2   │  │  CC Session 3   │
│  (dotfiles)     │  │  (rust-genai)   │  │  (gemicro)      │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────┐
              │   claude-event-bus                │
              │   localhost:8080                  │
              └───────────────────────────────────┘
```

## Installation

```bash
# Clone and install
git clone https://github.com/evansenter/claude-event-bus.git
cd claude-event-bus
pip install -e .

# Run the server
python -m event_bus.server
```

## Add to Claude Code

```bash
# Add globally (available in all projects)
claude mcp add --transport http --scope user event-bus http://localhost:8080/mcp
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `register_session` | Register this session with the event bus |
| `list_sessions` | List all active sessions |
| `publish_event` | Broadcast an event to all sessions |
| `get_events` | Poll for events since a given ID |
| `heartbeat` | Keep session alive (call periodically) |

## Event Types

```
session_registered   - "Session X started in /path/to/worktree"
session_heartbeat    - "Session X still alive"
task_started         - "Session X working on: implement auth"
task_completed       - "Session X finished: PR #42 created"
help_needed          - "Session X stuck: test failure in auth.rs"
```

## Roadmap

- [x] MVP: In-memory event bus for single machine
- [ ] Persistence: SQLite backend for crash recovery
- [ ] Multi-machine: Tailscale support for cross-device
- [ ] File locking: Conflict detection for shared files

## Related

- [RFC: Global event bus](https://github.com/evansenter/dotfiles/issues/41)
- [`/parallel-work` command](https://github.com/evansenter/dotfiles)
