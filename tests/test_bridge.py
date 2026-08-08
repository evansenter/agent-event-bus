"""Tests for the webhook-to-injection bridge (RFC #122 prototype)."""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from agent_event_bus import bridge
from agent_event_bus.bridge import (
    BridgeConfig,
    Injector,
    create_bridge_app,
    resolve_target_session,
    verify_signature,
)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_event(**overrides) -> dict:
    event = {
        "event_id": 1,
        "event_type": "help_needed",
        "payload": "need a review",
        "session_id": "sender-1",
        "timestamp": "2026-08-08T00:00:00",
        "channel": "session:target-1",
        "correlation_id": None,
        "signal_level": "actionable",
    }
    event.update(overrides)
    return event


@pytest.fixture
def config(tmp_path):
    return BridgeConfig(wake_dir=tmp_path / "wake", cooldown_seconds=30.0)


@pytest.fixture
def client(config):
    return TestClient(create_bridge_app(config))


class TestSignature:
    def test_valid_signature_accepted(self):
        body = b'{"x": 1}'
        assert verify_signature(body, sign(body, "s3cret"), "s3cret") is True

    def test_bad_signature_rejected(self):
        body = b'{"x": 1}'
        assert verify_signature(body, sign(body, "wrong"), "s3cret") is False
        assert verify_signature(body, None, "s3cret") is False
        assert verify_signature(body, "not-a-signature", "s3cret") is False

    def test_hook_rejects_unsigned_when_secret_configured(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", secret="s3cret")
        client = TestClient(create_bridge_app(config))
        body = json.dumps(make_event()).encode()

        assert client.post("/hook", content=body).status_code == 401

        signed = client.post(
            "/hook", content=body, headers={"X-Event-Bus-Signature": sign(body, "s3cret")}
        )
        assert signed.status_code == 200
        assert signed.json()["status"] == "delivered"


class TestTargetResolution:
    def test_dm_channel_targets_that_session(self):
        assert resolve_target_session(make_event(channel="session:abc")) == "abc"

    def test_broadcast_and_repo_channels_have_no_target(self):
        assert resolve_target_session(make_event(channel="all")) is None
        assert resolve_target_session(make_event(channel="repo:myrepo")) is None
        assert resolve_target_session(make_event(channel="session:")) is None


class TestHookFiltering:
    def test_below_actionable_ignored(self, client, config):
        for level in ("lifecycle", "info"):
            resp = client.post("/hook", content=json.dumps(make_event(signal_level=level)).encode())
            assert resp.json() == {"status": "ignored", "reason": "below actionable"}
        assert not (config.wake_dir / "target-1.jsonl").exists()

    def test_untargeted_actionable_ignored(self, client):
        resp = client.post("/hook", content=json.dumps(make_event(channel="repo:myrepo")).encode())
        assert resp.json() == {"status": "ignored", "reason": "no target session"}

    def test_invalid_json_rejected(self, client):
        assert client.post("/hook", content=b"not-json{").status_code == 400

    def test_actionable_dm_delivered_to_spool(self, client, config):
        resp = client.post("/hook", content=json.dumps(make_event()).encode())

        assert resp.json()["status"] == "delivered"
        assert resp.json()["action"] == "spool"
        lines = (config.wake_dir / "target-1.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["payload"] == "need a review"

    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"


def make_tmux_injector(tmp_path, sessions=("target-1",), cooldown=30.0):
    """A tmux-backend injector with pane mappings for the given sessions."""
    config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux", cooldown_seconds=cooldown)
    config.wake_dir.mkdir(parents=True)
    panes = {sid: f"%{i}" for i, sid in enumerate(sessions)}
    (config.wake_dir / "panes.json").write_text(json.dumps(panes))
    return Injector(config), config


def tmux_ok(cmd, **kwargs):
    class Result:
        returncode = 0

    return Result()


class TestInjectorCooldown:
    """The cooldown bounds successful injections only - spool writes are
    durable bookkeeping, and failed wakes must not burn the window."""

    def test_second_wake_within_cooldown_spools_only(self, tmp_path):
        injector, config = make_tmux_injector(tmp_path)

        with patch.object(bridge.subprocess, "run", tmux_ok):
            assert injector.deliver("target-1", make_event()) == "tmux"
            assert injector.deliver("target-1", make_event()) == "spool-cooldown"

        # Both events were spooled - cooldown bounds wakes, not durability
        lines = (config.wake_dir / "target-1.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_cooldown_is_per_session(self, tmp_path):
        injector, _ = make_tmux_injector(tmp_path, sessions=("target-1", "target-2"))

        with patch.object(bridge.subprocess, "run", tmux_ok):
            assert injector.deliver("target-1", make_event()) == "tmux"
            assert injector.deliver("target-2", make_event()) == "tmux"

    def test_wake_allowed_after_cooldown_expires(self, tmp_path):
        injector, _ = make_tmux_injector(tmp_path, cooldown=30.0)

        clock = {"now": 1000.0}
        with patch.object(bridge.time, "monotonic", lambda: clock["now"]):
            with patch.object(bridge.subprocess, "run", tmux_ok):
                assert injector.deliver("target-1", make_event()) == "tmux"
                clock["now"] += 10.0
                assert injector.deliver("target-1", make_event()) == "spool-cooldown"
                clock["now"] += 25.0  # 35s after the successful wake
                assert injector.deliver("target-1", make_event()) == "tmux"

    def test_failed_wake_does_not_burn_cooldown(self, tmp_path):
        """A transient tmux failure must not silence the session for a full
        cooldown - the next event retries immediately."""
        injector, _ = make_tmux_injector(tmp_path)

        def tmux_fail(cmd, **kwargs):
            raise bridge.subprocess.CalledProcessError(1, cmd)

        with patch.object(bridge.subprocess, "run", tmux_fail):
            assert injector.deliver("target-1", make_event()) == "spool"

        with patch.object(bridge.subprocess, "run", tmux_ok):
            assert injector.deliver("target-1", make_event()) == "tmux"

    def test_spool_backend_never_cooldowns(self, config):
        injector = Injector(config)

        assert injector.deliver("target-1", make_event()) == "spool"
        assert injector.deliver("target-1", make_event()) == "spool"

    def test_concurrent_spool_appends_do_not_tear(self, config):
        """Concurrent webhook deliveries append under the lock; every line
        must stay valid JSON even when payloads exceed the IO buffer."""
        import threading

        injector = Injector(config)
        event = make_event(payload="x" * 100_000)  # > default 8 KiB buffer

        threads = [threading.Thread(target=injector.deliver, args=("t", event)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = (config.wake_dir / "t.jsonl").read_text().splitlines()
        assert len(lines) == 8
        for line in lines:
            json.loads(line)

    def test_wake_dir_is_private(self, config):
        Injector(config).deliver("target-1", make_event())
        assert (config.wake_dir.stat().st_mode & 0o777) == 0o700


class TestTmuxBackend:
    def test_mapped_pane_gets_send_keys(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        config.wake_dir.mkdir(parents=True)
        (config.wake_dir / "panes.json").write_text(json.dumps({"target-1": "%7"}))
        injector = Injector(config)

        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)

            class Result:
                returncode = 0

            return Result()

        with patch.object(bridge.subprocess, "run", fake_run):
            action = injector.deliver("target-1", make_event())

        assert action == "tmux"
        assert commands[0][:4] == ["tmux", "send-keys", "-t", "%7"]
        # The event is also spooled (durable path is unconditional)
        assert (config.wake_dir / "target-1.jsonl").exists()

    def test_unmapped_session_falls_back_to_spool(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)

        with patch.object(bridge.subprocess, "run") as mock_run:
            action = injector.deliver("target-1", make_event())

        assert action == "spool"
        mock_run.assert_not_called()

    def test_tmux_failure_falls_back_to_spool(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        config.wake_dir.mkdir(parents=True)
        (config.wake_dir / "panes.json").write_text(json.dumps({"target-1": "%7"}))
        injector = Injector(config)

        def fake_run(cmd, **kwargs):
            raise bridge.subprocess.CalledProcessError(1, cmd)

        with patch.object(bridge.subprocess, "run", fake_run):
            action = injector.deliver("target-1", make_event())

        assert action == "spool"


class TestBusRegistration:
    def test_registers_webhook_with_bridge_url_and_secret(self, tmp_path):
        config = BridgeConfig(
            wake_dir=tmp_path / "wake", port=9999, secret="s3cret", bus_url="http://bus/mcp"
        )
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            calls.append({"tool": tool_name, "arguments": arguments, "url": url})
            if tool_name == "list_webhooks":
                return []
            return {"webhook_id": 42}

        with patch.object(bridge, "call_tool", fake_call_tool):
            webhook_id = bridge.register_with_bus(config)

        assert webhook_id == 42
        register = next(c for c in calls if c["tool"] == "register_webhook")
        assert register["arguments"]["url"] == "http://127.0.0.1:9999/hook"
        assert register["arguments"]["secret"] == "s3cret"
        assert register["url"] == "http://bus/mcp"

    def test_startup_removes_stale_webhooks_at_same_url(self, tmp_path):
        """Unclean exits leave active webhooks behind; each would duplicate
        every wake, so startup must clean up matching URLs first."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", port=9999)
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            calls.append({"tool": tool_name, "arguments": arguments})
            if tool_name == "list_webhooks":
                return [
                    {"webhook_id": 7, "url": "http://127.0.0.1:9999/hook"},
                    {"webhook_id": 8, "url": "http://elsewhere/hook"},
                ]
            return {"webhook_id": 43}

        with patch.object(bridge, "call_tool", fake_call_tool):
            webhook_id = bridge.register_with_bus(config)

        assert webhook_id == 43
        unregisters = [c["arguments"] for c in calls if c["tool"] == "unregister_webhook"]
        # Only the stale hook at OUR url is removed, not other consumers'
        assert unregisters == [{"webhook_id": 7}]

    def test_empty_secret_env_is_normalized_to_none(self, tmp_path, monkeypatch):
        """An accidentally empty secret must not split registration (skips
        the secret -> unsigned payloads) from verification (demands
        signatures) - the combination 401s every delivery silently."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "")
        secret = __import__("os").environ.get("AGENT_EVENT_BUS_BRIDGE_SECRET") or None
        config = BridgeConfig(wake_dir=tmp_path / "wake", secret=secret)

        # Verification is disabled, matching registration's omitted secret
        client = TestClient(create_bridge_app(config))
        resp = client.post("/hook", content=json.dumps(make_event()).encode())
        assert resp.status_code == 200

    def test_unregister_swallows_bus_unreachable(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")

        def fake_call_tool(*args, **kwargs):
            raise SystemExit(1)

        with patch.object(bridge, "call_tool", fake_call_tool):
            bridge.unregister_from_bus(config, 42)  # must not raise
