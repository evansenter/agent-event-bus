# Event Bus Usage Guide

## What is this?

The Event Bus enables communication between Claude Code sessions. When running multiple
CC sessions (e.g., in separate terminals or worktrees), this MCP server lets sessions:

- See what other sessions are active
- Coordinate work (signal when APIs are ready, request help)
- Send system notifications to the user

## Available Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, client_id?)` | Register yourself, get session_id + cursor |
| `list_sessions()` | See active sessions |
| `list_channels()` | See active channels |
| `publish_event(type, payload, channel?)` | Send event |
| `get_events(session_id?, resume?, order?, event_types?)` | Poll for events |
| `unregister_session(session_id?)` | Clean up on exit |
| `notify(title, message, sound?)` | System notification |

*Signatures simplified for quick start. Full parameters (machine, cwd, cursor, limit, channel, etc.) available - check MCP tool docstrings.*

## Quick Start

### 1. Register on startup
```
register_session(name="auth-feature", client_id="cc-session-abc")
→ {session_id: "cc-session-abc", display_id: "brave-tiger", cursor: "42", ...}
```
- `session_id` is your unique identifier (your `client_id`, or a UUID if not provided)
- `display_id` is human-readable ("brave-tiger") - for display only
- Use `session_id` for all API calls

### 2. Poll for events
```
get_events(session_id="my-unique-id", resume=True, order="asc")
→ {events: [...], next_cursor: "55"}
```
- `resume=True` picks up where you left off (cursor auto-tracked)
- `order="asc"` returns events chronologically

### 3. Publish to coordinate
```
publish_event("api_ready", "Auth endpoints merged", channel="repo:my-project")
```

### 4. Notify the user
```
notify("Build Complete", "All tests passing", sound=True)
```

### 5. Unregister when done
```
unregister_session(session_id="my-unique-id")
```

## Channels

**All sessions see all events** (broadcast model). Channels are metadata for context:

| Channel | When to Use |
|---------|-------------|
| `all` | General announcements (default) |
| `repo:{name}` | Coordinate work in a repo |
| `session:{id}` | Direct messages (triggers notification) |
| `machine:{name}` | Machine-specific coordination |

Use `get_events(channel=X)` to explicitly filter if needed.

## Event Polling

### The Simple Way (recommended)
```python
# Register with client_id for session resumption
result = register_session(name="my-feature", client_id="unique-id")
session_id = result["session_id"]

# Poll incrementally - cursor tracked automatically
events = get_events(session_id=session_id, resume=True, order="asc")
```

### Order Parameter
- `order="desc"` (default): Newest first - "what's happening?"
- `order="asc"`: Oldest first - catching up chronologically

### Filter by Event Type
```
get_events(event_types=["task_completed", "ci_completed"])
→ Only returns events of those types
```

Useful for focused polling (e.g., only discoveries):
```
get_events(event_types=["gotcha_discovered", "pattern_found", "improvement_suggested"])
```

### Manual Cursor (if needed)
```
get_events(cursor="42", order="asc")
→ {events: [...], next_cursor: "55"}
```
Pass `next_cursor` to subsequent calls. But `resume=True` is simpler.

## Common Patterns

### Signal when your work is ready
```
publish_event("api_ready", "Auth API merged to main", channel="repo:my-project")
```

### Ask another session for help
```
sessions = list_sessions()
auth_session = next(s for s in sessions if "auth" in s["name"])
publish_event("help_needed", "How do I call the new auth endpoint?",
              channel=f"session:{auth_session['session_id']}")
```

## How Direct Messages Work

MCP is request/response - the server can't push to CC sessions. DMs work via the human:

1. Session A sends: `publish_event("help", "Need review", channel="session:abc123")`
2. Server sends macOS notification to human
3. Human switches to that terminal
4. Human tells Claude: "check the event bus"
5. Claude polls and sees the message

## Authentication (Multi-Machine)

When running across multiple machines via Tailscale, the server requires identity headers:

- Requests must go through `tailscale serve` (injects `Tailscale-User-Login` header)
- Direct requests to `localhost:8080` are rejected with 401
- Only devices on your Tailnet can connect

For local-only setups, set `EVENT_BUS_AUTH_DISABLED=1` to bypass auth.

See `docs/TAILSCALE_SETUP.md` for full setup instructions.

## Best Practices

1. **Register with client_id** - Enables session resumption
2. **Use resume=True for polling** - Simplest incremental approach
3. **Include session_id in get_events** - Enables cursor tracking + heartbeat
4. **Use meaningful channels** - `repo:` or `session:` for context
5. **Keep payloads short** - Coordination, not data transfer
6. **Unregister on exit** - Keeps session list clean

## Event Type Conventions

Use consistent event types for discoverability:

| Event Type | When to Use |
|------------|-------------|
| `task_started` | Work begun on issue/task |
| `task_completed` | Significant task finished |
| `ci_completed` | CI finished (pass or fail) |
| `help_needed` | Request for assistance |
| `gotcha_discovered` | Non-obvious issue found |
| `pattern_found` | Useful pattern discovered |
| `test_flaky` | Flaky test identified |
| `blocker_found` | Blocking issue discovered |
| `error_broadcast` | Rate limits, outages |

**Naming**: Use `snake_case`, be specific (`ci_completed` not `done`), include context in payload.

**When to publish proactively**: Discoveries that would save other sessions time - gotchas, patterns, flaky tests, blockers. Don't publish routine work or one-off errors.

```python
# Good
publish_event("ci_completed", "CI passed on PR #42", channel="repo:my-project")

# Bad
publish_event("done", "finished")
```
