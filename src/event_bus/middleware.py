"""ASGI middleware for the event bus server."""

import json
import logging

logger = logging.getLogger("event-bus")


# ANSI color codes for pretty terminal output
class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    # Colors
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    # Bright variants
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_RED = "\033[91m"


def _format_args(args: dict) -> str:
    """Format tool arguments for pretty logging."""
    if not args:
        return ""
    # Single-line format for simple args
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 50:
            v = v[:47] + "..."
        parts.append(f"{k}={json.dumps(v)}")
    return ", ".join(parts)


def _format_result(result: dict, colorize: bool = True) -> str:
    """Format result for pretty logging.

    Args:
        result: The MCP result dict to format
        colorize: Whether to add ANSI color codes (default: True)
    """
    # Use Colors for ANSI output, or NoColors (empty strings) for plain text
    class NoColors:
        """Empty color codes for non-colorized output."""

        RESET = BOLD = DIM = ""
        GREEN = YELLOW = BLUE = MAGENTA = CYAN = RED = ""
        BRIGHT_GREEN = BRIGHT_YELLOW = BRIGHT_RED = ""

    c = Colors if colorize else NoColors

    # Extract just the useful part from MCP result structure
    # FastMCP returns: {"content": [...], "structuredContent": {...}, "isError": ...}
    if "structuredContent" in result:
        # structuredContent directly contains the tool result (no nested "result" key)
        content = result.get("structuredContent", {})
        if isinstance(content, dict) and "result" in content:
            content = content["result"]  # Handle legacy format if present
    elif "content" in result:
        content = result.get("content", [])
        if content and isinstance(content, list):
            # MCP tool result format
            text = content[0].get("text", "")
            try:
                content = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                content = text
    else:
        content = result

    # Format for display with colors
    if isinstance(content, dict):
        # Show key summary
        if "session_id" in content:
            return f"{c.CYAN}session={c.BOLD}{content['session_id']}{c.RESET}"
        if "events" in content:
            events = content.get("events", [])
            cursor = content.get("next_cursor", "?")
            count = len(events)
            color = c.BRIGHT_GREEN if count > 0 else c.DIM
            return f"{color}{count} events{c.RESET}{c.DIM}, cursor={cursor}{c.RESET}"
        if "event_id" in content:
            return f"{c.MAGENTA}event #{content['event_id']}{c.RESET} {c.DIM}[{content.get('channel', 'all')}]{c.RESET}"
        if "success" in content:
            if content["success"]:
                return f"{c.BRIGHT_GREEN}OK{c.RESET}"
            else:
                return f"{c.BRIGHT_RED}FAILED{c.RESET}"
        if "error" in content:
            return f"{c.BRIGHT_RED}ERROR:{c.RESET} {content['error']}"
        # Fallback: just list keys
        return f"{c.DIM}{', '.join(content.keys())}{c.RESET}"
    elif isinstance(content, list):
        return f"{c.CYAN}[{len(content)} items]{c.RESET}"
    else:
        s = str(content)
        s = s[:80] + "..." if len(s) > 80 else s
        return f"{c.DIM}{s}{c.RESET}"


class RequestLoggingMiddleware:
    """ASGI middleware that logs MCP tool calls with pretty formatting."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
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
        response_status = None

        async def send_wrapper(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status")
            elif message["type"] == "http.response.body":
                response_parts.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        # Log after request completes
        path = scope.get("path", "")
        method = scope.get("method", "")

        if path == "/mcp" and method == "POST":
            request_body = b"".join(body_parts)
            response_body = b"".join(response_parts)

            try:
                req_json = json.loads(request_body) if request_body else {}
                req_method = req_json.get("method", "?")
                req_params = req_json.get("params", {})

                # Format MCP tool calls nicely
                if req_method == "tools/call":
                    tool_name = req_params.get("name", "?")
                    tool_args = req_params.get("arguments", {})
                    args_str = _format_args(tool_args)

                    # Parse response for result summary
                    # Response is SSE format: "event: message\ndata: {...}\n"
                    response_text = response_body.decode("utf-8", errors="replace")

                    # Handle SSE format - extract JSON from "data: " lines
                    resp_json = {}
                    for line in response_text.split("\n"):
                        if line.startswith("data: "):
                            try:
                                resp_json = json.loads(line[6:])
                                break
                            except json.JSONDecodeError:
                                pass

                    result = resp_json.get("result", resp_json.get("error", {}))
                    result_str = _format_result(result)

                    # Pretty one-liner: tool(args) → result (with colors)
                    c = Colors
                    tool_colored = f"{c.GREEN}{c.BOLD}{tool_name}{c.RESET}"
                    args_colored = f"{c.DIM}{args_str}{c.RESET}" if args_str else ""
                    arrow = f"{c.DIM}→{c.RESET}"

                    if args_str:
                        logger.info(f"{tool_colored}({args_colored}) {arrow} {result_str}")
                    else:
                        logger.info(f"{tool_colored}() {arrow} {result_str}")
                else:
                    # Non-tool MCP methods (resources, etc.)
                    logger.debug(f"MCP {req_method}")

            except json.JSONDecodeError:
                logger.debug(f"Non-JSON request to {path}")
