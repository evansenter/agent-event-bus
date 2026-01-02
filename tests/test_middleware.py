"""Tests for middleware formatting functions."""

from unittest.mock import patch

from event_bus.middleware import (
    _BOLD,
    _CYAN,
    _DIM,
    _GREEN,
    _MAGENTA,
    _RED,
    _format_args,
    _format_list,
    _format_result,
    _format_session_id_value,
    _get_active_sessions_map,
    _is_human_readable_id,
    _parse_sse_response,
)


class TestFormatArgs:
    """Tests for _format_args function."""

    def test_empty_args(self):
        """Empty args returns empty string."""
        assert _format_args({}) == ""

    def test_simple_args(self):
        """Simple args are formatted as key=value."""
        result = _format_args({"limit": 10})
        assert "limit=10" in result

    def test_string_args(self):
        """String values are JSON-quoted."""
        result = _format_args({"type": "test"})
        assert 'type="test"' in result

    def test_highlighted_fields(self):
        """name and channel are highlighted with bold values."""
        result = _format_args({"name": "my-feature"})
        assert _CYAN in result
        assert _BOLD in result
        assert "name" in result
        assert "my-feature" in result

    def test_session_id_human_readable(self):
        """Human-readable session_id is shown bold."""
        result = _format_args({"session_id": "brave-tiger"})
        assert _CYAN in result
        assert _BOLD in result
        assert "session_id" in result
        assert "brave-tiger" in result

    def test_session_id_uuid_truncated(self):
        """UUID session_id is dimmed and truncated."""
        result = _format_args({"session_id": "b712a0ba-1ee6-4c18-a647-31a785147665"})
        assert _CYAN in result
        assert _DIM in result
        assert "session_id" in result
        assert "b712a0ba" in result  # First 8 chars
        assert "1ee6" not in result  # Rest is truncated

    def test_multiple_args(self):
        """Multiple args are comma-separated."""
        result = _format_args({"limit": 10, "order": "desc"})
        assert "limit=10" in result
        assert 'order="desc"' in result
        assert ", " in result


class TestFormatList:
    """Tests for _format_list function."""

    def test_empty_list(self):
        """Empty list shows 'empty'."""
        result = _format_list([])
        assert "empty" in result
        assert _DIM in result

    def test_session_list(self):
        """List with session_id shows names."""
        items = [
            {"session_id": "brave-tiger"},
            {"session_id": "happy-falcon"},
        ]
        result = _format_list(items)
        assert "brave-tiger" in result
        assert "happy-falcon" in result
        assert _CYAN in result

    def test_channel_list(self):
        """List with channel+subscribers shows channel names."""
        items = [
            {"channel": "all", "subscribers": 5},
            {"channel": "repo:test", "subscribers": 2},
        ]
        result = _format_list(items)
        assert "all" in result
        assert "repo:test" in result

    def test_generic_list(self):
        """List without session_id/channel shows count."""
        items = [{"foo": "bar"}, {"baz": "qux"}]
        result = _format_list(items)
        assert "2 items" in result


class TestFormatResult:
    """Tests for _format_result function."""

    def test_session_id_result(self):
        """Result with session_id shows session=name."""
        result = _format_result({"session_id": "tender-hawk"})
        assert "session=tender-hawk" in result
        assert _CYAN in result

    def test_events_result_empty(self):
        """Empty events result shows 0 events."""
        result = _format_result({"events": [], "next_cursor": "0"})
        assert "0 events" in result
        assert "cursor=0" in result

    def test_events_result_with_count(self):
        """Events result shows count and cursor."""
        events = [
            {"id": 1, "session_id": "test", "timestamp": "2026-01-01T12:00:00"},
            {"id": 2, "session_id": "test", "timestamp": "2026-01-01T12:05:00"},
        ]
        result = _format_result({"events": events, "next_cursor": "2"})
        assert "2 events" in result
        assert "cursor=2" in result

    def test_events_result_with_publishers(self):
        """Events result shows unique publishers."""
        events = [
            {"id": 1, "session_id": "brave-tiger", "timestamp": "2026-01-01T12:00:00"},
            {"id": 2, "session_id": "happy-falcon", "timestamp": "2026-01-01T12:05:00"},
        ]
        # Mock active sessions map (session_id → display_id)
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={"brave-tiger": "brave-tiger", "happy-falcon": "happy-falcon"},
        ):
            result = _format_result({"events": events, "next_cursor": "2"})
            assert "from:" in result
            assert "brave-tiger" in result
            assert "happy-falcon" in result

    def test_events_result_excludes_anonymous(self):
        """Anonymous publishers are not shown."""
        events = [
            {"id": 1, "session_id": "anonymous", "timestamp": "2026-01-01T12:00:00"},
        ]
        result = _format_result({"events": events, "next_cursor": "1"})
        assert "from:" not in result

    def test_events_result_excludes_uuids(self):
        """UUID session_ids are not shown (only human-readable names)."""
        events = [
            {
                "id": 1,
                "session_id": "b712a0ba-1ee6-4c18-a647-31a785147665",
                "timestamp": "2026-01-01T12:00:00",
            },
            {"id": 2, "session_id": "5f296cf4", "timestamp": "2026-01-01T12:05:00"},
        ]
        result = _format_result({"events": events, "next_cursor": "2"})
        assert "from:" not in result  # No human-readable IDs, so no "from:"

    def test_events_result_timespan_order_independent(self):
        """Timespan always shows oldest→newest regardless of event order."""
        # Events in reverse chronological order (desc)
        events = [
            {"id": 2, "session_id": "brave-tiger", "timestamp": "2026-01-01T12:30:00"},
            {"id": 1, "session_id": "brave-tiger", "timestamp": "2026-01-01T12:00:00"},
        ]
        # Mock active sessions map for consistent behavior
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={"brave-tiger": "brave-tiger"},
        ):
            result = _format_result({"events": events, "next_cursor": "2"})
            # Should show oldest→newest: 12:00→12:30
            assert "2026-01-01T12:00" in result
            assert "2026-01-01T12:30" in result
            assert "→" in result

    def test_event_id_result(self):
        """Result with event_id shows event #N."""
        result = _format_result({"event_id": 42, "channel": "all"})
        assert "event #42" in result
        assert "[all]" in result
        assert _MAGENTA in result

    def test_success_true_result(self):
        """Success=true shows OK."""
        result = _format_result({"success": True})
        assert "OK" in result
        assert _GREEN in result

    def test_success_false_result(self):
        """Success=false shows FAILED."""
        result = _format_result({"success": False})
        assert "FAILED" in result
        assert _RED in result

    def test_error_result(self):
        """Error result shows ERROR: message."""
        result = _format_result({"error": "Something went wrong"})
        assert "ERROR:" in result
        assert "Something went wrong" in result

    def test_structured_content_unwrapping(self):
        """FastMCP structuredContent wrapper is unwrapped."""
        wrapped = {
            "structuredContent": {"session_id": "test-session"},
            "content": [],
            "isError": False,
        }
        result = _format_result(wrapped)
        assert "session=test-session" in result

    def test_list_result(self):
        """List result delegates to _format_list."""
        result = _format_result([{"session_id": "a"}, {"session_id": "b"}])
        assert "a" in result
        assert "b" in result

    def test_fallback_shows_keys(self):
        """Unknown dict shows keys."""
        result = _format_result({"foo": "bar", "baz": 123})
        assert "foo" in result or "baz" in result


class TestParseSSEResponse:
    """Tests for _parse_sse_response function."""

    def test_valid_sse_response(self):
        """Valid SSE response extracts JSON from data line."""
        sse = 'event: message\ndata: {"result": {"session_id": "test"}}\n\n'
        result = _parse_sse_response(sse)
        assert result == {"result": {"session_id": "test"}}

    def test_multiline_sse_response(self):
        """SSE with multiple data lines uses first valid one."""
        sse = 'event: message\ndata: {"first": true}\nevent: other\ndata: {"second": true}\n\n'
        result = _parse_sse_response(sse)
        assert result == {"first": True}

    def test_empty_response(self):
        """Empty response returns empty dict."""
        result = _parse_sse_response("")
        assert result == {}

    def test_no_data_line(self):
        """Response without data line returns empty dict."""
        result = _parse_sse_response("event: message\n\n")
        assert result == {}

    def test_invalid_json(self):
        """Invalid JSON in data line returns empty dict."""
        result = _parse_sse_response("data: not-valid-json\n\n")
        assert result == {}

    def test_data_prefix_only(self):
        """data: prefix without content returns empty dict."""
        result = _parse_sse_response("data: \n\n")
        assert result == {}


class TestIsHumanReadableId:
    """Tests for _is_human_readable_id function."""

    def test_valid_human_readable_ids(self):
        """Docker-style adjective-noun IDs are human-readable."""
        assert _is_human_readable_id("brave-tiger") is True
        assert _is_human_readable_id("tender-hawk") is True
        assert _is_human_readable_id("happy-falcon") is True

    def test_uuid_not_human_readable(self):
        """UUIDs are not human-readable."""
        assert _is_human_readable_id("b712a0ba-1ee6-4c18-a647-31a785147665") is False

    def test_short_hex_not_human_readable(self):
        """Short hex strings (truncated UUIDs) are not human-readable."""
        assert _is_human_readable_id("5f296cf4") is False
        assert _is_human_readable_id("adb2d408") is False

    def test_anonymous_not_human_readable(self):
        """'anonymous' is not considered human-readable."""
        assert _is_human_readable_id("anonymous") is False

    def test_empty_not_human_readable(self):
        """Empty string is not human-readable."""
        assert _is_human_readable_id("") is False

    def test_single_word_not_human_readable(self):
        """Single word without hyphen is not human-readable."""
        assert _is_human_readable_id("tiger") is False

    def test_numbers_not_human_readable(self):
        """Words with numbers are not human-readable."""
        assert _is_human_readable_id("brave-tiger123") is False
        assert _is_human_readable_id("brave123-tiger") is False

    def test_uppercase_not_human_readable(self):
        """Uppercase words are not human-readable."""
        assert _is_human_readable_id("Brave-Tiger") is False
        assert _is_human_readable_id("BRAVE-TIGER") is False


class TestFormatSessionIdValue:
    """Tests for _format_session_id_value function."""

    def test_human_readable_bold(self):
        """Human-readable IDs are shown bold."""
        result = _format_session_id_value("brave-tiger")
        assert _BOLD in result
        assert "brave-tiger" in result

    def test_uuid_dimmed_truncated(self):
        """Long UUIDs are dimmed and truncated to 8 chars."""
        result = _format_session_id_value("b712a0ba-1ee6-4c18-a647-31a785147665")
        assert _DIM in result
        assert "b712a0ba" in result
        assert "…" in result  # Ellipsis indicates truncation
        assert "1ee6" not in result

    def test_short_id_dimmed_not_truncated(self):
        """Short non-human-readable IDs are dimmed but not truncated."""
        result = _format_session_id_value("abc123")
        assert _DIM in result
        assert "abc123" in result
        assert "…" not in result


class TestGetActiveSessionsMap:
    """Tests for _get_active_sessions_map function."""

    def test_returns_dict(self):
        """Returns a dict (possibly empty)."""
        result = _get_active_sessions_map()
        assert isinstance(result, dict)


class TestInactiveSessionHighlighting:
    """Tests for inactive session highlighting in event results."""

    def test_active_sessions_shown_in_cyan(self):
        """Active sessions are shown in cyan."""
        events = [
            {"id": 1, "session_id": "brave-tiger", "timestamp": "2026-01-01T12:00:00"},
        ]
        # Mock _get_active_sessions_map to return our session as active
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={"brave-tiger": "brave-tiger"},
        ):
            result = _format_result({"events": events, "next_cursor": "1"})
            assert "brave-tiger" in result
            assert _CYAN in result
            # Should NOT have red for this session
            # Check that brave-tiger appears after CYAN, not after RED
            cyan_pos = result.find(_CYAN)
            red_pos = result.find(_RED)
            tiger_pos = result.find("brave-tiger")
            # Either no red, or tiger appears after cyan before any red
            assert red_pos == -1 or tiger_pos < red_pos or cyan_pos < tiger_pos < red_pos

    def test_inactive_sessions_shown_in_red(self):
        """Inactive sessions are shown in red."""
        events = [
            {"id": 1, "session_id": "stale-falcon", "timestamp": "2026-01-01T12:00:00"},
        ]
        # Mock _get_active_sessions_map to return empty (no active sessions)
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={},
        ):
            result = _format_result({"events": events, "next_cursor": "1"})
            assert "stale-falcon" in result
            assert _RED in result

    def test_mixed_active_inactive_sessions(self):
        """Active and inactive sessions are colored differently."""
        events = [
            {"id": 1, "session_id": "active-tiger", "timestamp": "2026-01-01T12:00:00"},
            {"id": 2, "session_id": "gone-falcon", "timestamp": "2026-01-01T12:05:00"},
        ]
        # Mock to mark only active-tiger as active
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={"active-tiger": "active-tiger"},
        ):
            result = _format_result({"events": events, "next_cursor": "2"})
            assert "active-tiger" in result
            assert "gone-falcon" in result
            # Both colors should be present
            assert _CYAN in result
            assert _RED in result

    def test_active_sessions_sorted_first(self):
        """Active sessions appear before inactive sessions in output."""
        events = [
            {"id": 1, "session_id": "zebra-active", "timestamp": "2026-01-01T12:00:00"},
            {"id": 2, "session_id": "alpha-gone", "timestamp": "2026-01-01T12:05:00"},
            {"id": 3, "session_id": "beta-active", "timestamp": "2026-01-01T12:10:00"},
        ]
        # zebra-active and beta-active are active, alpha-gone is not
        # Despite alpha-gone being first alphabetically, active sessions come first
        with patch(
            "event_bus.middleware._get_active_sessions_map",
            return_value={"zebra-active": "zebra-active", "beta-active": "beta-active"},
        ):
            result = _format_result({"events": events, "next_cursor": "3"})
            # Active sessions should appear before inactive
            # Order should be: beta-active, zebra-active, alpha-gone
            beta_pos = result.find("beta-active")
            zebra_pos = result.find("zebra-active")
            alpha_pos = result.find("alpha-gone")
            # Active sessions (beta, zebra) should come before inactive (alpha)
            assert beta_pos < alpha_pos, (
                "Active beta-active should appear before inactive alpha-gone"
            )
            assert zebra_pos < alpha_pos, (
                "Active zebra-active should appear before inactive alpha-gone"
            )
            # Within active, alphabetical order
            assert beta_pos < zebra_pos, (
                "beta-active should appear before zebra-active (alphabetical)"
            )
