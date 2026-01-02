#!/bin/bash
# Test script for auto-notify on DMs feature
# Simulates two Claude Code sessions sending DMs via the event bus

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="${HOME}/.local/bin/event-bus-cli"

# Session IDs for cleanup
SESSION_A_ID=""
SESSION_B_ID=""

# Cleanup function to unregister sessions on exit
cleanup_sessions() {
    echo -e "\n${YELLOW}Cleaning up sessions...${NC}"
    [[ -n "$SESSION_A_ID" ]] && $CLI unregister --session-id "$SESSION_A_ID" 2>/dev/null && echo "  ✓ Unregistered $SESSION_A_ID" || true
    [[ -n "$SESSION_B_ID" ]] && $CLI unregister --session-id "$SESSION_B_ID" 2>/dev/null && echo "  ✓ Unregistered $SESSION_B_ID" || true
}

# Register cleanup trap for early exit
trap cleanup_sessions EXIT

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Event Bus DM Notification Test ===${NC}\n"

# Check if event-bus-cli exists
if [[ ! -x "$CLI" ]]; then
    echo -e "${RED}Error: event-bus-cli not found at $CLI${NC}"
    echo "Run 'make install' first to install the CLI"
    exit 1
fi

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo -e "${RED}Error: jq is required but not installed${NC}"
    echo "Install with: brew install jq"
    exit 1
fi

# Check if event bus server is running
if ! curl -s http://127.0.0.1:8080/health > /dev/null 2>&1; then
    echo -e "${YELLOW}Warning: Event bus server doesn't appear to be running${NC}"
    echo "Start it with: ./scripts/start.sh"
    echo ""
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo -e "${GREEN}Step 1: Register Session A (sender)${NC}"
SESSION_A_RESULT=$($CLI register --name "test-sender")
SESSION_A_ID=$(echo "$SESSION_A_RESULT" | jq -r '.session_id')
echo "  ✓ Registered as: $SESSION_A_ID"
echo

echo -e "${GREEN}Step 2: Register Session B (receiver)${NC}"
SESSION_B_RESULT=$($CLI register --name "test-receiver")
SESSION_B_ID=$(echo "$SESSION_B_RESULT" | jq -r '.session_id')
echo "  ✓ Registered as: $SESSION_B_ID"
echo

echo -e "${GREEN}Step 3: Verify both sessions are active${NC}"
$CLI sessions
echo

echo -e "${GREEN}Step 4: Send DM from Session A to Session B${NC}"
DM_MESSAGE="Hello from test-sender! Can you review my code? This is a test of the auto-notify feature with emoji 🎉"
echo "  Sending: \"$DM_MESSAGE\""
echo "  Channel: session:$SESSION_B_ID"
echo

# Send the DM
$CLI publish \
    --type "help_needed" \
    --payload "$DM_MESSAGE" \
    --session-id "$SESSION_A_ID" \
    --channel "session:$SESSION_B_ID" > /dev/null
echo "  ✓ Event published"
echo

echo -e "${YELLOW}⚠️  Did you see a macOS notification?${NC}"
echo "  Title should be: 📨 test-receiver • claude-event-bus"
echo "  Message should contain: From: test-sender"
echo "  And a preview of the DM message"
echo

read -p "Press Enter to check if Session B received the event..."
echo

echo -e "${GREEN}Step 5: Check events for Session B${NC}"
$CLI events --session-id "$SESSION_B_ID" --since 0
echo

echo -e "${GREEN}Step 6: Test with empty payload${NC}"
$CLI publish \
    --type "test" \
    --payload "" \
    --session-id "$SESSION_A_ID" \
    --channel "session:$SESSION_B_ID" > /dev/null
echo "  ✓ Sent DM with empty payload"
echo

echo -e "${GREEN}Step 7: Test with special characters${NC}"
SPECIAL_MESSAGE="Multi-line\nWith\ttabs\nAnd emoji 😊🎉\nNewlines too!"
$CLI publish \
    --type "test" \
    --payload "$SPECIAL_MESSAGE" \
    --session-id "$SESSION_A_ID" \
    --channel "session:$SESSION_B_ID" > /dev/null
echo "  ✓ Sent DM with special characters"
echo

echo -e "${GREEN}Step 8: Test notification to non-existent session${NC}"
$CLI publish \
    --type "test" \
    --payload "This should not notify" \
    --session-id "$SESSION_A_ID" \
    --channel "session:nonexistent-session" > /dev/null
echo "  ✓ Published to non-existent session (should not crash)"
echo

echo -e "${GREEN}Step 9: Test repo channel (should NOT notify)${NC}"
$CLI publish \
    --type "test" \
    --payload "Repo message" \
    --session-id "$SESSION_A_ID" \
    --channel "repo:claude-event-bus" > /dev/null
echo "  ✓ Published to repo channel (should NOT show notification)"
echo

echo -e "${BLUE}=== Test Complete ===${NC}\n"
echo "Summary of what was tested:"
echo "  ✓ Session registration with tips"
echo "  ✓ DM with normal payload (should show notification)"
echo "  ✓ DM with empty payload"
echo "  ✓ DM with special characters (emoji, newlines, tabs)"
echo "  ✓ DM to non-existent session (should not crash)"
echo "  ✓ Repo channel message (should NOT notify)"
echo "  ✓ Session cleanup (via EXIT trap)"
echo
echo -e "${YELLOW}Manual verification needed:${NC}"
echo "  1. Did you see notifications for DMs to session:$SESSION_B_ID?"
echo "  2. Did you NOT see notification for repo:claude-event-bus?"
echo
echo "Check server logs for any warnings:"
echo "  tail -f ~/.claude/contrib/event-bus/event-bus.log"
