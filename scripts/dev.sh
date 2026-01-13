#!/bin/bash
# Run event bus in development mode (foreground, auto-reload, verbose logging)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"
source .venv/bin/activate

SERVICE_WAS_RUNNING=false

# Stop service if running (to free port 8080)
if [[ "$(uname)" == "Darwin" ]]; then
    LABEL="com.evansenter.claude-event-bus"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then
        echo "Stopping LaunchAgent for dev mode..."
        launchctl unload "$PLIST" 2>/dev/null
        SERVICE_WAS_RUNNING=true
        osascript -e 'display notification "Stopped for dev mode" with title "Event Bus"' 2>/dev/null
    fi
else
    SERVICE_NAME="claude-event-bus"
    if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
        echo "Stopping systemd service for dev mode..."
        systemctl --user stop "$SERVICE_NAME"
        SERVICE_WAS_RUNNING=true
    fi
fi

# Restart service on exit
cleanup() {
    if [[ "$SERVICE_WAS_RUNNING" == "true" ]]; then
        echo ""
        if [[ "$(uname)" == "Darwin" ]]; then
            echo "Restarting LaunchAgent..."
            launchctl load "$PLIST"
            osascript -e 'display notification "LaunchAgent restarted" with title "Event Bus"' 2>/dev/null
        else
            echo "Restarting systemd service..."
            systemctl --user start "$SERVICE_NAME"
        fi
    fi
}
trap cleanup EXIT

echo "Starting event bus in dev mode (Ctrl+C to stop)..."
echo "Add to Claude Code: claude mcp add --transport http --scope user event-bus http://127.0.0.1:8080/mcp"
echo ""

# DEV_MODE enables request/response body logging
export EVENT_BUS_ICON="$PROJECT_DIR/assets/icon.png"
DEV_MODE=1 uvicorn event_bus.server:create_app --host 127.0.0.1 --port 8080 --reload --factory
