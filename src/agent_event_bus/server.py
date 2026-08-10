"""MCP Event Bus Server.

Provides tools for cross-session Claude Code communication:
- register_session: Announce session presence
- list_sessions: See active sessions
- list_channels: See channels with subscriber counts
- publish_event: Broadcast events (auto-refreshes heartbeat)
- get_events: Poll for new events (auto-refreshes heartbeat)
- ack_events: Advance a session's cursor to an id it already holds
- unregister_session: Clean up on exit
- notify: Send system notifications
- register_webhook: Register HTTP endpoint for push notifications
- list_webhooks: List registered webhooks
- set_webhook_active: Pause/resume a webhook without unregistering it
- unregister_webhook: Remove a webhook
"""

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

import anyio.to_thread
import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# SIGNATURE_HEADER: one name, three readers (this module's _dispatch_webhook
# sets it, the bridge's hook endpoint reads it, the bridge tests build theirs
# from it) - a rename touching only some of them would 401 every delivery
# silently. Defined in helpers.py (import-clean) so the bridge can read it
# without this module's import-time side effects; the `as` form marks the
# EXPLICIT re-export the tests resolve through server - an import cleanup
# must not drop it.
from agent_event_bus.helpers import (
    SIGNATURE_HEADER as SIGNATURE_HEADER,
)
from agent_event_bus.helpers import (
    WEBHOOK_CONTENT_TYPE,
    _dev_notify,
    extract_repo_from_cwd,
    is_client_alive,
    send_notification,
)
from agent_event_bus.middleware import RequestLoggingMiddleware, TailscaleAuthMiddleware
from agent_event_bus.session_ids import generate_session_id
from agent_event_bus.storage import Event, Session, SQLiteStorage, Webhook

# Configure logging
# Default log path: ~/.claude/contrib/agent-event-bus/agent-event-bus.log
# Override with AGENT_EVENT_BUS_LOG env var (matches AGENT_EVENT_BUS_DB pattern)
# Skip file logging during tests to avoid polluting production logs
_DEFAULT_LOG_FILE = Path.home() / ".claude" / "contrib" / "agent-event-bus" / "agent-event-bus.log"
LOG_FILE = Path(os.environ.get("AGENT_EVENT_BUS_LOG", str(_DEFAULT_LOG_FILE)))

logger = logging.getLogger("agent-event-bus")
logger.setLevel(logging.DEBUG if os.environ.get("DEV_MODE") else logging.INFO)

# File handler - skip during tests, guard against reimport duplication
# Check both PYTEST_CURRENT_TEST (set per-test) and AGENT_EVENT_BUS_TESTING (set in conftest.py)
if (
    not os.environ.get("PYTEST_CURRENT_TEST")
    and not os.environ.get("AGENT_EVENT_BUS_TESTING")
    and not logger.handlers
):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s │ %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(file_handler)

    # Console handler - only in dev mode
    if os.environ.get("DEV_MODE"):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(console_handler)

# Constants
MAX_PAYLOAD_PREVIEW = 50  # Max chars to show in notification previews
WEBHOOK_TIMEOUT = 5.0  # Seconds to wait for webhook response
WEBHOOK_MAX_RETRIES = 2  # Number of retries for failed webhooks

# Known signal levels (RFC #121 / #129). Validation is soft: unknown values
# are stored as-is with a warning, never rejected.
VALID_SIGNAL_LEVELS = ("lifecycle", "info", "actionable")

# Signal-level ordering for min_level filtering (#129): one canonical noise
# policy on the server so clients subscribe by level instead of each
# maintaining a denylist of low-signal event types.
SIGNAL_LEVEL_ORDER = {"lifecycle": 0, "info": 1, "actionable": 2}

# event_type -> derived level. Anything not listed is "info".
EVENT_TYPE_SIGNAL_LEVELS = {
    # lifecycle: registration/watching/rerun churn
    "session_registered": "lifecycle",
    "session_unregistered": "lifecycle",
    "ci_watching": "lifecycle",
    "ci_rerun": "lifecycle",
    "task_started": "lifecycle",
    "parallel_work_started": "lifecycle",
    # actionable: things aimed at someone
    "help_needed": "actionable",
    "blocker_found": "actionable",
    "ci_failed": "actionable",
    "error_broadcast": "actionable",
}


def _preview(text: str) -> str:
    """Truncate a payload for a notification or log preview."""
    return text[:MAX_PAYLOAD_PREVIEW] + "..." if len(text) > MAX_PAYLOAD_PREVIEW else text


def _get_signal_level(event: Event) -> str:
    """Effective signal level for an event.

    An explicit publish-time signal_level wins; DMs (session: channels) are
    always actionable; otherwise the level derives from event_type.
    """
    if event.meta and event.meta.get("signal_level") in SIGNAL_LEVEL_ORDER:
        return event.meta["signal_level"]
    if event.channel.startswith("session:"):
        return "actionable"
    return EVENT_TYPE_SIGNAL_LEVELS.get(event.event_type, "info")


# Initialize MCP server
mcp = FastMCP("agent-event-bus")

# SQLite-backed storage (persists across restarts)
storage = SQLiteStorage()

# The server's event loop, captured on the first tool call. Lets code running
# in worker threads (webhook dispatch) schedule coroutines on the real loop.
_server_loop: asyncio.AbstractEventLoop | None = None


async def _run_sync(func, /, **kwargs):
    """Run a sync tool implementation in a worker thread.

    FastMCP executes tool functions directly on the event loop, so a blocking
    call (SQLite under contention, a hung notification subprocess) freezes the
    whole server (issue #112). Offloading to anyio's thread pool keeps the loop
    free to accept and answer other requests.
    """
    global _server_loop
    _server_loop = asyncio.get_running_loop()
    return await anyio.to_thread.run_sync(functools.partial(func, **kwargs))


@mcp.resource("agent-event-bus://guide", description="Usage guide and best practices")
def usage_guide() -> str:
    """Return the event bus usage guide from external markdown file."""
    guide_path = Path(__file__).parent / "guide.md"
    try:
        return guide_path.read_text()
    except FileNotFoundError:
        return "# Event Bus Usage Guide\n\nGuide file not found. See CLAUDE.md for usage."


def _auto_heartbeat(session_id: str | None) -> None:
    """Refresh heartbeat for a session if it exists."""
    if session_id and session_id != "anonymous":
        storage.update_heartbeat(session_id, datetime.now())


def _get_session_channels(session: Session) -> list[str]:
    """Compute implicit channel subscriptions for a session.

    Sessions are auto-subscribed to channels based on their attributes.
    """
    return [
        "all",  # Broadcasts
        f"session:{session.id}",  # Direct messages to this session
        f"repo:{session.repo}",  # Same repo
        f"machine:{session.machine}",  # Same machine
    ]


def _get_live_sessions() -> list[Session]:
    """Get live sessions, cleaning up dead ones.

    For local sessions, checks if the client process is still alive.
    Remote sessions and sessions without client_id are assumed alive.

    Returns:
        List of sessions that are still alive
    """
    storage.cleanup_stale_sessions()
    local_hostname = socket.gethostname()
    live = []

    for s in storage.list_sessions():
        is_local = s.machine == local_hostname
        if not is_client_alive(s.client_id, is_local):
            storage.delete_session(s.id)
            continue
        live.append(s)

    return live


def _notify_dm_recipient(
    channel: str,
    payload: str,
    sender_session_id: str | None,
) -> None:
    """Send a notification to the recipient of a direct message.

    This handles the "human as router" pattern - we notify the human about
    incoming DMs so they can route the message to the correct Claude session.

    Args:
        channel: The target channel (must be "session:<id>" format)
        payload: The message payload (will be truncated for notification)
        sender_session_id: The sender's session ID for attribution
    """
    if not channel.startswith("session:"):
        return

    parts = channel.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        return  # Invalid format, silently skip

    target_id = parts[1]
    target_session = storage.get_session(target_id)

    if not target_session:
        return  # Session not found, silently skip

    # Get sender info for notification context
    sender_name = "anonymous"
    if sender_session_id:
        sender_session = storage.get_session(sender_session_id)
        if sender_session:
            sender_name = sender_session.name
        # If sender not found, keep "anonymous" - don't log (normal during tests/cleanup)

    # Send notification to alert the human
    try:
        project_name = target_session.get_project_name()
        send_notification(
            title=f"📨 {target_session.name} • {project_name}",
            message=f"From: {sender_name}\n{_preview(payload)}",
        )
    except Exception as e:
        # Notification failure is non-critical, but log for debugging
        logger.warning(f"Failed to notify session {target_id} of DM: {e}")


def _register_session_impl(
    name: str,
    machine: str | None = None,
    cwd: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Sync implementation of register_session (runs in a worker thread)."""
    storage.cleanup_stale_sessions()

    now = datetime.now()
    machine = machine or socket.gethostname()
    cwd = cwd or os.environ.get("PWD", os.getcwd())
    repo = extract_repo_from_cwd(cwd)

    # Check for existing session with same machine+client_id
    # include_deleted=True recovers codename from stale sessions (#104)
    existing = None
    if client_id is not None:
        existing = storage.find_session_by_client(machine, client_id, include_deleted=True)

    if existing:
        # Update existing session (reactivate if soft-deleted)
        existing.name = name
        existing.cwd = cwd
        existing.repo = repo
        existing.last_heartbeat = now
        existing.deleted_at = None
        storage.add_session(existing)  # INSERT OR REPLACE
        _dev_notify("register_session", f"{name} resumed → {existing.display_id}")

        # Use session's last_cursor if available (resume where they left off)
        # Otherwise fall back to current position
        resume_cursor = existing.last_cursor or storage.get_cursor()
        return {
            "session_id": existing.id,
            "display_id": existing.display_id,
            "name": name,
            "machine": machine,
            "cwd": cwd,
            "repo": repo,
            "active_sessions": storage.session_count(),
            "cursor": resume_cursor,
            "resumed": True,
            "tip": f"You are '{name}' ({existing.display_id}). Resuming from last seen cursor.",
        }

    # Create new session
    # Use client_id as session ID if provided (allows direct lookup by CC's session_id)
    # Otherwise generate a UUID for new sessions without client_id
    session_id = client_id if client_id else str(uuid.uuid4())
    # Always generate human-readable display_id for UI/logs
    display_id = generate_session_id()
    session = Session(
        id=session_id,
        display_id=display_id,
        name=name,
        machine=machine,
        cwd=cwd,
        repo=repo,
        registered_at=now,
        last_heartbeat=now,
        client_id=client_id,
    )
    storage.add_session(session)

    # Auto-publish registration event and capture its ID directly
    # (avoids race condition if another event is published between add and get)
    registration_event = storage.add_event(
        event_type="session_registered",
        payload=f"{name} started on {machine} in {cwd}",
        session_id=session_id,
    )

    result = {
        "session_id": session_id,
        "display_id": display_id,
        "name": name,
        "machine": machine,
        "cwd": cwd,
        "repo": repo,
        "active_sessions": storage.session_count(),
        "cursor": str(registration_event.id),
        "resumed": False,
        "tip": f"You are '{name}' ({display_id}). Use cursor to start polling: get_events(cursor=cursor).",
    }
    _dev_notify("register_session", f"{name} → {display_id}")
    return result


@mcp.tool()
async def register_session(
    name: str,
    machine: str | None = None,
    cwd: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Register with the event bus. Returns session_id and cursor for polling.

    Args:
        name: Session name (e.g., branch name, task)
        machine: Defaults to hostname
        cwd: Defaults to $PWD
        client_id: Enables session resumption via (machine, client_id)
    """
    return await _run_sync(
        _register_session_impl, name=name, machine=machine, cwd=cwd, client_id=client_id
    )


def _list_sessions_impl() -> list[dict]:
    """Sync implementation of list_sessions (runs in a worker thread)."""
    results = []

    for s in _get_live_sessions():
        results.append(
            {
                "session_id": s.id,
                "display_id": s.display_id,
                "name": s.name,
                "machine": s.machine,
                "repo": s.repo,
                "cwd": s.cwd,
                "client_id": s.client_id,
                "registered_at": s.registered_at.isoformat(),
                "last_heartbeat": s.last_heartbeat.isoformat(),
                "age_seconds": (datetime.now() - s.registered_at).total_seconds(),
                "subscribed_channels": _get_session_channels(s),
            }
        )

    _dev_notify("list_sessions", f"{len(results)} active")
    return results


@mcp.tool()
async def list_sessions() -> list[dict]:
    """List active sessions, ordered by most recently active."""
    return await _run_sync(_list_sessions_impl)


def _list_channels_impl() -> list[dict]:
    """Sync implementation of list_channels (runs in a worker thread)."""
    channel_subscribers: dict[str, int] = {}

    for s in _get_live_sessions():
        for ch in _get_session_channels(s):
            channel_subscribers[ch] = channel_subscribers.get(ch, 0) + 1

    # Build result - only channels with >0 subscribers (all of them at this point)
    results = [
        {"channel": ch, "subscribers": count} for ch, count in sorted(channel_subscribers.items())
    ]

    _dev_notify("list_channels", f"{len(results)} active channels")
    return results


@mcp.tool()
async def list_channels() -> list[dict]:
    """List channels with subscriber counts."""
    return await _run_sync(_list_channels_impl)


def _publish_event_impl(
    event_type: str,
    payload: str,
    session_id: str | None = None,
    channel: str = "all",
    title: str | None = None,
    tags: list[str] | None = None,
    correlation_id: str | None = None,
    signal_level: str | None = None,
) -> dict:
    """Sync implementation of publish_event (runs in a worker thread)."""
    # Auto-refresh heartbeat when session publishes
    _auto_heartbeat(session_id)

    # Validate channel format for known channel types
    if channel not in ["all"] and ":" in channel:
        channel_type, _, channel_value = channel.partition(":")
        if channel_type in ["session", "repo", "machine"]:
            if not channel_value:
                logger.warning(
                    f"Invalid {channel_type} channel format: '{channel}'. "
                    f"Expected '{channel_type}:<value>'"
                )

    # Soft validation (RFC #121): warn on unknown signal levels, never reject
    if signal_level and signal_level not in VALID_SIGNAL_LEVELS:
        logger.warning(
            f"Unknown signal_level '{signal_level}' (expected one of "
            f"{', '.join(VALID_SIGNAL_LEVELS)}). Storing as-is."
        )

    # Auto-notify on direct messages (DMs)
    _notify_dm_recipient(channel, payload, session_id)

    meta = {
        k: v
        for k, v in {"title": title, "tags": tags, "signal_level": signal_level}.items()
        if v is not None
    }
    event = storage.add_event(
        event_type=event_type,
        payload=payload,
        session_id=session_id or "anonymous",
        channel=channel,
        correlation_id=correlation_id,
        meta=meta or None,
    )

    # Dispatch to matching webhooks (async, non-blocking)
    _schedule_webhook_dispatch(event)

    _dev_notify("publish_event", f"{event_type} [{channel}] {_preview(payload)}")

    result = {
        "event_id": event.id,
        "event_type": event_type,
        "payload": payload,
        "channel": channel,
        # Effective level, so publishers see what the server assigned - soft
        # validation means an unknown signal_level is otherwise silently
        # replaced by the derived value (the warning only reaches the server
        # log, not the caller)
        "signal_level": _get_signal_level(event),
    }
    if correlation_id:
        result["correlation_id"] = correlation_id
    return result


@mcp.tool()
async def publish_event(
    event_type: str,
    payload: str,
    session_id: str | None = None,
    channel: str = "all",
    title: str | None = None,
    tags: list[str] | None = None,
    correlation_id: str | None = None,
    signal_level: str | None = None,
) -> dict:
    """Publish an event. Auto-refreshes heartbeat. Returns event_id.

    Args:
        event_type: e.g., 'task_completed', 'help_needed'
        payload: Event message
        session_id: Your session ID
        channel: "all", "session:{id}", "repo:{name}", or "machine:{name}"
        title: Optional short headline for the payload
        tags: Optional list of tags for downstream filtering
        correlation_id: Optional thread ID linking a request to its response
        signal_level: Optional "lifecycle", "info", or "actionable"
    """
    return await _run_sync(
        _publish_event_impl,
        event_type=event_type,
        payload=payload,
        session_id=session_id,
        channel=channel,
        title=title,
        tags=tags,
        correlation_id=correlation_id,
        signal_level=signal_level,
    )


def _event_wire_dict(event: Event, *, id_key: str) -> dict:
    """The one wire shape for an event, keyed by `id_key` for the event id.

    Two consumers serialize the same event: get_events (as "id") and webhook
    deliveries (as "event_id"). They differed only in that key, so they share
    a builder - a field added for one is now automatically visible to the
    other, and neither can silently drift from the other's `signal_level`.

    Both spellings are wire contracts. The bridge resolves its wake target
    from `channel`, filters on `signal_level`, and dedupes spool lines on
    `event_id` (tests/test_bridge.py pins those keys), and the CLI reads the
    get_events keys - so removing or renaming a key is a breaking change.
    Additions are additive: consumers read the keys they know.
    """
    d = {
        id_key: event.id,
        "event_type": event.event_type,
        "payload": event.payload,
        "session_id": event.session_id,
        "timestamp": event.timestamp.isoformat(),
        "channel": event.channel,
        "correlation_id": event.correlation_id,
        "signal_level": _get_signal_level(event),
    }
    if event.meta:
        for key in ("title", "tags"):
            if key in event.meta:
                d[key] = event.meta[key]
    return d


# (session_id, deleted_at, tool) triples already warned about, so an orphaned poller
# hammering the bus every 5s contributes one WARNING rather than 100k of them.
# Keyed on deleted_at too: a session that is revived and later deleted again is
# a fresh incident and warns again. Bounded by the sessions table.
_warned_deleted_sessions: set[tuple[str, str, str]] = set()


def _load_polling_session(session_id: str | None) -> Session | None:
    """Load the session behind a read, soft-deleted ones included (#140).

    One lookup serves both the deleted-session check and the resume branch's
    cursor read - `get_events` is the highest-frequency call on the bus, and
    an orphaned poller is exactly the load this change exists to surface.
    """
    if not session_id or session_id == "anonymous":
        return None
    return storage.get_session(session_id, include_deleted=True)


def _deleted_session_error(session: Session | None, tool: str = "get_events") -> dict | None:
    """Return an error dict if `session` is soft-deleted (#140).

    `tool` only names the caller in the warning. Every session-scoped read
    shares this one implementation rather than re-deriving the contract - a
    second copy would drift on the response shape clients branch on.

    A deleted session's cursor and heartbeat are both frozen (every write is
    guarded by `deleted_at IS NULL`), so an orphaned poller re-asks from the
    same position forever and gets an empty batch that is indistinguishable
    from "you are up to date" - while staying invisible in `list_sessions`.
    Failing the read loudly is the only thing either side can notice.

    Unregistered ids stay silent: callers legitimately pass foreign session
    ids (Claude Code's own UUIDs) that were never registered here. Only an id
    we know we deleted is an error.
    """
    if session is None or session.deleted_at is None:
        return None

    session_id = session.id
    deleted_at = session.deleted_at
    deleted_at_str = deleted_at.isoformat() if hasattr(deleted_at, "isoformat") else str(deleted_at)

    # `tool` is part of the key: a drain hook both polls and acks, and without
    # it whichever call loses the race is silenced forever - the operator sees
    # one get_events line and never learns the acks are failing too. Still
    # bounded (sessions x deletions x tools), still not the 100k-line problem
    # the set exists to prevent.
    warn_key = (session_id, deleted_at_str, tool)
    if warn_key not in _warned_deleted_sessions:
        _warned_deleted_sessions.add(warn_key)
        logger.warning(
            f"{tool}: rejecting calls from deleted session {session.display_id} "
            f"({session_id[:8]}..., deleted_at={deleted_at_str}) - an orphaned "
            f"client is still calling the bus. Further rejections are logged "
            f"per-call by the request middleware."
        )
        _dev_notify(tool, f"deleted session call: {session.display_id}")

    return {
        "error": "Session deleted",
        "session_deleted": True,
        "session_id": session_id,
        "display_id": session.display_id,
        "deleted_at": deleted_at_str,
        "hint": (
            "This session was unregistered or timed out, and its cursor is frozen. "
            "Call register_session to get a live session, or stop polling."
        ),
    }


def _event_to_dict(e: Event) -> dict:
    """An event as get_events returns it (event id under "id")."""
    return _event_wire_dict(e, id_key="id")


def _get_events_impl(
    cursor: str | None = None,
    limit: int = 50,
    session_id: str | None = None,
    order: Literal["asc", "desc"] = "desc",
    channel: str | None = None,
    resume: bool = False,
    event_types: list[str] | None = None,
    peek: bool = False,
    correlation_id: str | None = None,
    min_level: Literal["lifecycle", "info", "actionable"] | None = None,
) -> dict:
    """Sync implementation of get_events (runs in a worker thread)."""
    # Fail loudly for soft-deleted sessions (#140) - checked on every read
    # path, not just resume: a client feeding next_cursor back by hand never
    # touches the resume branch and would otherwise poll forever unnoticed.
    session = _load_polling_session(session_id)
    deleted = _deleted_session_error(session)
    if deleted:
        return deleted

    # Auto-refresh heartbeat when session polls
    _auto_heartbeat(session_id)

    # Resume from saved cursor if requested. `session` is the row loaded
    # above - active by this point, and _auto_heartbeat only touches
    # last_heartbeat, so its last_cursor is still current.
    # Only applies when: resume=True, session_id provided, cursor not provided
    if resume and session_id and cursor is None:
        if session and session.last_cursor:
            cursor = session.last_cursor
        elif session and (peek or channel or event_types or correlation_id):
            # Non-consuming reads (peek or narrowed) on a cursor-less session:
            # read from the tip without persisting it. Persisting here would
            # let a narrowed resume mark the entire backlog as seen.
            cursor = storage.get_cursor()
        elif session:
            # Session exists but has no saved cursor - persist tip so next resume works
            tip = storage.get_cursor()
            storage.update_session_cursor(session_id, tip)
            logger.debug(
                f"get_events: resume initialized cursor for session_id={session_id[:8]}..."
            )
            _dev_notify("get_events", f"resume initialized: cursor set for {session_id[:8]}...")
            return {
                "events": [],
                "next_cursor": tip,
                "has_more": False,
            }
        else:
            # Session was never registered (deleted ones are rejected above)
            logger.debug(
                f"get_events: resume failed, session not found session_id={session_id[:8]}..."
            )
            _dev_notify("get_events", f"resume failed: session not found {session_id[:8]}...")
            return {"error": "Session not found", "session_id": session_id}

    storage.cleanup_stale_sessions()

    # Broadcast model: with no explicit channel filter, every session sees
    # every event. Channel is metadata on the event, not a subscription, so
    # there is nothing implicit to derive from session_id.
    channels = [channel] if channel else None

    raw_events, next_cursor, has_more = storage.get_events(
        cursor=cursor,
        limit=limit,
        channels=channels,
        order=order,
        event_types=event_types,
        correlation_id=correlation_id,
    )

    # Persist high-water mark for session-based tracking (enables seamless resume)
    # We save the MAX event ID seen, not the pagination cursor. This ensures that
    # resume=True always starts from after the newest event seen, regardless of
    # what order was used for polling.
    # Note: Updates on any poll - any poll means the session has "seen" events up to this point.
    # Silently ignore unknown session_ids - callers may pass external session IDs
    # (like Claude Code's own UUIDs) that aren't registered with us.
    # peek=True reads without advancing: the events stay "unseen" so a later
    # consuming poll (e.g. the UserPromptSubmit hook) still returns them. This
    # lets a Stop-hook drain inspect pending events and decide whether to act
    # without stealing them from the normal pull path.
    # Narrowing filters (channel, event_types, correlation_id) make the read
    # non-consuming: the max id below is taken over the SQL-filtered batch,
    # so advancing the cursor would mark every non-matching lower-id event
    # as seen and silently drop it from a later resume. min_level filters
    # after this bookkeeping, so level-filtered noise still counts as seen.
    narrowed = bool(channel or event_types or correlation_id)
    if session_id and raw_events and not peek and not narrowed:
        high_water_mark = str(max(e.id for e in raw_events))
        storage.update_session_cursor(session_id, high_water_mark)

    # Level filtering happens after cursor bookkeeping: filtered-out events
    # still count as "seen" (they are noise by definition, not missed signal).
    # A page may therefore return fewer than `limit` events; keep paging by
    # next_cursor.
    if min_level:
        threshold = SIGNAL_LEVEL_ORDER[min_level]
        raw_events = [
            e for e in raw_events if SIGNAL_LEVEL_ORDER[_get_signal_level(e)] >= threshold
        ]

    events = [_event_to_dict(e) for e in raw_events]

    _dev_notify("get_events", f"{len(events)} events (cursor={cursor})")

    return {
        "events": events,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@mcp.tool()
async def get_events(
    cursor: str | None = None,
    limit: int = 50,
    session_id: str | None = None,
    order: Literal["asc", "desc"] = "desc",
    channel: str | None = None,
    resume: bool = False,
    event_types: list[str] | None = None,
    peek: bool = False,
    correlation_id: str | None = None,
    min_level: Literal["lifecycle", "info", "actionable"] | None = None,
) -> dict:
    """Get events. Auto-refreshes heartbeat. Returns events list and next_cursor for pagination.

    Narrowed reads (channel/event_types/correlation_id) never advance the
    session cursor; min_level does. A deleted session_id returns
    {"error": ..., "session_deleted": true} instead of an empty batch -
    re-register or stop polling.

    Args:
        cursor: Position from register_session or previous call
        limit: Max events (default: 50)
        session_id: Enables cursor auto-tracking
        order: "desc" (newest first) or "asc"
        channel: Filter to specific channel
        resume: Use saved cursor (requires session_id)
        event_types: Filter by types, e.g., ["task_completed"]
        peek: Read without advancing the session cursor (non-consuming)
        correlation_id: Filter to one correlation thread
        min_level: Drop events below this signal level (lifecycle < info < actionable)
    """
    return await _run_sync(
        _get_events_impl,
        cursor=cursor,
        limit=limit,
        session_id=session_id,
        order=order,
        channel=channel,
        resume=resume,
        event_types=event_types,
        peek=peek,
        correlation_id=correlation_id,
        min_level=min_level,
    )


def _ack_events_impl(session_id: str, cursor: str, allow_rewind: bool = False) -> dict:
    """Sync implementation of ack_events (runs in a worker thread)."""
    session = _load_polling_session(session_id)
    deleted = _deleted_session_error(session, tool="ack_events")
    if deleted:
        return deleted
    if session is None:
        return {"error": "Session not found", "session_id": session_id}

    # Refreshed here, before the validation below, so "auto-refreshes
    # heartbeat" holds for every call from a live session - the same point in
    # the flow _get_events_impl refreshes at. An ack the server refuses is
    # still the session saying it is alive.
    _auto_heartbeat(session_id)

    previous = session.last_cursor
    tip = storage.get_cursor()

    # What every refusal reports as `cursor`, and it has to be genuinely
    # INERT to re-ack, not merely accepted.
    #
    # For a session that has never saved a cursor, NO id is both. "0" is
    # lossless but persists, flipping the next resume from tip-relative to a
    # full history replay. The tip is inert only if nothing was published
    # between the peek and the ack - and it is read HERE, at ack time, so a
    # publish in that window makes re-acking it commit events the session
    # never saw. That is the loss this primitive exists to prevent, so the
    # honest answer is that such a session has no position to restore: null.
    # The resume path resolves a cursor-less session lazily (it initializes
    # from the tip at resume time), and only leaving the field null preserves
    # that. See guide.md - a null `cursor` means "re-ack the next_cursor your
    # peek returned", NOT "fall back to a bare resume": a cursor-less resume
    # initializes from the tip at resume time and would skip the same window.
    position = previous

    try:
        target = int(cursor)
    except (TypeError, ValueError):
        return {
            "error": f"Invalid cursor {cursor!r}: expected an event id",
            "session_id": session_id,
            "cursor": position,
        }
    if target < 0:
        return {
            "error": f"Invalid cursor {cursor!r}: must not be negative",
            "session_id": session_id,
            "cursor": position,
        }

    # Refuse to ack past the newest event. next_cursor never exceeds the tip,
    # so a cursor beyond it did not come from a read - and honoring it would
    # silently mark events that do not exist yet as seen, which is this API's
    # worst failure mode: consumed but never surfaced.
    # A bus with no events yet has no tip, and the only ackable position is 0.
    # Treating "no tip" as "no ceiling" would let a fresh session ack past
    # events that have not been published yet - the same loss, earlier.
    if target > (int(tip) if tip else 0):
        return {
            "error": (
                f"Cursor {cursor} is ahead of the newest event "
                f"({tip if tip else 'none published yet'})"
            ),
            "session_id": session_id,
            # `cursor` is ALWAYS the session's saved position, on every
            # refusal - never the tip. It has one meaning ("where you are"),
            # and re-acking it is safe whenever it is set; it is null for a
            # session that has never acked, which is the block above.
            # Handing back the tip here would make the one key a caller reads
            # unconditionally a loaded gun: acking it commits the whole
            # unsurfaced backlog, which is the exact loss this primitive
            # exists to prevent.
            "cursor": position,
            # The ceiling, under its own name, for a caller that wants to
            # clamp deliberately. Clamping is only safe if it has actually
            # surfaced everything up to here.
            "tip": tip,
        }
    # Keyed on `previous`, which is now exactly `position` - one notion of
    # where the session is serves both the guard and what a refusal reports.
    # A cursor-less session is deliberately unguarded: with no saved position
    # nothing is behind, and a low ack only makes the bus RE-SERVE the events
    # above it. That is a replay, the safe direction; the guard exists to stop
    # loss, not to stop repetition (which allow_rewind exists to ask for).
    #
    # Advisory under concurrency, deliberately: `previous` is read outside the
    # UPDATE, so two overlapping acks can both clear this on the same stale
    # read and the lower one can land last. Left as is because the outcome is
    # a re-serve rather than a drop - the ceiling above, which is the guard
    # that prevents loss, cannot go the other way, since a concurrent publish
    # only RAISES the tip and a stale tip read is therefore stricter, never
    # looser. Making this atomic means folding it into update_session_cursor's
    # WHERE, which would collapse "refused a rewind" and "lost the deletion
    # race" into one False; worth doing only if a consumer ever acks the same
    # session from two places at once.
    if previous is not None and not allow_rewind:
        try:
            moving_backwards = target < int(previous)
        except ValueError:
            moving_backwards = False  # unparseable stored cursor: let the ack through
        if moving_backwards:
            return {
                "error": (
                    f"Cursor {cursor} is behind the session's current position "
                    f"({previous}). Pass allow_rewind=true to replay deliberately."
                ),
                "session_id": session_id,
                "cursor": position,
            }

    # str(target), not the raw cursor: int() accepts " 42 ", "+42" and "4_2",
    # and the stored string is what surfaces again in previous_cursor, in
    # list_sessions, and in the rewind rejection message. Persist the
    # canonical form so those never read back as the caller's typing.
    canonical = str(target)
    if not storage.update_session_cursor(session_id, canonical):
        # Lost a race with deletion between the load above and this write
        # (the UPDATE is guarded by deleted_at IS NULL). Re-read so the caller
        # gets the same shape a rejected poll gets, rather than a success the
        # bus did not actually perform - cmd_ack exits non-zero on error
        # precisely so a drain hook cannot mistake one for the other.
        raced = _deleted_session_error(_load_polling_session(session_id), tool="ack_events")
        return raced or {"error": "Session not found", "session_id": session_id}

    _dev_notify("ack_events", f"{session.display_id} → {canonical}")
    return {
        "success": True,
        "session_id": session_id,
        "cursor": canonical,
        "previous_cursor": previous,
    }


@mcp.tool()
async def ack_events(session_id: str, cursor: str, allow_rewind: bool = False) -> dict:
    """Advance a session's saved cursor to an event id it already holds.

    Pairs with peek: peek with order="asc" and NO channel/event_types/
    correlation_id filter, act, then ack that batch's next_cursor. Other
    orderings or filters make next_cursor span events the peek never
    returned, and acking it discards them. Auto-refreshes heartbeat.

    A deleted session_id is refused with the same shape get_events returns.

    Args:
        session_id: Your session ID
        cursor: Event id to mark as seen, e.g. next_cursor from a peek
        allow_rewind: Permit moving the cursor backwards to replay (default: False)
    """
    return await _run_sync(
        _ack_events_impl, session_id=session_id, cursor=cursor, allow_rewind=allow_rewind
    )


def _unregister_session_impl(session_id: str | None = None, client_id: str | None = None) -> dict:
    """Sync implementation of unregister_session (runs in a worker thread)."""
    # Look up session by client_id if provided
    if client_id and not session_id:
        machine = socket.gethostname()
        session = storage.find_session_by_client(machine, client_id)
        if session:
            session_id = session.id
        else:
            _dev_notify("unregister_session", f"client_id {client_id} not found")
            return {"error": "Session not found", "client_id": client_id, "machine": machine}
    elif not session_id:
        _dev_notify("unregister_session", "no identifier provided")
        return {"error": "Must provide either session_id or client_id"}

    session = storage.get_session(session_id)
    if not session:
        _dev_notify("unregister_session", f"{session_id} not found")
        return {"error": "Session not found", "session_id": session_id}

    storage.delete_session(session_id)

    # Publish unregister event
    storage.add_event(
        event_type="session_unregistered",
        payload=f"{session.name} ended on {session.machine}",
        session_id=session_id,
    )

    _dev_notify("unregister_session", f"{session.name} ({session.display_id})")
    return {
        "success": True,
        "session_id": session_id,
        "display_id": session.display_id,
        "active_sessions": storage.session_count(),
    }


@mcp.tool()
async def unregister_session(session_id: str | None = None, client_id: str | None = None) -> dict:
    """Unregister from event bus. session_id takes precedence if both given.

    Args:
        session_id: Your session ID
        client_id: Alternative - looks up by (machine, client_id)
    """
    return await _run_sync(_unregister_session_impl, session_id=session_id, client_id=client_id)


def _notify_impl(title: str, message: str, sound: bool = False) -> dict:
    """Sync implementation of notify (runs in a worker thread)."""
    success = send_notification(title, message, sound)
    return {
        "success": success,
        "title": title,
        "message": message,
    }


@mcp.tool()
async def notify(title: str, message: str, sound: bool = False) -> dict:
    """Send a system notification.

    Args:
        title: Short title
        message: Body text
        sound: Play sound (default: False)
    """
    return await _run_sync(_notify_impl, title=title, message=message, sound=sound)


# Webhook support

# Module-level HTTP client for webhook dispatch (connection pooling)
_webhook_client: httpx.AsyncClient | None = None
_webhook_client_loop: asyncio.AbstractEventLoop | None = None


def _get_webhook_client() -> httpx.AsyncClient:
    """Get or create the shared webhook HTTP client for the current event loop.

    The client's connection pool is bound to the loop it was created on;
    reusing it from a different loop hangs or errors. When the loop changes
    (e.g. dispatch fell back to a fresh thread's loop), a new client is
    created; the stale one can't be aclosed from here (its loop is usually
    already dead) and is left for GC. That churn is bounded: production
    dispatch stays on the server loop, and the thread fallback closes its
    own client before its loop exits.
    """
    global _webhook_client, _webhook_client_loop
    loop = asyncio.get_running_loop()
    if _webhook_client is None or _webhook_client.is_closed or _webhook_client_loop is not loop:
        _webhook_client = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT)
        _webhook_client_loop = loop
    return _webhook_client


def _compute_signature(payload: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature for webhook payload."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _webhook_payload(event: Event) -> dict:
    """The JSON body every webhook receives (event id under "event_id").

    Kept as a named function - separate from _event_to_dict - because
    tests/test_bridge.py pins the delivery contract against THIS name.
    """
    return _event_wire_dict(event, id_key="event_id")


async def _dispatch_webhook(webhook: Webhook, event: Event) -> bool:
    """Send event to a single webhook. Returns True on success."""
    payload_bytes = json.dumps(_webhook_payload(event)).encode()

    # Single-sourced with the bridge's hook-endpoint requirement (its
    # anti-browser guard 415s any other media type) - see helpers.py
    headers = {"Content-Type": WEBHOOK_CONTENT_TYPE}
    if webhook.secret:
        signature = _compute_signature(payload_bytes, webhook.secret)
        headers[SIGNATURE_HEADER] = f"sha256={signature}"

    client = _get_webhook_client()
    for attempt in range(WEBHOOK_MAX_RETRIES + 1):
        try:
            response = await client.post(
                webhook.url,
                content=payload_bytes,
                headers=headers,
            )
            if response.status_code < 400:
                logger.debug(
                    f"Webhook {webhook.id} ({webhook.url}) delivered: {response.status_code}"
                )
                return True
            logger.warning(
                f"Webhook {webhook.id} ({webhook.url}) returned {response.status_code}, "
                f"attempt {attempt + 1}/{WEBHOOK_MAX_RETRIES + 1}"
            )
        except httpx.TimeoutException as e:
            logger.warning(
                f"Webhook {webhook.id} ({webhook.url}) timed out: {e}, "
                f"attempt {attempt + 1}/{WEBHOOK_MAX_RETRIES + 1}"
            )
        except httpx.RequestError as e:
            logger.warning(
                f"Webhook {webhook.id} ({webhook.url}) request failed: {e}, "
                f"attempt {attempt + 1}/{WEBHOOK_MAX_RETRIES + 1}"
            )
        if attempt < WEBHOOK_MAX_RETRIES:
            await asyncio.sleep(0.5 * (attempt + 1))  # Backoff

    return False


async def _dispatch_webhooks(event: Event) -> None:
    """Dispatch event to all matching webhooks (async, fire-and-forget)."""
    # This coroutine runs on the server loop; the webhook lookup hits SQLite,
    # so it must go to a worker thread like every other blocking call (#112)
    webhooks = await anyio.to_thread.run_sync(storage.get_matching_webhooks, event)
    if not webhooks:
        return

    logger.info(f"Dispatching event {event.id} to {len(webhooks)} webhook(s)")

    # Fire all webhooks concurrently
    results = await asyncio.gather(
        *[_dispatch_webhook(wh, event) for wh in webhooks],
        return_exceptions=True,
    )

    # Log results with exception details
    success_count = 0
    for wh, result in zip(webhooks, results):
        if result is True:
            success_count += 1
        elif isinstance(result, Exception):
            logger.error(
                f"Webhook {wh.id} ({wh.url}) raised exception for event {event.id}: {result}"
            )
        else:
            # result is False - dispatch failed after retries (already logged)
            pass

    if success_count < len(webhooks):
        logger.warning(
            f"Webhook dispatch: {success_count}/{len(webhooks)} succeeded for event {event.id}"
        )


def _handle_dispatch_task_exception(task: asyncio.Task, event_id: int) -> None:
    """Log exceptions from background webhook dispatch tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Webhook dispatch task failed for event {event_id}: {exc}")


def _run_dispatch_in_thread(event: Event) -> None:
    """Run webhook dispatch in a new thread with its own event loop."""

    async def dispatch_and_close() -> None:
        global _webhook_client
        try:
            await _dispatch_webhooks(event)
        finally:
            # This throwaway loop is about to die; close the client it
            # created so pooled sockets don't linger until GC. Only touch
            # the global if it still belongs to this loop.
            client = _webhook_client
            if (
                client is not None
                and not client.is_closed
                and _webhook_client_loop is asyncio.get_running_loop()
            ):
                _webhook_client = None
                await client.aclose()

    try:
        asyncio.run(dispatch_and_close())
    except Exception as e:
        logger.error(f"Webhook dispatch failed for event {event.id}: {e}")


def _schedule_webhook_dispatch(event: Event) -> None:
    """Schedule webhook dispatch in background (non-blocking).

    Tool implementations run in worker threads (no running loop), so the
    normal path hands the coroutine to the server loop captured by _run_sync.
    The thread fallback only remains for direct sync calls (e.g. tests).
    """
    # Capture event.id in closure to avoid issues if event object changes
    event_id = event.id

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        task = loop.create_task(_dispatch_webhooks(event))
        task.add_done_callback(lambda t: _handle_dispatch_task_exception(t, event_id))
        return

    server_loop = _server_loop
    if server_loop is not None and server_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_dispatch_webhooks(event), server_loop)
        # concurrent.futures.Future has the same cancelled()/exception() API
        future.add_done_callback(lambda f: _handle_dispatch_task_exception(f, event_id))
        return

    # No event loop anywhere (direct sync context) - run in background thread
    thread = threading.Thread(target=_run_dispatch_in_thread, args=(event,), daemon=True)
    thread.start()


def _register_webhook_impl(
    url: str,
    channel: str | None = None,
    event_types: list[str] | None = None,
    secret: str | None = None,
) -> dict:
    """Sync implementation of register_webhook (runs in a worker thread)."""
    webhook = storage.add_webhook(
        url=url,
        channel_filter=channel,
        event_types=event_types,
        secret=secret,
    )

    _dev_notify("register_webhook", f"#{webhook.id} → {url}")

    return {
        "webhook_id": webhook.id,
        "url": url,
        "channel": channel,
        "event_types": event_types,
        "created_at": webhook.created_at.isoformat(),
    }


@mcp.tool()
async def register_webhook(
    url: str,
    channel: str | None = None,
    event_types: list[str] | None = None,
    secret: str | None = None,
) -> dict:
    """Register a webhook to receive event notifications via HTTP POST.

    Args:
        url: HTTP(S) endpoint to POST events to
        channel: Filter to specific channel (None = all). Supports prefix matching.
        event_types: Filter to specific event types (None = all)
        secret: Shared secret for HMAC signing (optional)
    """
    return await _run_sync(
        _register_webhook_impl, url=url, channel=channel, event_types=event_types, secret=secret
    )


def _list_webhooks_impl(active_only: bool = True) -> list[dict]:
    """Sync implementation of list_webhooks (runs in a worker thread)."""
    webhooks = storage.list_webhooks(active_only=active_only)

    results = [
        {
            "webhook_id": wh.id,
            "url": wh.url,
            "channel": wh.channel_filter,
            "event_types": wh.event_types,
            "active": wh.active,
            "created_at": wh.created_at.isoformat(),
            "has_secret": wh.secret is not None,
        }
        for wh in webhooks
    ]

    _dev_notify("list_webhooks", f"{len(results)} webhook(s)")
    return results


@mcp.tool()
async def list_webhooks(active_only: bool = True) -> list[dict]:
    """List registered webhooks.

    Args:
        active_only: If True, only return active webhooks (default: True)
    """
    return await _run_sync(_list_webhooks_impl, active_only=active_only)


def _set_webhook_active_impl(webhook_id: int, active: bool) -> dict:
    """Sync implementation of set_webhook_active (runs in a worker thread)."""
    if not storage.set_webhook_active(webhook_id, active):
        return {"success": False, "error": "Webhook not found", "webhook_id": webhook_id}

    _dev_notify("set_webhook_active", f"#{webhook_id} {'enabled' if active else 'disabled'}")
    return {"success": True, "webhook_id": webhook_id, "active": active}


@mcp.tool()
async def set_webhook_active(webhook_id: int, active: bool) -> dict:
    """Pause or resume a webhook without unregistering it.

    Args:
        webhook_id: ID of the webhook
        active: False stops deliveries and keeps the registration; True resumes
    """
    return await _run_sync(_set_webhook_active_impl, webhook_id=webhook_id, active=active)


def _unregister_webhook_impl(webhook_id: int) -> dict:
    """Sync implementation of unregister_webhook (runs in a worker thread)."""
    deleted = storage.delete_webhook(webhook_id)

    if deleted:
        _dev_notify("unregister_webhook", f"#{webhook_id} removed")
        return {"success": True, "webhook_id": webhook_id}
    else:
        return {"success": False, "error": "Webhook not found", "webhook_id": webhook_id}


@mcp.tool()
async def unregister_webhook(webhook_id: int) -> dict:
    """Remove a webhook registration.

    Args:
        webhook_id: ID of the webhook to remove
    """
    return await _run_sync(_unregister_webhook_impl, webhook_id=webhook_id)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Liveness probe that bypasses the MCP handler (issue #112).

    Runs entirely on the event loop with no storage access, so it answers
    even when worker threads are saturated - a hung /health means the loop
    itself is blocked.
    """
    return JSONResponse({"status": "ok", "service": "agent-event-bus"})


def create_app():
    """Create the ASGI app with middleware stack.

    Middleware order (outer to inner):
    1. TailscaleAuthMiddleware - requires Tailscale identity headers
    2. RequestLoggingMiddleware - logs MCP tool calls

    All MCP tool calls are logged to ~/.claude/contrib/agent-event-bus/agent-event-bus.log.
    Use `tail -f ~/.claude/contrib/agent-event-bus/agent-event-bus.log` to watch activity.

    Set AGENT_EVENT_BUS_AUTH_DISABLED=1 to disable auth (for testing/local dev).
    """
    # stateless_http=True allows resilience to server restarts
    app = mcp.http_app(stateless_http=True)

    # Always wrap with logging middleware
    app = RequestLoggingMiddleware(app)

    # Wrap with auth middleware unless disabled
    auth_disabled = os.environ.get("AGENT_EVENT_BUS_AUTH_DISABLED", "").lower() in ("1", "true")
    if not auth_disabled:
        app = TailscaleAuthMiddleware(app)
        logger.info("Tailscale auth enabled - requests require identity headers")
    else:
        logger.warning("Tailscale auth DISABLED - all requests allowed")

    return app


def main():
    """Run the MCP server."""
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")

    logger.info(f"Starting Agent Event Bus on {host}:{port}")
    print(f"Starting Agent Event Bus on {host}:{port}")
    print(
        f"Add to Claude Code: claude mcp add --transport http --scope user agent-event-bus http://{host}:{port}/mcp"
    )

    # Disable uvicorn's access log - we have our own middleware logging
    # This keeps ~/.claude/contrib/agent-event-bus/agent-event-bus.log clean with just our pretty-printed tool calls
    uvicorn.run(create_app(), host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
