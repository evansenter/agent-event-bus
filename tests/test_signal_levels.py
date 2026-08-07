"""Tests for server-side signal-level tagging and min_level filtering (#129)."""

from datetime import datetime
from unittest.mock import patch

from agent_event_bus import cli, server
from agent_event_bus.storage import Event
from conftest import make_events_args

publish_event = server._publish_event_impl
get_events = server._get_events_impl
register_session = server._register_session_impl


def make_event(event_type="note", channel="all", meta=None):
    return Event(
        id=1,
        event_type=event_type,
        payload="p",
        session_id="s",
        timestamp=datetime.now(),
        channel=channel,
        meta=meta,
    )


class TestSignalLevelDerivation:
    def test_lifecycle_types(self):
        for event_type in (
            "session_registered",
            "session_unregistered",
            "ci_watching",
            "ci_rerun",
            "task_started",
            "parallel_work_started",
        ):
            assert server._get_signal_level(make_event(event_type)) == "lifecycle"

    def test_actionable_types(self):
        for event_type in ("help_needed", "blocker_found", "ci_failed", "error_broadcast"):
            assert server._get_signal_level(make_event(event_type)) == "actionable"

    def test_unknown_types_default_to_info(self):
        assert server._get_signal_level(make_event("gotcha_discovered")) == "info"
        assert server._get_signal_level(make_event("some_ad_hoc_type")) == "info"

    def test_dms_are_always_actionable(self):
        event = make_event("session_registered", channel="session:abc")
        assert server._get_signal_level(event) == "actionable"

    def test_explicit_level_overrides_derivation(self):
        event = make_event("session_registered", meta={"signal_level": "actionable"})
        assert server._get_signal_level(event) == "actionable"

    def test_unknown_explicit_level_falls_back_to_derived(self):
        event = make_event("session_registered", meta={"signal_level": "shouting"})
        assert server._get_signal_level(event) == "lifecycle"


class TestMinLevelFiltering:
    def test_min_level_info_drops_lifecycle(self):
        start = server.storage.get_cursor()
        publish_event(event_type="session_registered", payload="churn")
        publish_event(event_type="gotcha_discovered", payload="useful")
        publish_event(event_type="help_needed", payload="urgent")

        result = get_events(cursor=start, order="asc", min_level="info")
        assert [e["payload"] for e in result["events"]] == ["useful", "urgent"]

    def test_min_level_actionable_keeps_only_actionable(self):
        start = server.storage.get_cursor()
        publish_event(event_type="session_registered", payload="churn")
        publish_event(event_type="gotcha_discovered", payload="useful")
        publish_event(event_type="help_needed", payload="urgent")

        result = get_events(cursor=start, order="asc", min_level="actionable")
        assert [e["payload"] for e in result["events"]] == ["urgent"]

    def test_no_min_level_returns_everything(self):
        start = server.storage.get_cursor()
        publish_event(event_type="session_registered", payload="churn")
        publish_event(event_type="gotcha_discovered", payload="useful")

        result = get_events(cursor=start, order="asc")
        assert len(result["events"]) == 2

    def test_events_carry_signal_level(self):
        start = server.storage.get_cursor()
        publish_event(event_type="help_needed", payload="urgent")

        result = get_events(cursor=start, order="asc")
        assert result["events"][0]["signal_level"] == "actionable"

    def test_filtered_events_still_advance_session_cursor(self):
        """Noise filtered by min_level counts as seen - a later resume must
        not replay it."""
        registered = register_session(name="filter-test", client_id="filter-test-client")
        sid = registered["session_id"]

        published = publish_event(event_type="session_registered", payload="lifecycle noise")

        result = get_events(
            cursor=registered["cursor"], session_id=sid, order="asc", min_level="actionable"
        )
        assert result["events"] == []

        session = server.storage.get_session(sid)
        assert session.last_cursor == str(published["event_id"])


class TestCLIMinLevel:
    @patch("agent_event_bus.cli.call_tool")
    def test_min_level_passthrough(self, mock_call):
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args(min_level="actionable"))

        call_args = mock_call.call_args[0][1]
        assert call_args["min_level"] == "actionable"

    @patch("agent_event_bus.cli.call_tool")
    def test_min_level_absent_omitted(self, mock_call):
        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args())

        call_args = mock_call.call_args[0][1]
        assert "min_level" not in call_args

    def test_min_level_flag_parses(self):
        import sys as _sys

        with patch.object(_sys, "argv", ["cli", "events", "--min-level", "info"]):
            with patch("agent_event_bus.cli.cmd_events") as mock_cmd:
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.min_level == "info"
