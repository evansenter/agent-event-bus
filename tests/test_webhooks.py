"""Tests for webhook functionality."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from agent_event_bus.storage import Event, SQLiteStorage, Webhook


class TestWebhookStorage:
    """Tests for webhook storage operations."""

    @pytest.fixture
    def storage(self, temp_db):
        """Create a storage instance with a temporary database."""
        return SQLiteStorage(str(temp_db))

    def test_add_webhook(self, storage):
        """Test adding a webhook."""
        webhook = storage.add_webhook(
            url="https://example.com/webhook",
            channel_filter="session:",
            event_types=["greeting", "task_completed"],
            secret="test-secret",
        )

        assert webhook.id is not None
        assert webhook.url == "https://example.com/webhook"
        assert webhook.channel_filter == "session:"
        assert webhook.event_types == ["greeting", "task_completed"]
        assert webhook.secret == "test-secret"
        assert webhook.active is True

    def test_add_webhook_minimal(self, storage):
        """Test adding a webhook with minimal config."""
        webhook = storage.add_webhook(url="https://example.com/hook")

        assert webhook.url == "https://example.com/hook"
        assert webhook.channel_filter is None
        assert webhook.event_types is None
        assert webhook.secret is None

    def test_list_webhooks(self, storage):
        """Test listing webhooks."""
        storage.add_webhook(url="https://a.com")
        storage.add_webhook(url="https://b.com")

        webhooks = storage.list_webhooks()
        assert len(webhooks) == 2
        urls = {wh.url for wh in webhooks}
        assert urls == {"https://a.com", "https://b.com"}

    def test_list_webhooks_active_only(self, storage):
        """Test listing only active webhooks."""
        storage.add_webhook(url="https://active.com")
        wh2 = storage.add_webhook(url="https://inactive.com")
        storage.set_webhook_active(wh2.id, False)

        active_webhooks = storage.list_webhooks(active_only=True)
        assert len(active_webhooks) == 1
        assert active_webhooks[0].url == "https://active.com"

        all_webhooks = storage.list_webhooks(active_only=False)
        assert len(all_webhooks) == 2

    def test_get_webhook(self, storage):
        """Test getting a webhook by ID."""
        created = storage.add_webhook(url="https://test.com")

        webhook = storage.get_webhook(created.id)
        assert webhook is not None
        assert webhook.url == "https://test.com"

    def test_get_webhook_not_found(self, storage):
        """Test getting a non-existent webhook."""
        webhook = storage.get_webhook(9999)
        assert webhook is None

    def test_delete_webhook(self, storage):
        """Test deleting a webhook."""
        webhook = storage.add_webhook(url="https://delete-me.com")

        result = storage.delete_webhook(webhook.id)
        assert result is True

        assert storage.get_webhook(webhook.id) is None

    def test_delete_webhook_not_found(self, storage):
        """Test deleting a non-existent webhook."""
        result = storage.delete_webhook(9999)
        assert result is False

    def test_set_webhook_active(self, storage):
        """Test enabling/disabling a webhook."""
        webhook = storage.add_webhook(url="https://test.com")
        assert webhook.active is True

        storage.set_webhook_active(webhook.id, False)
        updated = storage.get_webhook(webhook.id)
        assert updated.active is False

        storage.set_webhook_active(webhook.id, True)
        updated = storage.get_webhook(webhook.id)
        assert updated.active is True


class TestWebhookMatching:
    """Tests for webhook event matching logic."""

    @pytest.fixture
    def storage(self, temp_db):
        return SQLiteStorage(str(temp_db))

    def _make_event(self, event_type="test", channel="all"):
        return Event(
            id=1,
            event_type=event_type,
            payload="test",
            session_id="test",
            timestamp=datetime.now(),
            channel=channel,
        )

    def test_match_all_events(self, storage):
        """Webhook with no filters matches all events."""
        storage.add_webhook(url="https://catch-all.com")
        event = self._make_event()

        matching = storage.get_matching_webhooks(event)
        assert len(matching) == 1

    def test_match_channel_exact(self, storage):
        """Webhook matches exact channel."""
        storage.add_webhook(url="https://test.com", channel_filter="repo:myrepo")

        event = self._make_event(channel="repo:myrepo")
        assert len(storage.get_matching_webhooks(event)) == 1

        event = self._make_event(channel="repo:other")
        assert len(storage.get_matching_webhooks(event)) == 0

    def test_match_channel_prefix(self, storage):
        """Webhook matches channel prefix."""
        storage.add_webhook(url="https://test.com", channel_filter="session:")

        event = self._make_event(channel="session:abc-123")
        assert len(storage.get_matching_webhooks(event)) == 1

        event = self._make_event(channel="repo:myrepo")
        assert len(storage.get_matching_webhooks(event)) == 0

    def test_match_event_type(self, storage):
        """Webhook matches specific event types."""
        storage.add_webhook(
            url="https://test.com",
            event_types=["task_completed", "help_needed"],
        )

        event = self._make_event(event_type="task_completed")
        assert len(storage.get_matching_webhooks(event)) == 1

        event = self._make_event(event_type="greeting")
        assert len(storage.get_matching_webhooks(event)) == 0

    def test_match_combined_filters(self, storage):
        """Webhook with both channel and event type filters."""
        storage.add_webhook(
            url="https://test.com",
            channel_filter="session:",
            event_types=["greeting"],
        )

        # Matches both filters
        event = self._make_event(event_type="greeting", channel="session:abc")
        assert len(storage.get_matching_webhooks(event)) == 1

        # Wrong channel
        event = self._make_event(event_type="greeting", channel="all")
        assert len(storage.get_matching_webhooks(event)) == 0

        # Wrong event type
        event = self._make_event(event_type="task_completed", channel="session:abc")
        assert len(storage.get_matching_webhooks(event)) == 0

    def test_inactive_webhook_not_matched(self, storage):
        """Inactive webhooks are not matched."""
        wh = storage.add_webhook(url="https://test.com")
        storage.set_webhook_active(wh.id, False)

        event = self._make_event()
        assert len(storage.get_matching_webhooks(event)) == 0


class TestWebhookDispatch:
    """Tests for webhook HTTP dispatch."""

    @pytest.fixture
    def storage(self, temp_db):
        return SQLiteStorage(str(temp_db))

    @pytest.mark.asyncio
    async def test_dispatch_webhook_success(self, storage):
        """Test successful webhook dispatch."""
        from agent_event_bus.server import _dispatch_webhook

        webhook = Webhook(
            id=1,
            url="https://example.com/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime.now(),
            active=True,
            secret=None,
        )
        event = Event(
            id=1,
            event_type="test",
            payload="hello",
            session_id="test",
            timestamp=datetime.now(),
            channel="all",
        )

        with patch("agent_event_bus.server._get_webhook_client") as mock_get_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await _dispatch_webhook(webhook, event)
            assert result is True

    @pytest.mark.asyncio
    async def test_dispatch_webhook_with_signature(self, storage):
        """Test webhook dispatch includes HMAC signature when secret is set."""
        from agent_event_bus.server import _dispatch_webhook

        webhook = Webhook(
            id=1,
            url="https://example.com/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime.now(),
            active=True,
            secret="my-secret",
        )
        event = Event(
            id=1,
            event_type="test",
            payload="hello",
            session_id="test",
            timestamp=datetime.now(),
            channel="all",
        )

        with patch("agent_event_bus.server._get_webhook_client") as mock_get_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            await _dispatch_webhook(webhook, event)

            # Check that signature header was included
            call_args = mock_client.post.call_args
            headers = call_args.kwargs.get("headers", {})
            assert "X-Event-Bus-Signature" in headers
            assert headers["X-Event-Bus-Signature"].startswith("sha256=")


class TestWebhookSignature:
    """Tests for webhook signature computation."""

    def test_compute_signature(self):
        """Test HMAC-SHA256 signature computation."""
        from agent_event_bus.server import _compute_signature

        payload = b'{"test": "data"}'
        secret = "my-secret"

        signature = _compute_signature(payload, secret)

        # Verify it's a valid hex string
        assert len(signature) == 64
        int(signature, 16)  # Should not raise

        # Same input should produce same output
        assert _compute_signature(payload, secret) == signature

        # Different secret should produce different output
        assert _compute_signature(payload, "other-secret") != signature


class TestWebhookMCPTools:
    """Tests for webhook MCP tool wrappers."""

    @pytest.fixture
    def storage(self, temp_db):
        return SQLiteStorage(str(temp_db))

    def test_register_webhook_tool(self):
        """Test register_webhook MCP tool."""
        from agent_event_bus import server

        register_webhook = server.register_webhook.fn

        result = register_webhook(
            url="https://example.com/hook",
            channel="repo:",
            event_types=["task_completed"],
            secret="test-secret",
        )

        assert "webhook_id" in result
        assert result["url"] == "https://example.com/hook"
        assert result["channel"] == "repo:"
        assert result["event_types"] == ["task_completed"]
        assert "created_at" in result

    def test_register_webhook_minimal(self):
        """Test register_webhook with minimal args."""
        from agent_event_bus import server

        register_webhook = server.register_webhook.fn

        result = register_webhook(url="https://example.com/simple")

        assert "webhook_id" in result
        assert result["url"] == "https://example.com/simple"
        assert result["channel"] is None
        assert result["event_types"] is None

    def test_list_webhooks_tool_redacts_secrets(self):
        """Test list_webhooks redacts secrets."""
        from agent_event_bus import server

        register_webhook = server.register_webhook.fn
        list_webhooks = server.list_webhooks.fn

        register_webhook(url="https://a.com", secret="my-secret")
        register_webhook(url="https://b.com")  # No secret

        result = list_webhooks()

        assert len(result) == 2
        # Secrets are redacted - only has_secret flag exposed
        for wh in result:
            assert "secret" not in wh
            assert "has_secret" in wh

        with_secret = next(wh for wh in result if wh["url"] == "https://a.com")
        without_secret = next(wh for wh in result if wh["url"] == "https://b.com")
        assert with_secret["has_secret"] is True
        assert without_secret["has_secret"] is False

    def test_list_webhooks_active_only(self):
        """Test list_webhooks active_only filter."""
        from agent_event_bus import server

        register_webhook = server.register_webhook.fn
        list_webhooks = server.list_webhooks.fn

        register_webhook(url="https://active.com")
        wh2 = register_webhook(url="https://inactive.com")
        server.storage.set_webhook_active(wh2["webhook_id"], False)

        active = list_webhooks(active_only=True)
        all_webhooks = list_webhooks(active_only=False)

        assert len(active) == 1
        assert active[0]["url"] == "https://active.com"
        assert len(all_webhooks) == 2

    def test_unregister_webhook_tool_success(self):
        """Test unregister_webhook success."""
        from agent_event_bus import server

        register_webhook = server.register_webhook.fn
        unregister_webhook = server.unregister_webhook.fn

        wh = register_webhook(url="https://delete-me.com")
        result = unregister_webhook(wh["webhook_id"])

        assert result["success"] is True
        assert result["webhook_id"] == wh["webhook_id"]

    def test_unregister_webhook_tool_not_found(self):
        """Test unregister_webhook with invalid ID."""
        from agent_event_bus import server

        unregister_webhook = server.unregister_webhook.fn

        result = unregister_webhook(webhook_id=99999)

        assert result["success"] is False
        assert "error" in result
        assert result["webhook_id"] == 99999


class TestWebhookIntegration:
    """Integration tests for webhook dispatch on publish."""

    @pytest.fixture
    def storage(self, temp_db):
        return SQLiteStorage(str(temp_db))

    def test_publish_event_schedules_webhook_dispatch(self):
        """Test that publish_event triggers webhook dispatch."""
        from unittest.mock import patch

        from agent_event_bus import server

        register_webhook = server.register_webhook.fn
        publish_event = server.publish_event.fn

        # Register a webhook
        register_webhook(url="https://example.com/hook")

        # Publish an event and verify _schedule_webhook_dispatch is called
        with patch("agent_event_bus.server._schedule_webhook_dispatch") as mock_dispatch:
            result = publish_event(
                event_type="test_event",
                payload="test payload",
            )

            # Verify dispatch was called with the event
            mock_dispatch.assert_called_once()
            dispatched_event = mock_dispatch.call_args[0][0]
            assert dispatched_event.id == result["event_id"]
            assert dispatched_event.event_type == "test_event"

    @pytest.mark.asyncio
    async def test_dispatch_webhook_retry_on_failure(self):
        """Test webhook retries on HTTP errors."""
        from agent_event_bus.server import _dispatch_webhook

        webhook = Webhook(
            id=1,
            url="https://example.com/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime.now(),
            active=True,
            secret=None,
        )
        event = Event(
            id=1,
            event_type="test",
            payload="hello",
            session_id="test",
            timestamp=datetime.now(),
            channel="all",
        )

        with patch("agent_event_bus.server._get_webhook_client") as mock_get_client:
            # Mock 500, 500, then 200 (succeeds on retry)
            mock_responses = [
                AsyncMock(status_code=500),
                AsyncMock(status_code=500),
                AsyncMock(status_code=200),
            ]
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=mock_responses)
            mock_get_client.return_value = mock_client

            result = await _dispatch_webhook(webhook, event)

            assert result is True
            assert mock_client.post.call_count == 3  # Retried twice, succeeded on 3rd

    @pytest.mark.asyncio
    async def test_dispatch_webhook_exhausts_retries(self):
        """Test webhook returns False after exhausting retries."""
        from agent_event_bus.server import _dispatch_webhook

        webhook = Webhook(
            id=1,
            url="https://example.com/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime.now(),
            active=True,
            secret=None,
        )
        event = Event(
            id=1,
            event_type="test",
            payload="hello",
            session_id="test",
            timestamp=datetime.now(),
            channel="all",
        )

        with patch("agent_event_bus.server._get_webhook_client") as mock_get_client:
            # All attempts fail
            mock_response = AsyncMock(status_code=500)
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_client

            result = await _dispatch_webhook(webhook, event)

            assert result is False
            # 1 initial + 2 retries = 3 calls
            assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_dispatch_webhook_handles_timeout(self):
        """Test webhook handles timeout gracefully."""
        import httpx

        from agent_event_bus.server import _dispatch_webhook

        webhook = Webhook(
            id=1,
            url="https://example.com/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime.now(),
            active=True,
            secret=None,
        )
        event = Event(
            id=1,
            event_type="test",
            payload="hello",
            session_id="test",
            timestamp=datetime.now(),
            channel="all",
        )

        with patch("agent_event_bus.server._get_webhook_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_get_client.return_value = mock_client

            result = await _dispatch_webhook(webhook, event)

            # Should return False, not raise
            assert result is False
