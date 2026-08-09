"""Tests for the webhook-to-injection bridge (RFC #122 prototype)."""

import fcntl
import json
import os
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
    "AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS",
    "AGENT_EVENT_BUS_WAKE_DIR",
    # Not AGENT_EVENT_BUS_*-prefixed, but main() honours it as THE debug
    # switch and CLAUDE.md tells developers to export it - exactly the
    # people who run this suite. An inherited DEBUG level would flip any
    # test asserting a debug line is absent (or make caplog noisier).
    "DEV_MODE",
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
def reset_reject_warn_state():
    """_log_rejection rate-limits per reason in process-wide module state
    (there is no per-request object to hang it on - the middleware 421 fires
    before any endpoint runs). A test that drives one reject reason leaves a
    real monotonic reading behind, so a later test asserting that reason's
    WARNING would pass or fail on 60s of elapsed monotonic time and run
    ordering. Clear it around every test."""
    bridge._reject_warn_state.clear()
    yield
    bridge._reject_warn_state.clear()


@pytest.fixture(autouse=True)
def restore_bridge_logger_level():
    """main() pins a level on the module logger (the DEV_MODE switch), so
    any test driving main() end to end would otherwise leak that level into
    later tests' caplog expectations - records silently dropped with no
    hint why. Snapshot and restore around every test."""
    level = bridge.logger.level
    yield
    bridge.logger.setLevel(level)


@pytest.fixture(autouse=True)
def isolate_lock_dir(monkeypatch, tmp_path):
    """The hook-URL singleton lock lives in a FIXED dir (DEFAULT_LOCK_DIR:
    $XDG_RUNTIME_DIR, else tempfile.gettempdir(), plus /agent-event-bus-
    bridge-<uid>) independent of --wake-dir, so tests driving
    _acquire_singleton_locks or main() would otherwise write into the
    developer's real runtime/temp lock dir and collide on the shared
    default-hook-URL lock. Point it at a per-test tmp dir."""
    monkeypatch.setattr(bridge, "DEFAULT_LOCK_DIR", tmp_path / "bridge-locks")


@pytest.fixture
def config(tmp_path):
    return BridgeConfig(wake_dir=tmp_path / "wake", cooldown_seconds=30.0)


# The bus's httpx dispatch always sends this media type (single-sourced in
# helpers.py); the endpoint requires it so browser fetch() cannot reach the
# handler preflight-free - a client-level default keeps every test speaking
# the bus's wire shape without per-call noise. Built from the real
# constant, contract-test style.
JSON_CT = {"Content-Type": bridge.WEBHOOK_CONTENT_TYPE}

# The endpoint also allowlists Host (the DNS-rebinding guard), so tests
# must arrive under a loopback literal the way real local callers do -
# TestClient's default base_url would send "Host: testserver"
LOOPBACK_BASE = "http://127.0.0.1"


@pytest.fixture
def client(config):
    return TestClient(create_bridge_app(config), base_url=LOOPBACK_BASE, headers=JSON_CT)


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
        client = TestClient(create_bridge_app(config), base_url=LOOPBACK_BASE, headers=JSON_CT)
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


class TestImportHygiene:
    def test_bridge_import_does_not_pull_in_the_bus_server(self):
        """server.py is not side-effect-free at module scope: it opens,
        creates, and MIGRATES the bus database (SQLiteStorage()), attaches
        the bus's file log handler, and builds the FastMCP app. The bridge
        is a pure HTTP client of the bus and must never import it - on a
        client-mode machine that would fabricate a phantom bus DB, and on
        the bus host it would run migrations against the irreplaceable live
        DB from a second process. conftest neutralizes all of that under
        pytest, so this is checked in a clean subprocess."""
        import subprocess as sp
        import sys

        code = (
            "import sys; import agent_event_bus.bridge; "
            "sys.exit(1 if 'agent_event_bus.server' in sys.modules else 0)"
        )
        result = sp.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


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

    def test_bus_webhook_payload_key_contract(self):
        """The wake pipeline reads exactly three keys off the bus's POST -
        channel (the ONLY thing resolve_target_session reads), signal_level
        (the actionable filter), event_id (the drain hook's dedupe key) -
        and every hook test here builds its own body via make_event(), so
        without this pin the bus could drop or rename a key and the whole
        suite would stay green while every real delivery resolved no
        target. Same shape as the _get_signal_level pin above: assert
        against the bus's real payload builder, then close the loop by
        resolving a target from its actual output."""
        from datetime import datetime

        from agent_event_bus.server import _webhook_payload
        from agent_event_bus.storage import Event as BusEvent

        dm = BusEvent(
            id=7,
            event_type="note",
            payload="hi",
            session_id="sender-1",
            timestamp=datetime(2026, 8, 8),
            channel="session:target-1",
        )
        payload = _webhook_payload(dm)
        assert payload["event_id"] == 7
        assert payload["signal_level"] == "actionable"
        # sender attribution, distinct from the wake target
        assert payload["session_id"] == "sender-1"
        assert bridge.resolve_target_session(payload) == "target-1"

    def test_bus_dispatch_sends_the_media_type_the_hook_requires(self):
        """Content-Type is a hard delivery precondition, which promotes it
        to a cross-module wire contract - and the client fixture supplies
        the header itself, so without this pin the bus could drop or change
        it and every bridge test would stay green while every real delivery
        415d with /health still registered:true.
        Captured off the REAL dispatch path, not asserted from a local
        literal (the constant is single-sourced in helpers.py, but
        single-sourcing can't catch the header being dropped entirely)."""
        import asyncio
        from datetime import datetime

        from agent_event_bus import server
        from agent_event_bus.storage import Event as BusEvent
        from agent_event_bus.storage import Webhook

        captured: dict = {}

        class FakeResponse:
            status_code = 200

        class FakeClient:
            async def post(self, url, content=None, headers=None):
                captured.update(headers or {})
                return FakeResponse()

        dm = BusEvent(
            id=1,
            event_type="note",
            payload="hi",
            session_id="sender-1",
            timestamp=datetime(2026, 8, 8),
            channel="session:target-1",
        )
        webhook = Webhook(
            id=1,
            url="http://127.0.0.1:8082/hook",
            channel_filter=None,
            event_types=None,
            created_at=datetime(2026, 8, 8),
        )
        with patch.object(server, "_get_webhook_client", FakeClient):
            assert asyncio.run(server._dispatch_webhook(webhook, dm)) is True
        media = captured["Content-Type"].split(";", 1)[0].strip().lower()
        assert media == bridge.WEBHOOK_CONTENT_TYPE == "application/json"

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

        def infos():
            return [
                r
                for r in caplog.records
                if r.levelno == logging.INFO and "no signal_level" in r.message
            ]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            resp = client.post("/hook", content=json.dumps(make_event(signal_level=None)).encode())
            assert resp.json() == {"status": "ignored", "reason": "below actionable"}
            # Persistent condition, DM-rate volume: repeats demote to debug
            client.post("/hook", content=json.dumps(make_event(signal_level=None)).encode())
            assert len(infos()) == 1
            # A delivery that DOES carry a level (an upgraded bus) re-arms,
            # so a later downgrade surfaces again instead of staying dark
            client.post("/hook", content=json.dumps(make_event(signal_level="info")).encode())
            client.post("/hook", content=json.dumps(make_event(signal_level=None)).encode())
        assert len(infos()) == 2
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
        # Deep nesting blows the interpreter recursion limit inside
        # json.loads - RecursionError, which is NOT a ValueError - at a
        # size far under MAX_BODY_BYTES. Same named 400, not a 500 with
        # bus retries behind it.
        assert client.post("/hook", content=b"[" * 300_000).status_code == 400

    def test_isdigit_but_not_int_content_length_does_not_500(self, config):
        """The precheck guards int() with a digit test - and it must be
        isdecimal(), not isdigit(): U+00B2 (superscript two) is isdigit()
        True but int() ValueError, and it is exactly latin-1 byte 0xB2,
        the encoding Starlette decodes headers in. h11 rejects it for the
        CLI, so this drives the ASGI app directly the way a mounting app
        (the advertised embedding surface) that passed the header through
        would - it must fall to the streamed count, not 500."""
        import asyncio

        app = create_bridge_app(config)
        body = json.dumps(make_event()).encode()
        # Raw ASGI headers are bytes; Starlette decodes them latin-1, so
        # b"\xb2" surfaces as "\xb2" - isdigit() True, int() ValueError
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/hook",
            "raw_path": b"/hook",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8082"),
                (b"content-type", b"application/json"),
                (b"content-length", b"\xb2"),
            ],
        }

        async def drive():
            sent = []
            chunks = [
                {"type": "http.request", "body": body, "more_body": False},
            ]

            async def receive():
                return chunks.pop(0)

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            return sent

        sent = asyncio.run(drive())
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 200  # fell through the precheck, delivered
        assert (config.wake_dir / "target-1.jsonl").exists()

    def test_client_disconnect_mid_body_is_a_named_400(self, config):
        """A peer that aborts mid-body makes request.stream() raise
        ClientDisconnect (uvicorn delivers http.disconnect); uncaught it
        500s with a traceback. Every other wire failure returns a named
        4xx, so this one must too. Driven at the ASGI layer - httpx can't
        model a half-sent body cleanly."""
        import asyncio

        app = create_bridge_app(config)
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/hook",
            "raw_path": b"/hook",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8082"), (b"content-type", b"application/json")],
        }

        async def drive():
            sent = []
            messages = [
                {"type": "http.request", "body": b'{"partial":', "more_body": True},
                {"type": "http.disconnect"},
            ]

            async def receive():
                return messages.pop(0)

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            return sent

        sent = asyncio.run(drive())
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 400
        assert not (config.wake_dir / "target-1.jsonl").exists()

    def test_unserializable_payload_is_a_named_400_not_500(self, config):
        """json.dumps in _spool has the same recursion sibling the
        json.loads arm catches, one screen up - and it runs a few frames
        deeper, so a payload nested just under the parse limit is admitted
        and then fails to encode. Deterministic (all three bus retries
        raise identically), so process() maps it to the same named 400
        every other wire-input path gives, not three tracebacks. Driven by
        forcing the encoder failure rather than tuning a fragile
        cross-version depth band."""
        client = TestClient(create_bridge_app(config), base_url=LOOPBACK_BASE, headers=JSON_CT)
        # _spool re-raises the encoder failure as the dedicated type; process
        # maps ONLY that (never a post-spool RecursionError) to the 400
        with patch.object(
            bridge.Injector, "deliver", side_effect=bridge._UnserializablePayloadError
        ):
            resp = client.post("/hook", content=json.dumps(make_event()).encode())
        assert resp.status_code == 400
        # A DISTINCT string from the parse-failure 400: the body was valid
        # JSON, it just cannot re-serialize to a standard-JSON spool line -
        # a different producer and fix than "not JSON".
        assert resp.json() == {"error": "payload not serializable to a spool line"}

    def test_spool_serialization_failure_creates_no_file_or_lock(self, config):
        """_spool serializes BEFORE it opens the file or takes the flock, so
        a json.dumps recursion failure - re-raised as _UnserializablePayloadError
        so process() maps only the pre-durable case to a 400 - leaves
        nothing behind: no spool line, no lock file, no held lock."""
        injector = Injector(config)
        with patch.object(bridge.json, "dumps", side_effect=RecursionError):
            with pytest.raises(bridge._UnserializablePayloadError):
                injector.deliver("target-1", make_event())
        assert not (config.wake_dir / "target-1.jsonl").exists()
        assert not (config.wake_dir / "target-1.lock").exists()

    def test_symlinked_lock_file_does_not_write_through(self, config, tmp_path):
        """--wake-dir may have been group/world-writable before the daemon
        first ran; the startup chmod narrows the dir but does not remove a
        pre-planted <sid>.lock symlink already inside it. O_NOFOLLOW makes
        the open fail (ELOOP -> OSError, the retryable arm) instead of
        following the link and taking an flock on a foreign inode - which
        would silently protect the wrong file. The link target must stay
        untouched (never created)."""
        injector = Injector(config)  # creates + chmods the wake dir
        outside = tmp_path / "outside-target"
        (config.wake_dir / "target-1.lock").symlink_to(outside)
        # The open of the symlinked lock raises ELOOP; deliver does not
        # swallow it (only the /hook path maps spool failures to a status)
        with pytest.raises(OSError):
            injector.deliver("target-1", make_event())
        assert not outside.exists()  # never followed through the link

    def test_symlinked_spool_file_is_refused_with_a_clear_message(self, config, tmp_path):
        """The <sid>.jsonl sibling of the lock symlink takes a different
        branch: spool_file.resolve() follows the link first, so the
        containment check fires before O_NOFOLLOW ever runs. The refusal
        must name the symlink (the id is charset-clean, so the cause is a
        planted link, not a hostile id) and never follow it."""
        injector = Injector(config)  # creates + chmods the wake dir
        outside = tmp_path / "outside-spool"
        (config.wake_dir / "target-1.jsonl").symlink_to(outside)
        with pytest.raises(ValueError, match="symlink"):
            injector.deliver("target-1", make_event())
        assert not outside.exists()  # never followed through the link

    def test_browser_shaped_post_is_rejected(self, config):
        """The loopback-needs-no-secret posture holds only if a browser
        cannot reach the handler: fetch(mode:"no-cors") from any web page
        can POST to 127.0.0.1 preflight-free as long as the Content-Type
        is CORS-safelisted (a string body defaults to text/plain) - the
        response is opaque but the spool write would already have
        happened. Requiring application/json (what the bus's httpx
        dispatch actually sends) forces a preflight the bridge never
        answers, so the browser never sends the POST at all."""
        # Loopback base (passes the Host guard) but no default headers -
        # this test exercises the Content-Type half in isolation
        client = TestClient(create_bridge_app(config), base_url=LOOPBACK_BASE)
        body = json.dumps(make_event()).encode()
        # The exact shape a page can emit without a preflight: fetch()
        # string-body default, form enctype, and no header at all
        for content_type in ("text/plain;charset=UTF-8", "application/x-www-form-urlencoded", None):
            headers = {"Content-Type": content_type} if content_type else {}
            resp = client.post("/hook", content=body, headers=headers)
            assert resp.status_code == 415, content_type
        assert not (config.wake_dir / "target-1.jsonl").exists()
        # Media-type parameters must not defeat the guard - the check is
        # about what a browser can send preflight-free, not strictness
        ok = client.post(
            "/hook", content=body, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "delivered"

    def test_dns_rebinding_host_is_rejected(self, config):
        """The Content-Type guard rests on the attacker page being
        cross-origin; DNS rebinding removes that premise (page served from
        evil.example:<our port>, A record then flipped to 127.0.0.1 - the
        POST is same-origin, so CORS never applies and application/json is
        sent verbatim). What rebinding cannot forge is Host: the browser
        fills it from the page's URL, so a rebound request necessarily
        carries the attacker's hostname. The allowlist is loopback
        literals plus the hook URL's hostname - the names legitimate
        callers actually arrive under."""
        client = TestClient(create_bridge_app(config), headers=JSON_CT)  # Host: testserver
        body = json.dumps(make_event()).encode()
        # The rebound shape: right media type, wrong Host
        for host in ("evil.example:8082", "evil.example", "testserver"):
            resp = client.post("/hook", content=body, headers={"Host": host})
            assert resp.status_code == 421, host
        assert not (config.wake_dir / "target-1.jsonl").exists()
        # Every name a legitimate local caller can arrive under - including
        # the bracketed-IPv6 forms TrustedHostMiddleware would mangle
        for host in ("127.0.0.1:8082", "localhost:8082", "localhost", "[::1]:8082", "[::1]"):
            resp = client.post("/hook", content=body, headers={"Host": host})
            assert resp.status_code == 200, host

    def test_hook_url_hostname_is_an_allowed_host(self, tmp_path):
        """A remote-hook topology's bus POSTs with Host = the hook URL's
        hostname (httpx fills it from the URL the bridge registered) - the
        allowlist must admit exactly that name or every real delivery
        would 421 while /health stays green."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://bridge.tailnet.example:8082/hook",
            secret="s3cret",
        )
        client = TestClient(create_bridge_app(config), headers=JSON_CT)
        body = json.dumps(make_event()).encode()
        signed = {"Host": "bridge.tailnet.example:8082", SIGNATURE_HEADER: sign(body, "s3cret")}
        resp = client.post("/hook", content=body, headers=signed)
        assert resp.status_code == 200
        assert resp.json()["status"] == "delivered"

    def test_health_carries_the_same_host_guard(self, config):
        """The DNS-rebinding guard covers /health too: a rebound tab must
        not even be able to CONFIRM a bridge runs here (the signal that the
        /hook probe is worth the round trip). A supervisor's loopback probe
        still passes."""
        client = TestClient(create_bridge_app(config))  # no default Host override
        assert client.get("/health", headers={"Host": "evil.example:8082"}).status_code == 421
        assert client.get("/health", headers={"Host": "127.0.0.1:8082"}).status_code == 200
        assert client.get("/health", headers={"Host": "[::1]"}).status_code == 200

    def test_non_object_json_rejected(self, client):
        """Valid JSON that isn't an object must be a 400, not an
        AttributeError-turned-500 with bus retries behind it."""
        for body in (b"123", b'["x"]', b'"bare"', b"null"):
            assert client.post("/hook", content=body).status_code == 400

    def test_nan_infinity_literals_rejected(self, client):
        """A spooled line must be STANDARD JSON - no NaN/Infinity, which
        jq/JSON.parse/Go all reject (a drainer skips the line and the wake is
        silently lost). Two producers turn a body into a non-standard value:
        the bare NaN/Infinity/-Infinity TOKENS json.loads accepts (caught by
        parse_constant), and an OVERFLOWING numeric literal (1e400 ->
        float(inf), which parse_constant never sees - caught at the write
        side by _spool's allow_nan=False). Both must be a named 400."""
        for body in (b'{"payload": NaN}', b'{"payload": Infinity}', b'{"x": -Infinity}'):
            assert client.post("/hook", content=body).status_code == 400, body
        # The overflow route needs an ACTIONABLE event so it reaches _spool
        for value in ("1e400", "-1e400"):
            body = json.dumps(make_event()).replace('"need a review"', value).encode()
            assert client.post("/hook", content=body).status_code == 400, value

    def test_spooled_line_round_trips_through_a_strict_parser(self, client, config):
        """Pin the guarantee on what gets WRITTEN, not only on what's
        rejected at the door: a delivered event's spool line must parse
        under a strict parser that rejects the non-standard constants."""
        assert client.post("/hook", content=json.dumps(make_event()).encode()).status_code == 200
        line = (config.wake_dir / "target-1.jsonl").read_text().splitlines()[0]

        def strict(literal):
            raise ValueError(f"non-standard constant {literal!r}")

        assert json.loads(line, parse_constant=strict)["event_id"] == 1

    def test_host_guard_precedes_routing(self, config):
        """The Host allowlist runs in middleware, ahead of the router, so a
        method or path mismatch cannot confirm a bridge is here: a foreign
        Host gets 421 even on GET /hook (else 405) and an unknown path
        (else 404). A loopback Host still sees the router's real codes."""
        client = TestClient(create_bridge_app(config))  # Host: testserver
        assert client.get("/hook", headers={"Host": "evil.example"}).status_code == 421
        assert client.get("/nope", headers={"Host": "evil.example"}).status_code == 421
        assert (
            client.request("HEAD", "/health", headers={"Host": "evil.example"}).status_code == 421
        )
        # A legitimate loopback Host reaches the router's real 405/404
        assert client.get("/hook", headers={"Host": "127.0.0.1"}).status_code == 405
        assert client.get("/nope", headers={"Host": "127.0.0.1"}).status_code == 404

    def test_bind_address_is_an_allowed_host(self, tmp_path):
        """A monitoring probe addressing the bound interface by IP is
        ordinary in the pinned non-loopback topology the guide recommends,
        so the effective bind address is allowlisted - else the probe 421s.
        A foreign Host is still rejected (the rebound-page Host carries the
        attacker hostname, never the bound literal)."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            bind="100.100.1.2",
            hook_url="http://host.tailnet.example:8082/hook",
            secret="s3cret",
        )
        client = TestClient(create_bridge_app(config))
        assert client.get("/health", headers={"Host": "100.100.1.2"}).status_code == 200
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421

    def test_allowed_host_admits_a_forwarding_proxy(self, tmp_path):
        """The gap --allowed-host exists for: a non-loopback hook URL derives
        bind 0.0.0.0, is_unspecified keeps the wildcard OUT of the allowlist,
        and nginx's proxy_pass default rewrites Host to its UPSTREAM address -
        so every forwarded dispatch 421s while /health stays green. --bind
        cannot cover it (pinning drops the wildcard, and a name-rewriting
        proxy has no address to pin). An operator-listed Host is admitted;
        an unlisted one is still rejected."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://bridge.example:8082/hook",  # non-loopback -> bind 0.0.0.0
            allowed_hosts=("10.0.0.5:8082",),
            secret="s3cret",
        )
        client = TestClient(create_bridge_app(config), headers=JSON_CT)
        body = json.dumps(make_event()).encode()
        signed = {"Host": "10.0.0.5:8082", SIGNATURE_HEADER: sign(body, "s3cret")}
        assert client.post("/hook", content=body, headers=signed).status_code == 200
        # The allowlist is still a guard, not a bypass
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421

    def test_blank_allowed_host_does_not_disable_the_guard(self, tmp_path):
        """THE bypass this sanitation exists for. `os.environ.get(X, "")
        .split(",")` yields ("",) - the obvious embedder idiom - and an empty
        entry canonicalizes to itself. The middleware defaults raw_host to ""
        when the Host header is absent or blank (h11 permits both), so that
        entry would MATCH those requests and turn the rebinding guard off for
        /hook and /health alike, silently, while /health still reports
        registered. Driven through create_bridge_app (not config_from_args)
        because the embedder path is the one that was unprotected."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://bridge.example:8082/hook",
            secret="s3cret",
            allowed_hosts=tuple("".split(",")),  # ("",) - verbatim idiom
        )
        client = TestClient(create_bridge_app(config), base_url="http://bridge.example:8082")
        assert client.get("/health", headers={"Host": ""}).status_code == 421
        assert client.get("/health", headers={"Host": "evil.example"}).status_code == 421
        # and the legitimate name still passes, so this isn't a blanket deny
        assert client.get("/health", headers={"Host": "bridge.example:8082"}).status_code == 200

    def test_allowed_host_entries_are_canonicalized_for_embedders(self, tmp_path):
        """The other two hand-built shapes, both of which leave the escape
        hatch silently inert rather than open: an unstripped entry can never
        match, and a BARE str is iterable, so an unsanitized loop would add
        one entry per CHARACTER. A bare IPv6 literal is accepted too - --bind
        takes them unbracketed, so that's the spelling operators arrive with,
        and _host_from_header alone would mangle it to "fd7a:"."""
        for allowed, host in (
            ((" proxy.example ",), "proxy.example"),  # unstripped
            ("proxy.example", "proxy.example"),  # bare str, not a tuple
            (("fd7a::1",), "[fd7a::1]"),  # bare IPv6 -> bracketed on the wire
        ):
            config = BridgeConfig(
                wake_dir=tmp_path / "wake",
                hook_url="http://bridge.example:8082/hook",
                secret="s3cret",
                allowed_hosts=allowed,
            )
            client = TestClient(create_bridge_app(config), base_url="http://bridge.example:8082")
            assert client.get("/health", headers={"Host": host}).status_code == 200, allowed
            # a per-character allowlist would admit these single letters
            assert client.get("/health", headers={"Host": "p"}).status_code == 421, allowed

    def test_non_string_allowed_host_is_a_named_config_error(self, tmp_path):
        """Same posture as the other hand-built-config type checks: a named
        BridgeConfigError, not a bare AttributeError off .strip()."""
        with pytest.raises(bridge.BridgeConfigError, match="allowed host"):
            create_bridge_app(
                BridgeConfig(
                    wake_dir=tmp_path / "wake",
                    hook_url="http://bridge.example:8082/hook",
                    secret="s3cret",
                    allowed_hosts=(123,),
                )
            )

    def test_rejected_host_names_itself_and_the_fix(self, config, caplog):
        """A 421 was the one reject with NO diagnostic anywhere on this side:
        the bus discards the response body and logs its own status on a
        different host in every non-loopback topology. Diagnosing it needs
        the rejected Host, so the warning carries it plus the flag that
        admits it. Rate-limited per reason, so the repeat drops to debug."""
        import logging

        client = TestClient(create_bridge_app(config))
        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            assert client.get("/health", headers={"Host": "10.0.0.5:8082"}).status_code == 421
            assert client.get("/health", headers={"Host": "10.0.0.6:8082"}).status_code == 421
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "the repeat must not re-warn inside the interval"
        assert "10.0.0.5:8082" in warnings[0].message
        assert "--allowed-host" in warnings[0].message

    def test_ipv6_bind_address_normalized_in_allowlist(self, tmp_path):
        """The bind address is stored NORMALIZED (str(ip_address)), so an
        uppercase or expanded IPv6 --bind still matches the lowercase,
        compressed Host a probe sends - the tailnet case the bind-address
        allowlisting exists for (a raw-string compare would 421 it)."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            bind="FD7A:115C:A1E0::1",  # uppercase; a probe sends lowercase
            hook_url="http://host.tailnet.example:8082/hook",
            secret="s3cret",
        )
        client = TestClient(create_bridge_app(config))
        # BOTH wire spellings must match: normalization happens on both the
        # stored entry and the incoming Host (in _host_from_header), so the
        # guard never depends on which spelling the probe/bus renders.
        for host in (
            "[fd7a:115c:a1e0::1]:8082",  # compressed
            "[FD7A:115C:A1E0::1]:8082",  # compressed, upper
            "[fd7a:115c:a1e0:0:0:0:0:1]:8082",  # expanded
            "[fd7a:115c:a1e0::1]",  # no port
        ):
            assert client.get("/health", headers={"Host": host}).status_code == 200, host

    def test_ipv6_hook_host_normalized_in_allowlist(self, tmp_path):
        """Same normalization on the hook side, and BOTH directions: an
        EXPANDED IPv6 hook-URL literal must match a compressed probe Host AND
        the expanded Host the bus's httpx client may render from the
        registered URL - _host_from_header normalizes the incoming side too,
        so the guard doesn't rest on httpx's spelling choice."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://[FD7A:115C:A1E0:0:0:0:0:1]:8082/hook",  # expanded
            secret="s3cret",
        )
        client = TestClient(create_bridge_app(config))
        for host in (
            "[fd7a:115c:a1e0::1]:8082",  # compressed
            "[fd7a:115c:a1e0:0:0:0:0:1]:8082",  # expanded (as registered)
            "[FD7A:115C:A1E0:0:0:0:0:1]:8082",  # expanded, upper
        ):
            assert client.get("/health", headers={"Host": host}).status_code == 200, host

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

    def test_spool_breadcrumb_demotes_after_first_delivery(self, config, caplog):
        """The INFO breadcrumb proves the chain works ONCE - webhooks have
        no machine scoping, so on a multi-machine bus the default backend
        would otherwise log one INFO per foreign-machine DM, the exact
        volume the tmux unmapped arm was demoted to debug to avoid.
        Repeats must still be visible under DEV_MODE, not skipped."""
        import logging

        injector = Injector(config)
        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            injector.deliver("target-1", make_event())
            injector.deliver("target-1", make_event(event_id=2))
            injector.deliver("other-session", make_event(event_id=3))
        spooled = [r for r in caplog.records if "Spooled event" in r.message]
        assert [r.levelno for r in spooled] == [logging.INFO, logging.DEBUG, logging.DEBUG]

    def test_spool_and_lock_files_created_0o600(self, config):
        """Spool lines carry full publisher-authored payloads, and the wake
        dir's 0o700 is the only other guard - one --wake-dir at a shared
        pre-existing path, or one later manual chmod (the documented prune
        and drain workflows invite it), and umask-mode files are world-
        readable. The create mode must be explicit, not inherited."""
        old_umask = os.umask(0o000)  # widest case: a plain open would land 0o666
        try:
            Injector(config).deliver("target-1", make_event())
        finally:
            os.umask(old_umask)
        for name in ("target-1.jsonl", "target-1.lock"):
            mode = (config.wake_dir / name).stat().st_mode & 0o777
            assert mode == 0o600, (name, oct(mode))

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

    def test_unpreparable_wake_dir_is_a_named_config_error(self, tmp_path, monkeypatch):
        """mkdir/chmod are the one startup filesystem precondition - they
        must surface as a named SystemExit like every other config input,
        not a bare traceback. (Parent is a FILE, not an unwritable dir:
        NotADirectoryError fires for root too, unlike PermissionError.)"""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        config = BridgeConfig(wake_dir=blocker / "wake")
        # BridgeConfigError on the construction path, so an embedder's
        # `except Exception` around app assembly can catch it
        with pytest.raises(bridge.BridgeConfigError, match="--wake-dir / AGENT_EVENT_BUS_WAKE_DIR"):
            Injector(config)
        # The CLI path still gets a clean message-and-exit via main()'s
        # translation, not a traceback
        import sys

        monkeypatch.setattr(
            sys, "argv", ["agent-event-bus-bridge", "--wake-dir", str(blocker / "wake")]
        )
        with pytest.raises(SystemExit, match="--wake-dir"):
            bridge.main()


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
        """The panes-guard lesson applies here too: the bound must key
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

    def test_panes_json_is_read_as_utf8_not_locale_codec(self, tmp_path):
        """The read decodes as UTF-8, not the locale codec (a
        supervisor-launched daemon gets no LANG, so glibc resolves C ->
        ASCII, and a healthy file with any byte >0x7f would wrongly hit the
        unparseable arm). A non-ASCII decoy value ('café') round-trips to
        the tmux wake, which proves the decode is UTF-8: an ASCII codec
        would UnicodeDecodeError on it and degrade to spool-unmapped."""
        injector, config = make_tmux_injector(tmp_path)
        (config.wake_dir / "panes.json").write_text(
            json.dumps({"target-1": "%0", "_note": "café"}), encoding="utf-8"
        )
        with patch.object(bridge.subprocess, "run", tmux_ok):
            assert injector.deliver("target-1", make_event()) == "tmux"

    def test_symlinked_panes_json_degrades_to_spool(self, tmp_path):
        """panes.json shares the wake dir's history, so a symlink planted at
        the name must not be followed (O_NOFOLLOW -> ELOOP -> the unreadable
        arm), degrading to spool rather than reading a foreign file."""
        injector, config = make_tmux_injector(tmp_path)
        (config.wake_dir / "panes.json").unlink()
        (config.wake_dir / "panes.json").symlink_to(tmp_path / "elsewhere.json")
        with patch.object(bridge.subprocess, "run") as mock_run:
            assert injector.deliver("target-1", make_event()) == "spool-unmapped"
        mock_run.assert_not_called()

    def test_fifo_panes_json_does_not_hang(self, tmp_path):
        """A planted FIFO with NO writer must not park the delivery worker
        forever (this path has no TMUX_TIMEOUT). O_NONBLOCK makes the read
        return EOF promptly; the delivery degrades to spool, not a hang."""
        injector, config = make_tmux_injector(tmp_path)
        (config.wake_dir / "panes.json").unlink()
        os.mkfifo(config.wake_dir / "panes.json")  # no writer -> would block
        with patch.object(bridge.subprocess, "run") as mock_run:
            assert injector.deliver("target-1", make_event()) == "spool-unmapped"
        mock_run.assert_not_called()

    def test_fifo_panes_json_with_writer_degrades_not_500(self, tmp_path):
        """A FIFO with a writer attached and nothing buffered makes the text
        read return None -> TypeError, which is NOT an OSError. It must still
        degrade to spool (the never-500 contract), not escape _tmux_pane -
        an escape would 500 an already-spooled delivery and the bus would
        retry it, duplicating lines."""
        injector, config = make_tmux_injector(tmp_path)
        fifo = config.wake_dir / "panes.json"
        fifo.unlink()
        os.mkfifo(fifo)
        writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # hold a writer, write nothing
        try:
            with patch.object(bridge.subprocess, "run") as mock_run:
                assert injector.deliver("target-1", make_event()) == "spool-unmapped"
            mock_run.assert_not_called()
        finally:
            os.close(writer)

    def test_oversized_panes_json_degrades_to_spool(self, tmp_path, monkeypatch):
        """The read is bounded (MAX_PANES_CHARS), so a runaway file can't
        pull unbounded data into memory per DM; the truncated read is
        invalid JSON and degrades to spool."""
        monkeypatch.setattr(bridge, "MAX_PANES_CHARS", 64)
        injector, config = make_tmux_injector(tmp_path)
        big = {"target-1": "%0", "_pad": "x" * 500}  # valid, but past the cap
        (config.wake_dir / "panes.json").write_text(json.dumps(big), encoding="utf-8")
        with patch.object(bridge.subprocess, "run") as mock_run:
            assert injector.deliver("target-1", make_event()) == "spool-unmapped"
        mock_run.assert_not_called()

    def test_valid_nondict_read_clears_stale_read_keys(self, tmp_path, caplog):
        """A valid-JSON-but-non-dict read (a list) proves the read AND parse
        conditions cleared even though the shape is wrong, so it must disarm
        unparseable/unreadable while it arms not-an-object - otherwise a
        genuinely-new later read/parse failure demotes to debug behind a
        stale key, silent at the default level."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            # 1. Unreadable (a directory) -> arms 'unreadable', warns
            panes.mkdir()
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 1
            # 2. A valid JSON list -> not-an-object arm; the valid parse must
            # clear the stale 'unreadable' key even though it returns early
            panes.rmdir()
            panes.write_text(json.dumps(["not", "a", "dict"]))
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 2  # not-an-object, a fresh condition
            # 3. Unreadable again -> genuinely new; must WARN, not debug
            # behind a key step 2 should have cleared
            panes.unlink()
            panes.mkdir()
            injector.deliver("target-1", make_event())
            assert len(warnings()) == 3

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

    def test_unexpected_panes_error_degrades_and_warns_once(self, tmp_path, caplog):
        """The never-500 catch-all: an exception OUTSIDE (ValueError,
        RecursionError, TypeError, OSError) - here a KeyError from a
        monkeypatched json.loads - must still degrade to spool (never 500 an
        already-spooled delivery), warn once, demote repeats, and re-arm on a
        healthy read. Pins the arm itself AND the 'unexpected' key's
        membership in the re-arm sets (a healthy read clears it)."""
        import logging

        injector, config = make_tmux_injector(tmp_path)  # panes.json maps target-1
        real_loads = bridge.json.loads
        boom = {"raise": True}

        def flaky_loads(s, *a, **k):
            if boom["raise"]:
                raise KeyError("outside the enumerated arms")
            return real_loads(s, *a, **k)

        def warnings():
            return [r for r in caplog.records if r.levelno == logging.WARNING]

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(bridge.json, "loads", flaky_loads):
                # Never 500: the KeyError degrades to spool, not an escape
                assert injector.deliver("target-1", make_event()) == "spool-unmapped"
                assert injector.deliver("target-1", make_event()) == "spool-unmapped"
                assert len(warnings()) == 1  # the repeat demoted to debug
                # A healthy read clears 'unexpected' (it's in _PANES_READ_KEYS)
                boom["raise"] = False
                with patch.object(bridge.subprocess, "run", tmux_ok):
                    assert injector.deliver("target-1", make_event()) == "tmux"
                boom["raise"] = True
                injector.deliver("target-1", make_event())  # warns only if re-armed
            assert len(warnings()) == 2

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

    def test_nul_pane_value_degrades_to_spool_not_valueerror(self, tmp_path, caplog):
        """A pane value carrying an embedded NUL (JSON encodes it as
        \\u0000) is a non-empty str, so a type-and-truthiness guard alone
        admits it to the send-keys argv - where subprocess.run raises
        ValueError BEFORE check or timeout, a class _tmux_wake's post-spool
        arms don't catch. The escape would 500 the webhook with the cooldown
        reservation already taken and the rollback skipped: the bus retries
        an already-spooled event while the session sits in a cooldown for a
        wake that never happened. The isprintable() guard must instead route
        it into the bad-pane-value arm - warning names the entry to repair -
        and the value must never reach argv."""
        import logging

        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        (config.wake_dir / "panes.json").write_text(json.dumps({"target-1": "%0\x00"}))

        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            with patch.object(bridge.subprocess, "run") as mock_run:
                assert injector.deliver("target-1", make_event()) == "spool-unmapped"
        mock_run.assert_not_called()
        assert any("not a pane id" in r.message for r in caplog.records)

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

    def test_warn_key_caps_bound_both_sets(self, tmp_path, caplog, monkeypatch):
        """The warn-key retention caps are load-bearing: without a pin a
        refactor could delete the clear, flip the comparison, or clear the
        WRONG set (the two sit a screen apart under different locking).
        Cap at 2, drive three distinct conditions per set, assert the bound
        holds and the first condition warns again after the clear."""
        import logging

        monkeypatch.setattr(bridge, "_WARN_KEYS_CAP", 2)

        def warnings(text):
            return [r for r in caplog.records if r.levelno == logging.WARNING and text in r.message]

        # Panes set: three sessions with bad values, then the first again
        config = BridgeConfig(wake_dir=tmp_path / "wake", backend="tmux")
        injector = Injector(config)
        panes = config.wake_dir / "panes.json"
        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            panes.write_text(json.dumps({"s1": 0, "s2": 0, "s3": 0}))
            for sid in ("s1", "s2", "s3"):
                injector.deliver(sid, make_event())
            assert len(injector._warned_panes_keys) <= 2  # cap held
            injector.deliver("s1", make_event())  # cleared at cap - warns again
        assert len(warnings("not a pane id")) == 4

        # Wake-fail set: three exception types for one session, then the first
        caplog.clear()
        injector2, _ = make_tmux_injector(tmp_path / "w2", sessions=("t1",), cooldown=0.0)

        def raiser(exc):
            def run(cmd, **kwargs):
                raise exc

            return run

        with caplog.at_level(logging.DEBUG, logger="agent-event-bus-bridge"):
            with patch.object(bridge.subprocess, "run", raiser(PermissionError("a"))):
                injector2.deliver("t1", make_event())
            with patch.object(
                bridge.subprocess, "run", raiser(bridge.subprocess.TimeoutExpired("x", 1))
            ):
                injector2.deliver("t1", make_event())
            with patch.object(
                bridge.subprocess, "run", raiser(bridge.subprocess.CalledProcessError(1, ["x"]))
            ):
                injector2.deliver("t1", make_event())  # third type - cap clears
            assert len(injector2._warned_wake_fail_keys) <= 2  # cap held
            with patch.object(bridge.subprocess, "run", raiser(PermissionError("a"))):
                injector2.deliver("t1", make_event())  # re-warns after clear
        assert len(warnings("tmux wake failed")) == 4

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
                with TestClient(app, base_url=LOOPBACK_BASE) as client:
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
                with TestClient(app, base_url=LOOPBACK_BASE) as client:
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
        """A second lifespan cycle on the same app must register again.
        Each cycle gets its own fresh stop event (see
        test_each_cycle_gets_its_own_stop_event), so re-entry naturally
        starts a new registration rather than resuming a stopped one."""
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

    def test_each_cycle_gets_its_own_stop_event(self, tmp_path):
        """Regression for the resurrect-a-stale-thread bug: each cycle must
        hand register_with_retry a FRESH stop event, never the shared caller
        event. The old design cleared ONE shared event on re-entry, which
        could un-set the event a prior cycle's thread (parked past the join
        timeout) still waited on - resurrecting it into a second
        registration that races state['webhook_id']."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        stops_seen: list = []

        def fake_retry(cfg, state, stop):
            stops_seen.append(stop)
            state["webhook_id"] = 41 + len(stops_seen)

        caller_stop = threading.Event()
        state: dict = {}
        with patch.object(bridge, "register_with_retry", fake_retry):
            with patch.object(bridge, "unregister_from_bus", lambda cfg, wid: None):
                app = create_bridge_app(
                    config, registration_state=state, registration_stop=caller_stop
                )
                with TestClient(app):
                    pass
                with TestClient(app):
                    pass
        assert len(stops_seen) == 2
        assert stops_seen[0] is not stops_seen[1]  # a fresh event per cycle
        assert caller_stop not in stops_seen  # never the shared caller event

    def test_create_bridge_app_validates_hand_built_configs(self, tmp_path):
        """Embedders skip argparse, so the invariants - including the
        exposed-listener secret requirement - must travel with the config:
        an off-box /hook with no authentication is refused at app
        construction, not just at the CLI."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", hook_url="http://box.example:8082/hook")
        # BridgeConfigError, not SystemExit: an embedder's `except Exception`
        # around app assembly must be able to catch it
        with pytest.raises(bridge.BridgeConfigError, match="AGENT_EVENT_BUS_BRIDGE_SECRET"):
            create_bridge_app(config)

    def test_assume_exposed_requires_secret(self, tmp_path):
        """The exposure check derives from config.bind, and on the embedding
        paths this module documents (uvicorn --factory --host, an ASGI
        mount) the HOSTING server owns the real bind - the config's None
        reads as loopback, so a wide-open unauthenticated listener would
        validate clean. assume_exposed is the embedder's opt-in to the same
        hard refusal the CLI gives a wide --bind, and its error must name
        that lever, not claim a loopback bind is reachable off-box."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", assume_exposed=True)
        with pytest.raises(bridge.BridgeConfigError, match="assume_exposed"):
            create_bridge_app(config)
        config.secret = "s3cret"
        create_bridge_app(config)  # the secret satisfies the requirement

    def test_hand_built_config_types_are_coerced_or_named(self, tmp_path):
        """An embedder passing raw env strings must get the named config
        error, not a bare TypeError out of a comparison - validate_config
        normalizes before checking, so string numbers simply work."""
        config = BridgeConfig(wake_dir=tmp_path / "wake", port="8082", cooldown_seconds="30")
        create_bridge_app(config)  # coerced, valid
        assert config.port == 8082
        assert config.cooldown_seconds == 30.0
        bad = BridgeConfig(wake_dir=tmp_path / "wake", port="eighty")
        with pytest.raises(bridge.BridgeConfigError, match="AGENT_EVENT_BUS_BRIDGE_PORT"):
            create_bridge_app(bad)
        # Path() was the one normalization that could still raise a bare
        # TypeError - the exact shape this block exists to prevent
        non_path = BridgeConfig(wake_dir=123)
        with pytest.raises(bridge.BridgeConfigError, match="AGENT_EVENT_BUS_WAKE_DIR"):
            create_bridge_app(non_path)
        # The three string inputs: a non-str bus_url/hook_url raises a bare
        # AttributeError out of urlsplit; a non-str bind (int) validates as a
        # bogus 0.0.0.x and fails only later in uvicorn.run - both must be
        # the named config error instead
        for kwargs, env in (
            ({"bus_url": 123}, "AGENT_EVENT_BUS_URL"),
            ({"hook_url": tmp_path}, "AGENT_EVENT_BUS_BRIDGE_HOOK_URL"),
            ({"bind": 123, "secret": "s"}, "AGENT_EVENT_BUS_BRIDGE_BIND"),
            # a bytes secret is truthy (satisfies the exposure requirement)
            # and then fails at RUNTIME - register_with_bus' json= raises
            # TypeError, verify_signature's .encode() AttributeErrors
            ({"secret": b"s3cret"}, "AGENT_EVENT_BUS_BRIDGE_SECRET"),
        ):
            bad_str = BridgeConfig(wake_dir=tmp_path / "wake", **kwargs)
            with pytest.raises(bridge.BridgeConfigError, match=env):
                create_bridge_app(bad_str)

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
        with TestClient(create_bridge_app(config), base_url=LOOPBACK_BASE) as client:
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

    def test_malformed_hook_port_refused_for_hand_built_configs(self, tmp_path):
        """SplitResult.port parses lazily, so a malformed port sails past
        the scheme/hostname refusals - and the bus stores the registered
        URL verbatim, so on the embedding path it would register cleanly
        and then fail every dispatch bus-side (httpx.InvalidURL) with
        /health green: the silent-inertness shape the scheme refusal
        already closes. The refusal must live in validate_config so
        embedders get the same named error as the CLI."""
        config = BridgeConfig(
            wake_dir=tmp_path / "wake",
            hook_url="http://bridge.example:80o82/hook",
            secret="s3cret",
        )
        with pytest.raises(bridge.BridgeConfigError, match="bad port"):
            create_bridge_app(config)

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

    def test_loopback_literal_hook_binds_that_literal(self, tmp_path):
        """The derived bind must be the loopback address the hook URL
        NAMES, not a hardcoded 127.0.0.1: loopback is all of 127.0.0.0/8
        plus ::1, so a hook on 127.0.1.1 (Debian's own-hostname address),
        a 127.0.0.2 alias, or [::1] would otherwise bind an interface the
        bus can never reach - ECONNREFUSED on every dispatch, with both
        sides still reading as loopback so every exposure/quadrant/family
        guard stays silent and /health reports registered:true."""
        for host, expected in (
            ("127.0.1.1", "127.0.1.1"),
            ("127.0.0.2", "127.0.0.2"),
            ("[::1]", "::1"),
            ("localhost", "127.0.0.1"),  # no single literal - keep the v4 default
        ):
            config = BridgeConfig(wake_dir=tmp_path / "wake", hook_url=f"http://{host}:8082/hook")
            assert bridge.bind_host(config) == expected, host

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
        (the exposure refusal, reachable through the --bind flag)."""
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

    def test_pinned_bind_differing_loopback_literal_warns(self, caplog):
        """The missing quadrant member: a PINNED --bind on one loopback
        literal under a hook URL naming a DIFFERENT same-family loopback
        literal. Both loopback (quadrant checks quiet), same family (family
        checks quiet), exposed False (no secret) - yet the bus POSTs where
        nothing listens. A derived bind can't hit this; a pinned one can."""
        import logging

        with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
            bridge.config_from_args(
                bridge.build_parser().parse_args(
                    ["--bind", "127.0.0.1", "--hook-url", "http://127.0.1.1:8082/hook"]
                )
            )
        assert any("127.0.0.1" in r.message and "127.0.1.1" in r.message for r in caplog.records)

    def test_matching_loopback_literal_is_quiet(self, caplog):
        """The same loopback literal on both sides is the normal pinned
        case - no warning. localhost is exempt too (no single literal to
        compare), which the differing-literal check must not trip on."""
        import logging

        for bind, hook in (
            ("127.0.0.1", "http://127.0.0.1:8082/hook"),
            ("127.0.0.1", "http://localhost:8082/hook"),
        ):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="agent-event-bus-bridge"):
                bridge.config_from_args(
                    bridge.build_parser().parse_args(["--bind", bind, "--hook-url", hook])
                )
            assert not any("different loopback" in r.message for r in caplog.records), (bind, hook)

    def test_allowed_hosts_from_flag_and_env(self, monkeypatch):
        """Comma-separated on both surfaces, blanks dropped so a trailing
        comma or an empty env var yields () rather than an "" entry that
        would match a Host-less request. Entries come back CANONICALIZED
        (in validate_config, so embedders get the same treatment): the port
        is stripped exactly as it is on the incoming Host, which is what
        makes a "10.0.0.5:8082" entry match a request under that authority."""
        args = bridge.build_parser().parse_args(["--allowed-host", "proxy.example, 10.0.0.5:8082,"])
        assert bridge.config_from_args(args).allowed_hosts == ("proxy.example", "10.0.0.5")
        monkeypatch.setenv("AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS", "edge.example")
        assert bridge.config_from_args(bridge.build_parser().parse_args([])).allowed_hosts == (
            "edge.example",
        )

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
        # main() pins here, for this and every other main()-driving test;
        # clean_bridge_env guarantees DEV_MODE starts unset for the off half.
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

    def test_singleton_lock_blocks_a_second_instance(self, tmp_path):
        """The startup sweep can't tell a stale row from a live peer's, so a
        second bridge on the same wake dir must refuse BEFORE it touches the
        bus - otherwise a double-start unregisters the running bridge's
        webhook and leaves it deaf. The flock'd singletons give the
        'already running' error; the holder keeps them until the fds close."""
        config = BridgeConfig(wake_dir=tmp_path / "wake")
        config.wake_dir.mkdir(parents=True)
        fds = bridge._acquire_singleton_locks(config)
        try:
            with pytest.raises(SystemExit, match="already"):
                bridge._acquire_singleton_locks(config)
        finally:
            for fd in fds:
                os.close(fd)
        # Released - a fresh instance acquires cleanly
        for fd in bridge._acquire_singleton_locks(config):
            os.close(fd)

    def test_singleton_hook_url_lock_blocks_across_wake_dirs(self, tmp_path):
        """The destructive sweep contends on the HOOK URL, not the wake dir:
        a second instance with a DIFFERENT --wake-dir but the same port must
        still refuse (else it unregisters the running bridge's row and fails
        EADDRINUSE, deafening it). The hook-URL lock closes that."""
        a = BridgeConfig(wake_dir=tmp_path / "a")  # both default hook URL
        b = BridgeConfig(wake_dir=tmp_path / "b")
        a.wake_dir.mkdir(parents=True)
        b.wake_dir.mkdir(parents=True)
        fds = bridge._acquire_singleton_locks(a)
        try:
            # Different wake dir, same hook URL -> the hook-URL lock refuses
            with pytest.raises(SystemExit, match="hook URL"):
                bridge._acquire_singleton_locks(b)
        finally:
            for fd in fds:
                os.close(fd)
        # A genuinely distinct hook URL coexists
        c = BridgeConfig(wake_dir=tmp_path / "c", hook_url="http://127.0.0.1:9099/hook", secret="s")
        c.wake_dir.mkdir(parents=True)
        fds_a = bridge._acquire_singleton_locks(a)
        fds_c = bridge._acquire_singleton_locks(c)
        for fd in (*fds_a, *fds_c):
            os.close(fd)

    def test_partial_acquisition_does_not_leak_the_first_lock(self, tmp_path):
        """Sequential acquisition must close an already-held lock when a
        later one refuses: an instance sharing a wake dir but with a
        DISTINCT hook URL takes the hook lock, then refuses on the wake lock
        - the hook fd must be released, or in-process a later legitimate
        start on that URL 421s against a bridge that is not running."""
        clash_url = "http://127.0.0.1:9191/hook"
        held = bridge._acquire_singleton_locks(BridgeConfig(wake_dir=tmp_path / "w1"))
        try:
            # Same wake dir as the holder, distinct hook URL: the hook lock
            # acquires, then the wake lock refuses -> the hook fd must not leak
            clash = BridgeConfig(wake_dir=tmp_path / "w1", hook_url=clash_url, secret="s")
            with pytest.raises(SystemExit, match="wake dir"):
                bridge._acquire_singleton_locks(clash)
        finally:
            for fd in held:
                os.close(fd)
        # If the hook fd leaked, this same-URL start (free wake dir) would
        # spuriously refuse; it must acquire cleanly
        fresh = BridgeConfig(wake_dir=tmp_path / "w2", hook_url=clash_url, secret="s")
        for fd in bridge._acquire_singleton_locks(fresh):
            os.close(fd)

    def test_lock_dir_symlink_is_refused(self, tmp_path, monkeypatch):
        """The hook-lock dir sits at a guessable path under a possibly-shared
        temp dir, so it is create-and-verified, not adopted: a symlink
        planted there fails closed with a named SystemExit (lstat, not stat),
        never chmod'd or written through."""
        victim = tmp_path / "victim"
        victim.mkdir()
        link = tmp_path / "planted-lockdir"
        link.symlink_to(victim)
        monkeypatch.setattr(bridge, "DEFAULT_LOCK_DIR", link)
        with pytest.raises(SystemExit, match="not a directory"):
            bridge._acquire_singleton_locks(BridgeConfig(wake_dir=tmp_path / "wake"))
        assert not list(victim.iterdir())  # nothing written through the link

    def test_flock_recreates_vanished_lock_dir_privately(self, tmp_path, monkeypatch):
        """If the hook-lock dir vanishes between _ensure_private_lock_dir and
        _flock_or_exit's mkdir, the re-create must be 0o700 - else the NEXT
        start's verify would refuse our OWN dir as group/world-accessible.
        Drive that exact sequence and assert the re-created dir still passes
        the verify (fails if mode=0o700 is dropped, under any umask)."""
        import shutil

        d = tmp_path / "lockdir"
        monkeypatch.setattr(bridge, "DEFAULT_LOCK_DIR", d)
        bridge._ensure_private_lock_dir(d)
        shutil.rmtree(d)  # simulate the vanish between verify and mkdir
        fd = bridge._flock_or_exit(d / "hook.x.lock", "conflict")
        try:
            bridge._ensure_private_lock_dir(d)  # must NOT raise on our re-created dir
        finally:
            os.close(fd)

    def test_lock_dir_group_accessible_is_refused(self, tmp_path, monkeypatch):
        """A pre-planted lock dir we own but that is group/world-accessible
        must be refused, not silently used - the same posture as the wake
        dir's 0o700."""
        loose = tmp_path / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)  # mkdir mode is umask-masked; force it wide
        monkeypatch.setattr(bridge, "DEFAULT_LOCK_DIR", loose)
        with pytest.raises(SystemExit, match="group/world-accessible"):
            bridge._acquire_singleton_locks(BridgeConfig(wake_dir=tmp_path / "wake"))

    def test_uncreatable_lock_dir_is_a_named_exit(self, tmp_path, monkeypatch):
        """The create arm of _ensure_private_lock_dir: an os.mkdir that
        cannot succeed (here a parent that is a regular file -> NotADirectory)
        must surface as the named 'set XDG_RUNTIME_DIR' message, not a bare
        traceback at the operator's first contact with the daemon."""
        (tmp_path / "afile").write_text("x")
        monkeypatch.setattr(bridge, "DEFAULT_LOCK_DIR", tmp_path / "afile" / "locks")
        with pytest.raises(SystemExit, match="Cannot create bridge lock dir"):
            bridge._acquire_singleton_locks(BridgeConfig(wake_dir=tmp_path / "wake"))

    def test_symlinked_singleton_lock_is_a_named_exit(self, tmp_path):
        """The os.open arm of _flock_or_exit: a symlinked singleton lock FILE
        must fail O_NOFOLLOW (ELOOP) into the named 'Cannot open bridge lock'
        message, and the link target must never be created through it. Driven
        on the wake-dir lock, reachable without touching DEFAULT_LOCK_DIR."""
        wake = tmp_path / "wake"
        wake.mkdir()
        victim = tmp_path / "victim"
        (wake / "bridge.singleton.lock").symlink_to(victim)
        with pytest.raises(SystemExit, match="Cannot open bridge lock"):
            bridge._acquire_singleton_locks(BridgeConfig(wake_dir=wake))
        assert not victim.exists()  # O_NOFOLLOW: never created through the link

    def test_main_refuses_and_never_sweeps_when_singleton_held(self, monkeypatch, tmp_path):
        """Pins the acquisition AND its ordering: with the lock already held,
        main() must SystemExit before uvicorn.run runs the lifespan (the
        destructive sweep). So neither uvicorn.run nor register_with_bus may
        be called. Deleting the _acquire call, or moving it after
        uvicorn.run, fails this."""
        import sys

        import uvicorn

        wake = tmp_path / "wake"
        config = BridgeConfig(wake_dir=wake)
        # Injector creates the wake dir; do it up front so the held lock and
        # main()'s lock target the same file
        Injector(config)
        held = bridge._acquire_singleton_locks(config)
        ran = []
        try:
            monkeypatch.setattr(sys, "argv", ["agent-event-bus-bridge", "--wake-dir", str(wake)])
            with patch.object(uvicorn, "run", lambda *a, **k: ran.append("uvicorn")):
                with patch.object(
                    bridge, "register_with_bus", lambda cfg: ran.append("register") or 1
                ):
                    with pytest.raises(SystemExit, match="already"):
                        bridge.main()
        finally:
            for fd in held:
                os.close(fd)
        assert ran == []  # neither the bind nor the sweep ran
