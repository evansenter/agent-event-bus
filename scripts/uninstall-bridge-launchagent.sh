#!/bin/bash
# Remove the bridge LaunchAgent. Leaves the bus, the database, and the wake
# directory alone - this only stops supervising the bridge.

set -e

PLIST_DEST="$HOME/Library/LaunchAgents/com.evansenter.agent-event-bus-bridge.plist"
LABEL="com.evansenter.agent-event-bus-bridge"

if launchctl list | grep -q "$LABEL"; then
    echo "Stopping bridge..."
    # SIGTERM via unload, so the shutdown path runs: the lifespan's shielded
    # stop-join-unregister removes the webhook row rather than leaving the bus
    # POSTing at a dead port.
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
else
    echo "Bridge service not loaded."
fi

if [[ -f "$PLIST_DEST" ]]; then
    rm "$PLIST_DEST"
    echo "Removed $PLIST_DEST"
fi

echo ""
echo "Bridge uninstalled. The bus, the database, and wake/ are untouched."
echo "Confirm the webhook row is gone with: agent-event-bus-cli webhook list"
echo ""
echo "The wake directory is transient and safe to clear BY HAND once no"
echo "bridge is running (clearing it under a live bridge orphans its"
echo "singleton lock inode):"
echo "  ~/.claude/contrib/agent-event-bus/wake/"
