# CLAUDE.md

MCP server for cross-session Claude Code communication. Sessions register, publish events, and poll for updates.

**Related**: [claude-session-analytics](https://github.com/evansenter/claude-session-analytics) shares design patterns.

## DATABASE PROTECTION

**The database at `~/.claude/contrib/event-bus/data.db` contains irreplaceable event history.**

### NEVER:
- Add code that deletes the database file
- Add `DROP TABLE` or `DELETE FROM` for user data
- Add "reset" or "clear all" functionality

### Safe operations:
- `make uninstall` - Preserves database
- `make reinstall` - Just reinstalls package
- Schema migrations via `@migration` decorator (increment `SCHEMA_VERSION` when adding)

### Before schema changes:
```bash
cp ~/.claude/contrib/event-bus/data.db ~/.claude/contrib/event-bus/data.db.backup-$(date +%Y%m%d-%H%M%S)
```

## Commands

```bash
make install    # Full install (venv + LaunchAgent + CLI + MCP)
make uninstall  # Remove everything (preserves DB)
make dev        # Install with dev dependencies
make check      # Format + lint + test
make restart    # Restart LaunchAgent
./scripts/dev.sh  # Dev mode (foreground, auto-reload)

# Single test
pytest tests/test_server.py::TestRegisterSession -v
```

### When to Restart

Code changes to `server.py`, `storage.py`, `helpers.py` require restart.
`guide.md` is read fresh each request. Dev mode auto-reloads.

## Architecture

```
src/event_bus/
├── server.py      # MCP tools and entry point
├── storage.py     # SQLite backend (Session, Event, SQLiteStorage)
├── helpers.py     # Notifications, repo extraction
├── middleware.py  # Request logging → ~/.claude/contrib/event-bus/event-bus.log
├── session_ids.py # Docker-style display_id generation
├── cli.py         # CLI wrapper for shell scripts
└── guide.md       # Usage guide (event-bus://guide resource)
```

## MCP Tools

`register_session`, `list_sessions`, `list_channels`, `publish_event`, `get_events`, `unregister_session`, `notify`

**Usage guide**: `event-bus://guide` resource. Keep it updated when changing APIs.

## API Design

CLI and MCP expose the same functionality:

| CLI | MCP | Pattern |
|-----|-----|---------|
| `register` | `register_session` | Short vs descriptive |
| `sessions` | `list_sessions` | Noun vs verb_noun |
| `events` | `get_events` | Noun vs verb_noun |

- CLI: kebab-case args (`--session-id`), short commands
- MCP: snake_case params, descriptive `verb_noun` pattern
- CLI-only: `--timeout`, `--json`, `--exclude-types`, `--track-state`

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
tail -f ~/.claude/contrib/event-bus/event-bus.log

# Override database path
EVENT_BUS_DB=/path/to/db.sqlite event-bus

# Dev mode console logging
DEV_MODE=1 event-bus

# Custom notification icon (requires terminal-notifier)
EVENT_BUS_ICON=/path/to/icon.png event-bus
```

Notifications: Uses terminal-notifier if installed (`brew install terminal-notifier`), falls back to osascript.

## See Also

- **Usage patterns, event types, channels**: `event-bus://guide` or `src/event_bus/guide.md`
- **CLI usage**: `event-bus-cli --help`
- **Installation**: `README.md`
