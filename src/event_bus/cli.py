#!/usr/bin/env python3
"""CLI wrapper for event bus - for use in shell scripts and automation.

Usage:
    event-bus-cli register [--name NAME] [--client-id ID]
    event-bus-cli unregister [--session-id ID | --client-id ID]
    event-bus-cli sessions
    event-bus-cli channels
    event-bus-cli publish --type TYPE --payload PAYLOAD [--channel CHANNEL] [--session-id ID]
    event-bus-cli events [--cursor CURSOR] [--session-id ID] [--limit N] [--exclude-types T1,T2]
                         [--timeout MS] [--track-state FILE] [--json] [--order asc|desc]
                         [--channel CHANNEL]
    event-bus-cli notify --title TITLE --message MSG [--sound]

Examples:
    # Register a session
    event-bus-cli register --name "my-feature" --client-id "abc123"

    # Unregister by session_id or client_id
    event-bus-cli unregister --session-id abc123
    event-bus-cli unregister --client-id abc123

    # List active sessions
    event-bus-cli sessions

    # List active channels
    event-bus-cli channels

    # Publish an event
    event-bus-cli publish --type "task_done" --payload "Finished API" --channel "repo:my-project"

    # Get recent events (newest first by default)
    event-bus-cli events --session-id abc123

    # Get events with JSON output (for scripting)
    event-bus-cli events --json --limit 10 --exclude-types session_registered,session_unregistered

    # Get events in chronological order (oldest first)
    event-bus-cli events --order asc

    # Get events from a specific channel
    event-bus-cli events --channel "repo:my-project"

    # Send notification
    event-bus-cli notify --title "Build Complete" --message "All tests passed"
"""

import argparse
import json
import os
import sys

import requests

DEFAULT_URL = "http://127.0.0.1:8080/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def call_tool(
    tool_name: str, arguments: dict, url: str = DEFAULT_URL, timeout_ms: int | None = None
) -> dict:
    """Call an MCP tool and return the result.

    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments
        url: Event bus URL
        timeout_ms: Timeout in milliseconds (default: 10000)
    """
    timeout_sec = (timeout_ms / 1000) if timeout_ms else 10
    try:
        resp = requests.post(
            url or DEFAULT_URL,
            headers=HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            timeout=timeout_sec,
        )
        resp.raise_for_status()

        # Parse SSE response
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                result = data.get("result", {})
                # Try structured content first, fall back to text
                structured = result.get("structuredContent", {}).get("result")
                if structured is not None:
                    return structured
                content = result.get("content", [])
                if content and content[0].get("text"):
                    return json.loads(content[0]["text"])
                return result
        return {}
    except requests.exceptions.ConnectionError:
        print("Error: Cannot connect to event bus. Is it running?", file=sys.stderr)
        print("Start with: event-bus", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_register(args):
    """Register a session."""
    arguments = {}
    if args.name:
        arguments["name"] = args.name
    else:
        # Default to current directory name
        arguments["name"] = os.path.basename(os.getcwd())
    if args.client_id:
        arguments["client_id"] = args.client_id
    arguments["cwd"] = os.getcwd()

    result = call_tool("register_session", arguments, url=args.url)
    print(json.dumps(result, indent=2))

    # Also print just the session_id for easy capture in scripts
    if "session_id" in result:
        print(f"\nSession ID: {result['session_id']}", file=sys.stderr)


def cmd_unregister(args):
    """Unregister a session."""
    arguments = {}
    if args.session_id:
        arguments["session_id"] = args.session_id
    if args.client_id:
        arguments["client_id"] = args.client_id

    if not arguments:
        print("Error: Must provide --session-id or --client-id", file=sys.stderr)
        sys.exit(1)

    result = call_tool("unregister_session", arguments, url=args.url)
    print(json.dumps(result, indent=2))


def cmd_sessions(args):
    """List active sessions."""
    result = call_tool("list_sessions", {}, url=args.url)
    if not result:
        print("No active sessions")
        return

    print(f"Active sessions ({len(result)}):\n")
    for s in result:
        print(f"  {s['session_id']}  {s['name']}")
        print(f"    repo: {s['repo']}, machine: {s['machine']}")
        print(f"    age: {int(s['age_seconds'])}s, client_id: {s.get('client_id', 'N/A')}")
        channels = s.get("subscribed_channels", [])
        if channels:
            print(f"    channels: {', '.join(channels)}")
        print()


def cmd_channels(args):
    """List active channels."""
    result = call_tool("list_channels", {}, url=args.url)
    if not result:
        print("No active channels")
        return

    print(f"Active channels ({len(result)}):\n")
    for ch in result:
        channel_name = ch.get("channel", "<unknown>")
        subscriber_count = ch.get("subscribers", 0)
        print(
            f"  {channel_name}  ({subscriber_count} subscriber{'s' if subscriber_count != 1 else ''})"
        )
    print()


def cmd_publish(args):
    """Publish an event."""
    arguments = {
        "event_type": args.type,
        "payload": args.payload,
    }
    if args.channel:
        arguments["channel"] = args.channel
    if args.session_id:
        arguments["session_id"] = args.session_id

    result = call_tool("publish_event", arguments, url=args.url)
    print(json.dumps(result, indent=2))


def cmd_events(args):
    """Get recent events."""
    # Handle --track-state: read cursor from file
    cursor = args.cursor
    if args.track_state:
        state_path = os.path.expanduser(args.track_state)
        try:
            with open(state_path) as f:
                cursor = f.read().strip() or None
        except FileNotFoundError:
            cursor = None  # Start from beginning if file doesn't exist

    arguments = {"order": args.order}
    if cursor is not None:
        arguments["cursor"] = cursor
    if args.limit is not None:
        arguments["limit"] = args.limit
    if args.session_id:
        arguments["session_id"] = args.session_id
    if args.channel:
        arguments["channel"] = args.channel

    result = call_tool("get_events", arguments, url=args.url, timeout_ms=args.timeout)

    # Result is now a dict with "events" and "next_cursor"
    events = result.get("events", [])
    next_cursor = result.get("next_cursor")

    # Apply --exclude-types filter
    if args.exclude_types:
        exclude_set = {t.strip() for t in args.exclude_types.split(",")}
        events = [e for e in events if e["event_type"] not in exclude_set]

    # Handle --track-state: write next_cursor to file
    if args.track_state and next_cursor:
        state_path = os.path.expanduser(args.track_state)
        # Ensure parent directory exists
        state_dir = os.path.dirname(state_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        with open(state_path, "w") as f:
            f.write(next_cursor)

    # Output format
    if args.json:
        output = {"events": events, "next_cursor": next_cursor}
        print(json.dumps(output))
    else:
        if not events:
            print("No events")
            return
        for e in events:
            print(f"[{e['id']}] {e['event_type']} ({e['channel']})")
            print(f"    {e['payload']}")
            print(f"    from: {e['session_id']} at {e['timestamp']}")
            print()


def cmd_notify(args):
    """Send a system notification."""
    arguments = {
        "title": args.title,
        "message": args.message,
    }
    if args.sound:
        arguments["sound"] = True

    result = call_tool("notify", arguments, url=args.url)
    if result.get("success"):
        print("Notification sent")
    else:
        print("Notification failed", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="CLI wrapper for claude-event-bus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("EVENT_BUS_URL", DEFAULT_URL),
        help="Event bus URL (default: http://127.0.0.1:8080/mcp)",
    )

    subparsers = parser.add_subparsers(dest="command")

    # register
    p_register = subparsers.add_parser("register", help="Register a session")
    p_register.add_argument("--name", help="Session name (default: directory name)")
    p_register.add_argument(
        "--client-id", help="Client identifier for deduplication (e.g., CC session ID or PID)"
    )
    p_register.set_defaults(func=cmd_register)

    # unregister
    p_unregister = subparsers.add_parser("unregister", help="Unregister a session")
    p_unregister.add_argument("--session-id", help="Session ID")
    p_unregister.add_argument(
        "--client-id",
        help="Client ID (alternative to --session-id, looks up by machine + client_id)",
    )
    p_unregister.set_defaults(func=cmd_unregister)

    # sessions
    p_sessions = subparsers.add_parser("sessions", help="List active sessions")
    p_sessions.set_defaults(func=cmd_sessions)

    # channels
    p_channels = subparsers.add_parser("channels", help="List active channels")
    p_channels.set_defaults(func=cmd_channels)

    # publish
    p_publish = subparsers.add_parser("publish", help="Publish an event")
    p_publish.add_argument("--type", required=True, help="Event type")
    p_publish.add_argument("--payload", required=True, help="Event payload")
    p_publish.add_argument("--channel", default="all", help="Target channel")
    p_publish.add_argument("--session-id", help="Your session ID")
    p_publish.set_defaults(func=cmd_publish)

    # events
    p_events = subparsers.add_parser("events", help="Get recent events")
    p_events.add_argument("--cursor", help="Cursor from previous call (for pagination)")
    p_events.add_argument("--session-id", help="Your session ID (for filtering)")
    p_events.add_argument("--limit", type=int, help="Maximum number of events to return")
    p_events.add_argument(
        "--exclude-types",
        help="Comma-separated event types to exclude (e.g., session_registered,session_unregistered)",
    )
    p_events.add_argument(
        "--timeout",
        type=int,
        default=10000,
        help="Request timeout in milliseconds (default: 10000)",
    )
    p_events.add_argument(
        "--track-state",
        help="File to read/write cursor for incremental polling",
    )
    p_events.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON with events array and next_cursor",
    )
    p_events.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="desc",
        help="Ordering: 'desc' for newest first (default), 'asc' for oldest first",
    )
    p_events.add_argument(
        "--channel",
        help="Filter to a specific channel (e.g., 'repo:my-project', 'all')",
    )
    p_events.set_defaults(func=cmd_events)

    # notify
    p_notify = subparsers.add_parser("notify", help="Send system notification")
    p_notify.add_argument("--title", required=True, help="Notification title")
    p_notify.add_argument("--message", required=True, help="Notification message")
    p_notify.add_argument("--sound", action="store_true", help="Play sound")
    p_notify.set_defaults(func=cmd_notify)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        print("\nUse -h or --help with any command for more details.")
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
