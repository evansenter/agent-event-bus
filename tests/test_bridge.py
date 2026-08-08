"""Tests for the webhook-to-injection bridge (RFC #122 prototype)."""

import fcntl
import json
import threading
import time
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
from agent_event_bus.server import _compute_signature


def sign(body: bytes, secret: str) -> str:
    # Built from the BUS's signing function, not a local copy: these are
    # contract tests, and a bus-side change to the canonical form must
    # fail them rather than leave real deliveries 401ing silently
    return "sha256=" + _compute_signature(body, secret)


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


BRIDGE_ENV = (
    "AGENT_EVENT_BUS_URL",
    "AGENT_EVENT_BUS_BRIDGE_PORT",
    "AGENT_EVENT_BUS_BRIDGE_BACKEND",
    "AGENT_EVENT_BUS_BRIDGE_COOLDOWN",
    "AGENT_EVENT_BUS_BRIDGE_SECRET",
    "AGENT_EVENT_BUS_BRIDGE_HOOK_URL",
    "AGENT_EVENT_BUS_BRIDGE_BIND",
    "AGENT_EVENT_BUS_WAKE_DIR",
)


@pytest.fixture(autouse=True)
def clean_bridge_env(monkeypatch):
    """Config tests parse the real environment via build_parser defaults, and
    client machines are told to export AGENT_EVENT_BUS_URL - the suite must
    not change meaning based on the developer's shell profile."""
    for name in BRIDGE_ENV:
        monkeypatch.delenv(name, raising=False)


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

    def test_non_ascii_signature_header_rejected_not_raised(self):
        """str compare_digest raises TypeError on non-ASCII input, and
        Starlette decodes headers as latin-1 - a hostile header byte above
        0x7f must be a clean reject (401), not an escape into a 500."""
        assert verify_signature(b'{"x": 1}', "sha256=caf\xe9", "s3cret") is False

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

    def test_uuid_and_display_ids_accepted(self):
        uuid = "067fd316-8cc7-5cb1-861f-4d9d67ba7ee8"
        assert resolve_target_session(make_event(channel=f"session:{uuid}")) == uuid
        assert resolve_target_session(make_event(channel="session:brave_trex-2")) == "brave_trex-2"

    def test_non_string_channel_has_no_target(self):
        """A non-string channel inside an otherwise valid event must resolve
        to no target, not raise .startswith into a 500."""
        for channel in (123, True, ["session:x"], {"c": 1}, None):
            assert resolve_target_session(make_event(channel=channel)) is None


class TestPathSafety:
    """The session id comes verbatim off the wire (the event's channel string,
    which the bus warns on but never rejects) and becomes a spool filename -
    traversal and absolute components must never reach the filesystem."""

    def test_unsafe_session_ids_resolve_to_none(self):
        for channel in (
            "session:../../victim/x",
            "session:/etc/cron.d/x",
            "session:a/b",
            "session:a\x00b",
            "session:..",
            # Charset-clean but too long: would be an unretryable OSError
            # (name too long) out of the spool open
            "session:" + "a" * 300,
        ):
            assert resolve_target_session(make_event(channel=channel)) is None

    def test_traversal_channel_is_ignored_and_writes_nothing(self, client, config, tmp_path):
        evil = make_event(channel="session:../../escape")
        resp = client.post("/hook", content=json.dumps(evil).encode())

        assert resp.json() == {"status": "ignored", "reason": "no target session"}
        assert list(tmp_path.rglob("*.jsonl")) == []

    def test_spool_refuses_escaping_path(self, config):
        """Belt-and-braces: even if a bad id reached _spool directly, the
        resolved-parent check must refuse to write outside the wake dir."""
        injector = Injector(config)
        with pytest.raises(ValueError, match="escapes wake dir"):
            injector._spool("../escape", make_event())
        assert not (config.wake_dir.parent / "escape.jsonl").exists()


class TestHookFiltering:
    def test_session_dm_signal_level_contract(self):
        """The bridge filters on the literal 'actionable' - pin that the bus
        really derives that level for session: DMs, the same way sign()
        builds on the bus's _compute_signature. If the bus stopped making
        DMs actionable, every bridge filter test would otherwise stay green
        while the bridge woke nobody."""
        from datetime import datetime

        from agent_event_bus.server import _get_signal_level
        from agent_event_bus.storage import Event as BusEvent

        dm = BusEvent(
            id=1,
            event_type="note",
            payload="hi",
            session_id="sender-1",
            timestamp=datetime(2026, 8, 8),
            channel="session:target-1",
        )
        assert _get_signal_level(dm) == "actionable"

    def test_below_actionable_ignored(self, client, config):
        for level in ("lifecycle", "info"):
            resp = client.post("/hook", content=json.dumps(make_event(signal_level=level)).encode())
            assert resp.json() == {"status": "ignored", "reason": "below actionable"}
        assert not (config.wake_dir / "target-1.jsonl").exists()

    def test_untargeted_actionable_ignored(self, client):
        resp = client.post("/hook", content=json.dumps(make_event(channel="repo:myrepo")).encode())
        assert resp.json() == {"status": "ignored", "reason": "no target session"}

    def test_non_string_channel_ignored_not_500(self, client):
        resp = client.post("/hook", content=json.dumps(make_event(channel=123)).encode())
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored", "reason": "no target session"}

    def test_invalid_json_rejected(self, client):
        assert client.post("/hook", content=b"not-json{").status_code == 400

    def test_non_object_json_rejected(self, client):
        """Valid JSON that isn't an object must be a 400, not an
        AttributeError-turned-500 with bus retries behind it."""
        for body in (b"123", b'["x"]', b'"bare"', b"null"):
            assert client.post("/hook", content=body).status_code == 400

    def test_oversized_body_rejected(self, client):
        """The body must be read before the HMAC can be checked, so bound
        what an unauthenticated peer can make the bridge buffer."""
        big = b'{"payload": "' + b"x" * (2 * 1024 * 1024) + b'"}'
        assert client.post("/hook", content=big).status_code == 413

    def test_oversized_chunked_body_rejected(self, client):
        """The post-read check is the half an attacker actually hits: a
        chunked body carries no content-length to precheck. (The body is
        still buffered once before the 413 - acknowledged residual.)"""

        def chunks():
            yield b'{"payload": "'
            yield b"x" * (2 * 1024 * 1024)
            yield b'"}'

        assert client.post("/hook", content=chunks()).status_code == 413

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

    def test_concurrent_deliveries_wake_only_once(self, tmp_path):
        """Two concurrent deliveries for the same session must produce one
        tmux wake - the reservation closes the check-then-act window between
        reading the cooldown and recording the wake."""
        injector, _ = make_tmux_injector(tmp_path)
        release = threading.Event()
        wakes = []

        def slow_tmux(cmd, **kwargs):
            wakes.append(cmd)
            release.wait(timeout=5)

            class Result:
                returncode = 0

            return Result()

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(injector.deliver("target-1", make_event()))
            )
            for _ in range(2)
        ]
        with patch.object(bridge.subprocess, "run", slow_tmux):
            for t in threads:
                t.start()
            # Wait until one thread is parked inside tmux AND the other has
            # already returned - release is still unset, so this is asserted
            # overlap, not a lucky serialization (under the pre-fix code both
            # threads would be parked in tmux here and results stays empty)
            for _ in range(500):
                if wakes and results:
                    break
                time.sleep(0.01)
            overlapped = bool(wakes) and bool(results) and not release.is_set()
            snapshot = list(results)
            release.set()
            for t in threads:
                t.join()

        assert overlapped, f"deliveries never overlapped (wakes={len(wakes)}, results={results})"
        assert snapshot == ["spool-cooldown"]
        assert sorted(results) == ["spool-cooldown", "tmux"]
        assert len(wakes) == 1

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

    def test_expired_last_wake_entries_are_pruned(self, tmp_path):
        """Sessions are ephemeral; entries past the cooldown can never gate
        a wake again and must not accumulate for the daemon's lifetime."""
        injector, _ = make_tmux_injector(tmp_path, sessions=("target-1", "target-2"), cooldown=30.0)

        clock = {"now": 1000.0}
        with patch.object(bridge.time, "monotonic", lambda: clock["now"]):
            with patch.object(bridge.subprocess, "run", tmux_ok):
                assert injector.deliver("target-1", make_event()) == "tmux"
                clock["now"] += 35.0
                assert injector.deliver("target-2", make_event()) == "tmux"

        assert "target-1" not in injector._last_wake
        assert "target-2" in injector._last_wake

    def test_concurrent_spool_appends_do_not_tear(self, config):
        """Concurrent webhook deliveries append under the lock; every line
        must stay valid JSON even when payloads exceed the IO buffer."""
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
        # The cross-process drain lock exists alongside the spool
        assert (config.wake_dir / "t.lock").exists()

    def test_spool_lock_contention_raises_after_deadline(self, config, monkeypatch):
        """The flock acquire is bounded: a stuck drainer holding the lock
        must produce an error (nothing is spooled yet, so the bus retry is
        meaningful) - not park a threadpool worker forever."""
        monkeypatch.setattr(bridge, "SPOOL_LOCK_ATTEMPTS", 3)
        monkeypatch.setattr(bridge, "SPOOL_LOCK_RETRY_SECONDS", 0.01)
        injector = Injector(config)
        lock_file = config.wake_dir / "t.lock"

        with lock_file.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # a stuck drainer
            try:
                with pytest.raises(OSError, match="Could not lock spool"):
                    injector.deliver("t", make_event())
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)

        # Nothing was written while the lock was contended
        assert not (config.wake_dir / "t.jsonl").exists()

    def test_stuck_drainer_does_not_stall_other_sessions(self, config, monkeypatch):
        """Pins the invariant that _spool runs OUTSIDE the global injector
        lock: one session's stuck drainer (holding its flock) must not block
        deliveries for other sessions. Under the pre-fix ordering, session
        b's delivery would block behind a's full lock deadline."""
        monkeypatch.setattr(bridge, "SPOOL_LOCK_ATTEMPTS", 100)
        monkeypatch.setattr(bridge, "SPOOL_LOCK_RETRY_SECONDS", 0.05)  # 5s deadline
        injector = Injector(config)
        a_lock = config.wake_dir / "a.lock"
        a_result: list = []

        with a_lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # a's drainer is stuck
            blocked = threading.Thread(
                target=lambda: a_result.append(injector.deliver("a", make_event()))
            )
            try:
                blocked.start()
                start = time.monotonic()
                assert injector.deliver("b", make_event()) == "spool"
                elapsed = time.monotonic() - start
                # b must complete while a is still parked on its flock -
                # well under a's 5s deadline
                assert elapsed < 1.0
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
        blocked.join(timeout=10.0)
        assert not blocked.is_alive()
        assert a_result == ["spool"]  # a completes once its lock frees

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

    def test_tmux_oserror_falls_back_to_spool(self, tmp_path):
        """A non-executable tmux binary (PermissionError) must degrade to
        spool, not escape as a 500 that makes the bus re-POST an event which
        is already durably spooled (duplicate lines)."""
        injector, _ = make_tmux_injector(tmp_path)

        def tmux_perm(cmd, **kwargs):
            raise PermissionError("tmux not executable")

        with patch.object(bridge.subprocess, "run", tmux_perm):
            assert injector.deliver("target-1", make_event()) == "spool"

    def test_malformed_panes_json_degrades_to_spool(self, tmp_path):
        """panes.json is maintained by an external component - every failure
        shape must read as 'unmapped', never escape as a 500."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"

        for content in (b'["not", "an", "object"]', b'"bare string"', b"null", b"\xff\xfe{}"):
            panes.write_bytes(content)
            with patch.object(bridge.subprocess, "run") as mock_run:
                assert injector.deliver("target-1", make_event()) == "spool"
            mock_run.assert_not_called()

    def test_unreadable_panes_json_degrades_to_spool(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        (config.wake_dir / "panes.json").mkdir()  # IsADirectoryError on read

        with patch.object(bridge.subprocess, "run") as mock_run:
            assert injector.deliver("target-1", make_event()) == "spool"
        mock_run.assert_not_called()


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
        # v1 only acts on DMs, so the bus drops broadcast traffic server-side
        assert register["arguments"]["channel"] == "session:"
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

    def test_empty_secret_env_is_normalized_to_none(self, monkeypatch):
        """An accidentally empty secret must not split registration (skips
        the secret -> unsigned payloads) from verification (demands
        signatures) - the combination 401s every delivery silently. Exercises
        the real config construction, so removing the normalization from
        config_from_args fails this test."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.secret is None

    def test_secret_env_is_adopted(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.secret == "s3cret"

    def test_registration_retries_until_bus_is_up(self, tmp_path):
        """call_tool exits the process on connection errors, and at boot the
        bus is often not up yet - the daemon must retry, not die."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        attempts = []

        def flaky_register(cfg):
            attempts.append(1)
            if len(attempts) < 3:
                raise SystemExit(1)
            return 42

        state: dict = {}
        with patch.object(bridge, "register_with_bus", flaky_register):
            bridge.register_with_retry(config, state, threading.Event(), initial_delay=0.01)

        assert state["webhook_id"] == 42
        assert len(attempts) == 3

    def test_registration_without_id_is_retryable(self, tmp_path):
        """A bus that answers but returns no webhook_id must be a retryable
        failure - not a silent success that leaves the bridge receiving
        nothing for the daemon's lifetime."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", bus_url="http://bus/mcp")

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            if tool_name == "list_webhooks":
                return []
            return {}  # answered, but no webhook_id

        with patch.object(bridge, "call_tool", fake_call_tool):
            with pytest.raises(SystemExit, match="no webhook_id"):
                bridge.register_with_bus(config)

    def test_non_dict_webhook_rows_are_skipped(self, tmp_path):
        """A malformed list_webhooks element must not AttributeError out of
        the dedupe loop - that would kill the registration thread with no
        retry, unlike every other registration failure."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", bus_url="http://bus/mcp")

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            if tool_name == "list_webhooks":
                return ["garbage", None, 42]
            return {"webhook_id": 5}

        with patch.object(bridge, "call_tool", fake_call_tool):
            assert bridge.register_with_bus(config) == 5

    def test_non_list_webhook_listing_is_retryable(self, tmp_path):
        """A bus that answers but can't list webhooks must not skip the
        stale-URL dedupe and register anyway - that stacks the duplicate
        deliveries the sweep exists to prevent. Retry instead."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", bus_url="http://bus/mcp")

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            if tool_name == "list_webhooks":
                return {}  # answered, but not a list
            return {"webhook_id": 5}

        with patch.object(bridge, "call_tool", fake_call_tool):
            with pytest.raises(SystemExit, match="list_webhooks"):
                bridge.register_with_bus(config)

    def test_registration_retries_on_unexpected_errors(self, tmp_path):
        """Any surprise inside register_with_bus must degrade to
        backoff-and-retry, not a dead thread and a deaf daemon."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        attempts = []

        def flaky_register(cfg):
            attempts.append(1)
            if len(attempts) < 2:
                raise ValueError("unexpected shape")
            return 42

        state: dict = {}
        with patch.object(bridge, "register_with_bus", flaky_register):
            bridge.register_with_retry(config, state, threading.Event(), initial_delay=0.01)

        assert state["webhook_id"] == 42
        assert len(attempts) == 2

    def test_registration_retry_honors_shutdown(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        stop = threading.Event()
        stop.set()

        def never_called(cfg):
            raise AssertionError("should not register after stop")

        with patch.object(bridge, "register_with_bus", never_called):
            bridge.register_with_retry(config, {}, stop, initial_delay=0.01)

    def test_unregister_swallows_bus_unreachable(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")

        def fake_call_tool(*args, **kwargs):
            raise SystemExit(1)

        with patch.object(bridge, "call_tool", fake_call_tool):
            bridge.unregister_from_bus(config, 42)  # must not raise


class TestDaemonLifecycle:
    """The lifespan owns registration: startup must actually fire the thread
    (a wiring failure is silent - the daemon binds, serves /health, and never
    registers), and shutdown must join it before the caller reads the id."""

    def test_lifespan_starts_registration_and_joins_on_shutdown(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        registered = threading.Event()

        def fake_register(cfg):
            registered.set()
            return 42

        state: dict = {}
        stop = threading.Event()
        with patch.object(bridge, "register_with_bus", fake_register):
            app = create_bridge_app(config, registration_state=state, registration_stop=stop)
            with TestClient(app) as client:
                assert registered.wait(timeout=5), "startup never fired registration"
                # /health surfaces the registration outcome - the one signal
                # that separates "working" from "listening but never registered"
                for _ in range(500):
                    if client.get("/health").json().get("registered"):
                        break
                    time.sleep(0.01)
                assert client.get("/health").json()["registered"] is True
        # Shutdown stopped and joined the thread, so the result is visible
        assert state["webhook_id"] == 42
        assert stop.is_set()

    def test_shutdown_waits_for_inflight_registration(self, tmp_path):
        """stop can't interrupt a register call already inside its HTTP POST;
        shutdown must join so the committed webhook_id isn't lost (and can be
        unregistered) on a clean exit."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        entered = threading.Event()

        def slow_register(cfg):
            entered.set()
            time.sleep(0.2)
            return 43

        state: dict = {}
        stop = threading.Event()
        with patch.object(bridge, "register_with_bus", slow_register):
            app = create_bridge_app(config, registration_state=state, registration_stop=stop)
            with TestClient(app):
                assert entered.wait(timeout=5)  # registration is in flight
            # Context exit runs lifespan shutdown while slow_register sleeps
        assert state["webhook_id"] == 43

    def test_app_without_registration_has_inert_lifespan(self, config):
        with TestClient(create_bridge_app(config)) as client:
            payload = client.get("/health").json()
        assert payload["status"] == "ok"
        # No registration state -> no claim either way
        assert "registered" not in payload


class TestEnvValidation:
    """argparse `choices` and `type` only run for command-line values, so
    env-supplied defaults need their own validation - a typo must be a named
    config error, not a silent spool-only bridge or a bare traceback."""

    def test_env_backend_typo_is_rejected(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_BACKEND", "tmuxx")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_BACKEND"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_env_backend_tmux_is_accepted(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_BACKEND", "tmux")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.backend == "tmux"

    def test_env_port_typo_is_a_config_error(self, monkeypatch):
        """Named error at CONFIG time - parser-build must stay clean so
        --help keeps working under a typo'd env var."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_PORT", "eight")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_PORT"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_env_cooldown_typo_is_a_config_error(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_COOLDOWN", "30s")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_COOLDOWN"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))


class TestHookUrlTopology:
    """A loopback hook URL registered on a remote bus makes the bus POST to
    itself - a silent no-op with a green health check - and every machine
    would claim the same URL string, letting one bridge's startup dedupe
    remove another machine's live webhook."""

    def test_remote_bus_with_loopback_hook_is_refused(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "https://bus.tailnet.example/mcp")
        with pytest.raises(SystemExit, match="hook-url"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_remote_bus_with_reachable_hook_is_accepted(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "https://bus.tailnet.example/mcp")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        args = bridge.build_parser().parse_args(
            ["--hook-url", "http://laptop.tailnet.example:8082/hook"]
        )
        config = bridge.config_from_args(args)
        assert bridge.bridge_hook_url(config) == "http://laptop.tailnet.example:8082/hook"

    def test_non_loopback_hook_without_secret_is_refused(self, monkeypatch):
        """Binding beyond localhost with no HMAC secret would let anyone who
        reaches the port append attacker-authored lines to a session spool -
        a hard refusal, matching the loopback-vs-remote guard (the bus
        itself defaults to auth-required)."""
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "https://bus.tailnet.example/mcp")
        args = bridge.build_parser().parse_args(
            ["--hook-url", "http://laptop.tailnet.example:8082/hook"]
        )
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_SECRET"):
            bridge.config_from_args(args)

    def test_local_bus_with_loopback_hook_is_accepted(self):
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert bridge.bridge_hook_url(config).startswith("http://127.0.0.1:")

    def test_loopback_range_bus_is_local(self, monkeypatch):
        """127.0.0.0/8 is all loopback - Debian/Ubuntu resolve the machine's
        hostname to 127.0.1.1, so a bus URL built from `hostname` must not
        read as remote."""
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "http://127.0.1.1:8080/mcp")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert bridge.bridge_hook_url(config).startswith("http://127.0.0.1:")

    def test_schemeless_bus_url_is_refused(self, monkeypatch):
        """A scheme-less URL parses with no hostname and would read as
        loopback, silently skipping the topology guard."""
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "bus.example:8080/mcp")
        with pytest.raises(SystemExit, match="http"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_hostless_bus_url_is_refused(self, monkeypatch):
        """http:///mcp has a valid scheme but hostname None, which would
        read as a *local* bus, skip the topology guard, and leave the
        registration thread retrying an unroutable URL forever."""
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "http:///mcp")
        with pytest.raises(SystemExit, match="bus URL"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_schemeless_hook_url_is_refused(self, monkeypatch):
        """The hook URL is what BOTH topology guards read - a scheme-less
        value parses to hostname None, reads as loopback, skips the guards,
        and registers a URL the bus can never POST to (silently inert with
        /health reporting registered)."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "laptop.example:8082/hook")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_HOOK_URL"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_hostless_hook_url_is_refused(self, monkeypatch):
        """A scheme with no host (http:///hook) also parses to hostname
        None - same silent-inertness escape, different malformation."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http:///hook")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_HOOK_URL"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_malformed_hook_port_is_refused(self, monkeypatch):
        """A hook URL port outside 0-65535 raises ValueError out of
        SplitResult.port - it must surface as a named config error, not a
        bare traceback. Secret set so the exposure refusal doesn't fire
        first (box.example is a non-loopback hook host)."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://box.example:99999/hook")
        with pytest.raises(SystemExit, match="bad port"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_out_of_range_port_is_refused(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_PORT", "99999")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_PORT"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_bad_numeric_env_does_not_break_help(self, monkeypatch, capsys):
        """A typo'd numeric env var must fail at CONFIG time with a named
        error - not at parser-build time, which would break --help, the
        first thing an operator reaches for when the daemon won't start."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_PORT", "eighty")
        with pytest.raises(SystemExit) as exc:
            bridge.build_parser().parse_args(["--help"])
        assert exc.value.code == 0  # help printed cleanly
        assert "--port" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_PORT"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_negative_cooldown_is_refused(self, monkeypatch):
        """A negative cooldown casts cleanly but silently disables the
        cooldown entirely (now - ts < -5 is never true)."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_COOLDOWN", "-5")
        with pytest.raises(SystemExit, match="COOLDOWN"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_empty_wake_dir_env_falls_back_to_default(self, monkeypatch):
        """An empty env var would make Path('') == cwd - the bridge would
        chmod and spool into whatever directory launched it, while the
        drain hook reads the real wake dir and finds nothing."""
        monkeypatch.setenv("AGENT_EVENT_BUS_WAKE_DIR", "")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.wake_dir == bridge.DEFAULT_WAKE_DIR

    def test_relative_wake_dir_is_refused(self):
        """A daemon's cwd is whatever its supervisor hands it."""
        args = bridge.build_parser().parse_args(["--wake-dir", "wake"])
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_WAKE_DIR"):
            bridge.config_from_args(args)

    def test_hook_url_env_is_adopted(self, monkeypatch):
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://box.local:9090/hook")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert bridge.bridge_hook_url(config) == "http://box.local:9090/hook"

    def test_hook_port_mismatch_warns(self, monkeypatch, caplog):
        """The bus POSTs to the hook URL's port while the listener binds
        --port; a mismatch is legitimate behind a proxy but must be named."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://box.local:9090/hook")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert any("9090" in r.message and "8082" in r.message for r in caplog.records)

    def test_registration_advertises_hook_url_override(self, tmp_path):
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            bus_url="https://bus.tailnet.example/mcp",
            hook_url="http://laptop.tailnet.example:8082/hook",
        )
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            calls.append({"tool": tool_name, "arguments": arguments})
            if tool_name == "list_webhooks":
                return []
            return {"webhook_id": 44}

        with patch.object(bridge, "call_tool", fake_call_tool):
            assert bridge.register_with_bus(config) == 44

        register = next(c for c in calls if c["tool"] == "register_webhook")
        assert register["arguments"]["url"] == "http://laptop.tailnet.example:8082/hook"


class TestBindHost:
    """The bind decision gates whether the wake-injection endpoint is
    reachable off-box - both directions must be pinned, not just the config
    guard that shares its predicate."""

    def test_loopback_hook_binds_localhost(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        assert bridge.bind_host(config) == "127.0.0.1"

    def test_reachable_hook_binds_wide(self, tmp_path):
        config = BridgeConfig(
            wake_dir=tmp_path / "wake", hook_url="http://box.example:8082/hook", secret="s3cret"
        )
        assert bridge.bind_host(config) == "0.0.0.0"

    def test_bind_override_pins_interface(self, tmp_path):
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://box.example:8082/hook",
            secret="s3cret",
            bind="100.100.1.2",
        )
        assert bridge.bind_host(config) == "100.100.1.2"

    def test_non_loopback_bind_without_secret_is_refused(self, monkeypatch):
        """--bind decides exposure before the hook URL does: a wide bind
        with the default local bus must not open an unsigned /hook off-box
        (the round-6 refusal, reachable through the round-7 flag)."""
        for bind in ("0.0.0.0", "100.100.1.2"):
            args = bridge.build_parser().parse_args(["--bind", bind])
            with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_SECRET"):
                bridge.config_from_args(args)

    def test_non_loopback_bind_with_secret_is_accepted(self, monkeypatch, caplog):
        """Accepted - but this is also the fourth quadrant (wide bind,
        loopback hook URL): the bus POSTs to 127.0.0.1 where nothing
        listens, so the warning must actually fire."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            config = bridge.config_from_args(
                bridge.build_parser().parse_args(["--bind", "0.0.0.0"])
            )
        assert bridge.bind_host(config) == "0.0.0.0"
        assert any("0.0.0.0" in r.message and "127.0.0.1" in r.message for r in caplog.records)

    def test_loopback_bind_is_accepted_without_secret(self):
        config = bridge.config_from_args(bridge.build_parser().parse_args(["--bind", "127.0.0.1"]))
        assert bridge.bind_host(config) == "127.0.0.1"

    def test_invalid_bind_is_refused(self):
        args = bridge.build_parser().parse_args(["--bind", "localhost:8082"])
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_BIND"):
            bridge.config_from_args(args)

    def test_loopback_bind_under_reachable_hook_warns(self, monkeypatch, caplog):
        """The inverse mismatch is silent inertness (the bus's POSTs are
        refused at the TCP level) - legitimate behind a same-box TLS
        terminator, so a named warning, not a refusal."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        args = bridge.build_parser().parse_args(
            ["--bind", "127.0.0.1", "--hook-url", "http://laptop.tailnet.example:8082/hook"]
        )
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(args)
        assert any(
            "laptop.tailnet.example" in r.message and "127.0.0.1" in r.message
            for r in caplog.records
        )

    def test_main_passes_bind_through_and_unregisters(self, monkeypatch, tmp_path):
        """End-to-end main(): the default local config binds loopback, the
        lifespan registers, and the finally unregisters the committed id."""
        import sys

        import uvicorn

        monkeypatch.setattr(
            sys, "argv", ["agent-event-bus-bridge", "--wake-dir", str(tmp_path / "wake")]
        )
        seen = {}
        unregistered = []

        def fake_run(app, host=None, port=None):
            seen["host"] = host
            seen["port"] = port
            # Run the lifespan the way uvicorn would, so registration fires
            with TestClient(app):
                pass

        with patch.object(uvicorn, "run", fake_run):
            with patch.object(bridge, "register_with_bus", lambda cfg: 99):
                with patch.object(
                    bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
                ):
                    bridge.main()

        assert seen["host"] == "127.0.0.1"
        assert seen["port"] == bridge.DEFAULT_BRIDGE_PORT
        assert unregistered == [99]
