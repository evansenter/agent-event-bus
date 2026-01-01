"""Tests for notification functionality."""

import os
from unittest.mock import MagicMock, patch

import pytest

from event_bus import server

# Access the underlying functions from FunctionTool wrappers
register_session = server.register_session.fn
publish_event = server.publish_event.fn
get_events = server.get_events.fn
notify = server.notify.fn


class TestNotify:
    """Tests for notify tool."""

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_terminal_notifier(self, mock_run, mock_which, mock_system):
        """Test notification on macOS with terminal-notifier."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        assert result["title"] == "Test"
        assert result["message"] == "Hello"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "terminal-notifier"
        assert "-group" in call_args
        assert "-sender" in call_args
        assert "com.apple.Terminal" in call_args

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_terminal_notifier_with_sound(self, mock_run, mock_which, mock_system):
        """Test notification with sound on macOS using terminal-notifier."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello", sound=True)

        call_args = mock_run.call_args[0][0]
        assert "-sound" in call_args
        assert "default" in call_args

    @patch.dict(os.environ, {"EVENT_BUS_ICON": "/tmp/test-icon.png"})
    @patch("event_bus.helpers.os.path.exists")
    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_with_custom_icon(self, mock_run, mock_which, mock_system, mock_exists):
        """Test notification with custom icon."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_exists.return_value = True
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello")

        call_args = mock_run.call_args[0][0]
        assert "-appIcon" in call_args
        assert "/tmp/test-icon.png" in call_args

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_osascript_fallback(self, mock_run, mock_which, mock_system):
        """Test notification falls back to osascript when terminal-notifier not available."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = None  # No terminal-notifier
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "osascript"

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_osascript_with_sound(self, mock_run, mock_which, mock_system):
        """Test notification with sound using osascript fallback."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = None  # No terminal-notifier
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello", sound=True)

        call_args = mock_run.call_args[0][0]
        assert "sound name" in call_args[2]

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_linux(self, mock_run, mock_which, mock_system):
        """Test notification on Linux."""
        mock_system.return_value = "Linux"
        mock_which.return_value = "/usr/bin/notify-send"
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["notify-send", "Test", "Hello"]

    @patch("event_bus.helpers.platform.system")
    def test_notify_unsupported_platform(self, mock_system):
        """Test notification on unsupported platform."""
        mock_system.return_value = "Windows"

        result = notify(title="Test", message="Hello")

        assert result["success"] is False


@pytest.mark.real_dm_notifications
class TestAutoNotifyOnDM:
    """Tests for auto-notify on direct messages."""

    @patch("event_bus.server.send_notification")
    def test_dm_triggers_notification(self, mock_notify):
        """Test that DM to a session triggers notification."""
        # Register target session
        target = register_session(name="target-session", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Register sender session
        sender = register_session(name="sender-session", machine="test", cwd="/test2")
        sender_id = sender["session_id"]

        # Send DM
        publish_event(
            event_type="help_needed",
            payload="Can you review my code?",
            session_id=sender_id,
            channel=f"session:{target_id}",
        )

        # Verify notification was sent with correct format
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "📨 target-session" in call_kwargs["title"]  # Title includes emoji and target name
        assert "test" in call_kwargs["title"]  # Title includes project name from cwd="/test"
        assert "sender-session" in call_kwargs["message"]  # Message includes sender name
        assert "Can you review my code?" in call_kwargs["message"]  # Message includes payload

    @patch("event_bus.server.send_notification")
    def test_dm_to_nonexistent_session_no_notification(self, mock_notify):
        """Test that DM to nonexistent session doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="test message",
            channel="session:nonexistent",
        )

        # No notification should be sent
        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_broadcast_no_notification(self, mock_notify):
        """Test that broadcast doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="broadcast message",
            channel="all",
        )

        # No notification should be sent
        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_dm_truncates_long_payload(self, mock_notify):
        """Test that DM notification truncates long payloads."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        long_payload = "x" * 100
        publish_event(
            event_type="test",
            payload=long_payload,
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        message = call_kwargs["message"]

        # Verify truncation happened (full payload not in message)
        assert long_payload not in message
        # Verify ellipsis indicates truncation
        assert "..." in message
        # Verify some portion of payload is present
        assert "xxx" in message  # At least some x's

    @patch("event_bus.server.send_notification")
    def test_dm_notification_failure_still_publishes_event(self, mock_notify):
        """Test that notification failures don't prevent event publishing."""
        mock_notify.side_effect = Exception("Notification system error")

        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Should not raise - event should still be published
        result = publish_event(
            event_type="test",
            payload="important message",
            channel=f"session:{target_id}",
        )

        assert "event_id" in result
        # Verify event was stored despite notification failure
        result = get_events(session_id=target_id)
        event_types = [e["event_type"] for e in result["events"]]
        assert "test" in event_types

    @patch("event_bus.server.send_notification")
    def test_dm_from_anonymous_sender(self, mock_notify):
        """Test that DM from anonymous sender shows 'anonymous' in notification."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Send DM without session_id
        publish_event(
            event_type="test",
            payload="anonymous message",
            session_id=None,  # Anonymous sender
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "anonymous" in call_kwargs["message"]
        assert "anonymous message" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_dm_from_deleted_sender_session(self, mock_notify):
        """Test that DM from deleted sender session shows anonymous."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Send DM with session_id that doesn't exist
        publish_event(
            event_type="test",
            payload="message from ghost",
            session_id="nonexistent-session",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "anonymous" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_repo_channel_no_notification(self, mock_notify):
        """Test that repo channel doesn't trigger notification."""
        register_session(name="target", machine="test", cwd="/test/myrepo")

        publish_event(
            event_type="test",
            payload="repo message",
            channel="repo:myrepo",
        )

        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_machine_channel_no_notification(self, mock_notify):
        """Test that machine channel doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="machine message",
            channel="machine:test",
        )

        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_dm_with_empty_payload(self, mock_notify):
        """Test that DM with empty payload still notifies."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        publish_event(
            event_type="test",
            payload="",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Should still have sender info even with empty payload
        assert "From:" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_dm_with_very_long_session_name(self, mock_notify):
        """Test notification with very long session name in title."""
        very_long_name = "a" * 200
        target = register_session(name=very_long_name, machine="test", cwd="/test")
        target_id = target["session_id"]

        publish_event(
            event_type="test",
            payload="test message",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Title should contain the long name
        assert very_long_name in call_kwargs["title"]

    @patch("event_bus.server.send_notification")
    def test_dm_with_special_characters(self, mock_notify):
        """Test notification handles emoji and special characters."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        special_payload = "Hello 🎉\nMultiline\tWith\ttabs and emoji 😊"
        publish_event(
            event_type="test",
            payload=special_payload,
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Should contain the special characters
        assert "🎉" in call_kwargs["message"] or "Hello" in call_kwargs["message"]
