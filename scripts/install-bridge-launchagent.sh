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

# Preflight the import rather than running `uv sync` here. A stale venv missing
# a bridge dependency would otherwise become an import crash-loop under
# KeepAlive - the one case where the log-truncation caveat bites hardest, since
# every respawn wipes the previous traceback. Checking beats syncing: `uv sync
# --no-dev` (what install-server runs) would silently strip pytest/ruff from a
# venv someone just set up with `make dev`.
if ! PYTHONPATH="$PROJECT_DIR/src" "$VENV_PYTHON" -c "import agent_event_bus.bridge" 2>/tmp/bridge-import-check.$$; then
    echo "Error: the venv cannot import agent_event_bus.bridge:"
    sed 's/^/  /' /tmp/bridge-import-check.$$
    rm -f /tmp/bridge-import-check.$$
    echo "Run: make dev   (or: uv sync)"
    exit 1
fi
rm -f /tmp/bridge-import-check.$$

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

if launchctl list | grep -q "$LABEL$"; then
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

# POLL rather than sleep a fixed interval. On a re-install over a live bridge,
# `launchctl unload` returns as soon as SIGTERM is delivered, so the outgoing
# process can still hold the singleton flock and port 8082 when the replacement
# starts. The replacement then exits on the lock - correctly; that ordering is
# what keeps the outgoing instance's unregister from racing a new registration
# - and KeepAlive only retries after ThrottleInterval (~10s). A flat 2s probe
# lands inside that window and reports a perfectly healthy idempotent
# re-install as a failure. 20s covers the throttle with room to spare.
HEALTH=""
for _ in $(seq 1 40); do
    HEALTH="$(curl -fsS --max-time 2 http://127.0.0.1:8082/health 2>/dev/null || true)"
    [[ -n "$HEALTH" ]] && break
    sleep 0.5
done

if ! launchctl list | grep -q "$LABEL$"; then
    echo "Error: bridge failed to start. Check $BRIDGE_ERR_FILE"
    exit 1
fi

echo ""
echo "Bridge installed and running."
# Labelled by what each file actually receives: bridge.py logs via
# logging.basicConfig with no stream=, which defaults to STDERR, so every
# bridge record lands in the .err file. The .log file gets uvicorn's access
# lines only. Following a "Logs:" pointer at the .log would show a reader no
# bridge messages at all.
echo "  Bridge log (records, warnings, errors): $BRIDGE_ERR_FILE"
echo "  Access log (uvicorn requests):          $BRIDGE_LOG_FILE"
echo "  Health: curl -s http://127.0.0.1:8082/health"
echo ""

# registered:false is not a failure here - it means registration is still
# backing off (bus not up yet), which resolves on its own. Report what we see
# rather than asserting success we have not confirmed.
if [[ -n "$HEALTH" ]]; then
    echo "  /health -> $HEALTH"
    case "$HEALTH" in
        *'"registered":true'*|*'"registered": true'*)
            echo "  Registered on the bus." ;;
        *)
            echo "  Not registered yet - retrying with backoff (is the bus up?)." ;;
    esac
else
    echo "  /health did not answer within 20s; check $BRIDGE_ERR_FILE."
fi

echo ""
echo "NOTE: nothing drains wake/<session>.jsonl yet (agent-event-bus#134)."
echo "      The bridge will spool actionable DMs durably, but no session is"
echo "      woken by them until a drain hook exists."
echo ""
echo "To uninstall: $SCRIPT_DIR/uninstall-bridge-launchagent.sh"
