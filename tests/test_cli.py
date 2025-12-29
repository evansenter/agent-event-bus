"""Tests for CLI wrapper."""

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from event_bus import cli


class TestCallTool:
    """Tests for call_tool function."""

    @patch("event_bus.cli.requests.post")
    def test_successful_call_structured_content(self, mock_post):
        """Test successful tool call with structured content response."""
        mock_response = MagicMock()
        mock_response.text = 'data: {"result": {"structuredContent": {"result": {"session_id": "abc123", "name": "test"}}}}\n'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("register_session", {"name": "test"})

        assert result == {"session_id": "abc123", "name": "test"}
        mock_post.assert_called_once()

    @patch("event_bus.cli.requests.post")
    def test_successful_call_text_content(self, mock_post):
        """Test successful tool call with text content response."""
        mock_response = MagicMock()
        mock_response.text = 'data: {"result": {"content": [{"text": "{\\"success\\": true}"}]}}\n'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("notify", {"title": "Test", "message": "Hello"})

        assert result == {"success": True}

    @patch("event_bus.cli.requests.post")
    def test_connection_error(self, mock_post):
        """Test connection error handling."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError()

        with pytest.raises(SystemExit) as exc_info:
            cli.call_tool("list_sessions", {})

        assert exc_info.value.code == 1

    @patch("event_bus.cli.requests.post")
    def test_empty_response(self, mock_post):
        """Test handling of empty response."""
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("list_sessions", {})

        assert result == {}

    @patch("event_bus.cli.requests.post")
    def test_multiline_sse_response(self, mock_post):
        """Test parsing multiline SSE response."""
        mock_response = MagicMock()
        mock_response.text = (
            "event: message\n"
            'data: {"result": {"structuredContent": {"result": [{"name": "session1"}]}}}\n'
            "\n"
        )
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("list_sessions", {})

        assert result == [{"name": "session1"}]


class TestCmdRegister:
    """Tests for register command."""

    @patch("event_bus.cli.call_tool")
    @patch("event_bus.cli.os.getcwd")
    def test_register_with_name(self, mock_getcwd, mock_call):
        """Test register with explicit name."""
        mock_getcwd.return_value = "/home/user/project"
        mock_call.return_value = {"session_id": "abc123", "name": "my-session"}

        args = Namespace(name="my-session", pid=None)
        cli.cmd_register(args)

        mock_call.assert_called_once_with(
            "register_session",
            {"name": "my-session", "cwd": "/home/user/project"},
        )

    @patch("event_bus.cli.call_tool")
    @patch("event_bus.cli.os.getcwd")
    def test_register_default_name(self, mock_getcwd, mock_call):
        """Test register with default name from directory."""
        mock_getcwd.return_value = "/home/user/my-project"
        mock_call.return_value = {"session_id": "abc123", "name": "my-project"}

        args = Namespace(name=None, pid=None)
        cli.cmd_register(args)

        mock_call.assert_called_once_with(
            "register_session",
            {"name": "my-project", "cwd": "/home/user/my-project"},
        )

    @patch("event_bus.cli.call_tool")
    @patch("event_bus.cli.os.getcwd")
    def test_register_with_pid(self, mock_getcwd, mock_call):
        """Test register with PID."""
        mock_getcwd.return_value = "/home/user/project"
        mock_call.return_value = {"session_id": "abc123"}

        args = Namespace(name="test", pid=12345)
        cli.cmd_register(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["pid"] == 12345


class TestCmdUnregister:
    """Tests for unregister command."""

    @patch("event_bus.cli.call_tool")
    def test_unregister(self, mock_call):
        """Test unregister session."""
        mock_call.return_value = {"success": True, "session_id": "abc123"}

        args = Namespace(session_id="abc123")
        cli.cmd_unregister(args)

        mock_call.assert_called_once_with(
            "unregister_session",
            {"session_id": "abc123"},
        )


class TestCmdSessions:
    """Tests for sessions command."""

    @patch("event_bus.cli.call_tool")
    def test_sessions_empty(self, mock_call, capsys):
        """Test listing no sessions."""
        mock_call.return_value = []

        args = Namespace()
        cli.cmd_sessions(args)

        captured = capsys.readouterr()
        assert "No active sessions" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_sessions_list(self, mock_call, capsys):
        """Test listing sessions."""
        mock_call.return_value = [
            {
                "session_id": "abc123",
                "name": "test-session",
                "repo": "my-repo",
                "machine": "my-machine",
                "age_seconds": 120,
                "pid": 12345,
            }
        ]

        args = Namespace()
        cli.cmd_sessions(args)

        captured = capsys.readouterr()
        assert "Active sessions (1)" in captured.out
        assert "abc123" in captured.out
        assert "test-session" in captured.out
        assert "my-repo" in captured.out


class TestCmdPublish:
    """Tests for publish command."""

    @patch("event_bus.cli.call_tool")
    def test_publish_basic(self, mock_call):
        """Test basic publish."""
        mock_call.return_value = {"event_id": 1}

        args = Namespace(type="test_event", payload="hello", channel=None, session_id=None)
        cli.cmd_publish(args)

        mock_call.assert_called_once_with(
            "publish_event",
            {"event_type": "test_event", "payload": "hello"},
        )

    @patch("event_bus.cli.call_tool")
    def test_publish_with_channel(self, mock_call):
        """Test publish with channel."""
        mock_call.return_value = {"event_id": 1}

        args = Namespace(
            type="test_event", payload="hello", channel="repo:my-repo", session_id="abc123"
        )
        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["channel"] == "repo:my-repo"
        assert call_args["session_id"] == "abc123"


class TestCmdEvents:
    """Tests for events command."""

    @patch("event_bus.cli.call_tool")
    def test_events_empty(self, mock_call, capsys):
        """Test getting no events."""
        mock_call.return_value = []

        args = Namespace(since=0, session_id=None)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        assert "No events" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_events_list(self, mock_call, capsys):
        """Test getting events."""
        mock_call.return_value = [
            {
                "id": 1,
                "event_type": "test_event",
                "channel": "all",
                "payload": "hello world",
                "session_id": "abc123",
                "timestamp": "2024-01-01T12:00:00",
            }
        ]

        args = Namespace(since=0, session_id=None)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        assert "[1] test_event (all)" in captured.out
        assert "hello world" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_events_with_filtering(self, mock_call):
        """Test events with session filtering."""
        mock_call.return_value = []

        args = Namespace(since=5, session_id="abc123")
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["since_id"] == 5
        assert call_args["session_id"] == "abc123"


class TestCmdNotify:
    """Tests for notify command."""

    @patch("event_bus.cli.call_tool")
    def test_notify_success(self, mock_call, capsys):
        """Test successful notification."""
        mock_call.return_value = {"success": True}

        args = Namespace(title="Test", message="Hello", sound=False)
        cli.cmd_notify(args)

        captured = capsys.readouterr()
        assert "Notification sent" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_notify_failure(self, mock_call):
        """Test failed notification."""
        mock_call.return_value = {"success": False}

        args = Namespace(title="Test", message="Hello", sound=False)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_notify(args)

        assert exc_info.value.code == 1

    @patch("event_bus.cli.call_tool")
    def test_notify_with_sound(self, mock_call, capsys):
        """Test notification with sound."""
        mock_call.return_value = {"success": True}

        args = Namespace(title="Test", message="Hello", sound=True)
        cli.cmd_notify(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["sound"] is True


class TestMainArgumentParsing:
    """Tests for main function argument parsing."""

    def test_register_parser(self):
        """Test register subcommand parsing."""
        import sys

        with patch.object(sys, "argv", ["cli", "register", "--name", "test", "--pid", "123"]):
            with patch("event_bus.cli.cmd_register") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.name == "test"
                assert args.pid == 123

    def test_unregister_parser(self):
        """Test unregister subcommand parsing."""
        import sys

        with patch.object(sys, "argv", ["cli", "unregister", "--session-id", "abc123"]):
            with patch("event_bus.cli.cmd_unregister") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.session_id == "abc123"

    def test_publish_parser(self):
        """Test publish subcommand parsing."""
        import sys

        with patch.object(
            sys,
            "argv",
            [
                "cli",
                "publish",
                "--type",
                "my_event",
                "--payload",
                "data",
                "--channel",
                "repo:test",
            ],
        ):
            with patch("event_bus.cli.cmd_publish") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.type == "my_event"
                assert args.payload == "data"
                assert args.channel == "repo:test"

    def test_notify_parser(self):
        """Test notify subcommand parsing."""
        import sys

        with patch.object(
            sys, "argv", ["cli", "notify", "--title", "Alert", "--message", "Hi", "--sound"]
        ):
            with patch("event_bus.cli.cmd_notify") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.title == "Alert"
                assert args.message == "Hi"
                assert args.sound is True

    def test_url_override(self):
        """Test URL can be overridden."""
        import sys

        with patch.object(sys, "argv", ["cli", "--url", "http://custom:9999/mcp", "sessions"]):
            with patch("event_bus.cli.cmd_sessions") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()
                # URL is passed to argument parser, verified it doesn't error
                assert mock_cmd.called
