"""Tests for helper functions."""

import os
import subprocess
from datetime import datetime
from unittest.mock import patch

from event_bus.helpers import (
    dev_notify,
    escape_applescript_string,
    extract_repo_from_cwd,
    is_client_alive,
    sanitize_display_name,
    send_notification,
)
from event_bus.storage import Session


class TestExtractRepoFromCwd:
    """Tests for extract_repo_from_cwd helper."""

    def test_simple_path(self):
        """Test extracting repo from simple path."""
        assert extract_repo_from_cwd("/home/user/myproject") == "myproject"

    def test_trailing_slash(self):
        """Test path with trailing slash."""
        assert extract_repo_from_cwd("/home/user/myproject/") == "myproject"

    def test_worktree_path(self):
        """Test extracting repo from worktree path."""
        assert (
            extract_repo_from_cwd("/home/user/myproject/.worktrees/feature-branch") == "myproject"
        )

    def test_empty_path(self):
        """Test empty path."""
        assert extract_repo_from_cwd("") == "unknown"

    def test_sanitizes_special_chars(self):
        """Test that special characters in path are sanitized."""
        # Newlines, tabs, carriage returns should become spaces
        assert extract_repo_from_cwd("/home/user/my\nproject") == "my project"
        assert extract_repo_from_cwd("/home/user/my\tproject") == "my project"
        assert extract_repo_from_cwd("/home/user/my\rproject") == "my project"


class TestEscapeApplescriptString:
    """Tests for escape_applescript_string security helper."""

    def test_backslash_escaping(self):
        """Test backslash characters are escaped."""
        assert escape_applescript_string("path\\to\\file") == "path\\\\to\\\\file"

    def test_quote_escaping(self):
        """Test double quote characters are escaped."""
        assert escape_applescript_string('say "hello"') == 'say \\"hello\\"'

    def test_combined_escaping_order(self):
        """Test backslashes are escaped before quotes (order matters)."""
        # Input: test\"  (backslash then quote)
        # After backslash escape: test\\"
        # After quote escape: test\\"
        assert escape_applescript_string('test\\"') == 'test\\\\\\"'

    def test_injection_attempt(self):
        """Test that command injection attempts are neutralized."""
        # Classic AppleScript injection: break out of string and execute shell
        malicious = '"; do shell script "rm -rf /"'
        escaped = escape_applescript_string(malicious)
        # Quotes should be escaped, preventing breakout
        assert '\\"' in escaped
        assert '" do shell script "' not in escaped

    def test_empty_string(self):
        """Test empty string passes through."""
        assert escape_applescript_string("") == ""

    def test_normal_text(self):
        """Test normal text without special chars passes through."""
        assert escape_applescript_string("Hello World") == "Hello World"

    def test_unicode_text(self):
        """Test Unicode characters pass through unchanged."""
        assert escape_applescript_string("Hello 世界 🎉") == "Hello 世界 🎉"


class TestSessionGetProjectName:
    """Tests for Session.get_project_name helper."""

    def test_get_project_name_with_repo(self):
        """Test that repo field takes precedence."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/myproject",
            repo="my-repo",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "my-repo"

    def test_get_project_name_fallback_to_cwd(self):
        """Test fallback to cwd basename when repo is empty."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/fallback-proj",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "fallback-proj"

    def test_get_project_name_none_cwd(self):
        """Test fallback to 'unknown' when cwd is None."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd=None,
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "unknown"

    def test_get_project_name_root_directory(self):
        """Test that root directory returns 'unknown' not empty string."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "unknown"

    def test_get_project_name_trailing_slash(self):
        """Test that trailing slashes are handled correctly."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/myproject/",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "myproject"

    def test_get_project_name_returns_repo_directly(self):
        """Test that repo field is returned directly without re-sanitization.

        Uses a value with special chars to prove repo is returned as-is.
        In practice, repo is sanitized at write time by extract_repo_from_cwd().
        """
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/project",
            repo="my\nrepo",  # Special char to prove no re-sanitization
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        # Returns as-is (not "my repo") - sanitization happened at write time
        assert session.get_project_name() == "my\nrepo"

    def test_get_project_name_fallback_sanitizes(self):
        """Test that fallback path sanitizes special characters (defense-in-depth)."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/project\nwith\tnewlines",
            repo="",  # Empty repo forces fallback to cwd
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        # Newlines and tabs should be replaced with spaces
        assert session.get_project_name() == "project with newlines"


class TestIsClientAlive:
    """Tests for is_client_alive helper."""

    def test_current_process_is_alive(self):
        """Test that current process PID is detected as alive on local machine."""
        assert is_client_alive(str(os.getpid()), is_local=True) is True

    def test_none_client_id(self):
        """Test that None client_id is treated as alive."""
        assert is_client_alive(None, is_local=True) is True

    def test_nonexistent_pid(self):
        """Test that nonexistent PID is detected as dead on local machine."""
        # Use a very high PID that's unlikely to exist
        assert is_client_alive("999999999", is_local=True) is False

    def test_remote_session_always_alive(self):
        """Test that remote sessions are always considered alive."""
        # Even with dead PID, remote sessions can't be checked
        assert is_client_alive("999999999", is_local=False) is True

    def test_non_numeric_client_id_treated_as_alive(self):
        """Test that non-numeric client_id is treated as alive."""
        # Can't check liveness of non-PID client IDs
        assert is_client_alive("abc-session-id", is_local=True) is True

    def test_empty_string_client_id_treated_as_alive(self):
        """Test that empty string client_id is treated as alive."""
        # Empty string can't be parsed as PID, so treated as alive
        assert is_client_alive("", is_local=True) is True


class TestSanitizeDisplayName:
    """Tests for sanitize_display_name helper."""

    def test_replaces_newlines(self):
        """Test that newlines are replaced with spaces."""
        assert sanitize_display_name("hello\nworld") == "hello world"

    def test_replaces_tabs(self):
        """Test that tabs are replaced with spaces."""
        assert sanitize_display_name("hello\tworld") == "hello world"

    def test_replaces_carriage_returns(self):
        """Test that carriage returns are replaced with spaces."""
        assert sanitize_display_name("hello\rworld") == "hello world"

    def test_replaces_multiple_special_chars(self):
        """Test that multiple special characters are all replaced."""
        assert sanitize_display_name("a\nb\tc\rd") == "a b c d"

    def test_preserves_normal_text(self):
        """Test that normal text without special chars passes through."""
        assert sanitize_display_name("hello world") == "hello world"

    def test_preserves_unicode(self):
        """Test that Unicode characters are preserved."""
        assert sanitize_display_name("hello 世界 🎉") == "hello 世界 🎉"

    def test_empty_string(self):
        """Test that empty string returns empty string."""
        assert sanitize_display_name("") == ""

    def test_only_special_chars(self):
        """Test string with only special characters."""
        assert sanitize_display_name("\n\t\r") == "   "


class TestDevNotify:
    """Tests for dev_notify helper."""

    @patch("event_bus.helpers.send_notification")
    def test_dev_notify_calls_send_notification_in_dev_mode(self, mock_send):
        """Test that dev_notify calls send_notification when DEV_MODE is set."""
        with patch.dict(os.environ, {"DEV_MODE": "1"}):
            dev_notify("test_tool", "test summary")
            mock_send.assert_called_once_with("🔧 test_tool", "test summary")

    @patch("event_bus.helpers.send_notification")
    def test_dev_notify_does_not_call_when_not_dev_mode(self, mock_send):
        """Test that dev_notify does nothing when DEV_MODE is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Ensure DEV_MODE is not set
            os.environ.pop("DEV_MODE", None)
            dev_notify("test_tool", "test summary")
            mock_send.assert_not_called()


class TestSendNotificationFailurePaths:
    """Tests for send_notification failure paths."""

    @patch("event_bus.helpers.platform.system", return_value="Darwin")
    @patch("event_bus.helpers.shutil.which", return_value="/usr/bin/terminal-notifier")
    @patch("event_bus.helpers.subprocess.run")
    def test_subprocess_called_process_error_returns_false(
        self, mock_run, mock_which, mock_system
    ):
        """Test that CalledProcessError is caught and returns False."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["terminal-notifier", "-title", "test"],
        )
        error.stderr = b"error output"
        error.stdout = b""
        mock_run.side_effect = error
        result = send_notification("Test", "Message")
        assert result is False

    @patch("event_bus.helpers.platform.system", return_value="Darwin")
    @patch("event_bus.helpers.shutil.which", return_value="/usr/bin/terminal-notifier")
    @patch("event_bus.helpers.subprocess.run")
    @patch("event_bus.helpers.logger")
    def test_subprocess_error_logs_details(
        self, mock_logger, mock_run, mock_which, mock_system
    ):
        """Test that CalledProcessError logs stderr and stdout."""
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["terminal-notifier"],
        )
        error.stderr = b"stderr content"
        error.stdout = b"stdout content"
        mock_run.side_effect = error
        send_notification("Test", "Message")
        # Verify logger.error was called with appropriate details
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "stderr content" in error_msg
        assert "stdout content" in error_msg

    @patch("event_bus.helpers.platform.system", return_value="Linux")
    @patch("event_bus.helpers.shutil.which", return_value=None)
    @patch("event_bus.helpers.logger")
    def test_linux_no_notify_send_returns_false(self, mock_logger, mock_which, mock_system):
        """Test that missing notify-send on Linux returns False and warns."""
        result = send_notification("Test", "Message")
        assert result is False
        mock_logger.warning.assert_called_once()
        assert "notify-send not found" in mock_logger.warning.call_args[0][0]

    @patch("event_bus.helpers.platform.system", return_value="Windows")
    @patch("event_bus.helpers.logger")
    def test_unsupported_platform_returns_false(self, mock_logger, mock_system):
        """Test that unsupported platform returns False and warns."""
        result = send_notification("Test", "Message")
        assert result is False
        mock_logger.warning.assert_called_once()
        assert "not supported" in mock_logger.warning.call_args[0][0]
