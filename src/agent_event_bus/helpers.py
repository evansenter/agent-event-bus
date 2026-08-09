"""Helper utilities for the event bus server."""

import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger("agent-event-bus")

# Notification subprocesses (terminal-notifier/osascript/notify-send) can hang
# indefinitely; a bounded timeout keeps a stuck notifier from wedging the
# worker that called it (issue #112).
NOTIFY_TIMEOUT = 5.0  # seconds

# The webhook signature header - a wire contract with three readers (the
# server's _dispatch_webhook sets it, the bridge's hook endpoint reads it,
# the bridge tests build theirs from it). It lives HERE, not in server.py,
# because server.py is not side-effect-free at module scope (it opens and
# migrates the bus database, attaches the bus log handler, and builds the
# FastMCP app) - and the bridge, a pure HTTP client of the bus, must be
# able to import the name without any of that. helpers.py is import-clean.
SIGNATURE_HEADER = "X-Event-Bus-Signature"


def _sanitize_name(name: str) -> str:
    """Sanitize a name by replacing problematic characters."""
    return name.replace("\n", " ").replace("\t", " ").replace("\r", " ")


def extract_repo_from_cwd(cwd: str) -> str:
    """Extract repo name from working directory.

    Sanitizes the result to remove newlines/tabs that could cause display issues.
    """
    # Try to get git repo name
    parts = cwd.rstrip("/").split("/")
    # Look for common patterns like .worktrees/branch-name
    if ".worktrees" in parts:
        idx = parts.index(".worktrees")
        if idx > 0:
            return _sanitize_name(parts[idx - 1])
    # Fall back to last directory component
    last = parts[-1] if parts else ""
    return _sanitize_name(last) if last else "unknown"


def is_client_alive(client_id: str | None, is_local: bool) -> bool:
    """Check if a client is still alive based on its client_id.

    For local sessions where client_id is a numeric PID, we check process liveness.
    For remote sessions or non-numeric client_ids, we can't check and assume alive.

    Args:
        client_id: The client identifier (may be a PID string or other identifier)
        is_local: Whether the session is on the local machine

    Returns:
        True if client is alive or we can't determine, False if definitely dead.
    """
    if client_id is None or not is_local:
        return True  # Can't check remote or unknown clients, assume alive

    # Try to parse as PID for liveness check
    try:
        pid = int(client_id)
    except ValueError:
        logger.debug(f"Skipping liveness check for non-numeric client_id: {client_id}")
        return True  # Non-numeric client_id, can't check, assume alive

    # Check PID liveness
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def escape_applescript_string(s: str) -> str:
    """Escape a string for safe inclusion in AppleScript double-quoted strings.

    Prevents command injection by escaping backslashes and double quotes.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str, sound: bool = False) -> bool:
    """Send a system notification. Returns True if successful.

    On macOS, prefers terminal-notifier (supports custom icons) with osascript fallback.
    Icon can be set via AGENT_EVENT_BUS_ICON environment variable (absolute path to PNG).
    """
    system = platform.system()

    try:
        if system == "Darwin":  # macOS
            # Prefer terminal-notifier for custom icon support
            if shutil.which("terminal-notifier"):
                cmd = [
                    "terminal-notifier",
                    "-title",
                    title,
                    "-message",
                    message,
                    "-group",
                    "agent-event-bus",  # Group notifications together
                    "-sender",
                    "com.apple.Terminal",  # Use Terminal's notification permissions
                ]
                if sound:
                    cmd.extend(["-sound", "default"])

                # Custom icon support via environment variable
                icon_path = os.environ.get("AGENT_EVENT_BUS_ICON")
                if icon_path and os.path.exists(icon_path):
                    cmd.extend(["-appIcon", icon_path])

                subprocess.run(cmd, check=True, capture_output=True, timeout=NOTIFY_TIMEOUT)
                return True

            # Fallback to osascript (no custom icon support)
            # Escape strings to prevent command injection
            safe_title = escape_applescript_string(title)
            safe_message = escape_applescript_string(message)
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            if sound:
                script += ' sound name "default"'
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=NOTIFY_TIMEOUT,
            )
            return True

        elif system == "Linux":
            # Skip on headless servers (no display or D-Bus session)
            if not os.environ.get("DISPLAY") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                return False  # Headless server, can't send notifications

            # Check for notify-send
            if shutil.which("notify-send"):
                cmd = ["notify-send", title, message]
                subprocess.run(cmd, check=True, capture_output=True, timeout=NOTIFY_TIMEOUT)
                return True
            else:
                return False  # notify-send not available

        else:
            return False  # Unsupported platform

    except subprocess.TimeoutExpired as e:
        logger.error(f"Notification command timed out after {NOTIFY_TIMEOUT}s: {e.cmd}")
        return False
    except subprocess.CalledProcessError as e:
        # Include stderr/stdout for debugging
        stderr = e.stderr.decode() if e.stderr else "no stderr"
        stdout = e.stdout.decode() if e.stdout else "no stdout"
        logger.error(
            f"Notification command failed (exit code {e.returncode}): {e.cmd}\n"
            f"Stdout: {stdout}\n"
            f"Stderr: {stderr}"
        )
        return False


def _dev_notify(tool_name: str, summary: str) -> None:
    """Send a notification in dev mode for tool calls."""
    if os.environ.get("DEV_MODE"):
        send_notification(f"🔧 {tool_name}", summary)
