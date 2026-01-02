"""ASGI middleware for the event bus server."""

import json
import logging

from event_bus.storage import SQLiteStorage

logger = logging.getLogger("event-bus")

# Lazy-loaded storage for session lookups
_storage: SQLiteStorage | None = None


def _get_storage() -> SQLiteStorage:
    """Get or create storage instance for session lookups."""
    global _storage
    if _storage is None:
        _storage = SQLiteStorage()
    return _storage


def _lookup_session_name(session_id: str) -> str | None:
    """Look up human-readable session name from a client_id or session_id.

    Returns the human-readable name if found, None otherwise.
    """
    try:
        storage = _get_storage()
        # First check if it's already a registered session_id
        session = storage.get_session(session_id)
        if session:
            return session.id  # Already human-readable

        # Try to find by client_id (search all sessions)
        for s in storage.list_sessions():
            if s.client_id == session_id:
                return s.id
        return None
    except Exception:
        return None


def _get_active_session_ids() -> set[str]:
    """Get set of currently active session IDs."""
    try:
        storage = _get_storage()
        return {s.id for s in storage.list_sessions()}
    except Exception:
        return set()


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
    for k, v in args.items():
        if k == "session_id" and isinstance(v, str):
            # Special handling for session_id - show human-readable names prominently
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
            # Show session names: tender-hawk, brave-tiger, ...
            names = [item.get("session_id", "?") for item in items]
            return f"{_CYAN}{', '.join(names)}{_RESET}"
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

    # FastMCP wraps results: {content: [...], structuredContent: {...}, isError: ...}
    # Extract the actual content from structuredContent
    if "structuredContent" in result:
        result = result.get("structuredContent", {})
        # Some tools return {result: {...}} inside structuredContent
        if isinstance(result, dict) and "result" in result and len(result) == 1:
            result = result["result"]
        # Handle list results (e.g., list_sessions returns a list)
        if isinstance(result, list):
            return _format_list(result)

    if not isinstance(result, dict):
        s = str(result)
        return s[:60] + "..." if len(s) > 60 else s

    # Handle common result patterns with colors
    if "session_id" in result:
        return f"{_CYAN}session={result['session_id']}{_RESET}"
    if "events" in result:
        events = result.get("events", [])
        count = len(events)
        cursor = result.get("next_cursor", "?")
        color = _GREEN if count > 0 else _DIM

        extra_info = []

        # Show unique publishers (only human-readable session names)
        # Inactive sessions are shown in red, active in cyan
        if count > 0:
            publishers = set()
            for e in events:
                sid = e.get("session_id", "")
                if _is_human_readable_id(sid):
                    publishers.add(sid)
            if publishers:
                active_sessions = _get_active_session_ids()
                # Sort: active first (alphabetically), then inactive (alphabetically)
                active = sorted(p for p in publishers if p in active_sessions)
                inactive = sorted(p for p in publishers if p not in active_sessions)
                sorted_publishers = (active + inactive)[:5]
                colored_names = []
                for name in sorted_publishers:
                    if name in active_sessions:
                        colored_names.append(f"{_CYAN}{name}{_RESET}")
                    else:
                        colored_names.append(f"{_RED}{name}{_RESET}")
                names_str = ", ".join(colored_names)
                if len(publishers) > 5:
                    names_str += f" +{len(publishers) - 5}"
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
                # Try to resolve to human-readable name
                resolved_name = _lookup_session_name(raw_session_id)
                if resolved_name:
                    caller_prefix = f"{_CYAN}[{resolved_name}]{_RESET} "
                elif _is_human_readable_id(raw_session_id):
                    # Already human-readable but not found (shouldn't happen)
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
