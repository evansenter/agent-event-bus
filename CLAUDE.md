# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

MCP server providing an event bus for cross-session Claude Code communication. Sessions can register, publish events, and poll for updates.

## Commands

```bash
# Full installation (venv + deps + LaunchAgent + CLI + MCP)
make install

# Uninstall everything
make uninstall

# Install with dev dependencies (for development)
make dev

# Run all quality gates (format, lint, test)
make check

# Run individual checks
make fmt      # Check formatting
make lint     # Run linter
make test     # Run tests

# Run a single test
pytest tests/test_server.py::TestRegisterSession -v

# Run in dev mode (foreground, auto-reload)
./scripts/dev.sh
```

**Note**: `make install` and `make uninstall` are idempotent - safe to run multiple times.

## Architecture

```
src/event_bus/
├── server.py      # MCP tools and server entry point
├── storage.py     # SQLite storage backend (Session, Event, SQLiteStorage)
├── helpers.py     # Utility functions (notifications, repo extraction)
├── middleware.py  # Request logging middleware for dev mode
├── session_ids.py # Human-readable ID generation (Docker-style names)
├── cli.py         # CLI wrapper for shell scripts/hooks
└── guide.md       # Usage guide served as MCP resource
```

- FastMCP framework for MCP protocol handling
- Uvicorn for HTTP transport

## MCP Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, machine?, cwd?, client_id?)` | Register session, get session_id + cursor for polling |
| `list_sessions()` | List active sessions (most recently active first) |
| `publish_event(event_type, payload, session_id?, channel?)` | Publish event to channel |
| `get_events(cursor?, limit?, session_id?, order?)` | Get events (order="desc" by default; use "asc" when polling) |
| `unregister_session(session_id)` | Clean up session on exit |
| `notify(title, message, sound?)` | Send system notification |

## MCP Resources

| Resource | Purpose |
|----------|---------|
| `event-bus://guide` | Usage guide and best practices for CC sessions |

**Important**: Keep `usage_guide()` in `server.py` up to date when changing APIs. This is how CC sessions learn to use the event bus effectively.

## API Consistency (CLI ↔ MCP)

The CLI and MCP tools expose the same functionality with consistent naming:

| CLI Command | MCP Tool | Notes |
|-------------|----------|-------|
| `register` | `register_session` | CLI short form, MCP descriptive |
| `unregister` | `unregister_session` | CLI short form, MCP descriptive |
| `sessions` | `list_sessions` | Noun (CLI) = verb+noun (MCP) |
| `publish` | `publish_event` | CLI short form, MCP descriptive |
| `events` | `get_events` | Noun (CLI) = verb+noun (MCP) |
| `notify` | `notify` | Identical |

**Conventions:**
- CLI uses kebab-case args (`--session-id`), MCP uses snake_case params (`session_id`)
- CLI uses short forms for commands, MCP uses descriptive `verb_noun` pattern
- CLI query commands use nouns (`sessions`, `events`); action commands use verbs (`publish`, `notify`)

**CLI-only features** (not in MCP):
- `--timeout` - HTTP request timeout
- `--track-state` - File-based cursor persistence
- `--json` - JSON output format
- `--exclude-types` - Event type filtering

**When modifying the API**: Update both CLI (`cli.py`) and MCP (`server.py`) together. Ensure parameter names match (kebab ↔ snake conversion) and document changes in both `guide.md` and this file.

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

## Event Type Conventions

Use consistent event types for discoverability across sessions.

| Event Type | Description | Example Payload |
|------------|-------------|-----------------|
| `rfc_created` | New RFC issue created | `"RFC created: #48 - Event bus integration"` |
| `rfc_responded` | Response posted to RFC | `"RFC response posted: #48"` |
| `parallel_work_started` | New worktree/session started | `"Started parallel work: issue-48 - Implement event bus"` |
| `ci_completed` | CI finished (pass or fail) | `"CI passed on PR #42"` |
| `message` | Generic message/announcement | `"Auth feature done, you can integrate now"` |
| `help_needed` | Request for assistance | `"Need review on auth.ts approach"` |
| `task_completed` | Significant task finished | `"Feature X is done and merged"` |

**Naming conventions:**
- Use `snake_case` for event types
- Be specific: `rfc_created` not just `created`
- Include context in payload: what happened and relevant identifiers (PR#, issue#)

## Design Decisions

- **Polling over push**: MCP is request/response, so sessions poll with `get_events(cursor)`
- **Session cleanup**: 24-hour heartbeat timeout + client liveness checks for local sessions
- **Auto-heartbeat**: `publish_event` and `get_events` auto-refresh heartbeat
- **SQLite persistence**: State persists across restarts in `~/.claude/event-bus.db`
- **Localhost binding**: Binds to 127.0.0.1 by default for security
- **Implicit subscriptions**: No explicit subscribe - sessions auto-subscribed to relevant channels
- **Human-readable IDs**: Session IDs use Docker-style names (e.g., `brave-tiger`) instead of UUIDs
- **Client deduplication**: Sessions are deduplicated by `(machine, client_id)` - allows session resumption across restarts

## CLI Wrapper

For shell scripts and hooks (e.g., CC SessionStart/SessionEnd hooks):

```bash
# Register session
event-bus-cli register --name "my-feature" --client-id "my-client-123"

# Unregister session
event-bus-cli unregister --session-id abc123

# List sessions
event-bus-cli sessions

# Publish event
event-bus-cli publish --type "task_done" --payload "Finished" --channel "repo:my-project"

# Get events (basic - newest first by default)
event-bus-cli events --session-id abc123

# Get events with JSON output (for scripting)
event-bus-cli events --json --limit 10 --exclude-types session_registered,session_unregistered

# Get events with automatic state tracking (ideal for hooks)
event-bus-cli events --track-state ~/.local/state/claude/cursor --json --timeout 200

# Poll for new events chronologically (use with cursor)
event-bus-cli events --cursor abc123 --order asc --session-id mysession

# Send notification
event-bus-cli notify --title "Done" --message "Build complete"
```

### Events Command Options

| Option | Description |
|--------|-------------|
| `--cursor ID` | Get events after this cursor (opaque string) |
| `--session-id ID` | Your session ID for channel filtering |
| `--limit N` | Maximum events to return |
| `--exclude-types T1,T2` | Comma-separated event types to filter out |
| `--timeout MS` | Request timeout in milliseconds (default: 10000) |
| `--track-state FILE` | Read/write cursor for incremental polling |
| `--json` | Output as JSON with `events` array and `next_cursor` |
| `--order asc\|desc` | Event ordering: desc (default, newest first) or asc (oldest first) |

## Configuration

```bash
# Override database path (default: ~/.claude/event-bus.db)
EVENT_BUS_DB=/path/to/db.sqlite event-bus

# Enable request/response logging (for dev mode)
DEV_MODE=1 event-bus

# Custom notification icon (requires terminal-notifier on macOS)
EVENT_BUS_ICON=/path/to/icon.png event-bus
```

## Notifications

On macOS, notifications use terminal-notifier (if installed) with osascript fallback:
- **terminal-notifier**: Supports custom icons via `EVENT_BUS_ICON` env var
- **osascript**: Built-in fallback, no custom icon support

Install terminal-notifier: `brew install terminal-notifier`

### Icon

A pixel art Birman cat icon is included in `assets/` and set by default in the LaunchAgent and dev.sh.

To regenerate or customize the icon:
```bash
cd scripts/icon-gen
GEMINI_API_KEY=key cargo run --release -- "your custom prompt"
cargo run --bin smart-crop   # AI-powered tight crop
cargo run --bin remove-bg    # Remove background
```

