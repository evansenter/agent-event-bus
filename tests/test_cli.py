"""Tests for CLI wrapper."""

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_events_args
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
    def test_debug_false_prints_error_and_exits(self, mock_post, capsys):
        """Test that debug=False (default) prints error and exits."""
        mock_post.side_effect = ValueError("Something went wrong")

        with pytest.raises(SystemExit) as exc_info:
            cli.call_tool("list_sessions", {}, debug=False)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Something went wrong" in captured.err

    @patch("event_bus.cli.requests.post")
    def test_debug_true_propagates_exception(self, mock_post):
        """Test that debug=True causes exception to propagate."""
        mock_post.side_effect = ValueError("Something went wrong")

        with pytest.raises(ValueError) as exc_info:
            cli.call_tool("list_sessions", {}, debug=True)

        assert "Something went wrong" in str(exc_info.value)

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

        args = Namespace(name="my-session", client_id=None, url=None, debug=False)
        cli.cmd_register(args)

        mock_call.assert_called_once_with(
            "register_session",
            {"name": "my-session", "cwd": "/home/user/project"},
            url=None,
            debug=False,
        )

    @patch("event_bus.cli.call_tool")
    @patch("event_bus.cli.os.getcwd")
    def test_register_default_name(self, mock_getcwd, mock_call):
        """Test register with default name from directory."""
        mock_getcwd.return_value = "/home/user/my-project"
        mock_call.return_value = {"session_id": "abc123", "name": "my-project"}

        args = Namespace(name=None, client_id=None, url=None, debug=False)
        cli.cmd_register(args)

        mock_call.assert_called_once_with(
            "register_session",
            {"name": "my-project", "cwd": "/home/user/my-project"},
            url=None,
            debug=False,
        )

    @patch("event_bus.cli.call_tool")
    @patch("event_bus.cli.os.getcwd")
    def test_register_with_client_id(self, mock_getcwd, mock_call):
        """Test register with client_id."""
        mock_getcwd.return_value = "/home/user/project"
        mock_call.return_value = {"session_id": "abc123"}

        args = Namespace(name="test", client_id="abc-session", url=None, debug=False)
        cli.cmd_register(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["client_id"] == "abc-session"


class TestCmdUnregister:
    """Tests for unregister command."""

    @patch("event_bus.cli.call_tool")
    def test_unregister_by_session_id(self, mock_call):
        """Test unregister session by session_id."""
        mock_call.return_value = {"success": True, "session_id": "abc123"}

        args = Namespace(session_id="abc123", client_id=None, url=None, debug=False)
        cli.cmd_unregister(args)

        mock_call.assert_called_once_with(
            "unregister_session",
            {"session_id": "abc123"},
            url=None,
            debug=False,
        )

    @patch("event_bus.cli.call_tool")
    def test_unregister_by_client_id(self, mock_call):
        """Test unregister session by client_id."""
        mock_call.return_value = {"success": True, "session_id": "abc123"}

        args = Namespace(session_id=None, client_id="my-client-123", url=None, debug=False)
        cli.cmd_unregister(args)

        mock_call.assert_called_once_with(
            "unregister_session",
            {"client_id": "my-client-123"},
            url=None,
            debug=False,
        )

    def test_unregister_requires_identifier(self):
        """Test unregister fails without session_id or client_id."""
        args = Namespace(session_id=None, client_id=None, url=None, debug=False)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_unregister(args)

        assert exc_info.value.code == 1


class TestCmdSessions:
    """Tests for sessions command."""

    @patch("event_bus.cli.call_tool")
    def test_sessions_empty(self, mock_call, capsys):
        """Test listing no sessions."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
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
                "client_id": "xyz789",
            }
        ]

        args = Namespace(url=None, debug=False)
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

        args = Namespace(
            type="test_event", payload="hello", channel=None, session_id=None, url=None, debug=False
        )
        cli.cmd_publish(args)

        mock_call.assert_called_once_with(
            "publish_event",
            {"event_type": "test_event", "payload": "hello"},
            url=None,
            debug=False,
        )

    @patch("event_bus.cli.call_tool")
    def test_publish_with_channel(self, mock_call):
        """Test publish with channel."""
        mock_call.return_value = {"event_id": 1}

        args = Namespace(
            type="test_event",
            payload="hello",
            channel="repo:my-repo",
            session_id="abc123",
            url=None,
            debug=False,
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
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args()
        cli.cmd_events(args)

        captured = capsys.readouterr()
        assert "No events" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_events_list(self, mock_call, capsys):
        """Test getting events."""
        mock_call.return_value = {
            "events": [
                {
                    "id": 1,
                    "event_type": "test_event",
                    "channel": "all",
                    "payload": "hello world",
                    "session_id": "abc123",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "next_cursor": "1",
        }

        args = make_events_args()
        cli.cmd_events(args)

        captured = capsys.readouterr()
        assert "[1] test_event (all)" in captured.out
        assert "hello world" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_events_with_filtering(self, mock_call):
        """Test events with session filtering."""
        mock_call.return_value = {"events": [], "next_cursor": "5"}

        args = make_events_args(cursor="5", session_id="abc123")
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["cursor"] == "5"
        assert call_args["session_id"] == "abc123"

    @patch("event_bus.cli.call_tool")
    def test_events_json_output(self, mock_call, capsys):
        """Test JSON output format."""
        import json

        mock_call.return_value = {
            "events": [
                {
                    "id": 42,
                    "event_type": "test_event",
                    "channel": "all",
                    "payload": "hello",
                    "session_id": "abc123",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "next_cursor": "42",
        }

        args = make_events_args(json=True)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "events" in output
        assert "next_cursor" in output
        assert output["next_cursor"] == "42"
        assert len(output["events"]) == 1
        assert output["events"][0]["event_type"] == "test_event"

    @patch("event_bus.cli.call_tool")
    def test_events_json_empty(self, mock_call, capsys):
        """Test JSON output with no events."""
        import json

        mock_call.return_value = {"events": [], "next_cursor": "10"}

        args = make_events_args(cursor="10", json=True)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["events"] == []
        assert output["next_cursor"] == "10"  # Preserves cursor when no events

    @patch("event_bus.cli.call_tool")
    def test_events_exclude_types(self, mock_call, capsys):
        """Test excluding event types."""
        mock_call.return_value = {
            "events": [
                {
                    "id": 1,
                    "event_type": "session_registered",
                    "channel": "all",
                    "payload": "noise",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                },
                {
                    "id": 2,
                    "event_type": "message",
                    "channel": "all",
                    "payload": "important",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:01",
                },
                {
                    "id": 3,
                    "event_type": "session_unregistered",
                    "channel": "all",
                    "payload": "noise",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:02",
                },
            ],
            "next_cursor": "1",
        }

        args = make_events_args(exclude_types="session_registered,session_unregistered", json=True)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        import json

        output = json.loads(captured.out)
        assert len(output["events"]) == 1
        assert output["events"][0]["event_type"] == "message"
        # next_cursor comes from the API, filtering happens client-side
        assert output["next_cursor"] == "1"

    @patch("event_bus.cli.call_tool")
    def test_events_limit(self, mock_call):
        """Test limit parameter is passed through."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(limit=5)
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["limit"] == 5

    @patch("event_bus.cli.call_tool")
    def test_events_timeout(self, mock_call):
        """Test timeout parameter is passed to call_tool."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(timeout=200)
        cli.cmd_events(args)

        # Check timeout_ms was passed
        call_kwargs = mock_call.call_args
        assert call_kwargs[1]["timeout_ms"] == 200

    @patch("event_bus.cli.call_tool")
    def test_events_track_state_read(self, mock_call, tmp_path, capsys):
        """Test reading cursor from state file."""
        state_file = tmp_path / "cursor"
        state_file.write_text("50")

        mock_call.return_value = {
            "events": [
                {
                    "id": 51,
                    "event_type": "test",
                    "channel": "all",
                    "payload": "data",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "next_cursor": "51",
        }

        args = make_events_args(track_state=str(state_file), json=True)
        cli.cmd_events(args)

        # Should have used "50" from file
        call_args = mock_call.call_args[0][1]
        assert call_args["cursor"] == "50"

    @patch("event_bus.cli.call_tool")
    def test_events_track_state_write(self, mock_call, tmp_path, capsys):
        """Test writing next_cursor to state file."""
        state_file = tmp_path / "cursor"

        mock_call.return_value = {
            "events": [
                {
                    "id": 100,
                    "event_type": "test",
                    "channel": "all",
                    "payload": "data",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "next_cursor": "100",
        }

        args = make_events_args(track_state=str(state_file))
        cli.cmd_events(args)

        # State file should have been updated with next_cursor
        assert state_file.read_text() == "100"

    @patch("event_bus.cli.call_tool")
    def test_events_track_state_creates_dir(self, mock_call, tmp_path, capsys):
        """Test that track_state creates parent directories."""
        state_file = tmp_path / "subdir" / "deep" / "cursor"

        mock_call.return_value = {
            "events": [
                {
                    "id": 42,
                    "event_type": "test",
                    "channel": "all",
                    "payload": "data",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                }
            ],
            "next_cursor": "42",
        }

        args = make_events_args(track_state=str(state_file))
        cli.cmd_events(args)

        # Should have created directories and written file
        assert state_file.exists()
        assert state_file.read_text() == "42"

    @patch("event_bus.cli.call_tool")
    def test_events_track_state_missing_file(self, mock_call, tmp_path, capsys):
        """Test track_state with missing file starts from None."""
        state_file = tmp_path / "nonexistent"

        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(cursor="999", track_state=str(state_file))
        cli.cmd_events(args)

        # Should not have passed cursor (file doesn't exist)
        call_args = mock_call.call_args[0][1]
        assert "cursor" not in call_args

    @patch("event_bus.cli.call_tool")
    def test_events_exclude_types_writes_state_even_when_all_filtered(self, mock_call, tmp_path):
        """Test that state file is updated even when all events are filtered out."""
        state_file = tmp_path / "cursor"

        # All events will be filtered out
        mock_call.return_value = {
            "events": [
                {
                    "id": 10,
                    "event_type": "session_registered",
                    "channel": "all",
                    "payload": "noise",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                },
            ],
            "next_cursor": "10",
        }

        args = make_events_args(
            exclude_types="session_registered", track_state=str(state_file), json=True
        )
        cli.cmd_events(args)

        # State file should be written with next_cursor, even though events list is empty
        assert state_file.read_text() == "10"


class TestCmdNotify:
    """Tests for notify command."""

    @patch("event_bus.cli.call_tool")
    def test_notify_success(self, mock_call, capsys):
        """Test successful notification."""
        mock_call.return_value = {"success": True}

        args = Namespace(title="Test", message="Hello", sound=False, url=None, debug=False)
        cli.cmd_notify(args)

        captured = capsys.readouterr()
        assert "Notification sent" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_notify_failure(self, mock_call):
        """Test failed notification."""
        mock_call.return_value = {"success": False}

        args = Namespace(title="Test", message="Hello", sound=False, url=None, debug=False)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_notify(args)

        assert exc_info.value.code == 1

    @patch("event_bus.cli.call_tool")
    def test_notify_with_sound(self, mock_call, capsys):
        """Test notification with sound."""
        mock_call.return_value = {"success": True}

        args = Namespace(title="Test", message="Hello", sound=True, url=None, debug=False)
        cli.cmd_notify(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["sound"] is True


class TestMainArgumentParsing:
    """Tests for main function argument parsing."""

    def test_register_parser(self):
        """Test register subcommand parsing."""
        import sys

        with patch.object(
            sys, "argv", ["cli", "register", "--name", "test", "--client-id", "abc123"]
        ):
            with patch("event_bus.cli.cmd_register") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.name == "test"
                assert args.client_id == "abc123"

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


class TestCmdChannels:
    """Tests for channels command."""

    @patch("event_bus.cli.call_tool")
    def test_channels_empty(self, mock_call, capsys):
        """Test listing no channels."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
        cli.cmd_channels(args)

        captured = capsys.readouterr()
        assert "No active channels" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_channels_list(self, mock_call, capsys):
        """Test listing channels."""
        mock_call.return_value = [
            {"channel": "all", "subscribers": 2},
            {"channel": "repo:my-repo", "subscribers": 2},
            {"channel": "session:abc123", "subscribers": 1},
        ]

        args = Namespace(url=None, debug=False)
        cli.cmd_channels(args)

        captured = capsys.readouterr()
        assert "Active channels (3)" in captured.out
        assert "all" in captured.out
        assert "2 subscribers" in captured.out
        assert "repo:my-repo" in captured.out
        assert "session:abc123" in captured.out
        assert "1 subscriber)" in captured.out  # Singular form

    @patch("event_bus.cli.call_tool")
    def test_channels_calls_list_channels_tool(self, mock_call):
        """Test that channels command calls list_channels tool."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
        cli.cmd_channels(args)

        mock_call.assert_called_once_with("list_channels", {}, url=None, debug=False)


class TestCmdSessionsWithChannels:
    """Tests for sessions command with subscribed_channels."""

    @patch("event_bus.cli.call_tool")
    def test_sessions_shows_channels(self, mock_call, capsys):
        """Test that sessions command shows subscribed_channels."""
        mock_call.return_value = [
            {
                "session_id": "abc123",
                "name": "test-session",
                "repo": "my-repo",
                "machine": "my-machine",
                "age_seconds": 120,
                "client_id": "xyz789",
                "subscribed_channels": [
                    "all",
                    "session:abc123",
                    "repo:my-repo",
                    "machine:my-machine",
                ],
            }
        ]

        args = Namespace(url=None, debug=False)
        cli.cmd_sessions(args)

        captured = capsys.readouterr()
        assert "channels: all, session:abc123, repo:my-repo, machine:my-machine" in captured.out

    @patch("event_bus.cli.call_tool")
    def test_sessions_handles_missing_channels(self, mock_call, capsys):
        """Test that sessions command handles missing subscribed_channels gracefully."""
        mock_call.return_value = [
            {
                "session_id": "abc123",
                "name": "test-session",
                "repo": "my-repo",
                "machine": "my-machine",
                "age_seconds": 120,
                "client_id": "xyz789",
                # No subscribed_channels field
            }
        ]

        args = Namespace(url=None, debug=False)
        cli.cmd_sessions(args)

        captured = capsys.readouterr()
        # Should not crash, just not show channels line
        assert "abc123" in captured.out
        assert "channels:" not in captured.out


class TestCmdEventsWithChannel:
    """Tests for events command with channel filter."""

    @patch("event_bus.cli.call_tool")
    def test_events_with_channel_filter(self, mock_call):
        """Test events with channel filter."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(channel="repo:my-repo")
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["channel"] == "repo:my-repo"

    @patch("event_bus.cli.call_tool")
    def test_events_without_channel_filter(self, mock_call):
        """Test events without channel filter (default behavior)."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args()
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert "channel" not in call_args

    def test_events_channel_parser(self):
        """Test events --channel argument parsing."""
        import sys

        with patch.object(
            sys,
            "argv",
            ["cli", "events", "--channel", "repo:my-repo"],
        ):
            with patch("event_bus.cli.cmd_events") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.channel == "repo:my-repo"
