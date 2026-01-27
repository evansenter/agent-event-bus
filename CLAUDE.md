# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

MCP server for cross-session Claude Code communication. Sessions register, publish events, and poll for updates.

**Related**: [claude-session-analytics](https://github.com/evansenter/claude-session-analytics) shares design patterns.

## DATABASE PROTECTION

**The database at `~/.claude/contrib/agent-event-bus/data.db` contains irreplaceable event history.**

### NEVER:
- Add code that deletes the database file
- Add `DROP TABLE` or `DELETE FROM` for user data
- Add "reset" or "clear all" functionality

### Safe operations:
- `make uninstall` - Preserves database
- `make install-server` - Idempotent, restarts service automatically
- Schema migrations via `@migration` decorator (increment `SCHEMA_VERSION` when adding)

### Before schema changes:
```bash
cp ~/.claude/contrib/agent-event-bus/data.db ~/.claude/contrib/agent-event-bus/data.db.backup-$(date +%Y%m%d-%H%M%S)
```

## Commands

```bash
make install-server  # Server: runs event bus locally (idempotent, restarts service)
make install-client REMOTE_URL=...  # Client: connects to remote server (idempotent)
make uninstall  # Remove everything (preserves DB)
make dev        # Install with dev dependencies
make check      # Format + lint + test
make restart    # Lightweight service restart (no dependency sync)
./scripts/dev.sh  # Dev mode (foreground, auto-reload)
```

**When to restart**: Code changes to `server.py`, `storage.py`, `helpers.py` require `make install-server` (or `make restart`). `guide.md` is read fresh each request. Dev mode auto-reloads.

## Testing

```bash
make check                                      # Full suite (format + lint + test)
pytest tests/test_server.py -v                  # Single file
pytest tests/test_server.py::TestRegisterSession -v  # Single class
pytest tests/test_server.py::TestRegisterSession::test_register_new_session -v  # Single test
pytest -k "heartbeat" -v                        # Tests matching pattern
```

## Architecture

```
src/agent_event_bus/
├── server.py      # MCP tools and entry point
├── storage.py     # SQLite backend (Session, Event, SQLiteStorage)
├── helpers.py     # Notifications, repo extraction
├── middleware.py  # Request logging → ~/.claude/contrib/agent-event-bus/agent-event-bus.log
├── session_ids.py # Docker-style display_id generation
├── cli.py         # CLI wrapper for shell scripts
└── guide.md       # Usage guide (agent-event-bus://guide resource)
```

## MCP Tools

`register_session`, `list_sessions`, `list_channels`, `publish_event`, `get_events`, `unregister_session`, `notify`

**Usage guide**: `agent-event-bus://guide` resource. Keep it updated when changing APIs.

### Tool Docstrings

**Keep docstrings minimal** - tool definitions consume tokens in every conversation (~200 tokens per verbose tool). `guide.md` is the canonical reference for detailed documentation; keep it updated with verbose explanations, usage patterns, and examples.

**Include:**
- First-line description (what the tool does)
- Brief `Args:` section (one line per param)
- Non-obvious behavior (e.g., "Auto-refreshes heartbeat")

**Exclude (put in guide.md instead):**
- `Returns:` sections (JSON results are self-documenting)
- Usage examples and patterns
- "Tip:" or "Note:" sections
- Implementation details

**Example:**
```python
@mcp.tool()
def publish_event(event_type: str, payload: str, ...) -> dict:
    """Publish an event. Auto-refreshes heartbeat.

    Args:
        event_type: e.g., 'task_completed', 'help_needed'
        payload: Event message
        session_id: Your session ID
        channel: "all", "session:{id}", "repo:{name}", or "machine:{name}"
    """
```

## API Design

CLI and MCP expose the same functionality:

| CLI | MCP | Pattern |
|-----|-----|---------|
| `register` | `register_session` | Short vs descriptive |
| `sessions` | `list_sessions` | Noun vs verb_noun |
| `events` | `get_events` | Noun vs verb_noun |

- CLI: kebab-case args (`--session-id`), short commands
- MCP: snake_case params, descriptive `verb_noun` pattern
- CLI-only: `--timeout`, `--json`, `--exclude-types`

**When modifying API**: Update CLI help, MCP docstrings, and `guide.md` together.

## Design Decisions

- **Polling over push**: MCP is request/response; sessions poll with `get_events(cursor)`
- **Broadcast model**: All sessions see all events; channels are metadata, not filters
- **Session cleanup**: 24-hour timeout + PID liveness checks for local sessions
- **Auto-heartbeat**: `publish_event` and `get_events` refresh heartbeat
- **Cursor auto-tracking**: `get_events(session_id=X)` persists cursor; `resume=True` uses it
- **UUID session IDs**: `session_id` is UUID; `display_id` is human-readable ("brave-tiger")
- **Client deduplication**: `(machine, client_id)` enables session resumption

## Operations

```bash
# Watch live activity
tail -f ~/.claude/contrib/agent-event-bus/agent-event-bus.log

# Override database path
AGENT_EVENT_BUS_DB=/path/to/db.sqlite agent-event-bus

# Dev mode console logging
DEV_MODE=1 agent-event-bus

# Custom notification icon (requires terminal-notifier)
AGENT_EVENT_BUS_ICON=/path/to/icon.png agent-event-bus

# Disable Tailscale auth (for testing/local dev)
AGENT_EVENT_BUS_AUTH_DISABLED=1 agent-event-bus

# CLI session attribution (used by hooks)
AGENT_EVENT_BUS_SESSION_ID=abc123 agent-event-bus-cli publish ...
```

Notifications: Uses terminal-notifier if installed (`brew install terminal-notifier`), falls back to osascript.

## See Also

- **Usage patterns, event types, channels**: `agent-event-bus://guide` or `src/agent_event_bus/guide.md`
- **CLI usage**: `agent-event-bus-cli --help`
- **Installation**: `README.md`
