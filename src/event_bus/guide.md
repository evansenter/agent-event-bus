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
| `get_events(since_id?, limit?, session_id?)` | Poll for new events |
| `unregister_session(session_id)` | Clean up when exiting |
| `notify(title, message, sound?)` | Send macOS notification to user |

## Quick Start

### 1. Register on startup
```
register_session(name="auth-feature")
→ {session_id: "brave-tiger", last_event_id: 42, repo: "my-project", ...}
```
Save `session_id` and `last_event_id` - you'll need them for polling.

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
# Use last_event_id from registration to start polling
get_events(since_id=42, session_id="brave-tiger")
→ [{id: 43, event_type: "api_ready", ...}, {id: 44, ...}]
```
Events come back in chronological order when using since_id.

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

`get_events` has two distinct behaviors based on `since_id`:

### "What's happening?" (since_id=0 or omitted)
```
get_events()
→ [{id: 50, ...}, {id: 49, ...}, {id: 48, ...}]  # Newest first (DESC)
```
Returns recent events, **newest first**. Use this for a quick check of recent activity.

### Polling loop (since_id > 0)
```
get_events(since_id=42)
→ [{id: 43, ...}, {id: 44, ...}, {id: 45, ...}]  # Chronological (ASC)
```
Returns events after the given ID, **in order**. Use this for catching up.

### Recommended Pattern
```python
# 1. On session start, get your starting point
result = register_session(name="my-feature")
session_id = result["session_id"]
last_seen = result["last_event_id"]  # Start from here

# 2. Poll periodically for new events
events = get_events(since_id=last_seen, session_id=session_id)
for event in events:
    # Process each event
    last_seen = event["id"]  # Track progress

# 3. To just peek at recent activity (one-off check)
recent = get_events()  # No since_id = newest first
```

## Common Patterns

### Signal when your work is ready for others
```
# You finished the API, another session is waiting to integrate
publish_event("api_ready", "Auth API merged to main", channel="repo:my-project")
```

### Wait for another session's work
```
# Use last_event_id from register_session as starting point
events = get_events(since_id=last_event_id, session_id=my_session_id)
for e in events:
    if e["event_type"] == "api_ready":
        # Now safe to integrate
    last_event_id = e["id"]  # Track for next poll
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
2. **Save session_id and last_event_id** - You'll need both for polling
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
5. Claude polls: `get_events(since_id=last_seen_id, session_id=my_id)` and sees the message

The notification alerts the **human** who routes the message to the correct session.

## Tips

- `register_session` returns `last_event_id` - use it to start polling from the right place
- Pass `client_id` to enable session resumption across restarts (e.g., CC session ID or PID)
- `get_events` and `publish_event` auto-refresh your heartbeat
- `get_events()` with no since_id returns newest first; with since_id returns chronological
- `list_sessions()` returns most recently active sessions first
- Sessions are auto-cleaned after 7 days of inactivity
- Local sessions with numeric client_ids (PIDs) are cleaned immediately on process death
- The repo name is auto-detected from your working directory
- SessionStart hooks can auto-register you on startup
