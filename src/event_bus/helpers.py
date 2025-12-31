"""Helper utilities for the event bus server."""

import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger("event-bus")


def extract_repo_from_cwd(cwd: str) -> str:
    """Extract repo name from working directory."""
    # Try to get git repo name
    parts = cwd.rstrip("/").split("/")
    # Look for common patterns like .worktrees/branch-name
    if ".worktrees" in parts:
        idx = parts.index(".worktrees")
        if idx > 0:
            return parts[idx - 1]
    # Fall back to last directory component
    last = parts[-1] if parts else ""
    return last if last else "unknown"


def is_pid_alive(pid: int | None) -> bool:
    """Check if a process with the given PID is still running."""
    if pid is None:
        return True  # Can't check, assume alive
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def send_notification(title: str, message: str, sound: bool = False) -> bool:
    """Send a system notification. Returns True if successful.

    On macOS, prefers terminal-notifier (supports custom icons) with osascript fallback.
    Icon can be set via EVENT_BUS_ICON environment variable (absolute path to PNG).
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
                    "event-bus",  # Group notifications together
                    "-sender",
                    "com.apple.Terminal",  # Use Terminal's notification permissions
                ]
                if sound:
                    cmd.extend(["-sound", "default"])

                # Custom icon support via environment variable
                icon_path = os.environ.get("EVENT_BUS_ICON")
                if icon_path and os.path.exists(icon_path):
                    cmd.extend(["-appIcon", icon_path])

                subprocess.run(cmd, check=True, capture_output=True)
                return True

            # Fallback to osascript (no custom icon support)
            script = f'display notification "{message}" with title "{title}"'
            if sound:
                script += ' sound name "default"'
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
            )
            return True

        elif system == "Linux":
            # Check for notify-send
            if shutil.which("notify-send"):
                cmd = ["notify-send", title, message]
                subprocess.run(cmd, check=True, capture_output=True)
                return True
            else:
                logger.warning("notify-send not found on Linux")
                return False

        else:
            logger.warning(f"Notifications not supported on {system}")
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


def dev_notify(tool_name: str, summary: str) -> None:
    """Send a notification in dev mode for tool calls."""
    if os.environ.get("DEV_MODE"):
        send_notification(f"🔧 {tool_name}", summary)
