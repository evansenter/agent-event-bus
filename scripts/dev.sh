#!/bin/bash
# Run event bus in development mode (foreground, auto-reload, verbose logging)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.evansenter.claude-event-bus"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$PROJECT_DIR"
source .venv/bin/activate

# Stop LaunchAgent if running (to free port 8080)
LAUNCHAGENT_WAS_RUNNING=false
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    echo "Stopping LaunchAgent for dev mode..."
    launchctl unload "$PLIST" 2>/dev/null
    LAUNCHAGENT_WAS_RUNNING=true
    osascript -e 'display notification "Stopped for dev mode" with title "Event Bus"' 2>/dev/null
fi

# Restart LaunchAgent on exit
cleanup() {
    if [[ "$LAUNCHAGENT_WAS_RUNNING" == "true" && -f "$PLIST" ]]; then
        echo ""
        echo "Restarting LaunchAgent..."
        launchctl load "$PLIST"
        osascript -e 'display notification "LaunchAgent restarted" with title "Event Bus"' 2>/dev/null
    fi
}
trap cleanup EXIT

echo "Starting event bus in dev mode (Ctrl+C to stop)..."
echo "Add to Claude Code: claude mcp add --transport http --scope user event-bus http://127.0.0.1:8080/mcp"
echo ""

# DEV_MODE enables request/response body logging
DEV_MODE=1 uvicorn event_bus.server:create_app --host 127.0.0.1 --port 8080 --reload --factory
