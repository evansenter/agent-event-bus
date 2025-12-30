#!/usr/bin/env python3
"""CLI wrapper for event bus - for use in shell scripts and hooks.

Usage:
    event-bus-cli register [--name NAME] [--pid PID]
    event-bus-cli unregister --session-id ID
    event-bus-cli sessions
    event-bus-cli publish --type TYPE --payload PAYLOAD [--channel CHANNEL] [--session-id ID]
    event-bus-cli events [--since ID] [--session-id ID]
    event-bus-cli notify --title TITLE --message MSG [--sound]

Examples:
    # Register session (for SessionStart hook)
    event-bus-cli register --name "my-feature" --pid $$

    # Unregister session (for SessionEnd hook)
    event-bus-cli unregister --session-id abc123

    # List active sessions
    event-bus-cli sessions

    # Publish an event
    event-bus-cli publish --type "task_done" --payload "Finished API" --channel "repo:my-project"

    # Get recent events
    event-bus-cli events --since 0 --session-id abc123

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


def call_tool(tool_name: str, arguments: dict, url: str = DEFAULT_URL) -> dict:
    """Call an MCP tool and return the result."""
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
            timeout=10,
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
    if args.pid:
        arguments["pid"] = args.pid
    arguments["cwd"] = os.getcwd()

    result = call_tool("register_session", arguments)
    print(json.dumps(result, indent=2))

    # Also print just the session_id for easy capture in scripts
    if "session_id" in result:
        print(f"\nSession ID: {result['session_id']}", file=sys.stderr)


def cmd_unregister(args):
    """Unregister a session."""
    result = call_tool("unregister_session", {"session_id": args.session_id})
    print(json.dumps(result, indent=2))


def cmd_sessions(args):
    """List active sessions."""
    result = call_tool("list_sessions", {})
    if not result:
        print("No active sessions")
        return

    print(f"Active sessions ({len(result)}):\n")
    for s in result:
        print(f"  {s['session_id']}  {s['name']}")
        print(f"    repo: {s['repo']}, machine: {s['machine']}")
        print(f"    age: {int(s['age_seconds'])}s, pid: {s.get('pid', 'N/A')}")
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

    result = call_tool("publish_event", arguments)
    print(json.dumps(result, indent=2))


def cmd_events(args):
    """Get recent events."""
    arguments = {}
    if args.since is not None:
        arguments["since_id"] = args.since
    if args.session_id:
        arguments["session_id"] = args.session_id

    result = call_tool("get_events", arguments)
    if not result:
        print("No events")
        return

    for e in result:
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

    result = call_tool("notify", arguments)
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
    p_register.add_argument("--pid", type=int, help="Process ID for deduplication")
    p_register.set_defaults(func=cmd_register)

    # unregister
    p_unregister = subparsers.add_parser("unregister", help="Unregister a session")
    p_unregister.add_argument("--session-id", required=True, help="Session ID")
    p_unregister.set_defaults(func=cmd_unregister)

    # sessions
    p_sessions = subparsers.add_parser("sessions", help="List active sessions")
    p_sessions.set_defaults(func=cmd_sessions)

    # publish
    p_publish = subparsers.add_parser("publish", help="Publish an event")
    p_publish.add_argument("--type", required=True, help="Event type")
    p_publish.add_argument("--payload", required=True, help="Event payload")
    p_publish.add_argument("--channel", default="all", help="Target channel")
    p_publish.add_argument("--session-id", help="Your session ID")
    p_publish.set_defaults(func=cmd_publish)

    # events
    p_events = subparsers.add_parser("events", help="Get recent events")
    p_events.add_argument("--since", type=int, default=0, help="Get events after this ID")
    p_events.add_argument("--session-id", help="Your session ID (for filtering)")
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
