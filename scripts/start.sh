#!/bin/bash
# Start the event bus server in the background

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_FILE="$PROJECT_DIR/event-bus.log"
PID_FILE="$PROJECT_DIR/.event-bus.pid"

# Check if already running
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Event bus already running (PID $(cat "$PID_FILE"))"
    exit 0
fi

# Activate venv and start server
source "$VENV_DIR/bin/activate"
export EVENT_BUS_ICON="$PROJECT_DIR/assets/icon.png"
nohup python -m event_bus.server > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "Event bus started (PID $!)"
echo "Logs: $LOG_FILE"
echo "Add to Claude Code: claude mcp add --transport http --scope user event-bus http://127.0.0.1:8080/mcp"
