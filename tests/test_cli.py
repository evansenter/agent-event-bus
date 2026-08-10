"""Tests for CLI wrapper."""

import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from agent_event_bus import cli
from conftest import make_events_args, make_publish_args

# Both names the CLI consults when --session-id is omitted. CLAUDE_CODE_SESSION_ID
# is injected by Claude Code into every subprocess it spawns - including the one
# running this suite - so without the scrub below an ambient value would silently
# satisfy assertions that expect NO attribution, and the suite would pass on a
# developer's machine while failing in CI (or the reverse).
SESSION_ID_ENV = ("AGENT_EVENT_BUS_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


@pytest.fixture(autouse=True)
def clean_session_id_env(monkeypatch):
    for name in SESSION_ID_ENV:
        monkeypatch.delenv(name, raising=False)


class TestCallTool:
    """Tests for call_tool function."""

    @patch("agent_event_bus.cli.requests.post")
    def test_successful_call_structured_content(self, mock_post):
        """Test successful tool call with structured content response."""
        mock_response = MagicMock()
        mock_response.text = 'data: {"result": {"structuredContent": {"result": {"session_id": "abc123", "name": "test"}}}}\n'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("register_session", {"name": "test"})

        assert result == {"session_id": "abc123", "name": "test"}
        mock_post.assert_called_once()

    @patch("agent_event_bus.cli.requests.post")
    def test_successful_call_text_content(self, mock_post):
        """Test successful tool call with text content response."""
        mock_response = MagicMock()
        mock_response.text = 'data: {"result": {"content": [{"text": "{\\"success\\": true}"}]}}\n'
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("notify", {"title": "Test", "message": "Hello"})

        assert result == {"success": True}

    @patch("agent_event_bus.cli.requests.post")
    def test_connection_error(self, mock_post):
        """Test connection error handling."""
        import requests

        mock_post.side_effect = requests.exceptions.ConnectionError()

        with pytest.raises(SystemExit) as exc_info:
            cli.call_tool("list_sessions", {})

        assert exc_info.value.code == 1

    @patch("agent_event_bus.cli.requests.post")
    def test_empty_response(self, mock_post):
        """Test handling of empty response."""
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = cli.call_tool("list_sessions", {})

        assert result == {}

    @patch("agent_event_bus.cli.requests.post")
    def test_debug_false_prints_error_and_exits(self, mock_post, capsys):
        """Test that debug=False (default) prints error and exits."""
        mock_post.side_effect = ValueError("Something went wrong")

        with pytest.raises(SystemExit) as exc_info:
            cli.call_tool("list_sessions", {}, debug=False)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Something went wrong" in captured.err

    @patch("agent_event_bus.cli.requests.post")
    def test_debug_true_propagates_exception(self, mock_post):
        """Test that debug=True causes exception to propagate."""
        mock_post.side_effect = ValueError("Something went wrong")

        with pytest.raises(ValueError) as exc_info:
            cli.call_tool("list_sessions", {}, debug=True)

        assert "Something went wrong" in str(exc_info.value)

    @patch("agent_event_bus.cli.requests.post")
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

    @patch("agent_event_bus.cli.call_tool")
    @patch("agent_event_bus.cli.os.getcwd")
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

    @patch("agent_event_bus.cli.call_tool")
    @patch("agent_event_bus.cli.os.getcwd")
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

    @patch("agent_event_bus.cli.call_tool")
    @patch("agent_event_bus.cli.os.getcwd")
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

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
    def test_sessions_empty(self, mock_call, capsys):
        """Test listing no sessions."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
        cli.cmd_sessions(args)

        captured = capsys.readouterr()
        assert "No active sessions" in captured.out

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_basic(self, mock_call):
        """Test basic publish."""
        mock_call.return_value = {"event_id": 1}

        args = make_publish_args()
        cli.cmd_publish(args)

        mock_call.assert_called_once_with(
            "publish_event",
            {"event_type": "test_event", "payload": "hello", "channel": "all"},
            url=None,
            debug=False,
        )

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_with_channel(self, mock_call):
        """Test publish with channel."""
        mock_call.return_value = {"event_id": 1}

        args = make_publish_args(channel="repo:my-repo", session_id="abc123")
        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["channel"] == "repo:my-repo"
        assert call_args["session_id"] == "abc123"

    @patch("agent_event_bus.cli.call_tool")
    @patch.dict("os.environ", {"AGENT_EVENT_BUS_SESSION_ID": "env-session-123"})
    def test_publish_session_id_from_env(self, mock_call):
        """Test publish uses session_id from env var when not provided."""
        mock_call.return_value = {"event_id": 1}

        args = make_publish_args()
        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["session_id"] == "env-session-123"

    @patch("agent_event_bus.cli.call_tool")
    @patch.dict("os.environ", {"AGENT_EVENT_BUS_SESSION_ID": "env-session-123"})
    def test_publish_explicit_session_id_overrides_env(self, mock_call):
        """Test explicit --session-id overrides env var."""
        mock_call.return_value = {"event_id": 1}

        args = make_publish_args(session_id="explicit-123")
        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["session_id"] == "explicit-123"

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_falls_back_to_claude_code_session_id(self, mock_call, monkeypatch):
        """publish shares the events precedence chain - it is the surface that
        was actually landing as "anonymous" from tool-spawned subprocesses."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")
        mock_call.return_value = {"event_id": 1}

        cli.cmd_publish(make_publish_args())

        assert mock_call.call_args[0][1]["session_id"] == "cc-session-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_explicit_env_beats_claude_code_fallback(self, mock_call, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-123")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")
        mock_call.return_value = {"event_id": 1}

        cli.cmd_publish(make_publish_args())

        assert mock_call.call_args[0][1]["session_id"] == "env-session-123"

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_omits_session_id_when_neither_is_set(self, mock_call):
        """The autouse scrub removes both, so this pins that an unattributed
        publish stays unattributed rather than picking up an ambient id."""
        mock_call.return_value = {"event_id": 1}

        cli.cmd_publish(make_publish_args())

        assert "session_id" not in mock_call.call_args[0][1]


class TestCmdEvents:
    """Tests for events command."""

    @patch("agent_event_bus.cli.call_tool")
    def test_events_empty(self, mock_call, capsys):
        """Test getting no events."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args()
        cli.cmd_events(args)

        captured = capsys.readouterr()
        assert "No events" in captured.out

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
    def test_events_with_filtering(self, mock_call):
        """Test events with session filtering."""
        mock_call.return_value = {"events": [], "next_cursor": "5"}

        args = make_events_args(cursor="5", session_id="abc123")
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["cursor"] == "5"
        assert call_args["session_id"] == "abc123"

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
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

        args = make_events_args(exclude="session_registered,session_unregistered", json=True)
        cli.cmd_events(args)

        captured = capsys.readouterr()
        import json

        output = json.loads(captured.out)
        assert len(output["events"]) == 1
        assert output["events"][0]["event_type"] == "message"
        # next_cursor comes from the API, filtering happens client-side
        assert output["next_cursor"] == "1"

    @patch("agent_event_bus.cli.call_tool")
    def test_events_include_types(self, mock_call, capsys):
        """Test --include flag passes event_types to server."""
        mock_call.return_value = {
            "events": [
                {
                    "id": 1,
                    "event_type": "task_completed",
                    "channel": "all",
                    "payload": "done",
                    "session_id": "abc",
                    "timestamp": "2024-01-01T12:00:00",
                },
            ],
            "next_cursor": "1",
        }

        args = make_events_args(include="task_completed,ci_completed", json=True)
        cli.cmd_events(args)

        # Verify event_types was passed to server
        call_args = mock_call.call_args[0][1]
        assert call_args["event_types"] == ["task_completed", "ci_completed"]

    @patch("agent_event_bus.cli.call_tool")
    def test_events_limit(self, mock_call):
        """Test limit parameter is passed through."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(limit=5)
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["limit"] == 5

    @patch("agent_event_bus.cli.call_tool")
    def test_events_timeout(self, mock_call):
        """Test timeout parameter is passed to call_tool."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(timeout=200)
        cli.cmd_events(args)

        # Check timeout_ms was passed
        call_kwargs = mock_call.call_args
        assert call_kwargs[1]["timeout_ms"] == 200

    def test_events_resume_requires_session_id(self, capsys):
        """Test that --resume flag requires --session-id."""
        args = make_events_args(resume=True, session_id=None)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_events(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "--resume requires --session-id" in captured.err

    @patch("agent_event_bus.cli.call_tool")
    def test_events_resume_with_session_id_works(self, mock_call, capsys):
        """Test that --resume with --session-id works correctly."""
        mock_call.return_value = {"events": [], "next_cursor": "100"}

        args = make_events_args(resume=True, session_id="test-session")
        cli.cmd_events(args)

        # Should pass resume=True to the server
        call_args = mock_call.call_args[0][1]
        assert call_args["resume"] is True
        assert call_args["session_id"] == "test-session"


class TestCmdEventsErrorSurfacing:
    """Tests for CLI surfacing server-side errors in events command."""

    @patch("agent_event_bus.cli.call_tool")
    def test_events_error_json_mode(self, mock_call, capsys):
        """Test that --json mode outputs server error as JSON and exits non-zero."""
        mock_call.return_value = {"error": "Session not found", "session_id": "bad-id"}

        args = make_events_args(json=True, session_id="bad-id", resume=True)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_events(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["error"] == "Session not found"
        assert output["session_id"] == "bad-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_events_error_text_mode(self, mock_call, capsys):
        """Test that text mode prints error to stderr and exits non-zero."""
        mock_call.return_value = {"error": "Session not found", "session_id": "bad-id"}

        args = make_events_args(session_id="bad-id", resume=True)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_events(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Session not found" in captured.err


class TestCmdUnregisterErrorSurfacing:
    """Tests for CLI surfacing server-side errors in unregister command."""

    @patch("agent_event_bus.cli.call_tool")
    def test_unregister_error_surfaces(self, mock_call, capsys):
        """Test that unregister surfaces server errors to stderr."""
        mock_call.return_value = {"error": "Session not found", "session_id": "bad-id"}

        args = Namespace(session_id="bad-id", client_id=None, url=None, debug=False)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_unregister(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Session not found" in captured.err


class TestCmdNotify:
    """Tests for notify command."""

    @patch("agent_event_bus.cli.call_tool")
    def test_notify_success(self, mock_call, capsys):
        """Test successful notification."""
        mock_call.return_value = {"success": True}

        args = Namespace(title="Test", message="Hello", sound=False, url=None, debug=False)
        cli.cmd_notify(args)

        captured = capsys.readouterr()
        assert "Notification sent" in captured.out

    @patch("agent_event_bus.cli.call_tool")
    def test_notify_failure(self, mock_call):
        """Test failed notification."""
        mock_call.return_value = {"success": False}

        args = Namespace(title="Test", message="Hello", sound=False, url=None, debug=False)

        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_notify(args)

        assert exc_info.value.code == 1

    @patch("agent_event_bus.cli.call_tool")
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
            with patch("agent_event_bus.cli.cmd_register") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.name == "test"
                assert args.client_id == "abc123"

    def test_unregister_parser(self):
        """Test unregister subcommand parsing."""
        import sys

        with patch.object(sys, "argv", ["cli", "unregister", "--session-id", "abc123"]):
            with patch("agent_event_bus.cli.cmd_unregister") as mock_cmd:
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
            with patch("agent_event_bus.cli.cmd_publish") as mock_cmd:
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
            with patch("agent_event_bus.cli.cmd_notify") as mock_cmd:
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
            with patch("agent_event_bus.cli.cmd_sessions") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()
                # URL is passed to argument parser, verified it doesn't error
                assert mock_cmd.called


class TestCmdChannels:
    """Tests for channels command."""

    @patch("agent_event_bus.cli.call_tool")
    def test_channels_empty(self, mock_call, capsys):
        """Test listing no channels."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
        cli.cmd_channels(args)

        captured = capsys.readouterr()
        assert "No active channels" in captured.out

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
    def test_channels_calls_list_channels_tool(self, mock_call):
        """Test that channels command calls list_channels tool."""
        mock_call.return_value = []

        args = Namespace(url=None, debug=False)
        cli.cmd_channels(args)

        mock_call.assert_called_once_with("list_channels", {}, url=None, debug=False)


class TestCmdSessionsWithChannels:
    """Tests for sessions command with subscribed_channels."""

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
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

    @patch("agent_event_bus.cli.call_tool")
    def test_events_with_channel_filter(self, mock_call):
        """Test events with channel filter."""
        mock_call.return_value = {"events": [], "next_cursor": None}

        args = make_events_args(channel="repo:my-repo")
        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["channel"] == "repo:my-repo"

    @patch("agent_event_bus.cli.call_tool")
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
            with patch("agent_event_bus.cli.cmd_events") as mock_cmd:
                mock_cmd.return_value = None
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.channel == "repo:my-repo"


class TestWebhookCommands:
    """Tests for CLI webhook subcommands.

    Regression coverage: the webhook register subparser's --url (webhook
    target) used to share an argparse dest with the global --url (bus
    address), so the MCP registration call was POSTed to the webhook target
    itself and never reached the bus.
    """

    def _run_main(self, argv, monkeypatch):
        """Run cli.main() with argv, capturing call_tool invocations."""
        import sys as _sys

        monkeypatch.delenv("AGENT_EVENT_BUS_URL", raising=False)
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, timeout_ms=None, debug=False):
            calls.append({"tool": tool_name, "arguments": arguments, "posted_to": url})
            if tool_name == "list_webhooks":
                return []
            return {"webhook_id": 1, "success": True}

        with patch.object(_sys, "argv", ["agent-event-bus-cli", *argv]):
            with patch.object(cli, "call_tool", fake_call_tool):
                cli.main()
        return calls

    def test_register_posts_to_bus_not_webhook_target(self, monkeypatch):
        calls = self._run_main(
            ["webhook", "register", "--url", "http://target.example/hook"], monkeypatch
        )

        assert len(calls) == 1
        assert calls[0]["tool"] == "register_webhook"
        # The webhook target goes in the arguments...
        assert calls[0]["arguments"]["url"] == "http://target.example/hook"
        # ...and the MCP call itself goes to the bus, not the target
        assert calls[0]["posted_to"] == cli.DEFAULT_URL

    def test_register_respects_global_bus_url(self, monkeypatch):
        calls = self._run_main(
            [
                "--url",
                "http://bus.example:9999/mcp",
                "webhook",
                "register",
                "--url",
                "http://target.example/hook",
            ],
            monkeypatch,
        )

        assert calls[0]["posted_to"] == "http://bus.example:9999/mcp"
        assert calls[0]["arguments"]["url"] == "http://target.example/hook"

    def test_register_passes_filters_and_secret(self, monkeypatch):
        calls = self._run_main(
            [
                "webhook",
                "register",
                "--url",
                "http://target.example/hook",
                "--channel",
                "repo:",
                "--event-types",
                "task_completed, ci_completed",
                "--secret",
                "shh",
            ],
            monkeypatch,
        )

        args = calls[0]["arguments"]
        assert args["channel"] == "repo:"
        assert args["event_types"] == ["task_completed", "ci_completed"]
        assert args["secret"] == "shh"

    def test_list_posts_to_bus(self, monkeypatch):
        calls = self._run_main(["webhook", "list"], monkeypatch)

        assert calls[0]["tool"] == "list_webhooks"
        assert calls[0]["arguments"] == {"active_only": True}
        assert calls[0]["posted_to"] == cli.DEFAULT_URL

    def test_list_all_includes_inactive(self, monkeypatch):
        calls = self._run_main(["webhook", "list", "--all"], monkeypatch)

        assert calls[0]["arguments"] == {"active_only": False}

    def test_unregister_posts_to_bus(self, monkeypatch):
        calls = self._run_main(["webhook", "unregister", "7"], monkeypatch)

        assert calls[0]["tool"] == "unregister_webhook"
        assert calls[0]["arguments"] == {"webhook_id": 7}
        assert calls[0]["posted_to"] == cli.DEFAULT_URL


class TestCmdEventsPeek:
    """CLI-level tests for events --peek (issue #131)."""

    @patch("agent_event_bus.cli.call_tool")
    def test_peek_passes_through(self, mock_call):
        mock_call.return_value = {"events": [], "next_cursor": None}
        args = make_events_args(session_id="abc", resume=True, peek=True)

        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["peek"] is True

    @patch("agent_event_bus.cli.call_tool")
    def test_peek_false_omitted(self, mock_call):
        mock_call.return_value = {"events": [], "next_cursor": None}
        args = make_events_args(peek=False)

        cli.cmd_events(args)

        call_args = mock_call.call_args[0][1]
        assert "peek" not in call_args

    def test_peek_flag_parses(self):
        """--peek wires through argument parsing to cmd_events."""
        import sys as _sys

        with patch.object(_sys, "argv", ["cli", "events", "--peek"]):
            with patch("agent_event_bus.cli.cmd_events") as mock_cmd:
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.peek is True


class TestCmdEventsEnvAttribution:
    """events falls back to $AGENT_EVENT_BUS_SESSION_ID like publish (#128)."""

    @patch("agent_event_bus.cli.call_tool")
    def test_session_id_from_env(self, mock_call, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-id")
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args())

        call_args = mock_call.call_args[0][1]
        assert call_args["session_id"] == "env-session-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_explicit_session_id_overrides_env(self, mock_call, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-id")
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args(session_id="explicit-id"))

        call_args = mock_call.call_args[0][1]
        assert call_args["session_id"] == "explicit-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_no_env_no_flag_omits_session_id(self, mock_call, monkeypatch):
        monkeypatch.delenv("AGENT_EVENT_BUS_SESSION_ID", raising=False)
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args())

        call_args = mock_call.call_args[0][1]
        assert "session_id" not in call_args

    @patch("agent_event_bus.cli.call_tool")
    def test_claude_code_session_id_is_the_fallback(self, mock_call, monkeypatch):
        """THE reason this fallback exists. The dotfiles that map
        CLAUDE_CODE_SESSION_ID -> AGENT_EVENT_BUS_SESSION_ID live in ~/.exports,
        sourced from ~/.zshrc - and zsh reads .zshrc for INTERACTIVE shells
        only, so a tool-spawned (non-interactive) subprocess never runs the
        mapping and publishes landed as "anonymous". Claude Code injects
        CLAUDE_CODE_SESSION_ID into that subprocess regardless, so reading it
        here fixes the attribution for every shell and machine at once."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")

        cli.cmd_events(make_events_args())

        assert mock_call.call_args[0][1]["session_id"] == "cc-session-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_explicit_env_var_beats_the_claude_code_fallback(self, mock_call, monkeypatch):
        """AGENT_EVENT_BUS_SESSION_ID is the tool-agnostic knob, so it wins -
        an operator who sets it deliberately is not overridden by the ambient
        one Claude Code happens to inject."""
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-id")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")

        cli.cmd_events(make_events_args())

        assert mock_call.call_args[0][1]["session_id"] == "env-session-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_flag_beats_both_env_vars(self, mock_call, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-id")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")

        cli.cmd_events(make_events_args(session_id="explicit-id"))

        assert mock_call.call_args[0][1]["session_id"] == "explicit-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_resume_satisfied_by_claude_code_session_id(self, mock_call, monkeypatch):
        """--resume needs a session id from somewhere; the fallback supplies
        one, so a drain hook running in a non-interactive shell no longer
        exits 1 with 'requires --session-id'."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session-id")
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args(resume=True))

        assert mock_call.call_args[0][1]["session_id"] == "cc-session-id"

    @patch("agent_event_bus.cli.call_tool")
    def test_resume_satisfied_by_env_session_id(self, mock_call, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_SESSION_ID", "env-session-id")
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args(resume=True))

        call_args = mock_call.call_args[0][1]
        assert call_args["resume"] is True
        assert call_args["session_id"] == "env-session-id"

    def test_resume_still_errors_without_any_session_id(self, monkeypatch, capsys):
        monkeypatch.delenv("AGENT_EVENT_BUS_SESSION_ID", raising=False)

        with pytest.raises(SystemExit):
            cli.cmd_events(make_events_args(resume=True))

        assert "requires --session-id" in capsys.readouterr().err


class TestCmdEventsHasMoreHint:
    """Human-readable output must surface has_more (the default desc order
    silently truncates a large backlog otherwise)."""

    @patch("agent_event_bus.cli.call_tool")
    def test_hint_printed_when_more_available(self, mock_call, capsys):
        mock_call.return_value = {
            "events": [
                {
                    "id": 1,
                    "event_type": "note",
                    "channel": "all",
                    "payload": "p",
                    "session_id": "s",
                    "timestamp": "t",
                }
            ],
            "next_cursor": "50",
            "has_more": True,
        }

        cli.cmd_events(make_events_args())

        captured = capsys.readouterr()
        assert "More events available" in captured.err

    @patch("agent_event_bus.cli.call_tool")
    def test_hint_with_empty_filtered_page(self, mock_call, capsys):
        # Reachable with --min-level: a full page of lifecycle churn filters
        # to zero events but has_more is true
        mock_call.return_value = {"events": [], "next_cursor": "50", "has_more": True}

        cli.cmd_events(make_events_args(min_level="actionable"))

        captured = capsys.readouterr()
        assert "No events" in captured.out
        assert "More events available" in captured.err

    @patch("agent_event_bus.cli.call_tool")
    def test_no_hint_without_more(self, mock_call, capsys):
        mock_call.return_value = {"events": [], "next_cursor": None, "has_more": False}

        cli.cmd_events(make_events_args())

        assert capsys.readouterr().err == ""

    @patch("agent_event_bus.cli.call_tool")
    def test_asc_hint_points_at_cursor(self, mock_call, capsys):
        mock_call.return_value = {"events": [], "next_cursor": "50", "has_more": True}

        cli.cmd_events(make_events_args(order="asc"))

        err = capsys.readouterr().err
        assert "More events available" in err
        assert "--cursor 50" in err


class TestCmdEventsStructuredDisplay:
    @patch("agent_event_bus.cli.call_tool")
    def test_signal_level_and_tags_shown(self, mock_call, capsys):
        mock_call.return_value = {
            "events": [
                {
                    "id": 42,
                    "event_type": "help_needed",
                    "channel": "all",
                    "payload": "p",
                    "session_id": "s",
                    "timestamp": "t",
                    "signal_level": "actionable",
                    "correlation_id": "rev-1",
                    "tags": ["review", "urgent"],
                }
            ],
            "next_cursor": "42",
            "has_more": False,
        }

        cli.cmd_events(make_events_args())

        out = capsys.readouterr().out
        assert "[42] help_needed (all) [actionable]" in out
        assert "corr:rev-1" in out
        assert "tags:review,urgent" in out
