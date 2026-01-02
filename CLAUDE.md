# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

MCP server providing an event bus for cross-session Claude Code communication. Sessions can register, publish events, and poll for updates.

**Related project**: [claude-session-analytics](https://github.com/evansenter/claude-session-analytics) - Historical analysis of Claude Code sessions. Both projects share design patterns and should be kept aligned.

## DATABASE PROTECTION

**The database at `~/.claude/contrib/event-bus/data.db` contains irreplaceable event history.**

### NEVER do any of the following:
- Add code that deletes the database file (`os.remove()`, `unlink()`, `rm`)
- Add `DROP TABLE` statements for `events` or `sessions`
- Add `DELETE FROM` for user data tables
- Add any "reset" or "clear all" functionality that destroys historical data

### Safe operations:
- `make uninstall` - Preserves database (only removes LaunchAgent + MCP config)
- `make reinstall` - Just reinstalls Python package
- Schema migrations via `@migration` decorator in `storage.py`

### If you need to test destructive operations:
Use a temporary database in tests (all tests already do this via `conftest.py`).

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

### When to Restart

Code changes require a server restart to take effect:

```bash
make restart   # Restarts the LaunchAgent
make reinstall # Full reinstall + restart (useful after updating dependencies)
```

| Change | Restart Needed? |
|--------|-----------------|
| `server.py`, `storage.py`, `helpers.py`, `cli.py` | **Yes** |
| `guide.md` | No (read fresh each request) |
| `CLAUDE.md`, tests, docs | No |

In dev mode (`./scripts/dev.sh`), the server auto-reloads on file changes.

## Architecture

```
src/event_bus/
├── server.py      # MCP tools and server entry point
├── storage.py     # SQLite storage backend (Session, Event, SQLiteStorage)
├── helpers.py     # Utility functions (notifications, repo extraction)
├── middleware.py  # Request logging middleware (logs to ~/.claude/contrib/event-bus/event-bus.log)
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
| `list_sessions()` | List active sessions with their subscribed channels |
| `list_channels()` | List active channels with subscriber counts |
| `publish_event(event_type, payload, session_id?, channel?)` | Publish event to channel |
| `get_events(cursor?, limit?, session_id?, order?, channel?)` | Get events (with optional channel filter) |
| `unregister_session(session_id?, client_id?)` | Clean up session on exit (provide either identifier) |
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
| `channels` | `list_channels` | Noun (CLI) = verb+noun (MCP) |
| `publish` | `publish_event` | CLI short form, MCP descriptive |
| `events` | `get_events` | Noun (CLI) = verb+noun (MCP) |
| `notify` | `notify` | Identical |

**Conventions:**
- CLI uses kebab-case args (`--session-id`), MCP uses snake_case params (`session_id`)
- CLI uses short forms for commands, MCP uses descriptive `verb_noun` pattern
- CLI query commands use nouns (`sessions`, `events`); action commands use verbs (`publish`, `notify`)

**CLI-only features** (not in MCP):
- `--timeout` - HTTP request timeout
- `--json` - JSON output format
- `--exclude-types` - Event type filtering

**When modifying the API**: Update all discovery surfaces together:
1. **CLI help text** - argparse descriptions in `cli.py` (visible via `event-bus-cli --help`)
2. **MCP tool docstrings** - in `server.py` (visible to CC via tool inspection)
3. **Usage guide** - `guide.md` (served as `event-bus://guide` resource)
4. **CLAUDE.md** - This file, for codebase context
5. **~/.claude/contrib/README.md** - User's local contrib directory (lists MCP server data locations)

Ensure parameter names match (kebab ↔ snake conversion) across CLI and MCP.

## MCP API Naming Conventions

Standard conventions for MCP tool and argument naming across related projects (session-analytics, event-bus).

### Tool Names

| Prefix | When to use | Example |
|--------|-------------|---------|
| `list_*` | Enumerate items (no complex filtering) | `list_sessions()` |
| `get_*` | Retrieve data with parameters/filters | `get_events(cursor=...)` |
| `search_*` | Full-text/fuzzy search | `search_messages(query=...)` |
| `analyze_*` | Compute derived insights | `analyze_trends(...)` |
| `ingest_*` | Load/import data | `ingest_logs(...)` |
| `verb_noun` | Perform actions | `register_session`, `publish_event` |

### Argument Names

| Concept | Standard Name | Notes |
|---------|---------------|-------|
| Session identifier | `session_id` | Not `session` or `sid` |
| Max results | `limit` | Not `count` or `max` |
| Pagination position | `cursor` | Opaque string, not `offset` or `since_id` |
| Sort direction | `order` | Values: `"asc"`, `"desc"` |
| Time window | `days` | Use fractional for hours: `days=0.5` = 12h |
| Project filter | `project` | Not `project_path` |
| Minimum threshold | `min_count` | Not `threshold` or `min_events` |

### CLI ↔ MCP Mapping

- CLI uses kebab-case: `--session-id`
- MCP uses snake_case: `session_id`
- CLI commands are short nouns/verbs: `sessions`, `publish`
- MCP tools are descriptive: `list_sessions`, `publish_event`

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
| `ci_completed` | CI finished (pass or fail) | `"CI passed on PR #42"` or `"CI failed on PR #42"` |
| `message` | Generic message/announcement | `"Auth feature done, you can integrate now"` |
| `help_needed` | Request for assistance | `"Need review on auth.ts approach"` |
| `task_completed` | Significant task finished | `"Feature X is done and merged"` |
| `task_started` | Work begun on issue/task | `"Started work on #42 - Add dark mode"` |
| `gotcha_discovered` | Non-obvious issue found | `"SQLite needs datetime adapters in Python 3.12+"` |
| `pattern_found` | Useful pattern discovered | `"Use (machine, client_id) as dedup key"` |
| `test_flaky` | Flaky test identified | `"test_concurrent_writes sometimes fails, safe to retry"` |
| `workaround_needed` | Temporary fix for known issue | `"Rate limit workaround: batch requests"` |
| `feedback_addressed` | PR feedback processed | `"Addressed feedback on PR #108: 2 implemented, 1 skipped"` |
| `error_broadcast` | Repeated failures or rate limits | `"API rate limited - wait 10min"` |
| `blocker_found` | Blocking issue discovered | `"Main branch CI broken"` |

**Naming conventions:**
- Use `snake_case` for event types
- Be specific: `rfc_created` not just `created`
- Include context in payload: what happened and relevant identifiers (PR#, issue#)
- Payloads are automatically JSON-escaped by the MCP layer - special characters are safe

**Proactive publishing:** Emit `gotcha_discovered`, `pattern_found`, `test_flaky`, or `workaround_needed` when you find something that would save other sessions time. Use `error_broadcast` or `blocker_found` for issues affecting multiple sessions.

## Design Decisions

- **Polling over push**: MCP is request/response, so sessions poll with `get_events(cursor)`
- **Session cleanup**: 24-hour heartbeat timeout + client liveness checks for local sessions
- **Auto-heartbeat**: `publish_event` and `get_events` auto-refresh heartbeat
- **Cursor auto-tracking**: When `session_id` is passed to `get_events()`, the cursor is persisted. On session resume, `register_session()` returns the last cursor - no missed events!
- **SQLite persistence**: State persists across restarts in `~/.claude/contrib/event-bus/data.db`
- **Localhost binding**: Binds to 127.0.0.1 by default for security
- **Implicit subscriptions**: No explicit subscribe - sessions auto-subscribed to relevant channels
- **Human-readable IDs**: Session IDs use Docker-style names (e.g., `brave-tiger`) instead of UUIDs
- **Client deduplication**: Sessions are deduplicated by `(machine, client_id)` - allows session resumption across restarts

## CLI Wrapper

For shell scripts and hooks (e.g., CC SessionStart/SessionEnd hooks):

```bash
# Register session
event-bus-cli register --name "my-feature" --client-id "my-client-123"

# Unregister session (by session_id or client_id)
event-bus-cli unregister --session-id abc123
event-bus-cli unregister --client-id "my-client-123"  # Simpler for hooks

# List sessions (shows subscribed channels)
event-bus-cli sessions

# List active channels
event-bus-cli channels

# Publish event
event-bus-cli publish --type "task_done" --payload "Finished" --channel "repo:my-project"

# Get events (basic - newest first by default)
event-bus-cli events --session-id abc123

# Get events with JSON output (for scripting)
event-bus-cli events --json --limit 10 --exclude-types session_registered,session_unregistered

# Get events with session_id (cursor auto-tracked in DB - preferred)
event-bus-cli events --session-id abc123 --cursor "$CURSOR" --order asc

# Poll for new events chronologically (use with cursor)
event-bus-cli events --cursor abc123 --order asc --session-id mysession

# Filter events to a specific channel
event-bus-cli events --channel "repo:my-project"

# Send notification
event-bus-cli notify --title "Done" --message "Build complete"
```

### Events Command Options

| Option | Description |
|--------|-------------|
| `--cursor ID` | Get events after this cursor (opaque string) |
| `--session-id ID` | Your session ID for channel filtering |
| `--channel CHANNEL` | Filter to a specific channel (e.g., `repo:my-project`) |
| `--limit N` | Maximum events to return |
| `--exclude-types T1,T2` | Comma-separated event types to filter out |
| `--timeout MS` | Request timeout in milliseconds (default: 10000) |
| `--json` | Output as JSON with `events` array and `next_cursor` |
| `--order asc\|desc` | Event ordering: desc (default, newest first) or asc (oldest first) |

## Logging

All MCP tool calls are logged to `~/.claude/contrib/event-bus/event-bus.log` with pretty formatting and ANSI colors:

```bash
# Watch live activity (colors render in terminal)
tail -f ~/.claude/contrib/event-bus/event-bus.log

# Example output:
# 22:30:15 │ list_sessions() → [7 items]
# 22:30:16 │ get_events(order="asc", limit=20) → 3 events, cursor=42
# 22:30:17 │ publish_event(event_type="task_done", payload="Finished") → event #43 [all]
```

## Configuration

```bash
# Override database path (default: ~/.claude/contrib/event-bus/data.db)
EVENT_BUS_DB=/path/to/db.sqlite event-bus

# Enable console logging in addition to file (for development)
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

