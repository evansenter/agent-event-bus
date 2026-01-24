"""ASGI middleware for the event bus server."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_bus.storage import SQLiteStorage

logger = logging.getLogger("event-bus")


def _get_storage() -> SQLiteStorage:
    """Get the shared storage instance from server.

    Uses late import to avoid circular dependency (server imports middleware).
    Returns the same SQLiteStorage instance used by the MCP tools.
    """
    from event_bus.server import storage

    return storage


def _lookup_session_display_id(session_id: str) -> str | None:
    """Look up human-readable display_id from a session_id.

    Session IDs are now UUIDs (or client_ids). This resolves them to
    human-readable display names like "brave-tiger".

    Returns the display_id if found, None otherwise.
    """
    try:
        storage = _get_storage()
        session = storage.get_session(session_id)
        return session.display_id if session else None
    except Exception:
        return None


def _get_active_sessions_map() -> dict[str, str]:
    """Get mapping of session_id → display_id for active sessions.

    Returns a dict where keys are session IDs (UUIDs) and values are
    human-readable display_ids (like "brave-tiger").
    """
    try:
        storage = _get_storage()
        return {s.id: s.display_id for s in storage.list_sessions()}
    except Exception:
        return {}


# ANSI color codes for tail -f viewing
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Tool color categories
_TOOL_COLORS = {
    # Actions with side effects (yellow)
    "publish_event": _YELLOW,
    "notify": _YELLOW,
    # Read operations (blue)
    "get_events": _BLUE,
    # Default (green) for everything else
}


def _is_human_readable_id(session_id: str) -> bool:
    """Check if a session ID is human-readable (Docker-style adjective-noun).

    Human-readable: "brave-tiger", "tender-hawk" (two lowercase words with hyphen)
    Not human-readable: "b712a0ba-1ee6-4c18-a647-31a785147665" (UUID)
    """
    if not session_id or session_id == "anonymous":
        return False
    parts = session_id.split("-")
    # Must be exactly two parts, both alphabetic lowercase
    if len(parts) != 2:
        return False
    return all(part.isalpha() and part.islower() for part in parts)


def _format_session_id_value(session_id: str) -> str:
    """Format a session_id value for display.

    Human-readable IDs (brave-tiger) are shown prominently.
    UUIDs/hex strings are dimmed and truncated.
    """
    if _is_human_readable_id(session_id):
        return f"{_BOLD}{session_id}{_RESET}"
    elif len(session_id) > 12:
        # Truncate long UUIDs to first 8 chars
        return f"{_DIM}{session_id[:8]}…{_RESET}"
    else:
        return f"{_DIM}{session_id}{_RESET}"


def _format_args(args: dict) -> str:
    """Format tool arguments concisely with key field highlighting."""
    if not args:
        return ""
    parts = []
    # Fields to highlight with colors (key identifiers only)
    highlight_fields = {"name", "channel"}
    # ID fields that should be formatted specially (dim UUIDs, bold human-readable)
    id_fields = {"session_id", "client_id"}
    for k, v in args.items():
        if k in id_fields and isinstance(v, str):
            # Special handling for ID fields - show human-readable names prominently, dim UUIDs
            formatted_val = _format_session_id_value(v)
            parts.append(f"{_CYAN}{k}{_RESET}={formatted_val}")
        elif k in highlight_fields:
            # Highlight key fields: cyan key, bold value
            val = json.dumps(v)
            parts.append(f"{_CYAN}{k}{_RESET}={_BOLD}{val}{_RESET}")
        else:
            # Normal formatting
            val = json.dumps(v)
            parts.append(f"{k}={val}")
    return ", ".join(parts)


def _format_list(items: list) -> str:
    """Format a list result, showing actual names."""
    n = len(items)
    if n == 0:
        return f"{_DIM}empty{_RESET}"
    # Infer type from first item's keys and show names
    first = items[0] if isinstance(items[0], dict) else None
    if first:
        if "session_id" in first:
            # Show session display_ids (human-readable names): tender-hawk, brave-tiger, ...
            # Prefer display_id if available, look it up if not, format UUID as fallback
            names = []
            for item in items:
                display_id = item.get("display_id")
                if display_id:
                    names.append(f"{_CYAN}{display_id}{_RESET}")
                else:
                    # Try to look up the display_id from session_id
                    sid = item.get("session_id", "?")
                    resolved = _lookup_session_display_id(sid) if sid != "?" else None
                    if resolved:
                        names.append(f"{_CYAN}{resolved}{_RESET}")
                    else:
                        # Format the ID (dim truncated UUID)
                        names.append(_format_session_id_value(sid))
            return ", ".join(names)
        if "channel" in first and "subscribers" in first:
            # Show channel names: all, repo:foo, machine:bar, ...
            names = [item.get("channel", "?") for item in items]
            return f"{_CYAN}{', '.join(names)}{_RESET}"
    return f"{_CYAN}{n} items{_RESET}"


def _format_result(result) -> str:
    """Format result for logging with ANSI colors for tail -f viewing."""
    if not isinstance(result, dict):
        if isinstance(result, list):
            return _format_list(result)
        s = str(result)
        return s[:60] + "..." if len(s) > 60 else s

    # FastMCP wraps results in two possible formats:
    # 1. {content: [...], structuredContent: {...}, isError: ...} - prefer structuredContent
    # 2. {content: [{type: 'text', text: '...'}], isError: ...} - extract from content
    if "structuredContent" in result:
        result = result.get("structuredContent", {})
        # Some tools return {result: {...}} inside structuredContent
        if isinstance(result, dict) and "result" in result and len(result) == 1:
            result = result["result"]
        # Handle list results (e.g., list_sessions returns a list)
        if isinstance(result, list):
            return _format_list(result)
    elif "content" in result and isinstance(result.get("content"), list):
        # Extract JSON from content array: [{type: 'text', text: '{...}'}]
        content_list = result["content"]
        if content_list and isinstance(content_list[0], dict):
            text = content_list[0].get("text", "")
            if text:
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    pass  # Fall through to use result as-is

    if not isinstance(result, dict):
        s = str(result)
        return s[:60] + "..." if len(s) > 60 else s

    # Handle common result patterns with colors
    if "session_id" in result:
        # For session results, prefer display_id if available, otherwise look it up
        sid = result["session_id"]
        display_id = result.get("display_id") or _lookup_session_display_id(sid)
        if display_id:
            return f"{_CYAN}session={display_id}{_RESET}"
        else:
            # Fallback: dim the UUID
            formatted_id = _format_session_id_value(sid)
            return f"session={formatted_id}"
    if "events" in result:
        events = result.get("events", [])
        count = len(events)
        cursor = result.get("next_cursor", "?")
        color = _GREEN if count > 0 else _DIM

        extra_info = []

        # Show unique publishers with display names
        # Inactive sessions are shown in red, active in cyan
        if count > 0:
            # Get active session mapping: session_id → display_id
            active_sessions = _get_active_sessions_map()

            # Collect unique publishers and resolve to display_ids
            publisher_display_ids: dict[str, bool] = {}  # display_id → is_active
            for e in events:
                sid = e.get("session_id", "")
                if sid and sid != "anonymous":
                    if sid in active_sessions:
                        # Active session - use its display_id
                        display_id = active_sessions[sid]
                        publisher_display_ids[display_id] = True
                    else:
                        # Inactive session - try to resolve display_id, or use human-readable if already
                        display_id = _lookup_session_display_id(sid)
                        if display_id:
                            publisher_display_ids[display_id] = False
                        elif _is_human_readable_id(sid):
                            # Legacy: old-style human-readable ID that's not in our DB
                            publisher_display_ids[sid] = False

            if publisher_display_ids:
                # Sort: active first (alphabetically), then inactive (alphabetically)
                active = sorted(d for d, is_active in publisher_display_ids.items() if is_active)
                inactive = sorted(
                    d for d, is_active in publisher_display_ids.items() if not is_active
                )
                sorted_publishers = (active + inactive)[:5]
                colored_names = []
                for name in sorted_publishers:
                    if publisher_display_ids.get(name, False):
                        colored_names.append(f"{_CYAN}{name}{_RESET}")
                    else:
                        colored_names.append(f"{_RED}{name}{_RESET}")
                names_str = ", ".join(colored_names)
                if len(publisher_display_ids) > 5:
                    names_str += f" +{len(publisher_display_ids) - 5}"
                extra_info.append(f"from: {names_str}")

        # Show timespan if we have events with timestamps
        # Use min/max to always show oldest→newest regardless of order param
        if count > 0:
            try:
                timestamps = [e.get("timestamp", "") for e in events if e.get("timestamp")]
                if timestamps:
                    oldest = min(timestamps)[:16]
                    newest = max(timestamps)[:16]
                    if oldest != newest:
                        extra_info.append(f"{_DIM}{oldest} → {newest}{_RESET}")
                    elif oldest:
                        extra_info.append(f"{_DIM}{oldest}{_RESET}")
            except (KeyError, IndexError, TypeError):
                pass

        suffix = f" ({', '.join(extra_info)})" if extra_info else ""
        return f"{color}{count} events{_RESET}, cursor={cursor}{suffix}"
    if "event_id" in result:
        return f"{_MAGENTA}event #{result['event_id']}{_RESET} [{result.get('channel', 'all')}]"
    if "sessions" in result:
        return f"{_CYAN}{len(result['sessions'])} sessions{_RESET}"
    if "channels" in result:
        return f"{_CYAN}{len(result['channels'])} channels{_RESET}"
    if "success" in result:
        return f"{_GREEN}OK{_RESET}" if result["success"] else f"{_RED}FAILED{_RESET}"
    if "error" in result:
        return f"{_RED}ERROR:{_RESET} {result['error']}"

    # Fallback: show keys
    keys = ", ".join(result.keys()) if result else "{}"
    return f"{_DIM}{keys}{_RESET}"


def _parse_sse_response(response_text: str) -> dict:
    """Parse SSE format response to extract JSON result."""
    # SSE format: "event: message\ndata: {...}\n\n"
    for line in response_text.split("\n"):
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                pass
    return {}


class TailscaleAuthMiddleware:
    """ASGI middleware that requires Tailscale identity headers.

    When running behind `tailscale serve`, Tailscale injects identity headers
    (Tailscale-User-Login, Tailscale-User-Name) into requests. This middleware
    rejects requests that don't have these headers.
    """

    # Header injected by tailscale serve (lowercase for ASGI)
    TAILSCALE_USER_HEADER = b"tailscale-user-login"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check for Tailscale identity header
        headers = dict(scope.get("headers", []))
        tailscale_user = headers.get(self.TAILSCALE_USER_HEADER)

        if not tailscale_user:
            # No Tailscale identity - reject with 401
            logger.warning(
                f"Rejected unauthenticated request to {scope.get('path', '/')} "
                f"from {scope.get('client', ('unknown',))[0]}"
            )
            await self._send_unauthorized(send)
            return

        # Log authenticated user (decode bytes to string)
        user = tailscale_user.decode("utf-8", errors="replace")
        logger.debug(f"Authenticated request from {user}")

        # Allow request through
        await self.app(scope, receive, send)

    async def _send_unauthorized(self, send):
        """Send a 401 Unauthorized response."""
        body = b'{"error": "Unauthorized", "message": "Tailscale identity required"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )


class RequestLoggingMiddleware:
    """ASGI middleware that logs MCP tool calls with pretty formatting."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # Only log MCP POST requests
        if path != "/mcp" or method != "POST":
            await self.app(scope, receive, send)
            return

        # Collect request body
        body_parts = []

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
            return message

        # Collect response body
        response_parts = []

        async def send_wrapper(message):
            if message["type"] == "http.response.body":
                response_parts.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        # Log after request completes
        request_body = b"".join(body_parts)
        response_body = b"".join(response_parts)

        try:
            req_json = json.loads(request_body) if request_body else {}
            req_method = req_json.get("method", "?")

            # Only log tool calls
            if req_method != "tools/call":
                return

            req_params = req_json.get("params", {})
            tool_name = req_params.get("name", "?")
            tool_args = req_params.get("arguments", {})

            # Extract caller from session_id arg (if present)
            caller_prefix = ""
            raw_session_id = tool_args.get("session_id")
            if raw_session_id and isinstance(raw_session_id, str):
                # Try to resolve to human-readable display_id
                display_id = _lookup_session_display_id(raw_session_id)
                if display_id:
                    caller_prefix = f"{_CYAN}[{display_id}]{_RESET} "
                elif _is_human_readable_id(raw_session_id):
                    # Legacy: already human-readable but not in DB
                    caller_prefix = f"{_CYAN}[{raw_session_id}]{_RESET} "
                else:
                    # UUID we couldn't resolve - show truncated
                    short_id = raw_session_id[:8] if len(raw_session_id) > 8 else raw_session_id
                    caller_prefix = f"{_DIM}[{short_id}…]{_RESET} "

            # Format args without session_id (it's shown as caller prefix)
            args_without_session = {k: v for k, v in tool_args.items() if k != "session_id"}
            args_str = _format_args(args_without_session)

            # Parse SSE response
            response_text = response_body.decode("utf-8", errors="replace")
            resp_json = _parse_sse_response(response_text)
            result = resp_json.get("result", resp_json.get("error", {}))
            result_str = _format_result(result)

            # Log one-liner: [caller] tool(args) → result (with colors for tail -f)
            # Use tool-specific colors: yellow for publish/notify, blue for get_events
            tool_color = _TOOL_COLORS.get(tool_name, _GREEN)
            tool_colored = f"{tool_color}{_BOLD}{tool_name}{_RESET}"
            args_colored = f"{_DIM}{args_str}{_RESET}" if args_str else ""
            arrow = f"{_DIM}→{_RESET}"

            if args_str:
                logger.info(f"{caller_prefix}{tool_colored}({args_colored}) {arrow} {result_str}")
            else:
                logger.info(f"{caller_prefix}{tool_colored}() {arrow} {result_str}")

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Skipping malformed MCP request: {e}")
