"""ASGI middleware for the event bus server."""

import json
import logging

logger = logging.getLogger("event-bus")

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


def _format_args(args: dict) -> str:
    """Format tool arguments concisely with key field highlighting."""
    if not args:
        return ""
    parts = []
    # Fields to highlight with colors
    highlight_fields = {"session_id", "name", "channel", "client_id"}
    for k, v in args.items():
        val = json.dumps(v)
        if k in highlight_fields:
            # Highlight key fields: cyan key, bold value
            parts.append(f"{_CYAN}{k}{_RESET}={_BOLD}{val}{_RESET}")
        else:
            # Normal dim formatting
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
        if count > 0:
            publishers = set()
            for e in events:
                sid = e.get("session_id", "")
                if _is_human_readable_id(sid):
                    publishers.add(sid)
            if publishers:
                names = ", ".join(sorted(publishers)[:5])  # Limit to 5
                if len(publishers) > 5:
                    names += f" +{len(publishers) - 5}"
                extra_info.append(f"{_CYAN}from: {names}{_RESET}")

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
            args_str = _format_args(tool_args)

            # Parse SSE response
            response_text = response_body.decode("utf-8", errors="replace")
            resp_json = _parse_sse_response(response_text)
            result = resp_json.get("result", resp_json.get("error", {}))
            result_str = _format_result(result)

            # Log one-liner: tool(args) → result (with colors for tail -f)
            # Use tool-specific colors: yellow for publish/notify, blue for get_events
            tool_color = _TOOL_COLORS.get(tool_name, _GREEN)
            tool_colored = f"{tool_color}{_BOLD}{tool_name}{_RESET}"
            args_colored = f"{_DIM}{args_str}{_RESET}" if args_str else ""
            arrow = f"{_DIM}→{_RESET}"

            if args_str:
                logger.info(f"{tool_colored}({args_colored}) {arrow} {result_str}")
            else:
                logger.info(f"{tool_colored}() {arrow} {result_str}")

        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # Skip malformed requests
