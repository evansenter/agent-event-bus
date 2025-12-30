# Claude Event Bus

MCP server for cross-session Claude Code communication and coordination.

## Overview

When running multiple Claude Code sessions (via `/parallel-work` or separate terminals), each session is isolated. This MCP server provides an event bus for sessions to:

- **Announce presence** - Know what other sessions are active
- **Broadcast status** - Share progress updates and task completion
- **Coordinate work** - Signal dependencies and handoffs
- **Send notifications** - System notifications with custom icon support

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
python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# Install as LaunchAgent (recommended - auto-starts on login, also installs CLI)
./scripts/install-launchagent.sh

# Or install just the CLI (for use in hooks/scripts)
./scripts/install-cli.sh

# Or run in dev mode (foreground, auto-reload)
./scripts/dev.sh
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
| `publish_event` | Publish event to a channel (broadcast, repo, session) |
| `get_events` | Poll for events (filtered by subscriptions) |
| `unregister_session` | Clean up session on exit |
| `notify` | Send system notification with optional sound |

## Channel-Based Messaging

Events can be targeted to specific channels:

| Channel | Who receives |
|---------|--------------|
| `all` | Everyone (default, broadcast) |
| `session:{id}` | Direct message to one session |
| `repo:{name}` | All sessions in that repo |
| `machine:{name}` | All sessions on that machine |

## CLI Wrapper

For shell scripts and hooks:

```bash
event-bus-cli register --name "my-feature" --pid $$
event-bus-cli publish --type "done" --payload "Finished"
event-bus-cli notify --title "Build" --message "Complete" --sound
event-bus-cli unregister --session-id $SESSION_ID
```

## Notifications

On macOS, notifications display a custom Birman cat icon (requires `terminal-notifier`):

```bash
brew install terminal-notifier
```

The LaunchAgent and dev.sh set `EVENT_BUS_ICON` automatically.

## Scripts

| Script | Purpose |
|--------|---------|
| `install-launchagent.sh` | Install as macOS LaunchAgent (auto-start) + CLI |
| `install-cli.sh` | Install CLI to ~/.local/bin for hooks/scripts |
| `uninstall-launchagent.sh` | Remove LaunchAgent |
| `dev.sh` | Run in foreground with auto-reload |

## Icon Generation

The notification icon can be regenerated using Gemini:

```bash
cd scripts/icon-gen
GEMINI_API_KEY=key cargo run --release -- "your prompt"
cargo run --bin smart-crop   # AI-powered tight crop
cargo run --bin remove-bg    # Remove background
```

## Roadmap

- [x] Event bus with channel-based messaging
- [x] SQLite persistence
- [x] System notifications with custom icon
- [x] LaunchAgent for auto-start
- [x] CLI wrapper for shell scripts
- [ ] Tailscale support for multi-machine

## Related

- [RFC: Global event bus](https://github.com/evansenter/dotfiles/issues/41)
- [`/parallel-work` command](https://github.com/evansenter/dotfiles)
