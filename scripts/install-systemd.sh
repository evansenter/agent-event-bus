#!/bin/bash
# Install the agent event bus as a Linux systemd user service (auto-starts on login)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
SERVICE_TEMPLATE="$SCRIPT_DIR/agent-event-bus.service"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_DEST="$SERVICE_DIR/agent-event-bus.service"
SERVICE_NAME="agent-event-bus"

# Check venv exists
if [[ ! -f "$VENV_PYTHON" ]]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv"
    echo "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

# Create directories if needed
mkdir -p "$SERVICE_DIR"
mkdir -p "$HOME/.claude/contrib/agent-event-bus"

# Stop existing service if running
if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
    echo "Stopping existing service..."
    systemctl --user stop "$SERVICE_NAME"
fi

# Generate service file with correct paths
echo "Installing systemd service..."
sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$SERVICE_TEMPLATE" > "$SERVICE_DEST"

# Reload systemd and start service
systemctl --user daemon-reload
echo "Starting service..."
systemctl --user enable --now "$SERVICE_NAME"

# Verify it's running
sleep 1
if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
    echo ""
    echo "Agent Event Bus installed and running!"
    echo "  Logs: ~/.claude/contrib/agent-event-bus/agent-event-bus.log"
    echo "  Errors: ~/.claude/contrib/agent-event-bus/agent-event-bus.err"
    echo "  Status: systemctl --user status $SERVICE_NAME"
    echo ""

    # Also install CLI for use in hooks/scripts
    echo "Installing CLI..."
    "$SCRIPT_DIR/install-cli.sh"
    echo ""
    echo "To uninstall: $SCRIPT_DIR/uninstall-systemd.sh"
else
    echo "Error: Service failed to start. Check logs:"
    echo "  journalctl --user -u $SERVICE_NAME"
    echo "  ~/.claude/contrib/agent-event-bus/agent-event-bus.err"
    exit 1
fi
