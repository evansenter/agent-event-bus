# Event Bus Usage Guide

## What is this?

The Event Bus enables communication between Claude Code sessions. When you're
running multiple CC sessions (e.g., via `/parallel-work` or separate terminals),
each session is isolated. This MCP server lets sessions:

- See what other sessions are active
- Coordinate work (signal when APIs are ready, request help)
- Send system notifications to the user

## Available Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, machine?, cwd?, client_id?)` | Register yourself, get a session_id |
| `list_sessions()` | See all active sessions |
| `publish_event(type, payload, channel?)` | Send event to a channel |
| `get_events(cursor?, limit?, session_id?, order?)` | Poll for new events |
| `unregister_session(session_id)` | Clean up when exiting |
| `notify(title, message, sound?)` | Send macOS notification to user |

## Quick Start

### 1. Register on startup
```
register_session(name="auth-feature")
→ {session_id: "brave-tiger", cursor: "42", repo: "my-project", ...}
```
Save `session_id` - the cursor is tracked automatically when you pass session_id to get_events().

### 2. Check who else is working
```
list_sessions()
→ [{name: "auth-feature", ...}, {name: "api-refactor", ...}]
```
Sessions are ordered by most recently active first.

### 3. Publish events to coordinate
```
publish_event("api_ready", "Auth endpoints merged", channel="repo:my-project")
```

### 4. Poll for events
```
# Use cursor from registration to start polling
get_events(cursor="42", session_id="brave-tiger", order="asc")
→ {events: [{id: 43, event_type: "api_ready", ...}], next_cursor: "43"}
```
Use `order="asc"` for chronological order when polling, `order="desc"` (default) for newest first.

### 5. Notify the user
```
notify("Build Complete", "All tests passing", sound=True)
```

### 6. Unregister when done
```
unregister_session(session_id="brave-tiger")
```

## Channels

Events are published to channels. Sessions auto-subscribe based on their attributes:

| Channel | Who Receives | When to Use |
|---------|--------------|-------------|
| `all` | Everyone | Rare - major announcements only |
| `repo:{name}` | Same repository | **Most common** - coordinate parallel work |
| `session:{id}` | One session | Direct messages, help requests |
| `machine:{name}` | Same machine | Local coordination |

**Default is `all`**, but prefer `repo:` for most coordination to avoid noise.

## Event Polling

`get_events` returns a dict with `events` list and `next_cursor` for pagination:
```
get_events()
→ {events: [...], next_cursor: "50"}
```

### Cursor behavior

- **`cursor=None`** (default): Returns recent events, newest first. Use for quick "what's happening?" checks.
- **`cursor="<id>"`**: Returns events after that position. Use with `order="asc"` for chronological polling.

The cursor is an opaque string - don't parse it, just pass the value from `next_cursor` or `register_session`.

### Automatic cursor tracking

When you pass `session_id` to `get_events()`, your cursor position is automatically saved. On session resume (same `client_id`), you get the last cursor you polled with - no missed events!

### Default order (newest first)
```
get_events()
→ {events: [{id: 50, ...}, {id: 49, ...}], next_cursor: "49"}
```
Returns recent events, **newest first** (DESC). Use this for a quick check of recent activity.

### Polling loop (cursor + order="asc")
```
get_events(cursor="42", order="asc")
→ {events: [{id: 43, ...}, {id: 44, ...}], next_cursor: "44"}
```
Returns events after the cursor, **in chronological order**. Use this for catching up.

### Order parameter
- `order="desc"` (default): Newest first - good for "what's happening?"
- `order="asc"`: Oldest first - good for polling/catching up

### Recommended Pattern
```python
# 1. On session start (or resume), register with client_id
result = register_session(name="my-feature", client_id="my-unique-id")
session_id = result["session_id"]
cursor = result["cursor"]  # Resume point (auto-tracked on resume)

# 2. Poll periodically for new events (oldest first to process in order)
result = get_events(cursor=cursor, session_id=session_id, order="asc")
for event in result["events"]:
    # Process each event
    pass
cursor = result["next_cursor"]  # For next poll (also auto-saved!)

# 3. On session resume, your last position is preserved
# Just call register_session with same client_id - cursor picks up where you left off
```

## Common Patterns

### Signal when your work is ready for others
```
# You finished the API, another session is waiting to integrate
publish_event("api_ready", "Auth API merged to main", channel="repo:my-project")
```

### Wait for another session's work
```
# Use cursor from register_session as starting point
result = get_events(cursor=cursor, session_id=my_session_id, order="asc")
for e in result["events"]:
    if e["event_type"] == "api_ready":
        # Now safe to integrate
cursor = result["next_cursor"]  # Track for next poll
```

### Ask another session for help
```
# Find the session working on auth
sessions = list_sessions()
auth_session = next(s for s in sessions if "auth" in s["name"])

# Send them a direct message
publish_event("help_needed", "How do I call the new auth endpoint?",
              channel=f"session:{auth_session['session_id']}")
```

### Notify user when task completes
```
# Useful for long-running tasks
notify("PR Created", "https://github.com/org/repo/pull/123", sound=True)
```

## Best Practices

### Session Registration
1. **Register on session start** - Makes you discoverable; enables DMs
2. **Save session_id and cursor** - You'll need both for polling
3. **Include session_id in get_events** - Enables filtering and auto-heartbeat
4. **Poll at natural breakpoints** - Check for messages before/after major tasks
5. **Unregister on exit** - Keeps the session list clean

### Communication
6. **Use repo channels** - Avoids noise from unrelated projects (prefer `repo:` over `all`)
7. **Keep payloads short** - They're for coordination, not data transfer
8. **DMs auto-notify** - When you send to `session:{id}`, the human gets notified

### Notifications
9. **Notify sparingly** - Only for things the user needs to know
10. **Understand the human-as-router pattern** - Notifications go to the user, not Claude

## How Direct Messages Work (Human as Router)

MCP is request/response only - the server can't push to Claude Code sessions.
When you send a DM, here's what happens:

1. Session A sends: `publish_event("help", "Need review", channel="session:brave-tiger")`
2. Server sends macOS notification: "📨 Message for my-feature (From: auth-work)"
   (Uses terminal-notifier if installed, falls back to osascript)
3. Human sees notification, switches to that terminal
4. Human tells Claude: "check the event bus"
5. Claude polls: `get_events(cursor=last_cursor, session_id=my_id)` and sees the message

The notification alerts the **human** who routes the message to the correct session.

## Tips

- `register_session` returns `cursor` - use it to start polling from the right place
- Pass `client_id` to enable session resumption across restarts (e.g., CC session ID or PID)
- **Cursor auto-tracking**: When you pass `session_id` to `get_events()`, your cursor is auto-saved. On resume, you pick up where you left off!
- `get_events` and `publish_event` auto-refresh your heartbeat
- `get_events()` defaults to newest first (`order="desc"`); use `order="asc"` when polling with cursor
- `list_sessions()` returns most recently active sessions first
- Sessions are auto-cleaned after 24 hours of inactivity
- Local sessions with numeric client_ids (PIDs) are cleaned immediately on process death
- The repo name is auto-detected from your working directory
- SessionStart hooks can auto-register you on startup

## Event Type Conventions

Use consistent event types across commands and sessions for discoverability.

### Standard Event Types

| Event Type | Description | Example Payload |
|------------|-------------|-----------------|
| `rfc_created` | New RFC issue created | `"RFC created: #48 - Event bus integration"` |
| `rfc_responded` | Response posted to RFC | `"RFC response posted: #48"` |
| `parallel_work_started` | New worktree/session started | `"Started parallel work: issue-48 - Implement event bus"` |
| `ci_completed` | CI finished (pass or fail) | `"CI passed on PR #42"` or `"CI failed on PR #42"` |
| `message` | Generic message/announcement | `"Auth feature done, you can integrate now"` |
| `help_needed` | Request for assistance | `"Need review on auth.ts approach"` |
| `task_completed` | Significant task finished | `"Feature X is done and merged"` |

### Naming Conventions

- Use `snake_case` for event types
- Be specific: `rfc_created` not just `created`
- Include context in payload: what happened and any relevant identifiers (PR#, issue#)
- Payloads are automatically JSON-escaped by the MCP layer - special characters are safe

### Examples

```python
# Good: specific type with context in payload
publish_event("ci_completed", "CI passed on PR #42", channel="repo:my-project")
publish_event("rfc_created", "RFC created: #48 - Event bus integration")

# Bad: vague type, no context
publish_event("done", "finished")
publish_event("update", "something happened")
```
