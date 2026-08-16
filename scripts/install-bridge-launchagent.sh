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

# Preflight the imports rather than running `uv sync` here. uvicorn is listed
# explicitly because bridge.py imports it lazily INSIDE main(), so importing
# the module alone would not surface its absence - and a missing uvicorn is
# precisely the crash-loop this check exists to prevent. A stale venv missing
# a bridge dependency would otherwise become an import crash-loop under
# KeepAlive - respawning every ThrottleInterval forever, never serving a
# delivery, with /health never answering to say so. Checking beats syncing: `uv sync
# --no-dev` (what install-server runs) would silently strip pytest/ruff from a
# venv someone just set up with `make dev`.
# mktemp, not a predictable /tmp path: the 2> redirect follows symlinks, so a
# pre-planted file of a guessable name would be truncated as the installing
# user. The trap also covers an interrupt between creation and cleanup.
IMPORT_ERR="$(mktemp "${TMPDIR:-/tmp}/agent-event-bus-bridge-import.XXXXXX")"
trap 'rm -f "$IMPORT_ERR"' EXIT
# uvicorn is named explicitly because bridge.py imports it lazily INSIDE
# main(), so importing the module alone would not surface its absence. Keep
# this list in sync with the deferred imports in bridge.py:main() - a second
# one added there silently reopens the crash-loop hole this check closes.
if ! PYTHONPATH="$PROJECT_DIR/src" "$VENV_PYTHON" -c "import agent_event_bus.bridge, uvicorn" 2>"$IMPORT_ERR"; then
    echo "Error: the venv cannot import the bridge and its runtime deps:"
    sed 's/^/  /' "$IMPORT_ERR"
    echo "Run: make dev   (or: uv sync)"
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
# The log paths are overridable and may point outside DATA_DIR; launchd
# cannot create a missing parent for StandardOutPath/StandardErrorPath and
# the job simply fails to start.
mkdir -p "$(dirname "$BRIDGE_LOG_FILE")" "$(dirname "$BRIDGE_ERR_FILE")"

REPLACED_LOADED_JOB=false
if launchctl list | grep -q "$LABEL$"; then
    echo "Stopping existing bridge service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    REPLACED_LOADED_JOB=true
fi

echo "Installing bridge LaunchAgent..."
sed -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__BRIDGE_LOG_FILE__|$BRIDGE_LOG_FILE|g" \
    -e "s|__BRIDGE_ERR_FILE__|$BRIDGE_ERR_FILE|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

echo "Starting bridge..."
launchctl load "$PLIST_DEST"

# POLL rather than sleep a fixed interval - but only after the handoff has
# had time to resolve, when there was one. `launchctl unload` returns as soon
# as SIGTERM is delivered, and the outgoing bridge keeps port 8082 through its
# shielded stop-join-unregister. So an immediate probe is answered by the
# instance that is about to DIE: HEALTH would be set from it, the loop would
# break, and the `launchctl list` check below reports only that the JOB is
# loaded, not that a process is alive - so the script would print "installed
# and running" and the outgoing registered: value while the replacement had
# just exited on the singleton flock, not to return for ~ThrottleInterval.
# /health carries nothing instance-specific to tell them apart, so wait the
# throttle out first. Skipped on a fresh install, where no job was displaced.
#
# The 12 is ThrottleInterval (10, set in the plist) plus margin, and the two
# are coupled with nothing but this comment to link them: raise the plist's
# ThrottleInterval without raising this and the gate under-waits, letting the
# dying instance answer the poll again - the exact failure it was added to
# close. The plist comment points back here.
#
# Two honest limits. The flag is set from `launchctl list`, which reports the
# JOB is loaded, not that a process is alive - a loaded-but-dead job pays the
# wait for no handoff. And 12s bounds ThrottleInterval, not the outgoing
# shutdown: an unregister retrying against a slow bus can outlive it, and an
# unsupervised `uv run agent-event-bus-bridge` already holding 8082 produces
# the same shape on the fresh-install path. Both dissolve once /health carries
# something instance-specific (a pid or start timestamp), which would let the
# poll assert it is talking to the new process and drop this sleep entirely.
if [[ "$REPLACED_LOADED_JOB" == true ]]; then
    echo "Waiting out the restart throttle so /health reports the NEW instance..."
    sleep 12
fi

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
    # No elapsed figure: the poll's real bound is 20s only if every probe
    # fails instantly (40 x sleep 0.5) and ~100s if they all hang out to
    # --max-time 2, and the re-install path adds the 12s throttle wait ahead
    # of it. The log path is what the operator needs anyway.
    echo "  /health did not answer; check $BRIDGE_ERR_FILE."
fi

echo ""
# Was "nothing drains the spool, so no session is woken" - true until the mux
# backend landed (#149), and printed by this script for months after it
# stopped being true. Sessions are now woken by INJECTION, not by draining the
# spool; the spool is durable bookkeeping and the fallback for anything that
# cannot be injected. What actually gates a wake is the pane mapping, so name
# that instead - it is the part an operator has to install.
if [[ -s "$DATA_DIR/wake/panes.json" ]] && grep -q '[^[:space:]{}]' "$DATA_DIR/wake/panes.json" 2>/dev/null; then
    echo "NOTE: wake/panes.json has entries, so mapped sessions on this host can"
    echo "      be woken by a DM to their session_id. Unmapped sessions (and"
    echo "      sessions on other machines) are spooled only."
else
    echo "NOTE: wake/panes.json is empty, so nothing here can be woken yet -"
    echo "      every DM will spool and resolve to \"spool-unmapped\"."
    echo "      Sessions map themselves via a SessionStart hook running:"
    echo "        agent-event-bus-cli panes set --session-id \"\$SESSION_ID\""
    echo "      See docs/BRIDGE.md, \"Maintaining the pane mapping\"."
fi
echo ""
echo "To uninstall: $SCRIPT_DIR/uninstall-bridge-launchagent.sh"
