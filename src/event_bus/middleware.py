"""ASGI middleware for the event bus server."""

import json
import logging

logger = logging.getLogger("event-bus")


class RequestLoggingMiddleware:
    """ASGI middleware that logs request and response bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, receive_send):
        if scope["type"] != "http":
            await self.app(scope, receive, receive_send)
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
            await receive_send(message)

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

                # Format nicely for MCP tool calls
                if req_method == "tools/call":
                    tool_name = req_params.get("name", "?")
                    tool_args = req_params.get("arguments", {})
                    logger.info(f"→ {tool_name}({json.dumps(tool_args)})")
                else:
                    logger.debug(f"→ {req_method}: {json.dumps(req_params)}")

                # Log response
                resp_json = json.loads(response_body) if response_body else {}
                result = resp_json.get("result", resp_json.get("error", {}))

                # Truncate large responses
                result_str = json.dumps(result)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "..."

                logger.info(f"← [{response_status}] {result_str}")

            except json.JSONDecodeError:
                logger.debug(f"→ {method} {path} (non-JSON body)")
                logger.debug(f"← [{response_status}]")
