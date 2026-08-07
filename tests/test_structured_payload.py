"""Tests for the RFC #121 substrate: optional structured payload fields
(title, tags, signal_level), first-class correlation_id, and the v4 schema
migration."""

import sqlite3
from argparse import Namespace
from datetime import datetime
from unittest.mock import patch

from agent_event_bus import cli, server

publish_event = server._publish_event_impl
get_events = server._get_events_impl


class TestStorageStructuredFields:
    def test_add_event_roundtrips_correlation_and_meta(self, storage):
        created = storage.add_event(
            event_type="task_request",
            payload="please review",
            session_id="s1",
            correlation_id="thread-42",
            meta={"title": "Review request", "tags": ["review", "urgent"]},
        )
        assert created.correlation_id == "thread-42"

        events, _ = storage.get_events(order="asc")
        assert len(events) == 1
        assert events[0].correlation_id == "thread-42"
        assert events[0].meta == {"title": "Review request", "tags": ["review", "urgent"]}

    def test_plain_events_have_no_meta(self, storage):
        storage.add_event(event_type="note", payload="hi", session_id="s1")

        events, _ = storage.get_events(order="asc")
        assert events[0].correlation_id is None
        assert events[0].meta is None

    def test_empty_meta_normalized_to_none(self, storage):
        created = storage.add_event(event_type="note", payload="hi", session_id="s1", meta={})
        assert created.meta is None

        events, _ = storage.get_events(order="asc")
        assert events[0].meta is None

    def test_filter_by_correlation_id(self, storage):
        storage.add_event(
            event_type="task_request", payload="a", session_id="s1", correlation_id="t-1"
        )
        storage.add_event(event_type="unrelated", payload="b", session_id="s2")
        storage.add_event(
            event_type="task_response", payload="c", session_id="s2", correlation_id="t-1"
        )
        storage.add_event(
            event_type="task_request", payload="d", session_id="s1", correlation_id="t-2"
        )

        events, _ = storage.get_events(order="asc", correlation_id="t-1")
        assert [e.payload for e in events] == ["a", "c"]

    def test_correlation_filter_composes_with_type_filter(self, storage):
        storage.add_event(
            event_type="task_request", payload="a", session_id="s1", correlation_id="t-1"
        )
        storage.add_event(
            event_type="task_response", payload="b", session_id="s2", correlation_id="t-1"
        )

        events, _ = storage.get_events(
            order="asc", correlation_id="t-1", event_types=["task_response"]
        )
        assert [e.payload for e in events] == ["b"]

    def test_corrupt_payload_meta_is_dropped_not_fatal(self, storage):
        storage.add_event(event_type="note", payload="hi", session_id="s1")
        with storage._connect() as conn:
            conn.execute("UPDATE events SET payload_meta = 'not-json{'")

        events, _ = storage.get_events(order="asc")
        assert events[0].meta is None


class TestMigrationV4:
    def test_migrates_v3_database(self, temp_db):
        """A pre-v4 database gains the new columns and keeps its events."""
        # Build a v3-shaped database by hand
        conn = sqlite3.connect(temp_db)
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                display_id TEXT NOT NULL,
                name TEXT NOT NULL,
                machine TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                client_id TEXT,
                last_cursor TEXT,
                deleted_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                channel TEXT NOT NULL DEFAULT 'all'
            )
        """)
        conn.execute(
            "INSERT INTO events (event_type, payload, session_id, timestamp) VALUES (?, ?, ?, ?)",
            ("legacy", "old event", "old-session", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        from agent_event_bus.storage import SQLiteStorage

        storage = SQLiteStorage(db_path=temp_db)

        # Old events survive and read back with None structured fields
        events, _ = storage.get_events(order="asc")
        assert len(events) == 1
        assert events[0].payload == "old event"
        assert events[0].correlation_id is None
        assert events[0].meta is None

        # New columns and index exist
        with storage._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            assert "correlation_id" in columns
            assert "payload_meta" in columns
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
            assert "idx_events_correlation" in indexes

    def test_fresh_database_gets_same_schema(self, storage):
        with storage._connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            assert "correlation_id" in columns
            assert "payload_meta" in columns
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
            assert "idx_events_correlation" in indexes


class TestServerStructuredPublish:
    def test_publish_and_get_structured_fields(self):
        # The suite shares one events table; anchor at the current tip so
        # only this test's events are in play.
        start = server.storage.get_cursor()
        published = publish_event(
            event_type="task_request",
            payload="please review PR 42",
            title="Review request",
            tags=["review"],
            correlation_id="thread-1",
        )
        assert published["correlation_id"] == "thread-1"

        result = get_events(cursor=start, order="asc")
        event = next(e for e in result["events"] if e["id"] == published["event_id"])
        assert event["correlation_id"] == "thread-1"
        assert event["title"] == "Review request"
        assert event["tags"] == ["review"]

    def test_plain_publish_has_null_correlation(self):
        start = server.storage.get_cursor()
        published = publish_event(event_type="note", payload="hi")
        assert "correlation_id" not in published

        result = get_events(cursor=start, order="asc")
        event = next(e for e in result["events"] if e["id"] == published["event_id"])
        assert event["correlation_id"] is None
        assert "title" not in event
        assert "tags" not in event

    def test_get_events_correlation_filter(self):
        start = server.storage.get_cursor()
        publish_event(event_type="task_request", payload="a", correlation_id="t-1")
        publish_event(event_type="noise", payload="b")
        publish_event(event_type="task_response", payload="c", correlation_id="t-1")

        result = get_events(cursor=start, order="asc", correlation_id="t-1")
        assert [e["payload"] for e in result["events"]] == ["a", "c"]

    def test_unknown_signal_level_warns_but_stores(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
            published = publish_event(event_type="note", payload="hi", signal_level="shouting")

        assert any("Unknown signal_level" in r.message for r in caplog.records)
        # Stored as-is (soft validation)
        raw_events, _ = server.storage.get_events(order="desc", limit=5)
        event = next(e for e in raw_events if e.id == published["event_id"])
        assert event.meta == {"signal_level": "shouting"}


class TestCLIStructuredFlags:
    @patch("agent_event_bus.cli.call_tool")
    def test_publish_passes_structured_fields(self, mock_call):
        mock_call.return_value = {"event_id": 1}
        args = Namespace(
            type="task_request",
            payload="please review",
            channel="all",
            session_id=None,
            title="Review request",
            tags="review, urgent",
            correlation_id="thread-1",
            signal_level="actionable",
            url=None,
            debug=False,
        )

        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        assert call_args["title"] == "Review request"
        assert call_args["tags"] == ["review", "urgent"]
        assert call_args["correlation_id"] == "thread-1"
        assert call_args["signal_level"] == "actionable"

    @patch("agent_event_bus.cli.call_tool")
    def test_publish_omits_absent_structured_fields(self, mock_call):
        mock_call.return_value = {"event_id": 1}
        args = Namespace(
            type="note",
            payload="hi",
            channel="all",
            session_id=None,
            title=None,
            tags=None,
            correlation_id=None,
            signal_level=None,
            url=None,
            debug=False,
        )

        cli.cmd_publish(args)

        call_args = mock_call.call_args[0][1]
        for key in ("title", "tags", "correlation_id", "signal_level"):
            assert key not in call_args

    @patch("agent_event_bus.cli.call_tool")
    def test_events_correlation_filter_passthrough(self, mock_call):
        from conftest import make_events_args

        mock_call.return_value = {"events": [], "next_cursor": None}

        cli.cmd_events(make_events_args(correlation_id="thread-1"))

        call_args = mock_call.call_args[0][1]
        assert call_args["correlation_id"] == "thread-1"

    def test_publish_flags_parse(self):
        import sys as _sys

        argv = [
            "cli",
            "publish",
            "--type",
            "task_request",
            "--payload",
            "p",
            "--title",
            "T",
            "--tags",
            "a,b",
            "--correlation-id",
            "t-9",
            "--signal-level",
            "actionable",
        ]
        with patch.object(_sys, "argv", argv):
            with patch("agent_event_bus.cli.cmd_publish") as mock_cmd:
                cli.main()

                args = mock_cmd.call_args[0][0]
                assert args.title == "T"
                assert args.tags == "a,b"
                assert args.correlation_id == "t-9"
                assert args.signal_level == "actionable"
