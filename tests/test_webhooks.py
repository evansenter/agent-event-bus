"""Tests for webhook functionality."""

import json
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
        wh1 = storage.add_webhook(url="https://active.com")
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

        with patch("agent_event_bus.server.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

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

        with patch("agent_event_bus.server.httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            await _dispatch_webhook(webhook, event)

            # Check that signature header was included
            call_args = mock_instance.post.call_args
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
