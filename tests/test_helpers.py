"""Tests for helper functions."""

import os
from datetime import datetime
from unittest.mock import patch

from event_bus.helpers import (
    _dev_notify,
    escape_applescript_string,
    extract_repo_from_cwd,
    is_client_alive,
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
            display_id="test-display",
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
            display_id="test-display",
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
            display_id="test-display",
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
            display_id="test-display",
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
            display_id="test-display",
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
            display_id="test-display",
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
            display_id="test-display",
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

    def test_permission_error_returns_true(self, monkeypatch):
        """Test that PermissionError (process exists but can't signal) returns True."""

        def raise_permission_error(pid, sig):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr("os.kill", raise_permission_error)

        # Should return True (process exists, we just can't signal it)
        assert is_client_alive("12345", is_local=True) is True


class TestDevNotify:
    """Tests for _dev_notify helper."""

    def test_dev_notify_sends_notification_in_dev_mode(self, monkeypatch):
        """Test _dev_notify sends notification when DEV_MODE is set."""
        monkeypatch.setenv("DEV_MODE", "1")

        with patch("event_bus.helpers.send_notification") as mock_notify:
            mock_notify.return_value = True
            _dev_notify("test_tool", "summary message")
            mock_notify.assert_called_once_with("🔧 test_tool", "summary message")

    def test_dev_notify_does_nothing_without_dev_mode(self, monkeypatch):
        """Test _dev_notify is silent when DEV_MODE is not set."""
        monkeypatch.delenv("DEV_MODE", raising=False)

        with patch("event_bus.helpers.send_notification") as mock_notify:
            _dev_notify("test_tool", "summary")
            mock_notify.assert_not_called()
