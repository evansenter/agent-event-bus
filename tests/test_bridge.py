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
from agent_event_bus.server import SIGNATURE_HEADER, _compute_signature


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


@pytest.fixture(autouse=True)
def reset_unsafe_warn_state():
    """The unsafe-id rate limit is deliberately process-wide module state
    (resolve_target_session runs before any Injector exists), so any test
    driving that arm leaves a real monotonic reading behind - a later test
    asserting the WARNING would then pass or fail on 60s of wall clock and
    run ordering, with no hint why. Reset it around every test."""
    bridge._unsafe_warn_state["last"] = -float("inf")
    yield
    bridge._unsafe_warn_state["last"] = -float("inf")


@pytest.fixture(autouse=True)
def restore_bridge_logger_level():
    """main() pins a level on the module logger (the DEV_MODE switch), so
    any test driving main() end to end would otherwise leak that level into
    later tests' caplog expectations - records silently dropped with no
    hint why. Snapshot and restore around every test."""
    level = bridge.logger.level
    yield
    bridge.logger.setLevel(level)


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
            "/hook", content=body, headers={SIGNATURE_HEADER: sign(body, "s3cret")}
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

    def test_unsafe_id_warning_is_rate_limited(self, caplog, monkeypatch):
        """The rejection arm is publisher-drivable and the channel string is
        publisher-CHOSEN, so a bound keyed on the value (the old dedup set)
        still let a publisher who varies the id force one WARNING per event.
        The bound must be content-independent: one warning per interval,
        repeats at debug, and a persistent condition re-warns next interval
        instead of going dark forever."""
        import logging

        monkeypatch.setitem(bridge._unsafe_warn_state, "last", -float("inf"))
        clock = {"now": 1000.0}
        monkeypatch.setattr(bridge, "_now", lambda: clock["now"])

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            # Varying strings within one interval: one warning, rest debug -
            # the shape the per-channel set could not bound
            assert resolve_target_session(make_event(channel="session:x/1")) is None
            assert resolve_target_session(make_event(channel="session:x/2")) is None
            assert resolve_target_session(make_event(channel="session:x/3")) is None
            assert len(warnings()) == 1
            assert "session:x/1" in warnings()[0].message
            # Past the interval the arm warns again
            clock["now"] += bridge._UNSAFE_WARN_INTERVAL_SECONDS
            assert resolve_target_session(make_event(channel="session:x/4")) is None
        assert len(warnings()) == 2

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


class TestBusTimingContract:
    def test_internal_deadlines_leave_headroom_under_bus_webhook_timeout(self):
        """The bus's per-attempt clock (WEBHOOK_TIMEOUT) starts before the
        bridge's, so a bridge-side bound at or above it guarantees the bus
        times out and retries instead of ever seeing the bounded response
        (500 for a stuck spool lock, 200 for a failed tmux wake). Pin the
        headroom - including the sum, since one request can hit lock
        contention AND a slow tmux."""
        from agent_event_bus.server import WEBHOOK_TIMEOUT

        spool_deadline = bridge.SPOOL_LOCK_ATTEMPTS * bridge.SPOOL_LOCK_RETRY_SECONDS
        assert spool_deadline < WEBHOOK_TIMEOUT
        assert bridge.TMUX_TIMEOUT < WEBHOOK_TIMEOUT
        assert spool_deadline + bridge.TMUX_TIMEOUT < WEBHOOK_TIMEOUT


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

    def test_trailing_slash_hook_is_404_not_redirect(self, client):
        """redirect_slashes must stay off: the bus's httpx client doesn't
        follow redirects and counts any status under 400 as delivered, so a
        307 for /hook/ would make a trailing-slash hook URL read as a
        healthy webhook while the bridge processes nothing. A 404 is
        retried and logged bus-side - loud instead of silently 'delivered'."""
        resp = client.post(
            "/hook/",
            content=json.dumps(make_event()).encode(),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_below_actionable_ignored(self, client, config):
        for level in ("lifecycle", "info"):
            resp = client.post("/hook", content=json.dumps(make_event(signal_level=level)).encode())
            assert resp.json() == {"status": "ignored", "reason": "below actionable"}
        assert not (config.wake_dir / "target-1.jsonl").exists()

    def test_missing_signal_level_filtered_loudly(self, client, config, caplog):
        """A bus predating derived levels (#129) sends no signal_level at
        all - every delivery lands on this arm forever, so the version skew
        must be visible at INFO rather than silently filtering 100% of
        deliveries. The INFO line IS the point of the branch; pin it like
        the spool breadcrumb."""
        import logging

        with caplog.at_level(logging.INFO, logger="agent-event-bus-bridge"):
            resp = client.post("/hook", content=json.dumps(make_event(signal_level=None)).encode())
        assert resp.json() == {"status": "ignored", "reason": "below actionable"}
        assert any("no signal_level" in r.message for r in caplog.records)
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
        # json.loads(bytes) DECODES before parsing: invalid UTF-8 raises
        # UnicodeDecodeError (a ValueError, not JSONDecodeError) and must
        # be a 400 too, not a 500 with bus retries behind it
        assert client.post("/hook", content=b"\x80abc").status_code == 400

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
        """The streamed count is the half an attacker actually hits: a
        chunked body carries no content-length to precheck, and the bound
        must hold WHILE reading - the 413 fires mid-stream, not after an
        unbounded buffer."""

        def chunks():
            yield b'{"payload": "'
            yield b"x" * (2 * 1024 * 1024)
            yield b'"}'

        assert client.post("/hook", content=chunks()).status_code == 413

    def test_actionable_dm_delivered_to_spool(self, client, config, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="agent-event-bus-bridge"):
            resp = client.post("/hook", content=json.dumps(make_event()).encode())

        assert resp.json()["status"] == "delivered"
        assert resp.json()["action"] == "spool"
        lines = (config.wake_dir / "target-1.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["payload"] == "need a review"
        # The happy-path breadcrumb is an operability guarantee - the spool
        # backend's terminal must show more than the registration line, so
        # pin the INFO line carrying the event id like the warnings around it
        assert any("Spooled event 1" in r.message for r in caplog.records)

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

    def test_second_wake_within_cooldown_spools_only(self, tmp_path, caplog):
        import logging

        injector, config = make_tmux_injector(tmp_path)

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(bridge.subprocess, "run", tmux_ok):
                assert injector.deliver("target-1", make_event()) == "tmux"
                assert injector.deliver("target-1", make_event()) == "spool-cooldown"

        # The one deliberately-suppressed arm still names itself under
        # DEV_MODE - the only case where the bridge chose not to wake a
        # session it could have
        assert any("Cooldown active" in r.message for r in caplog.records)
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
        with patch.object(bridge, "_now", lambda: clock["now"]):
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
            # Unbounded on purpose: the poll loop below is the failure
            # detector, and a timeout here would let a slow second thread
            # turn the snapshot assertions into a confusing wrong-thread
            # failure. release.set() runs unconditionally before the joins.
            release.wait()

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
            assert injector.deliver("target-1", make_event()) == "spool-tmux-failed"

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
        with patch.object(bridge, "_now", lambda: clock["now"]):
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

    def test_wake_dir_removed_at_runtime_self_heals(self, config):
        """Hand-clearing the wake dir is the documented interim workflow
        while pruning is a follow-up - a delivery after an rm -rf must
        recreate it (privately) and spool, not 500 on every event until
        someone restarts the bridge."""
        import shutil

        injector = Injector(config)
        injector.deliver("target-1", make_event())
        shutil.rmtree(config.wake_dir)
        assert injector.deliver("target-1", make_event()) == "spool"
        assert (config.wake_dir / "target-1.jsonl").exists()
        assert (config.wake_dir.stat().st_mode & 0o777) == 0o700

    def test_wake_dir_is_private(self, config):
        Injector(config).deliver("target-1", make_event())
        assert (config.wake_dir.stat().st_mode & 0o777) == 0o700

    def test_unpreparable_wake_dir_is_a_named_config_error(self, tmp_path):
        """mkdir/chmod are the one startup filesystem precondition - they
        must surface as a named SystemExit like every other config input,
        not a bare traceback. (Parent is a FILE, not an unwritable dir:
        NotADirectoryError fires for root too, unlike PermissionError.)"""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        config = BridgeConfig(wake_dir=blocker / "wake")
        with pytest.raises(SystemExit, match="--wake-dir / AGENT_EVENT_BUS_WAKE_DIR"):
            Injector(config)


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
        # Full argv, not a prefix: WAKE_PROMPT as ONE argument without -l,
        # then Enter - dropping the Enter (or switching to -l without a
        # separate Enter send) would type the prompt and never submit it,
        # a wake that wakes nobody while everything else stays green
        assert commands[0] == ["tmux", "send-keys", "-t", "%7", bridge.WAKE_PROMPT, "Enter"]
        # The event is also spooled (durable path is unconditional)
        assert (config.wake_dir / "target-1.jsonl").exists()

    def test_unmapped_session_falls_back_to_spool(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)

        with patch.object(bridge.subprocess, "run") as mock_run:
            action = injector.deliver("target-1", make_event())

        # Distinct from "spool" AND from "spool-tmux-failed": unmapped is
        # the normal outcome for a foreign-machine session (webhooks have no
        # machine scoping), so it must not read as a broken tmux setup. The
        # distinct return value is what a direct /hook caller sees; the
        # debug line under DEV_MODE is what an operator sees.
        assert action == "spool-unmapped"
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

        assert action == "spool-tmux-failed"

    def test_tmux_oserror_falls_back_to_spool(self, tmp_path):
        """A non-executable tmux binary (PermissionError) must degrade to
        spool, not escape as a 500 that makes the bus re-POST an event which
        is already durably spooled (duplicate lines)."""
        injector, _ = make_tmux_injector(tmp_path)

        def tmux_perm(cmd, **kwargs):
            raise PermissionError("tmux not executable")

        with patch.object(bridge.subprocess, "run", tmux_perm):
            assert injector.deliver("target-1", make_event()) == "spool-tmux-failed"

    def test_persistent_tmux_failure_warns_once(self, tmp_path, caplog):
        """A missing or broken tmux binary fails every wake with the same
        exception type forever - the last unbounded per-DM WARNING for a
        stuck local condition. First sighting warns, same-type repeats are
        debug, a different failure type warns fresh, and a successful wake
        re-arms the guard."""
        import logging

        injector, _ = make_tmux_injector(tmp_path, cooldown=0.0)

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(
                bridge.subprocess, "run", side_effect=PermissionError("tmux not executable")
            ):
                injector.deliver("target-1", make_event())
                injector.deliver("target-1", make_event())
            assert len(warnings()) == 1  # same-type repeat was debug
            with patch.object(
                bridge.subprocess,
                "run",
                side_effect=bridge.subprocess.CalledProcessError(1, ["tmux"]),
            ):
                injector.deliver("target-1", make_event())  # new type - warns
            assert len(warnings()) == 2
            with patch.object(bridge.subprocess, "run", tmux_ok):
                assert injector.deliver("target-1", make_event()) == "tmux"  # re-arms
            with patch.object(bridge.subprocess, "run", side_effect=PermissionError("again")):
                injector.deliver("target-1", make_event())
        assert len(warnings()) == 3

    def test_wake_failure_bound_is_per_session(self, tmp_path, caplog):
        """The round-38 panes lesson applies here too: the bound must key
        per (session, exception type). A second broken session must WARN
        rather than be demoted behind the first's key, and a healthy
        session's success must not re-arm a broken one into warn-per-DM."""
        import logging

        injector, _ = make_tmux_injector(
            tmp_path, sessions=("target-1", "target-2", "target-3"), cooldown=0.0
        )

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        def broken_panes(cmd, **kwargs):
            if cmd[3] in {"%0", "%1"}:  # target-1, target-2 have stale panes
                raise bridge.subprocess.CalledProcessError(1, cmd)

            class Result:
                returncode = 0

            return Result()

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(bridge.subprocess, "run", broken_panes):
                injector.deliver("target-1", make_event())  # warns
                injector.deliver("target-2", make_event())  # different session - warns too
                assert len(warnings()) == 2
                assert injector.deliver("target-3", make_event()) == "tmux"  # healthy
                injector.deliver("target-1", make_event())  # still bounded - debug
        assert len(warnings()) == 2

    def test_malformed_panes_json_degrades_to_spool(self, tmp_path):
        """panes.json is maintained by an external component - every failure
        shape must read as 'unmapped', never escape as a 500."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"

        for content in (b'["not", "an", "object"]', b'"bare string"', b"null", b"\xff\xfe{}"):
            panes.write_bytes(content)
            with patch.object(bridge.subprocess, "run") as mock_run:
                assert injector.deliver("target-1", make_event()) == "spool-unmapped"
            mock_run.assert_not_called()

    def test_persistent_panes_failure_warns_once(self, tmp_path, caplog):
        """A stuck-broken panes.json must not emit one WARNING per DM - and
        the guard must key on the REASON, not the message: a torn
        non-atomic write yields a different parse position on every read,
        so message-keyed dedupe would warn per delivery (the value-keyed
        defect in local-file form). First sighting warns, repeats are
        debug, a healthy read re-arms."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            # Same reason, VARYING exception text (the torn-writer shape:
            # a different parse position per read) - still one warning
            panes.write_bytes(b"{broken")
            injector.deliver("target-1", make_event())
            panes.write_bytes(b"[1, 2,")
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 1  # the varying repeat was debug
            # Re-break with the SAME reason after each re-arm path, so the
            # re-arm lines are load-bearing: a different reason would warn
            # regardless and mask a deleted re-arm
            panes.write_text("{}")  # healthy read re-arms the guard
            injector.deliver("target-1", make_event())
            panes.write_bytes(b"[1,")  # same reason again - warns only if re-armed
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 2
            panes.unlink()  # normal unmapped state re-arms too
            injector.deliver("target-1", make_event())
            panes.write_bytes(b"{again")  # same reason - warns only if re-armed
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 3
            # Escalation across classes: a parse failure turning into a
            # persistent I/O failure is a DIFFERENT reason and must warn,
            # not be demoted as a repeat - there may never be a healthy
            # read to re-arm once the file is a directory
            panes.unlink()
            panes.mkdir()  # IsADirectoryError on read
            injector.deliver("target-1", make_event())
        assert len(warnings()) == 4

    def test_bad_pane_value_warns_and_degrades_to_spool(self, tmp_path, caplog):
        """A present-but-wrong-typed pane value (0 instead of "%0", "",
        null) is a misconfiguration whose repair is nothing like "the
        mapping is absent" - it must warn (once, via the reason-keyed
        bound) instead of folding into the unmapped debug line, and still
        degrade to spool."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            # null FIRST: .get() can't tell null from absent, so ordering
            # null last would let a .get()-based implementation pass on the
            # other two values' warnings. null is also the likeliest real
            # misconfiguration (TMUX_PANE unset -> panes[sid] = null).
            for bad in (None, 0, ""):
                panes.write_text(json.dumps({"target-1": bad}))
                with patch.object(bridge.subprocess, "run") as mock_run:
                    assert injector.deliver("target-1", make_event()) == "spool-unmapped"
                mock_run.assert_not_called()
            assert len(warnings()) == 1  # same reason across values - repeats debug
            # The repr names the entry to fix - null warned first
            assert "(None)" in warnings()[0].message
            # Per-shape, not aggregate: the two repeats were DEMOTED (seen,
            # logged at debug, carrying their own reprs), not skipped - an
            # aggregate count can't tell those apart
            demoted = [
                r
                for r in caplog.records
                if r.levelno == logging.DEBUG and "not a pane id" in r.message
            ]
            assert len(demoted) == 2
            assert any("(0)" in r.message for r in demoted)
            assert any("('')" in r.message for r in demoted)
            # A genuinely ABSENT key is the healthy shape and re-arms; a bad
            # value must not - so bad -> absent -> bad warns twice
            panes.write_text(json.dumps({"other-session": "%1"}))
            injector.deliver("target-1", make_event())
            panes.write_text(json.dumps({"target-1": None}))
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 2
            # A missing FILE is this session's absent read too: a full
            # clean-state cycle (delete, recreate with a bad entry) must
            # re-arm the warning - the repair-didn't-take signal
            panes.unlink()
            injector.deliver("target-1", make_event())
            panes.write_text(json.dumps({"target-1": 0}))
            injector.deliver("target-1", make_event())
        assert len(warnings()) == 3
        assert "not a pane id" in warnings()[0].message

    def test_bad_pane_value_bound_survives_interleaved_sessions(self, tmp_path, caplog):
        """The guard must be keyed per CONDITION, not per last delivery: on
        a multi-machine bus the unmapped arm is the documented NORMAL
        outcome, so a bad entry interleaved with absent (or healthy)
        sessions is the steady state - a single re-arm slot would oscillate
        into one WARNING per DM."""
        import logging

        # cooldown 0 so every target-2 delivery attempts (and succeeds at)
        # a real wake - the healthy-entry read is the arm under test
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux", cooldown_seconds=0.0)
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"
        panes.write_text(json.dumps({"target-1": 0, "target-2": "%5"}))

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(bridge.subprocess, "run", tmux_ok):
                for _ in range(3):
                    injector.deliver("target-1", make_event())  # bad entry
                    injector.deliver("other-session", make_event())  # absent - normal
                    assert injector.deliver("target-2", make_event()) == "tmux"  # healthy
        assert len(warnings()) == 1  # neither absent nor healthy reads re-armed it

    def test_unreadable_panes_json_degrades_to_spool(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        (config.wake_dir / "panes.json").mkdir()  # IsADirectoryError on read

        with patch.object(bridge.subprocess, "run") as mock_run:
            assert injector.deliver("target-1", make_event()) == "spool-unmapped"
        mock_run.assert_not_called()


class TestBusRegistration:
    def test_registers_webhook_with_bridge_url_and_secret(self, tmp_path):
        config = BridgeConfig(
            wake_dir=tmp_path / "wake", port=9999, secret="s3cret", bus_url="http://bus/mcp"
        )
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, debug=False, **kwargs):
            calls.append({"tool": tool_name, "arguments": arguments, "url": url, "debug": debug})
            if tool_name == "list_webhooks":
                return []
            return {"webhook_id": 42}

        with patch.object(bridge, "call_tool", fake_call_tool):
            webhook_id = bridge.register_with_bus(config)

        assert webhook_id == 42
        # debug=True is the whole mechanism behind the named-cause logs:
        # without it call_tool swallows a 401/timeout/bad body into a bare
        # SystemExit(1) with the reason on stderr. Every registration call
        # must carry it, or the daemon silently reverts to logging
        # "SystemExit(1)" for every failure alike.
        assert all(c["debug"] is True for c in calls)
        register = next(c for c in calls if c["tool"] == "register_webhook")
        assert register["arguments"]["url"] == "http://127.0.0.1:9999/hook"
        assert register["arguments"]["secret"] == "s3cret"
        # v1 only acts on DMs, so the bus drops broadcast traffic server-side
        assert register["arguments"]["channel"] == "session:"
        assert register["url"] == "http://bus/mcp"

    def test_session_filter_prefix_match_contract(self, tmp_path):
        """The registered channel="session:" filter only cuts traffic if
        the bus prefix-matches it - pin that through the REAL matcher, the
        same idiom as sign() and the signal-level contract test. If
        get_matching_webhooks were ever tightened to exact match (a bare
        "session:" is not a channel anyone publishes to), the bridge would
        receive nothing while every mocked test here stayed green."""
        from datetime import datetime

        from agent_event_bus.storage import Event as BusEvent
        from agent_event_bus.storage import SQLiteStorage

        storage = SQLiteStorage(str(tmp_path / "contract.db"))
        storage.add_webhook(url="http://127.0.0.1:8082/hook", channel_filter="session:")

        def bus_event(channel):
            return BusEvent(
                id=1,
                event_type="note",
                payload="hi",
                session_id="sender-1",
                timestamp=datetime(2026, 8, 8),
                channel=channel,
            )

        assert storage.get_matching_webhooks(bus_event("session:abc"))
        assert not storage.get_matching_webhooks(bus_event("repo:foo"))
        assert not storage.get_matching_webhooks(bus_event("all"))

    def test_unregister_unexpected_result_warns_not_asserts(self, tmp_path, caplog):
        """Shutdown-side twin of the sweep's result check: best-effort, so a
        bus that answers but doesn't delete is a warning (the next startup
        sweep reclaims the row) - but the log must not claim 'Unregistered'
        for a removal that never happened."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake")

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            return {}  # a JSON-RPC error response falls through to this

        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            with patch.object(bridge, "call_tool", fake_call_tool):
                bridge.unregister_from_bus(config, 42)
        assert any("unexpected result" in r.message for r in caplog.records)

    def test_failed_stale_removal_is_retryable(self, tmp_path):
        """unregister_webhook reports logical failure in-band (success-False
        dict; call_tool raises only on transport errors) - a sweep that
        didn't actually delete the row must not proceed to register and
        stack the duplicate deliveries it exists to prevent. Already-gone
        ("Webhook not found") stays benign: the row being absent is the goal."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", port=9999)

        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            if tool_name == "list_webhooks":
                return [{"webhook_id": 7, "url": "http://127.0.0.1:9999/hook"}]
            if tool_name == "unregister_webhook":
                return {}  # a JSON-RPC error response falls through to this
            return {"webhook_id": 43}

        with patch.object(bridge, "call_tool", fake_call_tool):
            with pytest.raises(SystemExit, match="unexpected result"):
                bridge.register_with_bus(config)

    def test_already_gone_stale_row_is_benign(self, tmp_path):
        def fake_call_tool(tool_name, arguments, url=None, **kwargs):
            if tool_name == "list_webhooks":
                return [{"webhook_id": 7, "url": "http://127.0.0.1:9999/hook"}]
            if tool_name == "unregister_webhook":
                return {"success": False, "error": "Webhook not found", "webhook_id": 7}
            return {"webhook_id": 43}

        config = BridgeConfig(wake_dir=tmp_path / "wake", port=9999)
        with patch.object(bridge, "call_tool", fake_call_tool):
            assert bridge.register_with_bus(config) == 43

    def test_unregister_calls_unregister_webhook_with_id_and_url(self, tmp_path):
        """Pin the wire call unregister_from_bus makes (tool name, id, bus
        URL) - a wrong argument here only ever surfaces as a stale row that
        the next startup sweep silently reclaims, so no other test would
        catch it."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", bus_url="http://bus/mcp")
        calls = []

        def fake_call_tool(tool_name, arguments, url=None, debug=False, **kwargs):
            calls.append({"tool": tool_name, "arguments": arguments, "url": url, "debug": debug})
            return {"success": True}

        with patch.object(bridge, "call_tool", fake_call_tool):
            bridge.unregister_from_bus(config, 42)

        assert calls == [
            {
                "tool": "unregister_webhook",
                "arguments": {"webhook_id": 42},
                "url": "http://bus/mcp",
                "debug": True,
            }
        ]

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
            if tool_name == "unregister_webhook":
                return {"success": True, "webhook_id": arguments["webhook_id"]}
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

    def test_registration_retries_until_bus_is_up(self, tmp_path, caplog):
        """call_tool exits the process on connection errors, and at boot the
        bus is often not up yet - the daemon must retry, not die. The bare
        SystemExit(1) must render as the connection error it provably is
        (debug=True re-raises everything else), not as "SystemExit(1)"."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake")
        attempts = []

        def flaky_register(cfg):
            attempts.append(1)
            if len(attempts) < 3:
                raise SystemExit(1)
            return 42

        state: dict = {}
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            with patch.object(bridge, "register_with_bus", flaky_register):
                bridge.register_with_retry(config, state, threading.Event(), initial_delay=0.01)

        assert state["webhook_id"] == 42
        assert len(attempts) == 3
        assert any("bus unreachable (connection error)" in r.message for r in caplog.records)

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

    def test_registration_retries_on_unexpected_errors(self, tmp_path, caplog):
        """Any surprise inside register_with_bus must degrade to
        backoff-and-retry, not a dead thread and a deaf daemon - and the
        log must carry the real cause (the repr arm), not misrender it as
        a connection error."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake")
        attempts = []

        def flaky_register(cfg):
            attempts.append(1)
            if len(attempts) < 2:
                raise ValueError("unexpected shape")
            return 42

        state: dict = {}
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            with patch.object(bridge, "register_with_bus", flaky_register):
                bridge.register_with_retry(config, state, threading.Event(), initial_delay=0.01)

        assert state["webhook_id"] == 42
        assert len(attempts) == 2
        assert any(
            "ValueError" in r.message and "unexpected shape" in r.message for r in caplog.records
        )
        assert not any("bus unreachable" in r.message for r in caplog.records)

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

    def test_unregister_logs_real_cause_for_non_connection_failures(self, tmp_path, caplog):
        """The except-Exception arm: with debug=True a 401/timeout/bad body
        re-raises out of call_tool - shutdown must stay best-effort AND the
        one log line an operator has when a row leaks must carry the real
        cause, not the connection-error misdiagnosis."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake")

        def raising_call_tool(*args, **kwargs):
            raise RuntimeError("401 Unauthorized")

        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            with patch.object(bridge, "call_tool", raising_call_tool):
                bridge.unregister_from_bus(config, 42)  # must not raise
        assert any("401" in r.message and "#42" in r.message for r in caplog.records)
        assert not any("bus unreachable" in r.message for r in caplog.records)

    def test_call_tool_debug_contract(self):
        """A bare SystemExit(1) meaning 'connection error' is a cross-module
        contract with cli.call_tool: its ConnectionError arm exits WITHOUT
        consulting debug, while the trailing except honours debug=True and
        re-raises. Pin both halves against the real call_tool - the same
        idiom as sign() and the signal-level and session-filter contract
        tests - since either half moving would turn the named-cause log
        lines into confident misdiagnoses with every mocked test green."""
        import requests

        from agent_event_bus import cli

        with patch.object(
            cli.requests, "post", side_effect=requests.exceptions.ConnectionError("refused")
        ):
            with pytest.raises(SystemExit) as exc:
                cli.call_tool("list_webhooks", {}, url="http://127.0.0.1:1/mcp", debug=True)
        assert exc.value.code == 1

        with patch.object(
            cli.requests, "post", side_effect=requests.exceptions.HTTPError("401 Client Error")
        ):
            with pytest.raises(requests.exceptions.HTTPError, match="401"):
                cli.call_tool("list_webhooks", {}, url="http://127.0.0.1:1/mcp", debug=True)


class TestDaemonLifecycle:
    """The lifespan owns registration end to end: startup must actually fire
    the thread (a wiring failure is silent - the daemon binds, serves /health,
    and never registers), and shutdown must join it, then unregister the
    committed id itself and pop it - main()'s finally is belt-and-braces
    only, for exits that skip the lifespan."""

    def test_lifespan_starts_registration_and_joins_on_shutdown(self, tmp_path):
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        registered = threading.Event()
        unregistered: list = []

        def fake_register(cfg):
            registered.set()
            return 42

        state: dict = {}
        stop = threading.Event()
        with patch.object(bridge, "register_with_bus", fake_register):
            with patch.object(
                bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
            ):
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
        # Shutdown stopped and joined the thread, then unregistered the
        # committed id and popped it - the app owns the row end to end, and
        # a main()-style belt-and-braces finally finds nothing to repeat
        assert unregistered == [42]
        assert state.get("webhook_id") is None
        assert stop.is_set()

    def test_cancelled_shutdown_still_stops_and_joins(self, tmp_path):
        """TestClient's context exit is always a CLEAN shutdown, so the two
        subtlest lifespan lines - the try/finally around the yield and the
        shielded join - are invisible to the tests above. Drive the lifespan
        directly and tear it down under an active cancellation: without the
        try/finally the cleanup never runs at all; without the shield,
        anyio.to_thread.run_sync raises at its own cancellation checkpoint
        and the join is skipped, so the post-stop commit below is never
        observed and the webhook row leaks."""
        import anyio

        config = BridgeConfig(wake_dir=tmp_path / "wake")

        def slow_register(cfg):
            time.sleep(0.2)  # the commit lands only if shutdown really joins
            return 44

        state: dict = {}
        stop = threading.Event()
        unregistered: list = []
        with patch.object(bridge, "register_with_bus", slow_register):
            with patch.object(
                bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
            ):
                app = create_bridge_app(config, registration_state=state, registration_stop=stop)

                async def scenario():
                    with anyio.CancelScope() as scope:
                        async with app.router.lifespan_context(app):
                            scope.cancel()
                        # exiting the lifespan now tears down under an active
                        # cancellation - the path uvicorn's forced shutdown takes

                anyio.run(scenario)

        assert stop.is_set()
        # The unregister call observing 44 is the proof the join really ran
        # under cancellation - the id is committed only after slow_register's
        # sleep, and the lifespan pops it after handing it off
        assert unregistered == [44]
        assert state.get("webhook_id") is None

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
        unregistered: list = []
        with patch.object(bridge, "register_with_bus", slow_register):
            with patch.object(
                bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
            ):
                app = create_bridge_app(config, registration_state=state, registration_stop=stop)
                with TestClient(app):
                    assert entered.wait(timeout=5)  # registration is in flight
                # Context exit runs lifespan shutdown while slow_register sleeps
        # The join observed the in-flight commit, so the unregister got it
        assert unregistered == [43]

    def test_health_reports_unregistered_while_registration_pending(self, tmp_path):
        """The registered:false arm - the state a supervisor readiness probe
        gates on while register_with_retry backs off against a down bus -
        was the one /health shape without a test. Pins that the field means
        COMMITTED, not merely configured."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        release = threading.Event()

        def blocked_register(cfg):
            release.wait(timeout=10)
            return 77

        state: dict = {}
        stop = threading.Event()
        unregistered: list = []
        with patch.object(bridge, "register_with_bus", blocked_register):
            with patch.object(
                bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
            ):
                app = create_bridge_app(config, registration_state=state, registration_stop=stop)
                with TestClient(app) as client:
                    # Registration is in flight, deterministically not yet
                    # committed (blocked on `release`)
                    assert client.get("/health").json()["registered"] is False
                    release.set()
                    for _ in range(500):
                        if client.get("/health").json()["registered"]:
                            break
                        time.sleep(0.01)
                    assert client.get("/health").json()["registered"] is True
        assert unregistered == [77]

    def test_lifespan_is_reentrant(self, tmp_path):
        """A second lifespan cycle on the same app must register again: the
        first shutdown set() the stop event, and without clearing it on
        startup the second cycle binds, serves /health, and never registers
        - the silent shape the pair check exists to prevent, reached
        through the embedding surface the docstring advertises."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        registrations = []
        unregistered: list = []

        def fake_register(cfg):
            registrations.append(1)
            return 40 + len(registrations)

        state: dict = {}
        stop = threading.Event()
        with patch.object(bridge, "register_with_bus", fake_register):
            with patch.object(
                bridge, "unregister_from_bus", lambda cfg, wid: unregistered.append(wid)
            ):
                app = create_bridge_app(config, registration_state=state, registration_stop=stop)
                with TestClient(app):
                    pass
                with TestClient(app):
                    pass
        assert len(registrations) == 2
        assert unregistered == [41, 42]

    def test_create_bridge_app_validates_hand_built_configs(self, tmp_path):
        """Embedders skip argparse, so the invariants - including the
        exposed-listener secret requirement - must travel with the config:
        an off-box /hook with no authentication is refused at app
        construction, not just at the CLI."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", hook_url="http://box.example:8082/hook")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_SECRET"):
            create_bridge_app(config)

    def test_half_wired_registration_pair_is_rejected(self, config):
        """Passing only one of registration_state/registration_stop would
        bind and serve /health but never register - silently, with
        registered:false forever. A partial pair is unambiguously a
        programming error in an embedding, so it raises."""
        with pytest.raises(ValueError, match="pair"):
            create_bridge_app(config, registration_state={})
        with pytest.raises(ValueError, match="pair"):
            create_bridge_app(config, registration_stop=threading.Event())

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
        # The preflight consults the real PATH; this test is about env
        # parsing, not about tmux being installed on the test box
        monkeypatch.setattr(bridge.shutil, "which", lambda _: "/usr/bin/tmux")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.backend == "tmux"

    def test_tmux_backend_without_binary_is_refused(self, monkeypatch):
        """A tmux backend on a box without tmux would degrade every wake to
        spool-tmux-failed for the daemon's lifetime - one named startup
        error beats discovering it per-DM."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_BACKEND", "tmux")
        monkeypatch.setattr(bridge.shutil, "which", lambda _: None)
        with pytest.raises(SystemExit, match="tmux binary"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_empty_backend_and_bus_url_env_fall_back_to_defaults(self, monkeypatch):
        """The last two env vars without the `or DEFAULT` normalization: an
        accidentally empty export must mean the default, not a refused
        empty string - same accident the secret and wake-dir already
        handle."""
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_BACKEND", "")
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "")
        config = bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert config.backend == "spool"
        assert config.bus_url == bridge.DEFAULT_URL

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

    def test_non_finite_cooldown_is_refused(self, monkeypatch):
        """nan never prunes-compares True (cooldown never engages); inf
        never prunes at all (one wake ever, then silence)."""
        for value in ("nan", "inf"):
            monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_COOLDOWN", value)
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

    def test_hook_url_without_port_warns_on_scheme_default_mismatch(self, monkeypatch, caplog):
        """A missing port is not 'no opinion' - the bus POSTs to the scheme
        default (80/443), so a forgotten :8082 is exactly the mismatch the
        warning names, and skipping it left the likeliest typo silent."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        for url, default in (
            ("http://box.local/hook", "80"),
            ("https://box.local/hook", "443"),
        ):
            caplog.clear()
            monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", url)
            with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
                bridge.config_from_args(bridge.build_parser().parse_args([]))
            assert any(
                f"advertises port {default} " in r.message and "8082" in r.message
                for r in caplog.records
            )

    def test_hook_url_scheme_default_matching_port_is_quiet(self, monkeypatch, caplog):
        """The substituted default must not warn when it matches - kept on
        http so the separate https-no-terminator warning doesn't muddy the
        pure port-substitution case."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://box.local/hook")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args(["--port", "80"]))
        assert not any("advertises port" in r.message for r in caplog.records)

    def test_https_hook_with_agreeing_port_warns_no_terminator(self, monkeypatch, caplog):
        """The listener never speaks TLS, and a terminator fronts one port
        while forwarding to another - so https with the ports AGREEING
        means no terminator exists and every dispatch dies in the
        handshake, with every other topology guard quiet."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        monkeypatch.setenv(
            "AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "https://bridge.tailnet.example:8082/hook"
        )
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert any("TLS terminator" in r.message for r in caplog.records)

    def test_https_hook_with_mismatched_port_no_tls_warning(self, monkeypatch, caplog):
        """https fronting 443 while forwarding to --port is the legitimate
        terminator shape: the port-mismatch warning names it, and the TLS
        warning must stay quiet rather than double-warn."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "https://box.local/hook")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert not any("TLS terminator" in r.message for r in caplog.records)
        assert any("advertises port 443 " in r.message for r in caplog.records)

    def test_hook_path_mismatch_warns(self, monkeypatch, caplog):
        """POST /hook is the only route served - any other advertised path
        404s every dispatch, so it must be named like the port mismatch."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://box.local:8082/webhook")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))
        assert any("/webhook" in r.message and "/hook" in r.message for r in caplog.records)

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

    def test_wildcard_bind_with_loopback_hook_is_quiet(self, monkeypatch, caplog):
        """A wildcard bind listens on loopback TOO, so the local bus's POST
        to 127.0.0.1 lands exactly as advertised - the fourth-quadrant
        warning must NOT fire (its advice would replace a working
        loopback-only hop with an externally routable one). The secret is
        still required: 0.0.0.0 genuinely exposes the port."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        for bind in ("0.0.0.0", "::"):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
                config = bridge.config_from_args(bridge.build_parser().parse_args(["--bind", bind]))
            assert bridge.bind_host(config) == bind
            assert not any("advertises loopback" in r.message for r in caplog.records)
            # '::' is dual-stack, so the pinned-v6-bind family warning must
            # not fire for it either
            assert not any("IPv6 only" in r.message for r in caplog.records)

    def test_pinned_bind_with_loopback_hook_warns(self, monkeypatch, caplog):
        """The real fourth quadrant: a PINNED non-loopback bind does not
        listen on 127.0.0.1, so the advertised loopback hook URL is refused
        at TCP - the warning must fire naming both values."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args(["--bind", "100.100.1.2"]))
        assert any("100.100.1.2" in r.message and "127.0.0.1" in r.message for r in caplog.records)

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

    def test_ipv6_hook_with_ipv4_bind_warns(self, monkeypatch, caplog):
        """0.0.0.0 binds IPv4 only: an IPv6 hook literal with the derived
        wide bind is refused at TCP while every quadrant warning stays
        quiet (both sides non-loopback, port and path agreeing), so the
        address-family mismatch needs its own name."""
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        args = bridge.build_parser().parse_args(
            ["--hook-url", "http://[fd7a:115c:a1e0::1]:8082/hook"]
        )
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            config = bridge.config_from_args(args)
        assert bridge.bind_host(config) == "0.0.0.0"
        assert any("IPv6" in r.message and "0.0.0.0" in r.message for r in caplog.records)

    def test_ipv4_hook_with_pinned_ipv6_bind_warns(self, caplog):
        """Mirror of the v6-hook/v4-bind arm: --bind ::1 under the default
        v4 loopback hook URL is all-loopback, so neither the exposure nor
        the quadrant guards fire - but the listener binds IPv6 only while
        the bus POSTs to 127.0.0.1, refused at TCP. A PINNED v6 bind must
        warn; '::' stays quiet because dual-stack picks up v4."""
        import logging

        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(bridge.build_parser().parse_args(["--bind", "::1"]))
        assert any("IPv4 address" in r.message and "::1" in r.message for r in caplog.records)

    def test_unbalanced_ipv6_bracket_is_a_named_config_error(self, monkeypatch):
        """urlsplit itself raises ValueError('Invalid IPv6 URL') on an
        unbalanced bracket - one call before every named guard, and it must
        be a named config error like the rest, not a bare traceback."""
        monkeypatch.setenv("AGENT_EVENT_BUS_URL", "http://[::1:8080/mcp")
        with pytest.raises(SystemExit, match="bus URL"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

        monkeypatch.delenv("AGENT_EVENT_BUS_URL")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_HOOK_URL", "http://[fd7a:115c::1:8082/hook")
        with pytest.raises(SystemExit, match="AGENT_EVENT_BUS_BRIDGE_HOOK_URL"):
            bridge.config_from_args(bridge.build_parser().parse_args([]))

    def test_ipv6_hook_with_ipv6_bind_is_quiet(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_SECRET", "s3cret")
        args = bridge.build_parser().parse_args(
            ["--hook-url", "http://[fd7a:115c:a1e0::1]:8082/hook", "--bind", "::"]
        )
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(args)
        assert not any("IPv6" in r.message for r in caplog.records)

    def test_dev_mode_enables_debug_logging(self, monkeypatch, tmp_path):
        """CLAUDE.md advertises DEV_MODE=1 as THE debug switch for this
        package (server.py honors it); without a level path every
        logger.debug in the module - the only per-event visibility into
        why a delivery did nothing - is dead code in the shipped daemon."""
        import logging
        import sys

        import uvicorn

        monkeypatch.setattr(
            sys, "argv", ["agent-event-bus-bridge", "--wake-dir", str(tmp_path / "wake")]
        )

        def run_quiet(app, host=None, port=None):
            return None

        # The autouse restore_bridge_logger_level fixture undoes the level
        # main() pins here, for this and every other main()-driving test
        monkeypatch.delenv("DEV_MODE", raising=False)
        with patch.object(uvicorn, "run", run_quiet):
            bridge.main()
        assert not bridge.logger.isEnabledFor(logging.DEBUG)
        monkeypatch.setenv("DEV_MODE", "1")
        with patch.object(uvicorn, "run", run_quiet):
            bridge.main()
        assert bridge.logger.isEnabledFor(logging.DEBUG)

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
