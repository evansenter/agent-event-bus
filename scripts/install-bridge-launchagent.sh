#!/bin/bash
# Install the RFC #122 bridge as a macOS LaunchAgent (auto-starts on login,
# restarts on crash). The bus has its own unit - this supervises only the
# webhook->injection bridge, which until now had no install target at all.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
PLIST_TEMPLATE="$SCRIPT_DIR/com.evansenter.agent-event-bus-bridge.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.evansenter.agent-event-bus-bridge.plist"
LABEL="com.evansenter.agent-event-bus-bridge"

DATA_DIR="$HOME/.claude/contrib/agent-event-bus"
BRIDGE_LOG_FILE="${AGENT_EVENT_BUS_BRIDGE_LOG:-$DATA_DIR/agent-event-bus-bridge.log}"
BRIDGE_ERR_FILE="${AGENT_EVENT_BUS_BRIDGE_ERR:-$DATA_DIR/agent-event-bus-bridge.err}"

if [[ ! -f "$VENV_PYTHON" ]]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv"
    echo "Run: make install-server  (or: uv sync)"
    exit 1
fi

# The bridge is useless without a bus to register against. It would retry with
# backoff rather than die (register_with_retry), so this is a warning and not a
# hard failure - but starting a bridge on a box with no bus is almost always a
# mistake worth naming at install time rather than discovering in the log.
if ! launchctl list | grep -q "com.evansenter.agent-event-bus$"; then
    echo "Warning: the bus LaunchAgent does not appear to be loaded."
    echo "         The bridge will start and retry registration with backoff,"
    echo "         but it cannot deliver anything until the bus is up."
    echo "         Install it with: make install-server"
    echo ""
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$DATA_DIR"

if launchctl list | grep -q "$LABEL"; then
    echo "Stopping existing bridge service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

echo "Installing bridge LaunchAgent..."
sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__BRIDGE_LOG_FILE__|$BRIDGE_LOG_FILE|g" \
    -e "s|__BRIDGE_ERR_FILE__|$BRIDGE_ERR_FILE|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

echo "Starting bridge..."
launchctl load "$PLIST_DEST"

# Give it a moment to bind and attempt registration before probing. The
# listener binds during startup; registration runs in a background thread and
# may still be backing off against a bus that is not up yet.
sleep 2

if ! launchctl list | grep -q "$LABEL"; then
    echo "Error: bridge failed to start. Check $BRIDGE_ERR_FILE"
    exit 1
fi

echo ""
echo "Bridge installed and running."
echo "  Logs:   $BRIDGE_LOG_FILE"
echo "  Errors: $BRIDGE_ERR_FILE"
echo "  Health: curl -s http://127.0.0.1:8082/health"
echo ""

# registered:false is not a failure here - it means registration is still
# backing off (bus not up yet), which resolves on its own. Report what we see
# rather than asserting success we have not confirmed.
HEALTH="$(curl -fsS --max-time 3 http://127.0.0.1:8082/health 2>/dev/null || true)"
if [[ -n "$HEALTH" ]]; then
    echo "  /health -> $HEALTH"
    case "$HEALTH" in
        *'"registered":true'*|*'"registered": true'*)
            echo "  Registered on the bus." ;;
        *)
            echo "  Not registered yet - retrying with backoff (is the bus up?)." ;;
    esac
else
    echo "  /health did not answer yet; check $BRIDGE_ERR_FILE if this persists."
fi

echo ""
echo "NOTE: nothing drains wake/<session>.jsonl yet (agent-event-bus#134)."
echo "      The bridge will spool actionable DMs durably, but no session is"
echo "      woken by them until a drain hook exists."
echo ""
echo "To uninstall: $SCRIPT_DIR/uninstall-bridge-launchagent.sh"
