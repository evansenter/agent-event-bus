#!/bin/bash
# Install the event bus as a macOS LaunchAgent (auto-starts on login)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
PLIST_TEMPLATE="$SCRIPT_DIR/com.evansenter.claude-event-bus.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.evansenter.claude-event-bus.plist"
LABEL="com.evansenter.claude-event-bus"

# Check venv exists
if [[ ! -f "$VENV_PYTHON" ]]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv"
    echo "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

# Create LaunchAgents directory if needed
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/.claude"

# Stop existing service if running
if launchctl list | grep -q "$LABEL"; then
    echo "Stopping existing service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

# Generate plist with correct paths
echo "Installing LaunchAgent..."
sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

# Load the service
echo "Starting service..."
launchctl load "$PLIST_DEST"

# Verify it's running
sleep 1
if launchctl list | grep -q "$LABEL"; then
    echo ""
    echo "Event bus installed and running!"
    echo "  Logs: ~/.claude/event-bus.log"
    echo "  Errors: ~/.claude/event-bus.err"
    echo ""
    echo "To uninstall: $SCRIPT_DIR/uninstall-launchagent.sh"
    osascript -e 'display notification "LaunchAgent installed and running" with title "Event Bus"' 2>/dev/null
else
    echo "Error: Service failed to start. Check ~/.claude/event-bus.err"
    osascript -e 'display notification "Failed to start - check logs" with title "Event Bus" sound name "Basso"' 2>/dev/null
    exit 1
fi
