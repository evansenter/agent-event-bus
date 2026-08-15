#!/usr/bin/env python3
"""CLI wrapper for agent event bus - for use in shell scripts and automation.

Usage:
    agent-event-bus-cli register [--name NAME] [--client-id ID]
    agent-event-bus-cli unregister [--session-id ID | --client-id ID]
    agent-event-bus-cli sessions
    agent-event-bus-cli channels
    agent-event-bus-cli publish --type TYPE --payload PAYLOAD [--channel CHANNEL] [--session-id ID]
                         [--title TITLE] [--tags T1,T2] [--correlation-id ID]
                         [--signal-level lifecycle|info|actionable]
    agent-event-bus-cli events [--cursor CURSOR] [--session-id ID] [--limit N] [--include T1,T2]
                         [--exclude T1,T2] [--timeout MS] [--json] [--order asc|desc]
                         [--channel CHANNEL] [--resume] [--peek] [--correlation-id ID]
                         [--min-level lifecycle|info|actionable]
    agent-event-bus-cli ack --cursor N [--session-id ID] [--allow-rewind] [--json]
    agent-event-bus-cli notify --title TITLE --message MSG [--sound]
    agent-event-bus-cli panes set [--session-id ID] [--mux tmux|zellij --pane ID
                         [--mux-session NAME]] [--wake-dir DIR] [--json]
    agent-event-bus-cli panes clear [--session-id ID] [--keep-pane-entries]
    agent-event-bus-cli wake-state busy|idle [--session-id ID] [--wake-dir DIR]
    agent-event-bus-cli webhook register --url URL [--channel CH] [--event-types T1,T2] [--secret S]
    agent-event-bus-cli webhook list [--all]
    agent-event-bus-cli webhook disable WEBHOOK_ID
    agent-event-bus-cli webhook enable WEBHOOK_ID
    agent-event-bus-cli webhook unregister WEBHOOK_ID

Examples:
    # Register a session
    agent-event-bus-cli register --name "my-feature" --client-id "abc123"

    # Unregister by session_id or client_id
    agent-event-bus-cli unregister --session-id abc123
    agent-event-bus-cli unregister --client-id abc123

    # List active sessions
    agent-event-bus-cli sessions

    # List active channels
    agent-event-bus-cli channels

    # Publish an event
    agent-event-bus-cli publish --type "task_done" --payload "Finished API" --channel "repo:my-project"

    # Get recent events (newest first by default)
    agent-event-bus-cli events --session-id abc123

    # Get events with JSON output (for scripting)
    agent-event-bus-cli events --json --limit 10 --exclude session_registered,session_unregistered

    # Get events in chronological order (oldest first)
    agent-event-bus-cli events --order asc

    # Get events from a specific channel
    agent-event-bus-cli events --channel "repo:my-project"

    # Resume from saved cursor (incremental polling - no duplicates)
    agent-event-bus-cli events --session-id abc123 --resume --order asc

    # Peek: read new events without consuming them (cursor stays put)
    agent-event-bus-cli events --session-id abc123 --resume --peek

    # Filter by event type
    agent-event-bus-cli events --include task_completed,ci_completed
    agent-event-bus-cli events --include gotcha_discovered,pattern_found --exclude session_registered

    # Drop lifecycle noise server-side (lifecycle < info < actionable)
    agent-event-bus-cli events --min-level info

    # Thread a request to its responses with a correlation id
    agent-event-bus-cli publish --type task_request --payload "Review PR #42?" --correlation-id review-42
    agent-event-bus-cli events --correlation-id review-42 --order asc

    # Drain safely under a server-side filter: peek, act, then ack what you saw.
    # A bounded consume cannot do this - min-level filters the view while the
    # cursor advances over the raw batch, so the counts refer to different
    # windows. Peek and ack refer to the same window by construction.
    # A pass drains at most --limit (default 50), so re-peek until has_more is
    # false to clear a backlog in one hook run. The peek belongs INSIDE the
    # loop - the ack moves the cursor, so the next pass is a fresh window.
    while :; do
        # A session deleted or timed out mid-drain FAILS this poll rather than
        # returning an empty batch, and `events` exits non-zero on it - so
        # under `set -e` the hook aborts here, loudly. Without it, .has_more
        # reads null on the error object and the loop breaks instead.
        OUT=$(agent-event-bus-cli events --session-id "$SID" --resume --peek \
                --min-level actionable --order asc --json)
        echo "$OUT" | jq -r '.events[].payload'
        # `// empty` + the test: on a bus with no events at all next_cursor is
        # null, and `jq -r` would print the string "null" for an ack to reject.
        CUR=$(echo "$OUT" | jq -r '.next_cursor // empty')
        # `if`, not `[ -n "$CUR" ] && ...`: lifted out of this loop as a hook's
        # last command, the && form would exit 1 on the nothing-to-ack run and
        # abort early under `set -e`.
        if [ -n "$CUR" ]; then
            agent-event-bus-cli ack --session-id "$SID" --cursor "$CUR"
        fi
        # has_more, NOT the event count: it counts the RAW batch, before
        # --min-level filters it, so a pass of 50 lifecycle events prints
        # nothing and still has more to drain.
        [ "$(echo "$OUT" | jq -r '.has_more')" = "true" ] || break
    done

    # Send notification
    agent-event-bus-cli notify --title "Build Complete" --message "All tests passed"
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

# The wake-dir contract. Importing it (rather than re-implementing the write
# in the shell hook that calls this) is what keeps the writer and the bridge's
# reader from drifting - a mismatch between them produces no error anywhere,
# just wakes that never happen.
from agent_event_bus.wake import (
    InvalidTargetError,
    MuxTarget,
    clear_busy,
    clear_pane_entry,
    detect_target,
    parse_target,
    set_busy,
    set_pane_entry,
    wake_dir_from_env,
)

DEFAULT_URL = "http://127.0.0.1:8080/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class BusError(Exception):
    """A call to the event bus failed."""


class BusUnreachableError(BusError):
    """Nothing answered at the bus URL - wrong address, or the bus is down.

    Distinguished from every other failure because it is the one that means
    "try again later" rather than "this request was wrong": the bridge
    retries on it at boot, while a 401 or a malformed body is a real fault.
    """


def call_tool(
    tool_name: str,
    arguments: dict,
    url: str = DEFAULT_URL,
    timeout_ms: int | None = None,
) -> dict:
    """Call an MCP tool and return the result.

    Raises BusUnreachableError when nothing answers at `url`, and lets every other
    failure (HTTP status, timeout, undecodable body) propagate with its own
    type and message.

    This is a library function, not a command: it never prints and never
    exits. Rendering a failure and picking an exit code belong to main().
    That split matters beyond tidiness - bridge.py imports this function, and
    it needs to tell a retryable "bus not up yet" from a genuine fault by
    exception type rather than by inspecting a process exit code.

    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments
        url: Event bus URL
        timeout_ms: Timeout in milliseconds (default: 10000)
    """
    timeout_sec = (timeout_ms / 1000) if timeout_ms else 10
    target = url or DEFAULT_URL
    try:
        resp = requests.post(
            target,
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
    except requests.exceptions.ConnectionError as e:
        raise BusUnreachableError(f"Cannot connect to agent event bus at {target}") from e

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

    # Print session info for easy capture in scripts
    if "session_id" in result:
        display_id = result.get("display_id") or result.get("session_id")
        print(f"\nRegistered as: {display_id}", file=sys.stderr)
        # Only show session_id if different from display_id (UUID case)
        if result.get("session_id") != display_id:
            print(f"Session ID: {result['session_id']}", file=sys.stderr)


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

    if "error" in result:
        # The hint, like cmd_publish/cmd_events/cmd_ack. This is the surface
        # shutdown hooks actually call, and the deleted-session hint is the
        # actionable half - without it the caller gets a better error string
        # and nothing to do about it. Exit code unchanged: it errored before,
        # it errors now, only the reason is right.
        message = f"Error: {result['error']}"
        if result.get("hint"):
            message += f"\n{result['hint']}"
        print(message, file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


def cmd_sessions(args):
    """List active sessions."""
    result = call_tool("list_sessions", {}, url=args.url)
    if not result:
        print("No active sessions")
        return

    print(f"Active sessions ({len(result)}):\n")
    for s in result:
        # Show display_id (human-readable) prominently, with name
        display_id = s.get("display_id") or s.get("session_id", "?")
        print(f"  {display_id}  {s['name']}")
        print(f"    repo: {s['repo']}, machine: {s['machine']}")
        # Show client_id if present (needed for statusline lookup)
        client_id = s.get("client_id")
        if client_id:
            print(f"    client_id: {client_id}")
        print(f"    age: {int(s['age_seconds'])}s")
        # Show session_id (UUID) separately if different from display_id
        session_id = s.get("session_id", "")
        if session_id and session_id != display_id:
            # Truncate long UUIDs for display
            if len(session_id) > 16:
                session_id = session_id[:8] + "…"
            print(f"    session_id: {session_id}")
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


def _session_id_from_env() -> str | None:
    """Session id for an omitted --session-id, in precedence order.

    AGENT_EVENT_BUS_SESSION_ID is the explicit, tool-agnostic knob and wins.
    CLAUDE_CODE_SESSION_ID is the fallback because Claude Code injects it into
    every subprocess it spawns, so it is present exactly where the explicit one
    tends not to be.

    The fallback exists because setting the explicit var from a shell profile
    cannot be relied on: rc files are read for INTERACTIVE shells only, and a
    tool-spawned subprocess is not interactive, so any profile-based mapping
    is simply absent there and publishes land as "anonymous" (`zsh -i -c`
    sees such a mapping, `zsh -c` does not). That is shell startup semantics
    rather than a property of any OS or any particular dotfile layout, so the
    fix belongs here, where it holds for every shell, spawner, and machine.

    The two ids are the same value by construction: the SessionStart hook
    registers on the bus with client_id = the Claude Code session id, which the
    bus adopts as its session id.
    """
    return os.environ.get("AGENT_EVENT_BUS_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")


def cmd_publish(args):
    """Publish an event."""
    arguments = {
        "event_type": args.type,
        "payload": args.payload,
    }
    if args.channel:
        arguments["channel"] = args.channel
    # Use explicit --session-id, fall back to env var
    session_id = args.session_id or _session_id_from_env()
    if session_id:
        arguments["session_id"] = session_id
    # Optional structured payload fields (RFC #121)
    if args.title:
        arguments["title"] = args.title
    if args.tags:
        arguments["tags"] = [t.strip() for t in args.tags.split(",")]
    if args.correlation_id:
        arguments["correlation_id"] = args.correlation_id
    if args.signal_level:
        arguments["signal_level"] = args.signal_level

    result = call_tool("publish_event", arguments, url=args.url)
    print(json.dumps(result, indent=2))

    if result.get("session_deleted"):
        # Exit status stays 0 and the event id is real: #144 stores the event
        # and flags it. The warning goes to stderr because the callers this
        # actually reaches are hooks that discard stdout, and it names the id
        # the operator would grep for.
        #
        # Only the preamble is written here - naming the id an operator would
        # grep for is the CLI-specific half. The rest is the server's `hint`,
        # echoed rather than reworded: a second wording of one contract drifts,
        # and this one had already dropped "will not come back on its own",
        # which is what makes re-registering mandatory rather than optional.
        name = result.get("display_id") or session_id
        warning = f"Warning: session {name} was deleted at {result.get('deleted_at')}."
        if result.get("hint"):
            warning += f" {result['hint']}"
        print(warning, file=sys.stderr)


def cmd_events(args):
    """Get recent events."""
    # Use explicit --session-id, fall back to env var (matches cmd_publish)
    session_id = args.session_id or _session_id_from_env()

    # Validate --resume requires a session id (flag or env var)
    if args.resume and not session_id:
        print("Error: --resume requires --session-id", file=sys.stderr)
        sys.exit(1)

    cursor = args.cursor
    arguments = {"order": args.order}
    if cursor is not None:
        arguments["cursor"] = cursor
    if args.limit is not None:
        arguments["limit"] = args.limit
    if session_id:
        arguments["session_id"] = session_id
    if args.channel:
        arguments["channel"] = args.channel
    if args.resume:
        arguments["resume"] = True
    if args.peek:
        arguments["peek"] = True
    if args.include:
        arguments["event_types"] = [t.strip() for t in args.include.split(",")]
    if args.correlation_id:
        arguments["correlation_id"] = args.correlation_id
    if args.min_level:
        arguments["min_level"] = args.min_level

    result = call_tool("get_events", arguments, url=args.url, timeout_ms=args.timeout)

    # Check for server-side errors (e.g., session not found or deleted)
    if "error" in result:
        if args.json:
            print(json.dumps(result))
        else:
            message = f"Error: {result['error']}"
            # #140: the hint carries the actionable half ("re-register or stop
            # polling") - dropping it leaves an orphaned poller with no next step
            if result.get("hint"):
                message += f"\n{result['hint']}"
            print(message, file=sys.stderr)
        sys.exit(1)

    # Result is now a dict with "events", "next_cursor", and "has_more"
    events = result.get("events", [])
    next_cursor = result.get("next_cursor")
    has_more = result.get("has_more", False)

    # Apply --exclude filter (client-side for flexibility)
    if args.exclude:
        exclude_set = {t.strip() for t in args.exclude.split(",")}
        events = [e for e in events if e["event_type"] not in exclude_set]

    # Output format
    if args.json:
        output = {"events": events, "next_cursor": next_cursor, "has_more": has_more}
        print(json.dumps(output))
    else:
        if not events:
            print("No events")
        for e in events:
            header = f"[{e['id']}] {e['event_type']} ({e['channel']})"
            if e.get("signal_level"):
                header += f" [{e['signal_level']}]"
            print(header)
            if e.get("title"):
                print(f"    title: {e['title']}")
            print(f"    {e['payload']}")
            from_line = f"    from: {e['session_id']} at {e['timestamp']}"
            if e.get("correlation_id"):
                from_line += f" corr:{e['correlation_id']}"
            if e.get("tags"):
                from_line += f" tags:{','.join(e['tags'])}"
            print(from_line)
            print()
        if has_more:
            # Without this hint a large backlog gets silently truncated on
            # screen; the actionable next step depends on the order
            if args.order == "asc":
                hint = f"More events available; re-poll with --cursor {next_cursor}."
            else:
                hint = "More events available; use --order asc with --cursor to drain the backlog."
            print(hint, file=sys.stderr)


def cmd_ack(args):
    """Advance the session cursor to an event id already held."""
    session_id = args.session_id or _session_id_from_env()
    if not session_id:
        print(
            "Error: ack requires --session-id (or $AGENT_EVENT_BUS_SESSION_ID, "
            "else $CLAUDE_CODE_SESSION_ID)",
            file=sys.stderr,
        )
        sys.exit(1)

    arguments = {"session_id": session_id, "cursor": args.cursor}
    if args.allow_rewind:
        arguments["allow_rewind"] = True

    result = call_tool("ack_events", arguments, url=args.url)

    if "error" in result:
        # Mirrors cmd_events exactly, both halves. --json gets the error dict on
        # stdout, so a drain hook can branch on session_deleted instead of
        # parsing an empty stdout; otherwise the human form carries the hint,
        # which is the actionable half ("re-register or stop polling"). Either
        # way this is the same #140 shape a poll returns - a verb that dropped
        # one half would make this the one place the contract is not shared.
        if args.json:
            print(json.dumps(result))
        else:
            message = f"Error: {result['error']}"
            if result.get("hint"):
                message += f"\n{result['hint']}"
            print(message, file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result))
    else:
        # "start", not a dangling arrow, and the same word `make logs` uses
        # for this case - one event should not read two ways.
        previous = result.get("previous_cursor") or "start"
        print(f"Cursor acked: {previous} → {result['cursor']}")


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


def _resolve_wake_session_id(args) -> str:
    """Session id for the wake-dir commands, or exit 2.

    Exit 2 rather than 1 so a hook can tell "you didn't tell me which
    session" (a wiring bug it should surface) from a bus or filesystem
    failure. Neither is worth failing a hook over, but they need different
    fixes.
    """
    session_id = args.session_id or _session_id_from_env()
    if not session_id:
        print(
            "No session id: pass --session-id, or set AGENT_EVENT_BUS_SESSION_ID "
            "/ CLAUDE_CODE_SESSION_ID",
            file=sys.stderr,
        )
        sys.exit(2)
    return session_id


def cmd_panes(args):
    """Maintain <wake_dir>/panes.json - the bridge's session -> pane mapping.

    Writing this file is what makes the mux backend able to wake anything;
    without it every DM resolves to spool-unmapped. It lives here rather than
    in the session hook because the contract has two halves that must agree
    (see wake.py), and because the locking half is not expressible in portable
    shell - macOS ships no flock(1).
    """
    session_id = _resolve_wake_session_id(args)
    wake_dir = Path(args.wake_dir) if args.wake_dir else wake_dir_from_env()

    # Both verbs run at a session lifecycle boundary, and reaching either one
    # PROVES no turn is in flight - so both clear the turn-state marker.
    #
    # Not a convenience. The idle gate's no-TTL argument rests on the marker
    # self-healing at SessionStart, and wiring that only into a separate
    # `wake-state idle` call would make the invariant depend on an operator
    # remembering a second command: a session hard-killed mid-turn and then
    # resumed would map its pane correctly and still sit gated `spool-busy`
    # while idle, until its human happened to complete a turn. At SessionEnd
    # it also stops the marker leaking - `wake.py` deliberately names it
    # outside the `<sid>.jsonl*` glob the spool-pruning follow-up will sweep,
    # so nothing else would ever collect it.
    #
    # Before the target detection below, not after: `set` returns early
    # outside a multiplexer, and clearing turn state does not depend on
    # having a pane to wake.
    clear_busy(wake_dir, session_id)

    if args.panes_command == "set":
        if args.mux:
            target = MuxTarget(mux=args.mux, pane=args.pane, session=args.mux_session)
            try:
                # Validate explicit input through the same parser the bridge
                # reads with, so a hand-written entry cannot be accepted here
                # and silently ignored there.
                target = parse_target(target.to_json())
            except InvalidTargetError as e:
                print(f"Invalid target: {e}", file=sys.stderr)
                sys.exit(2)
        else:
            target = detect_target()
        if target is None:
            # NOT an error, and specifically NOT a null/empty entry: a session
            # outside any multiplexer has no pane to wake, and the contract
            # says omit rather than write a placeholder. An omitted entry
            # takes the bridge's quiet absent path; a null one warns and asks
            # an operator to repair something that is working as intended.
            if args.json:
                print(json.dumps({"skipped": "no multiplexer detected"}))
            else:
                print("No multiplexer detected; pane mapping not written")
            return
        result = set_pane_entry(wake_dir, session_id, target)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"Mapped {session_id} -> {target.describe()}")
            if result["evicted"]:
                # Worth a line: it means a previous session died in this pane
                # without its SessionEnd hook running, which is also the
                # condition that would have had the bridge type into it.
                print(f"Evicted stale mapping(s) on the same pane: {', '.join(result['evicted'])}")
        return

    # clear
    target = None if args.keep_pane_entries else detect_target()
    result = clear_pane_entry(wake_dir, session_id, target)
    if args.json:
        print(json.dumps(result))
    else:
        print(f"Cleared pane mapping(s): {', '.join(result['removed']) or 'none'}")


def cmd_wake_state(args):
    """Mark the session mid-turn (busy) or between turns (idle).

    The bridge injects only into an idle session. Mid-turn injection is
    redundant - a Stop hook surfaces directed events at end-of-turn - and it
    is the one window where a permission dialog can be on screen to consume
    the keystrokes.
    """
    session_id = _resolve_wake_session_id(args)
    wake_dir = Path(args.wake_dir) if args.wake_dir else wake_dir_from_env()
    if args.state == "busy":
        set_busy(wake_dir, session_id)
    else:
        clear_busy(wake_dir, session_id)
    if args.json:
        print(json.dumps({"session_id": session_id, "state": args.state}))
    else:
        print(f"Wake state for {session_id}: {args.state}")


def cmd_webhook_register(args):
    """Register a webhook."""
    # args.webhook_url is the endpoint to register; args.url stays the bus URL.
    # These must not share an argparse dest: the subparser's value would
    # overwrite the global --url and the MCP call would be POSTed to the
    # webhook target instead of the bus.
    arguments = {"url": args.webhook_url}
    if args.channel:
        arguments["channel"] = args.channel
    if args.event_types:
        arguments["event_types"] = [t.strip() for t in args.event_types.split(",")]
    if args.secret:
        arguments["secret"] = args.secret

    result = call_tool("register_webhook", arguments, url=args.url)
    print(json.dumps(result, indent=2))
    if "webhook_id" in result:
        print(f"\nWebhook registered: #{result['webhook_id']}", file=sys.stderr)


def cmd_webhook_list(args):
    """List registered webhooks."""
    arguments = {"active_only": not args.all}
    result = call_tool("list_webhooks", arguments, url=args.url)

    if not result:
        print("No webhooks registered")
        return

    print(f"Webhooks ({len(result)}):\n")
    for wh in result:
        status = "✓" if wh.get("active") else "✗"
        secret_indicator = " 🔐" if wh.get("has_secret") else ""
        print(f"  [{status}] #{wh['webhook_id']}{secret_indicator}")
        print(f"      URL: {wh['url']}")
        if wh.get("channel"):
            print(f"      Channel: {wh['channel']}")
        if wh.get("event_types"):
            print(f"      Events: {', '.join(wh['event_types'])}")
        print(f"      Created: {wh['created_at']}")
        print()


def cmd_webhook_set_active(args):
    """Pause or resume a webhook, keeping its registration."""
    # One handler behind two subcommands - the verb the user typed IS the
    # desired state, so there is no flag to get backwards.
    active = args.webhook_command == "enable"
    result = call_tool(
        "set_webhook_active",
        {"webhook_id": args.webhook_id, "active": active},
        url=args.url,
    )
    if result.get("success"):
        print(f"Webhook #{args.webhook_id} {'enabled' if active else 'disabled'}")
    else:
        print(f"Failed: {result.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def cmd_webhook_unregister(args):
    """Unregister a webhook."""
    result = call_tool("unregister_webhook", {"webhook_id": args.webhook_id}, url=args.url)
    if result.get("success"):
        print(f"Webhook #{args.webhook_id} removed")
    else:
        print(f"Failed: {result.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="CLI wrapper for agent-event-bus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENT_EVENT_BUS_URL", DEFAULT_URL),
        help="Event bus URL (default: http://127.0.0.1:8080/mcp)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show full stack traces on errors",
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
    p_publish.add_argument(
        "--session-id",
        help="Your session ID (default: $AGENT_EVENT_BUS_SESSION_ID, else $CLAUDE_CODE_SESSION_ID)",
    )
    p_publish.add_argument("--title", help="Optional short headline for the payload")
    p_publish.add_argument("--tags", help="Comma-separated tags for downstream filtering")
    p_publish.add_argument("--correlation-id", help="Thread ID linking a request to its response")
    p_publish.add_argument(
        "--signal-level",
        choices=["lifecycle", "info", "actionable"],
        help="Signal level override (default: derived from event type)",
    )
    p_publish.set_defaults(func=cmd_publish)

    # events
    p_events = subparsers.add_parser("events", help="Get recent events")
    p_events.add_argument("--cursor", help="Cursor from previous call (for pagination)")
    p_events.add_argument(
        "--session-id",
        help="Your session ID for cursor tracking (default: "
        "$AGENT_EVENT_BUS_SESSION_ID, else $CLAUDE_CODE_SESSION_ID)",
    )
    p_events.add_argument("--limit", type=int, help="Maximum number of events to return")
    p_events.add_argument(
        "--exclude",
        help="Comma-separated event types to exclude (e.g., session_registered,session_unregistered)",
    )
    p_events.add_argument(
        "--timeout",
        type=int,
        default=10000,
        help="Request timeout in milliseconds (default: 10000)",
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
        help="Filter to a specific channel (e.g., 'repo:my-project', 'all') "
        "(non-consuming: does not advance the session cursor)",
    )
    p_events.add_argument(
        "--resume",
        action="store_true",
        help="Resume from saved cursor position (requires --session-id, ignored if --cursor provided)",
    )
    p_events.add_argument(
        "--peek",
        action="store_true",
        help="Read without advancing the session cursor (non-consuming; events stay unseen for the next poll)",
    )
    p_events.add_argument(
        "--include",
        help="Comma-separated event types to include (e.g., task_completed,ci_completed) "
        "(non-consuming: does not advance the session cursor)",
    )
    p_events.add_argument(
        "--correlation-id",
        help="Filter to one correlation thread "
        "(non-consuming: does not advance the session cursor)",
    )
    p_events.add_argument(
        "--min-level",
        choices=["lifecycle", "info", "actionable"],
        help="Drop events below this signal level (server-side; replaces client denylists)",
    )
    p_events.set_defaults(func=cmd_events)

    # ack
    p_ack = subparsers.add_parser(
        "ack", help="Advance the session cursor to an event id you already hold"
    )
    p_ack.add_argument(
        "--cursor",
        required=True,
        help="Event id to mark as seen - the next_cursor from an --order asc peek "
        "with no --channel/--event-types/--correlation-id (--min-level is safe)",
    )
    p_ack.add_argument(
        "--session-id",
        help="Your session ID (default: $AGENT_EVENT_BUS_SESSION_ID, else $CLAUDE_CODE_SESSION_ID)",
    )
    p_ack.add_argument(
        "--allow-rewind",
        action="store_true",
        help="Permit moving the cursor backwards to replay (refused by default)",
    )
    p_ack.add_argument("--json", action="store_true", help="Output the raw JSON result")
    p_ack.set_defaults(func=cmd_ack)

    # notify
    p_notify = subparsers.add_parser("notify", help="Send system notification")
    p_notify.add_argument("--title", required=True, help="Notification title")
    p_notify.add_argument("--message", required=True, help="Notification message")
    p_notify.add_argument("--sound", action="store_true", help="Play sound")
    p_notify.set_defaults(func=cmd_notify)

    # panes (parent command with subcommands) - the bridge's pane mapping
    p_panes = subparsers.add_parser(
        "panes", help="Maintain the bridge's session -> terminal pane mapping"
    )
    panes_subparsers = p_panes.add_subparsers(dest="panes_command")

    for verb, helptext in (
        ("set", "Map this session to its terminal pane (SessionStart)"),
        ("clear", "Remove this session's pane mapping (SessionEnd)"),
    ):
        p = panes_subparsers.add_parser(verb, help=helptext)
        p.add_argument(
            "--session-id",
            help="Bus session id (default: AGENT_EVENT_BUS_SESSION_ID, "
            "then CLAUDE_CODE_SESSION_ID)",
        )
        p.add_argument("--wake-dir", help="Override AGENT_EVENT_BUS_WAKE_DIR")
        p.add_argument("--json", action="store_true", help="JSON output")
        if verb == "set":
            # Detection from the environment is the default; the explicit
            # flags exist for testing and for a wrapper that knows better
            # than $TMUX_PANE (a session whose pane is not the hook's).
            p.add_argument("--mux", choices=["tmux", "zellij"], help="Override auto-detection")
            p.add_argument("--pane", help="Pane id (required with --mux)")
            p.add_argument(
                "--mux-session",
                help="Multiplexer session name (required with --mux zellij)",
            )
        else:
            p.add_argument(
                "--keep-pane-entries",
                action="store_true",
                help="Remove only this session id, not other entries on the same pane",
            )
        p.set_defaults(func=cmd_panes)

    # wake-state - the bridge's idle gate
    p_wake_state = subparsers.add_parser(
        "wake-state", help="Mark this session mid-turn (busy) or between turns (idle)"
    )
    p_wake_state.add_argument("state", choices=["busy", "idle"])
    p_wake_state.add_argument(
        "--session-id",
        help="Bus session id (default: AGENT_EVENT_BUS_SESSION_ID, then CLAUDE_CODE_SESSION_ID)",
    )
    p_wake_state.add_argument("--wake-dir", help="Override AGENT_EVENT_BUS_WAKE_DIR")
    p_wake_state.add_argument("--json", action="store_true", help="JSON output")
    p_wake_state.set_defaults(func=cmd_wake_state)

    # webhook (parent command with subcommands)
    p_webhook = subparsers.add_parser("webhook", help="Manage webhooks")
    webhook_subparsers = p_webhook.add_subparsers(dest="webhook_command")

    # webhook register
    p_wh_register = webhook_subparsers.add_parser("register", help="Register a webhook")
    # dest must differ from the global --url (bus address) or argparse
    # overwrites it with the webhook target
    p_wh_register.add_argument(
        "--url", dest="webhook_url", required=True, help="Webhook URL to POST events to"
    )
    p_wh_register.add_argument(
        "--channel", help="Filter to channel (supports prefix, e.g., 'session:')"
    )
    p_wh_register.add_argument("--event-types", help="Comma-separated event types to filter")
    p_wh_register.add_argument("--secret", help="Shared secret for HMAC signing")
    p_wh_register.set_defaults(func=cmd_webhook_register)

    # webhook list
    p_wh_list = webhook_subparsers.add_parser("list", help="List webhooks")
    p_wh_list.add_argument("--all", action="store_true", help="Include inactive webhooks")
    p_wh_list.set_defaults(func=cmd_webhook_list)

    # webhook disable / enable - pause deliveries without losing the registration
    for verb, effect in (("disable", "Pause"), ("enable", "Resume")):
        p_wh_active = webhook_subparsers.add_parser(
            verb, help=f"{effect} deliveries, keeping the registration"
        )
        p_wh_active.add_argument("webhook_id", type=int, help=f"Webhook ID to {verb}")
        p_wh_active.set_defaults(func=cmd_webhook_set_active)

    # webhook unregister
    p_wh_unregister = webhook_subparsers.add_parser("unregister", help="Remove a webhook")
    p_wh_unregister.add_argument("webhook_id", type=int, help="Webhook ID to remove")
    p_wh_unregister.set_defaults(func=cmd_webhook_unregister)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        print("\nUse -h or --help with any command for more details.")
        sys.exit(1)

    # Handle webhook subcommand
    if args.command == "webhook":
        if args.webhook_command is None:
            p_webhook.print_help()
            sys.exit(1)

    if args.command == "panes":
        if args.panes_command is None:
            p_panes.print_help()
            sys.exit(1)
        # argparse cannot express "required with --mux", and a --mux without
        # a --pane would otherwise reach MuxTarget as None and fail there
        # with a less useful message.
        if args.panes_command == "set" and args.mux and not args.pane:
            print("--pane is required with --mux", file=sys.stderr)
            sys.exit(2)

    # The one place that turns a failure into output and an exit code.
    # call_tool and the cmd_* handlers stay quiet about transport failures so
    # that policy lives here; a handler's own sys.exit for a logical error
    # (SystemExit is not an Exception) passes through untouched.
    try:
        args.func(args)
    except Exception as e:
        # --debug is checked before the friendly arms so it means one thing
        # everywhere: give me the traceback. Handling BusUnreachableError
        # first instead would swallow the stack for the single failure a user
        # is most likely to be debugging when they reach for the flag.
        if args.debug:
            raise
        print(f"Error: {e}", file=sys.stderr)
        if isinstance(e, BusUnreachableError):
            print("Start with: agent-event-bus", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
