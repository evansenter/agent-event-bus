# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

MCP server for cross-session Claude Code communication. Sessions register, publish events, and poll for updates.

**Related**: [agent-session-analytics](https://github.com/evansenter/agent-session-analytics) shares design patterns.

---

## Naming Conventions

Follow these patterns consistently (aligned with agent-session-analytics):

| Type | Value |
|------|-------|
| Repo | `agent-event-bus` |
| Python package | `agent_event_bus` |
| MCP server name | `agent-event-bus` |
| CLI commands | `agent-event-bus`, `agent-event-bus-cli`, `agent-event-bus-bridge` (supervised on macOS via `make install-bridge`; elsewhere run via `uv run`) |
| Resource URI | `agent-event-bus://guide` |
| Data directory | `~/.claude/contrib/agent-event-bus/` |
| Database | `~/.claude/contrib/agent-event-bus/data.db` |
| Log files | `agent-event-bus.log`, `agent-event-bus.err`; bridge: `agent-event-bus-bridge.log`, `agent-event-bus-bridge.err` (the bridge's are launchd's stdout/stderr capture; launchd APPENDS across restarts, so no file handler of its own is needed. Bridge records go to `.err` - it logs to stderr - while `.log` gets uvicorn access lines) |
| LaunchAgent | `com.evansenter.agent-event-bus.plist`; bridge: `com.evansenter.agent-event-bus-bridge.plist` (separate unit - a bus host need not run a bridge) |
| systemd service | `agent-event-bus.service` |
| Wake spool dir | `~/.claude/contrib/agent-event-bus/wake/` (bridge spool/lock files + `panes.json` + `bridge.singleton.lock` - transient; the DB protection below does NOT extend to it. Safe to hand-clear **while no bridge is running** - clearing it under a live bridge orphans its `bridge.singleton.lock` inode, so a second instance acquires a fresh one) |
| Bridge hook-lock dir | `$XDG_RUNTIME_DIR/agent-event-bus-bridge-<uid>/`, else the system temp dir (`$TMPDIR`, or `/tmp` when unset; macOS: per-user `/var/folders/.../T`) - zero-byte, uid-scoped `hook.<hash>.lock` files, machine-scoped so a same-URL double-start refuses regardless of `$HOME`. Create-and-verified private (not adopted). Safe to remove when no bridge is running |

**Environment variables**: `AGENT_EVENT_BUS_*` prefix (e.g., `_DB`, `_LOG`, `_ERR`, `_URL`, `_AUTH_DISABLED`, `_ICON`, `_TESTING`, `_SESSION_ID`; bridge: `_BRIDGE_PORT`, `_BRIDGE_BACKEND`, `_BRIDGE_COOLDOWN`, `_BRIDGE_SECRET`, `_BRIDGE_HOOK_URL`, `_BRIDGE_BIND`, `_BRIDGE_ALLOWED_HOSTS`, `_WAKE_DIR`; and `_BRIDGE_LOG` / `_BRIDGE_ERR`, which -
unlike every other `_BRIDGE_*` name - are **install-time only**: they are read by
`scripts/install-bridge-launchagent.sh` and baked into the plist, so they must be in
the environment of `make install-bridge` itself. Setting them for a running bridge
does nothing, exactly as with the bus's `_LOG` / `_ERR` pair).
One consulted variable is deliberately outside the prefix: **`CLAUDE_CODE_SESSION_ID`**, which the CLI reads as a session-attribution fallback when `--session-id` and `AGENT_EVENT_BUS_SESSION_ID` are both absent (#137).

---

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

### WAL mode:
The database runs in WAL mode: `data.db-wal` and `data.db-shm` are **part of
the database**. Never copy or move `data.db` alone (a plain `cp` can silently
miss recently committed events sitting in the WAL) - use `sqlite3 .backup`,
which is WAL-aware and safe against a running server.

### Before schema changes:
```bash
sqlite3 ~/.claude/contrib/agent-event-bus/data.db ".backup $HOME/.claude/contrib/agent-event-bus/data.db.backup-$(date +%Y%m%d-%H%M%S)"
```

## Commands

```bash
make install-server  # Server: runs event bus locally (idempotent, restarts service)
make install-client REMOTE_URL=...  # Client: connects to remote server (idempotent)
make uninstall  # Remove everything (preserves DB)
make dev        # Install with dev dependencies
make check      # Format + lint + test
make restart    # Lightweight service restart (no dependency sync)
make install-bridge    # Supervise the RFC #122 bridge (macOS LaunchAgent, idempotent)
make uninstall-bridge  # Stop supervising it (leaves the bus, DB, and wake/ alone)
./scripts/dev.sh  # Dev mode (foreground, auto-reload)
```

**When to restart**: Code changes to `server.py`, `storage.py`, `helpers.py` require `make install-server` (or `make restart`). `guide.md` is read fresh each request. Dev mode auto-reloads. After `bridge.py` changes, restart the bridge: `make install-bridge` if it is supervised (macOS LaunchAgent, idempotent), else whatever runs `uv run agent-event-bus-bridge`.

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
├── session_ids.py # Dinosaur-themed display_id generation
├── cli.py         # CLI wrapper for shell scripts
├── bridge.py      # Webhook→injection re-awakening bridge (RFC #122, experimental)
└── guide.md       # Usage guide (agent-event-bus://guide resource)

docs/BRIDGE.md     # Bridge operator docs - deliberately NOT in guide.md, which
                   # is served as an MCP resource into every session that reads it
```

## MCP Tools

`register_session`, `list_sessions`, `list_channels`, `publish_event`, `get_events`, `ack_events`, `unregister_session`, `notify`, `register_webhook`, `list_webhooks`, `set_webhook_active`, `unregister_webhook`

**Usage guide**: `agent-event-bus://guide` resource. Keep it updated when changing APIs.

**Concurrency invariant**: FastMCP runs tool functions on the server's event loop, so every tool is an async wrapper that offloads its sync `*_impl` body via `_run_sync` (worker thread). Never put blocking work (SQLite, subprocess) directly in a tool function - it freezes the whole server (#112). `GET /health` bypasses MCP for liveness checks.

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
| `ack` | `ack_events` | Short vs descriptive |

- CLI: kebab-case args (`--session-id`), short commands
- MCP: snake_case params, descriptive `verb_noun` pattern
- CLI-only: `--timeout`, `--json`, `--exclude-types`

**When modifying API**: Update CLI help, MCP docstrings, and `guide.md` together.

## Design Decisions

- **Polling over push**: MCP is request/response; sessions poll with `get_events(cursor)`
- **Broadcast model**: All sessions see all events; channels are metadata, not filters
- **Session cleanup**: 24-hour timeout + PID liveness checks for local sessions
- **Auto-heartbeat**: `publish_event`, `get_events` and `ack_events` refresh heartbeat (a consumer that only acks would otherwise be swept as stale out from under itself)
- **Peek/ack pairing (#134)**: a bounded consume cannot bound anything under a server-side filter - `min_level` filters the returned view while the cursor advances over the RAW batch behind it, so "consume the N I just saw" advances past a different window than the peek showed. `ack_events(session_id, cursor)` commits an id the caller already holds, so peek and ack name one window by construction. **Peek with `order="asc"`, and only from an unfiltered or `min_level` peek**: under `desc` the batch is the newest slice while `next_cursor` is the tip, so acking it discards the older, never-surfaced backlog; under a `channel`/`event_types`/`correlation_id` filter `next_cursor` is the MATCHED batch max, so acking it buries every lower-id non-match (which is exactly the loss the next-but-one bullet has `get_events` refuse to perform on its own)
- **Cursor auto-tracking**: `get_events(session_id=X)` persists cursor; `resume=True` uses it
- **Non-consuming narrowed reads**: `channel`/`event_types`/`correlation_id` filters never advance the session cursor (their SQL-filtered max would mark non-matching events as seen); `min_level` filters post-bookkeeping and does
- **High-water cursors**: `next_cursor` is the batch MAX id in both orders; feeding it back never re-serves events
- **Deleted sessions fail loudly (#140)**: `get_events` with a soft-deleted `session_id` returns `{"error": "Session deleted", "session_deleted": true, ...}` on *every* read path, not just `resume`. A deleted session's cursor and heartbeat are both frozen, so an empty batch is indistinguishable from "up to date" while the session is also absent from `list_sessions` - the failure was silent on every axis. Ids never registered here stay silent (foreign session ids are a supported way to read the bus)
- **UUID session IDs**: `session_id` is UUID; `display_id` is human-readable ("brave-trex")
- **Client deduplication**: `(machine, client_id)` enables session resumption
- **Structured payload (RFC #121)**: `payload` stays free-form; optional `title`/`tags`/`correlation_id`/`signal_level` ride alongside (soft validation - warn, never reject)
- **Signal levels (#129)**: lifecycle < info < actionable, derived server-side from event_type (DMs always actionable; explicit `signal_level` wins); filter with `min_level`

## Operations

```bash
# Watch live activity (auto-detects local vs remote bus from MCP config)
make logs

# Force a remote tail (overrides auto-detect)
make logs BUS_HOST=your-server.tailnet.ts.net

# Override database path
AGENT_EVENT_BUS_DB=/path/to/db.sqlite agent-event-bus

# Override log/error file paths — the install scripts substitute these into
# the launchd plist / systemd unit, so they must be in the environment of
# `make install-server` itself. Prefix on the make invocation (or `export`
# them first) — bare shell assignments below will NOT apply at install time.
AGENT_EVENT_BUS_LOG=/path/to/custom.log AGENT_EVENT_BUS_ERR=/path/to/custom.err make install-server

# Dev mode console logging
DEV_MODE=1 agent-event-bus

# Custom notification icon (requires terminal-notifier)
AGENT_EVENT_BUS_ICON=/path/to/icon.png agent-event-bus

# Disable Tailscale auth (for testing/local dev)
AGENT_EVENT_BUS_AUTH_DISABLED=1 agent-event-bus

# CLI session attribution (used by hooks). When --session-id is omitted, the
# CLI reads AGENT_EVENT_BUS_SESSION_ID, then falls back to
# CLAUDE_CODE_SESSION_ID (which Claude Code injects into every subprocess it
# spawns). The fallback matters because shell-profile mappings of one to the
# other typically live in an rc file that only INTERACTIVE shells read, so a
# tool-spawned subprocess never runs them and publishes land as "anonymous".
AGENT_EVENT_BUS_SESSION_ID=abc123 agent-event-bus-cli publish ...
```

Notifications: Uses terminal-notifier if installed (`brew install terminal-notifier`), falls back to osascript.

## See Also

- **Usage patterns, event types, channels**: `agent-event-bus://guide` or `src/agent_event_bus/guide.md`
- **Re-awakening bridge (operators)**: `docs/BRIDGE.md`
- **CLI usage**: `agent-event-bus-cli --help`
- **Installation**: `README.md`
