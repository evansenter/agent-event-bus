"""Webhook-to-injection bridge: re-awaken idle Claude Code sessions (RFC #122).

EXPERIMENTAL prototype. The bus has a push subsystem (webhooks) that nothing
consumes, while delivery to sessions is pull-only - a DM to an idle session
sits unread until its human happens to prompt. This daemon closes the loop:

    publish_event(channel="session:X", ...)
        -> bus webhook POST -> bridge (localhost)
        -> filter to actionable signal
        -> inject a wake-up for session X

Injection backends:
  spool  (default) append the event as a JSON line to
         <wake_dir>/<session_id>.jsonl - portable; a Stop/UserPromptSubmit
         hook can drain the spool at its next opportunity
  tmux   `tmux send-keys` a wake prompt into the session's pane, then fall
         back to spool when no pane mapping exists. Pane mappings are read
         from <wake_dir>/panes.json ({session_id: pane_id}), which something
         session-side (e.g. a SessionStart hook publishing $TMUX_PANE) must
         maintain.

Loop prevention: in the tmux backend a per-session cooldown (default 30s)
bounds injections; an event that arrives during cooldown is spooled instead,
so nothing is lost. In the default spool backend the cooldown never engages -
a spool line only becomes a wake when the drain hook acts on it, so bounding
that belongs to the drain hook.

Run:  agent-event-bus-bridge [--backend tmux] [--port 8082] ...
The bridge registers its own webhook on the bus at startup (HMAC-signed when
AGENT_EVENT_BUS_BRIDGE_SECRET is set) and unregisters it on clean shutdown.
"""

import argparse
import fcntl
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

# Explicit submodule import: `import anyio` alone doesn't bind to_thread -
# it currently resolves only through starlette's own imports. Matches
# server.py and middleware.py.
import anyio.to_thread
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_event_bus.cli import DEFAULT_URL, call_tool

logger = logging.getLogger("agent-event-bus-bridge")

DEFAULT_BRIDGE_PORT = 8082
DEFAULT_COOLDOWN_SECONDS = 30.0
DEFAULT_WAKE_DIR = Path.home() / ".claude" / "contrib" / "agent-event-bus" / "wake"

# tmux send-keys is bounded like the notifier subprocesses in helpers.py -
# a hung tmux must not wedge the bridge. Sized under the bus's
# WEBHOOK_TIMEOUT (5s per attempt, server.py): the bus's clock starts
# before ours, so a bound at or above its timeout means the bus never
# sees the response this bound exists to produce - just a timeout plus
# retries (and a duplicate spool line per retry).
TMUX_TIMEOUT = 2.0

# Bound on joining the registration thread at shutdown. register_with_bus
# makes 2 + N sequential call_tool requests (list, one unregister per
# stale row, register), each under the CLI's 10s timeout - 35s covers the
# common N <= 1 case. A longer sweep (several unclean exits) or a slower
# bus can still leak a row; the startup dedupe reclaims it.
REGISTRATION_JOIN_TIMEOUT = 35.0

# Real bus payloads are a few KB; the body must be read before the HMAC can
# be checked, so bound what an unauthenticated peer can make us buffer
MAX_BODY_BYTES = 1_048_576  # 1 MiB

# Bound the wait for the per-session spool flock. The drain contract keeps
# the drainer's hold down to a couple of renames, so seconds of contention
# means a stuck drainer (SIGSTOP, network mount) - and an unbounded block
# here parks a threadpool worker per pending event until /hook starves.
# Raising after the deadline is correct: nothing is durably stored yet, so
# the bus retry is meaningful (unlike post-spool errors). Like TMUX_TIMEOUT,
# the deadline (attempts x retry) must stay under the bus's WEBHOOK_TIMEOUT
# (5s per attempt) - and their SUM must too, or a slow-lock-then-slow-tmux
# request times out bus-side even though each bound individually held.
SPOOL_LOCK_ATTEMPTS = 20
SPOOL_LOCK_RETRY_SECONDS = 0.1

# Must stay a fixed multi-word constant: the send-keys call passes it as
# one argument WITHOUT -l, so tmux would interpret a value matching a key
# name (Enter, C-c, Escape) as a keystroke instead of typing it. If this
# ever becomes configurable or carries payload text, switch the call to
# `send-keys -l` for the text plus a separate `send-keys Enter`.
WAKE_PROMPT = "Check the event bus - a directed event arrived for this session."


@dataclass
class BridgeConfig:
    """Runtime configuration for the bridge daemon."""

    bus_url: str = DEFAULT_URL
    port: int = DEFAULT_BRIDGE_PORT
    backend: str = "spool"  # "spool" | "tmux"
    secret: str | None = None
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    wake_dir: Path = field(default_factory=lambda: DEFAULT_WAKE_DIR)
    # URL the bus POSTs to; None means loopback (bus on this machine)
    hook_url: str | None = None
    # Interface to bind; None derives it from the hook URL (see bind_host)
    bind: str | None = None


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Check the bus's X-Event-Bus-Signature HMAC against the raw body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII
    # input, and Starlette decodes headers as latin-1 - a hostile header
    # byte above 0x7f must be a 401, not a 500. bytes keeps constant-time.
    return hmac.compare_digest(
        f"sha256={expected}".encode(),
        signature_header.encode("latin-1", errors="replace"),
    )


# Session ids are UUIDs (36 chars) or display ids ("brave-trex"); nothing
# else. The channel string is publisher-controlled wire input and the id
# becomes a spool filename, so path separators, "..", and any other
# unexpected byte must never reach the filesystem - the bus warns on
# malformed channels but does not reject them. The length bound keeps a
# too-long name from turning into an unretryable OSError out of the spool
# open (and caps spool-file blast radius).
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

# The unsafe-id rejection below is publisher-drivable (the bus warns on
# malformed channels but never rejects them), so a per-event WARNING would
# let any publisher choose how much the bridge writes to its log. Warn on
# the first sighting of each distinct channel string, debug the rest.
# Cleared at a cap rather than LRU-evicted: past that many distinct garbage
# channels, one repeat warning per channel is the lesser noise. Unlocked -
# a rare race just duplicates a warning.
_warned_unsafe_channels: set[str] = set()
_WARNED_UNSAFE_CHANNELS_CAP = 64


def resolve_target_session(event: dict) -> str | None:
    """Which session should this event wake?

    v1 wakes only for direct messages (session:<id> channels) - the
    unambiguous "aimed at exactly this session" case. Broadcast actionable
    events (help_needed on a repo channel, ...) have no single target and
    are left for normal polling.
    """
    # channel is a field inside a wire-supplied object - a non-string value
    # (123, true, a list) must resolve to no target, not raise .startswith
    # into a 500 with bus retries behind it
    channel = event.get("channel")
    if not isinstance(channel, str) or not channel.startswith("session:"):
        return None
    target = channel.split(":", 1)[1]
    if not SESSION_ID_PATTERN.fullmatch(target):
        if channel in _warned_unsafe_channels:
            logger.debug(f"Ignoring event with unsafe session id in channel {channel!r}")
        else:
            if len(_warned_unsafe_channels) >= _WARNED_UNSAFE_CHANNELS_CAP:
                _warned_unsafe_channels.clear()
            _warned_unsafe_channels.add(channel)
            logger.warning(f"Ignoring event with unsafe session id in channel {channel!r}")
        return None
    return target


class Injector:
    """Delivers wake-ups, bounded by a per-session cooldown."""

    def __init__(self, config: BridgeConfig):
        self.config = config
        self._lock = threading.Lock()
        self._last_wake: dict[str, float] = {}
        # Once at construction, not per delivery: keeps the lock hold to the
        # append itself, and a deliberate later permission change by the
        # operator isn't silently reverted on the next event. The dir is
        # private (0o700) - spool files carry full event payloads.
        try:
            self.config.wake_dir.mkdir(parents=True, exist_ok=True)
            self.config.wake_dir.chmod(0o700)
        except OSError as e:
            # The one startup filesystem precondition: a named config error
            # like every other operator-facing input, not a bare traceback
            raise SystemExit(
                f"Cannot prepare wake dir {self.config.wake_dir} "
                f"(check --wake-dir / AGENT_EVENT_BUS_WAKE_DIR): {e}"
            ) from None

    def deliver(self, session_id: str, event: dict) -> str:
        """Wake session_id for event. Returns the action taken: "tmux",
        "spool" (spool backend working as designed), "spool-cooldown",
        "spool-unmapped" (tmux backend, no pane mapping - the NORMAL outcome
        for a session on another machine, since webhooks have no machine
        scoping), or "spool-tmux-failed" (tmux backend, the send-keys
        attempt itself failed - the arm that means tmux on this box is
        broken). The distinction rides the 200 response's `action` field,
        the one in-band signal, since the unmapped arm logs only at debug.

        The cooldown bounds successful injections only: spool writes are
        durable bookkeeping, not wakes, and a failed tmux attempt must not
        burn the window - the next event should retry (e.g. after a
        SessionStart hook repairs the pane mapping).
        """
        # The per-session flock inside _spool serializes appends on its own
        # (flock contends per open file description, in-process included),
        # and it can block on an external drainer holding the drain lock -
        # so it must NOT run under the global lock, or one stalled drain
        # would stall every session's delivery
        self._spool(session_id, event)

        if self.config.backend != "tmux":
            return "spool"

        # Pane lookup BEFORE the cooldown machinery: on a multi-machine bus
        # the unmapped arm is the normal outcome, not a failure, so there is
        # no reservation to take or roll back. Debug, not info: per-event
        # INFO on an arm every foreign-machine DM lands on is permanent
        # noise - matches the below-actionable filter arm and _tmux_pane's
        # silent missing-file case.
        pane = self._tmux_pane(session_id)
        if pane is None:
            logger.debug(f"No tmux pane mapping for {session_id[:8]}...; spooled only")
            return "spool-unmapped"

        with self._lock:
            now = time.monotonic()
            # Housekeeping: entries past the cooldown can never gate a wake
            # again, and sessions are ephemeral - don't retain them forever
            self._last_wake = {
                sid: ts
                for sid, ts in self._last_wake.items()
                if (now - ts) < self.config.cooldown_seconds
            }
            last = self._last_wake.get(session_id)
            if last is not None:
                return "spool-cooldown"
            # Reserve the window before releasing the lock: two concurrent
            # deliveries for the same session must not both pass the check
            # and double-inject
            self._last_wake[session_id] = now

        # tmux runs outside the lock (bounded at TMUX_TIMEOUT, but other
        # sessions' deliveries shouldn't wait on it). The pane can go stale
        # between the lookup above and here - send-keys just fails, which is
        # the arm below.
        if self._tmux_wake(session_id, pane):
            return "tmux"
        with self._lock:
            # Roll back the reservation so the failure doesn't burn the
            # window - unless a later delivery already re-claimed it. There
            # is no previous value to restore: any entry that survived the
            # prune returned spool-cooldown above.
            if self._last_wake.get(session_id) == now:
                del self._last_wake[session_id]
        return "spool-tmux-failed"

    def _spool(self, session_id: str, event: dict) -> None:
        """Always-on durable path: one JSON line per event, per session.

        Serialized by the per-session flock below - against other threads in
        this process and against the drain hook alike.
        """
        spool_file = self.config.wake_dir / f"{session_id}.jsonl"
        # Defense in depth behind resolve_target_session's charset check: a
        # traversal or absolute component in a wire-supplied id must never
        # produce a write outside the wake dir
        if spool_file.resolve().parent != self.config.wake_dir.resolve():
            raise ValueError(f"Spool path escapes wake dir: {session_id!r}")
        # flock a sibling lock file around the append: flock contends per
        # open file description, so it serializes writers in this process and
        # the drain hook (another process) alike - without it, the drainer's
        # rename could slip between our open and our flush and the line would
        # land in a file the drainer already read (and will delete)
        lock_file = self.config.wake_dir / f"{session_id}.lock"
        with lock_file.open("a") as lock_fd:
            for _ in range(SPOOL_LOCK_ATTEMPTS):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(SPOOL_LOCK_RETRY_SECONDS)
            else:
                raise OSError(
                    f"Could not lock spool for {session_id} within "
                    f"{SPOOL_LOCK_ATTEMPTS * SPOOL_LOCK_RETRY_SECONDS:.0f}s "
                    "(stuck drainer?)"
                )
            try:
                with spool_file.open("a") as f:
                    f.write(json.dumps(event) + "\n")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _tmux_pane(self, session_id: str) -> str | None:
        """Look up the session's tmux pane from <wake_dir>/panes.json.

        The mapping is maintained by an external session-side component, so
        every failure shape - unreadable file, non-UTF-8 bytes from a torn
        write, valid JSON that isn't an object - must degrade to "unmapped":
        an exception escaping here would 500 the webhook and make the bus
        retry an already-spooled event.
        """
        panes_file = self.config.wake_dir / "panes.json"
        try:
            panes = json.loads(panes_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(f"Unreadable panes.json ({e}); treating as unmapped")
            return None
        if not isinstance(panes, dict):
            logger.warning("panes.json is not an object; treating as unmapped")
            return None
        pane = panes.get(session_id)
        return pane if isinstance(pane, str) and pane else None

    def _tmux_wake(self, session_id: str, pane: str) -> bool:
        """Type the wake prompt into the given pane. False on failure."""
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, WAKE_PROMPT, "Enter"],
                check=True,
                capture_output=True,
                timeout=TMUX_TIMEOUT,
            )
            logger.info(f"Woke {session_id[:8]}... via tmux pane {pane}")
            return True
        except (subprocess.SubprocessError, OSError) as e:
            # SubprocessError covers CalledProcessError/TimeoutExpired;
            # OSError covers a missing or non-executable tmux binary. An
            # exception escaping here would 500 the webhook and make the bus
            # retry an event that is already durably spooled (duplicate lines)
            logger.warning(f"tmux wake failed for {session_id[:8]}... ({e}); spooled only")
            return False


def create_bridge_app(
    config: BridgeConfig,
    registration_state: dict | None = None,
    registration_stop: threading.Event | None = None,
) -> Starlette:
    """Build the ASGI app: POST /hook (webhook receiver) + GET /health.

    When registration_state/registration_stop are given, the app's lifespan
    owns bus registration end to end: startup launches register_with_retry
    in a background thread (so the listener binds first and a down bus can't
    kill the daemon), and shutdown stops it, joins - stop can't interrupt a
    register call already inside its HTTP POST, so without the join a clean
    exit could read webhook_id before the thread writes it and leak the row -
    and unregisters the committed id. The app created the row, so the app
    removes it; any embedding (uvicorn --factory, an ASGI mount) gets clean
    shutdown without needing a main()-style finally of its own.
    """
    injector = Injector(config)
    registration_thread: threading.Thread | None = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal registration_thread
        if registration_state is not None and registration_stop is not None:
            registration_thread = threading.Thread(
                target=register_with_retry,
                args=(config, registration_state, registration_stop),
                daemon=True,
            )
            registration_thread.start()
        # try/finally: a cancelled or erroring lifespan (forced shutdown,
        # timeout_graceful_shutdown) is thrown in AT the yield - without the
        # guard the stop-and-join is skipped and the row leaks exactly the
        # way the join was added to prevent
        try:
            yield
        finally:
            if registration_thread is not None:
                registration_stop.set()
                # Off the event loop: a join that waits on an in-flight
                # call_tool POST would otherwise freeze signal handling for
                # up to the timeout (#112 invariant, shutdown edition).
                # Shielded because run_sync is itself a cancellation
                # checkpoint - and this finally exists precisely for the
                # cancelled-shutdown path; unshielded, the join would be
                # skipped exactly when it matters
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(
                        functools.partial(registration_thread.join, REGISTRATION_JOIN_TIMEOUT)
                    )
                    # pop, not get: hand the id off exactly once, so main()'s
                    # belt-and-braces finally (for exits that skip the
                    # lifespan) can't double-unregister. Same off-loop
                    # treatment as the join - unregister_from_bus is a
                    # blocking call_tool POST - inside the same shield.
                    webhook_id = registration_state.pop("webhook_id", None)
                    if webhook_id is not None:
                        await anyio.to_thread.run_sync(
                            functools.partial(unregister_from_bus, config, webhook_id)
                        )

    def process(body: bytes, signature: str | None) -> tuple[dict, int]:
        """Sync webhook handling (runs in a worker thread - the file appends
        and tmux subprocess must stay off the event loop, same invariant as
        the bus server's #112 fix)."""
        if config.secret and not verify_signature(body, signature, config.secret):
            return {"error": "bad signature"}, 401

        try:
            event = json.loads(body)
        # ValueError, not just JSONDecodeError: json.loads(bytes) DECODES
        # before parsing, and invalid UTF-8 raises UnicodeDecodeError (a
        # ValueError but not a JSONDecodeError). The bus retries any >=400
        # identically, so a 400 buys a clean named error in both logs
        # instead of a traceback per attempt - not fewer retries.
        except ValueError:
            return {"error": "invalid JSON"}, 400
        if not isinstance(event, dict):
            # Valid JSON that isn't an object (123, [], "x"): a named 400,
            # not an AttributeError traceback (retried by the bus either
            # way) - this endpoint is network reachable, more so once it
            # binds beyond loopback
            return {"error": "expected a JSON object"}, 400

        # The bus always sends the derived signal_level; only actionable
        # events (DMs, help_needed, blockers, CI failures) justify a wake
        level = event.get("signal_level")
        if level != "actionable":
            if level is None:
                # A bus predating derived levels (#129) sends none at all -
                # every delivery would land here forever, so make the
                # version skew visible instead of filtering silently
                logger.info(
                    f"Event {event.get('event_id')} carries no signal_level "
                    "(bus predates derived levels?); filtering it"
                )
            else:
                logger.debug(f"Ignoring event {event.get('event_id')}: level {level!r}")
            return {"status": "ignored", "reason": "below actionable"}, 200

        target = resolve_target_session(event)
        if target is None:
            logger.debug(
                f"Ignoring event {event.get('event_id')}: "
                f"channel {event.get('channel')!r} has no target session"
            )
            return {"status": "ignored", "reason": "no target session"}, 200

        action = injector.deliver(target, event)
        return {"status": "delivered", "action": action, "session_id": target}, 200

    async def hook_endpoint(request: Request) -> JSONResponse:
        # Precheck the honest case cheaply; the streamed count below covers
        # a missing or lying content-length (e.g. chunked encoding)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "body too large"}, status_code=413)
        # Stream with a running count, not request.body(): body() concatenates
        # every chunk unconditionally, so a chunked POST (no content-length to
        # precheck) could make the daemon buffer arbitrarily many bytes before
        # a post-read check ever ran - the bound must hold WHILE reading
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                return JSONResponse({"error": "body too large"}, status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        signature = request.headers.get("x-event-bus-signature")
        payload, status = await run_in_threadpool(process, body, signature)
        return JSONResponse(payload, status_code=status)

    async def health(request: Request) -> JSONResponse:
        payload = {"status": "ok", "service": "agent-event-bus-bridge"}
        if registration_state is not None:
            # Registration is deliberately non-fatal, so this is the one
            # place an operator (or a supervisor readiness check) can tell
            # "working" from "listening but never registered"
            payload["registered"] = registration_state.get("webhook_id") is not None
        return JSONResponse(payload)

    app = Starlette(
        routes=[
            Route("/hook", hook_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    # POST /hook/ must be a loud 404 (bus retries and logs it), not a 307:
    # the bus's httpx client doesn't follow redirects and counts any status
    # under 400 as delivered, so the default slash-redirect would make a
    # trailing-slash hook URL read as a perfectly healthy webhook while the
    # bridge processes nothing.
    app.router.redirect_slashes = False
    return app


def bridge_hook_url(config: BridgeConfig) -> str:
    """The URL the bus POSTs to. Loopback by default (bus on this machine);
    --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL overrides it with an
    address the bus host can reach when the bus is remote."""
    return config.hook_url or f"http://127.0.0.1:{config.port}/hook"


LOOPBACK_HOSTS = {"localhost"}


def _is_host_loopback(host: str) -> bool:
    """Loopback is all of 127.0.0.0/8 plus ::1 (Debian/Ubuntu resolve the
    machine's own hostname to 127.0.1.1), not just the literal 127.0.0.1."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in LOOPBACK_HOSTS


def _is_host_wildcard(host: str) -> bool:
    """0.0.0.0 / :: are unspecified, not loopback, to ipaddress - but a
    wildcard bind listens on every interface INCLUDING loopback, so a
    loopback hook URL is perfectly reachable under one. Exempt them from
    the advertises-loopback mismatch (they still count as exposed for the
    secret requirement - that part is genuinely true)."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    return True if host is None else _is_host_loopback(host)


def bind_host(config: BridgeConfig) -> str:
    """Which interface the listener binds: --bind wins, else loopback for a
    loopback hook URL (the local bus is the sole caller), else all
    interfaces. config_from_args requires the HMAC secret whenever the
    effective bind OR the hook URL is non-loopback."""
    if config.bind:
        return config.bind
    if _is_loopback(bridge_hook_url(config)):
        return "127.0.0.1"
    return "0.0.0.0"  # noqa: S104 - deliberate; secret enforced at config time


def register_with_bus(config: BridgeConfig) -> int:
    """Register this bridge's webhook on the bus. Returns webhook_id;
    raises SystemExit on any failure (bus unreachable, or no id returned).

    Idempotent: an unclean exit (SIGKILL, crash, reboot) skips main()'s
    finally, leaving a stale active webhook at this URL - and the bus neither
    dedupes by URL nor deactivates failing hooks, so each stale row would
    duplicate every wake. Remove matching URLs before registering.
    """
    hook_url = bridge_hook_url(config)

    existing = call_tool("list_webhooks", {"active_only": True}, url=config.bus_url)
    if not isinstance(existing, list):
        # Proceeding without the dedupe would stack the duplicate deliveries
        # this sweep exists to prevent - retryable failure, like the no-id
        # case below
        raise SystemExit(f"list_webhooks returned unexpected result: {existing!r}")
    for wh in existing:
        # Guard the element shape too: an AttributeError here would
        # escape register_with_retry and kill the registration thread
        if not isinstance(wh, dict):
            continue
        if wh.get("url") == hook_url and wh.get("webhook_id") is not None:
            removal = call_tool(
                "unregister_webhook", {"webhook_id": wh["webhook_id"]}, url=config.bus_url
            )
            # unregister_webhook reports logical failure in-band (a
            # success-False dict) rather than raising, and call_tool only
            # raises on transport errors - proceeding after a failed removal
            # would re-create the duplicate deliveries the sweep exists to
            # prevent. Already-gone ("Webhook not found") is the goal state.
            ok = isinstance(removal, dict) and (
                removal.get("success") or removal.get("error") == "Webhook not found"
            )
            if not ok:
                raise SystemExit(
                    f"unregister_webhook #{wh['webhook_id']} returned "
                    f"unexpected result: {removal!r}"
                )
            logger.info(f"Removed stale bridge webhook #{wh['webhook_id']}")

    result = call_tool(
        "register_webhook",
        {
            "url": hook_url,
            # v1 acts only on session:<id> DMs, so let the bus drop broadcast
            # traffic server-side (channel filters prefix-match). When v2
            # widens to broadcast actionable events, remove this filter - the
            # bridge still filters on signal_level locally either way.
            "channel": "session:",
            **({"secret": config.secret} if config.secret else {}),
        },
        url=config.bus_url,
    )
    webhook_id = result.get("webhook_id") if isinstance(result, dict) else None
    if webhook_id is None:
        # A bus that answers but returns no id must be a retryable failure,
        # not silent success - register_with_retry treats SystemExit as retry
        raise SystemExit(f"register_webhook returned no webhook_id: {result!r}")
    logger.info(f"Registered bridge webhook #{webhook_id} on {config.bus_url}")
    return webhook_id


def unregister_from_bus(config: BridgeConfig, webhook_id: int) -> None:
    """Best-effort webhook cleanup on shutdown."""
    try:
        result = call_tool("unregister_webhook", {"webhook_id": webhook_id}, url=config.bus_url)
    except SystemExit:
        # call_tool exits on connection errors; shutdown must not care
        logger.warning(f"Could not unregister webhook #{webhook_id} (bus unreachable)")
        return
    # Same accept-shape as the startup sweep: the bus reports logical
    # failure in-band (success-False dict), and already-gone is the goal
    # state. Shutdown stays best-effort, so a surprise is a warning here
    # rather than the sweep's retryable SystemExit - but the log must not
    # assert a removal it never checked; the next startup sweep reclaims.
    ok = isinstance(result, dict) and (
        result.get("success") or result.get("error") == "Webhook not found"
    )
    if ok:
        logger.info(f"Unregistered bridge webhook #{webhook_id}")
    else:
        logger.warning(f"unregister_webhook #{webhook_id} returned unexpected result: {result!r}")


def register_with_retry(
    config: BridgeConfig,
    state: dict,
    stop: threading.Event,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Keep trying to register until the bus is reachable (or stop is set).

    The bridge and the bus are typically launched by the same supervisor, so
    "bus not up yet" is the normal case at boot - call_tool's SystemExit on
    connection errors must not kill the daemon. Runs in a background thread
    started from the app's startup hook, so the listener binds (essentially)
    first and webhook deliveries have a live port to hit.
    """
    delay = initial_delay
    while not stop.is_set():
        try:
            state["webhook_id"] = register_with_bus(config)
            return
        # SystemExit is call_tool's failure shape; Exception covers anything
        # unexpected inside register_with_bus - either way the thread must
        # back off and retry, never die silently
        except (SystemExit, Exception) as e:
            logger.warning(
                f"Registration on {config.bus_url} failed ({e!r}); retrying in {delay:.0f}s"
            )
        if stop.wait(delay):
            return
        delay = min(delay * 2, max_delay)


def _to_number(value, cast, flag: str, env: str):
    """Cast a CLI/env-supplied numeric value at CONFIG time, not at
    parser-build time - a typo in the env var must not break --help (the
    first thing an operator reaches for when the daemon refuses to start)."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SystemExit(
            f"Invalid value {value!r} (check {flag} / {env}): expected a number"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agent-event-bus re-awakening bridge (RFC #122)")
    parser.add_argument(
        "--bus-url",
        # `or`, matching every sibling: an accidentally empty export must
        # fall back to the default, not become a refused empty URL
        default=os.environ.get("AGENT_EVENT_BUS_URL") or DEFAULT_URL,
        help="Bus MCP endpoint to register the webhook on",
    )
    parser.add_argument(
        "--port",
        # Raw string default, cast in config_from_args - see _to_number
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_PORT") or str(DEFAULT_BRIDGE_PORT),
        help=f"Localhost port to listen on (default: {DEFAULT_BRIDGE_PORT})",
    )
    parser.add_argument(
        "--backend",
        choices=["spool", "tmux"],
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_BACKEND") or "spool",
        help="Wake mechanism: spool file only, or tmux send-keys + spool",
    )
    parser.add_argument(
        "--cooldown",
        # Raw string default, cast in config_from_args - see _to_number
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_COOLDOWN") or str(DEFAULT_COOLDOWN_SECONDS),
        help="Minimum seconds between tmux wakes per session (tmux backend "
        "only; the spool backend's loop prevention belongs to the drain hook)",
    )
    parser.add_argument(
        "--wake-dir",
        type=Path,
        # `or`: an empty env var would make Path("") == cwd - the bridge
        # would chmod and spool into whatever directory launched it
        default=Path(os.environ.get("AGENT_EVENT_BUS_WAKE_DIR") or str(DEFAULT_WAKE_DIR)),
        help="Directory for spool files and panes.json",
    )
    parser.add_argument(
        "--hook-url",
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_HOOK_URL") or None,
        help="URL the bus POSTs events to (default: http://127.0.0.1:<port>/hook; "
        "must be an address the bus host can reach when the bus is remote)",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_BIND") or None,
        help="Interface to bind (default: 127.0.0.1 for a loopback hook URL, "
        "0.0.0.0 otherwise; pin e.g. your tailnet address to narrow exposure)",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    """Build the runtime config from parsed args plus environment."""
    # argparse enforces `choices` only for values given on the command line -
    # an env-supplied default bypasses the check, and an unknown backend would
    # otherwise silently mean "spool" (deliver only tests != "tmux")
    if args.backend not in ("spool", "tmux"):
        raise SystemExit(
            f"Invalid backend {args.backend!r} (check AGENT_EVENT_BUS_BRIDGE_BACKEND): "
            "expected 'spool' or 'tmux'"
        )
    config = BridgeConfig(
        bus_url=args.bus_url,
        port=_to_number(args.port, int, "--port", "AGENT_EVENT_BUS_BRIDGE_PORT"),
        backend=args.backend,
        # `or None`: an accidentally empty env var must not put registration
        # (which would skip the secret, so unsigned payloads) and verification
        # (which would demand signatures) on opposite sides - that 401s every
        # delivery silently
        secret=os.environ.get("AGENT_EVENT_BUS_BRIDGE_SECRET") or None,
        cooldown_seconds=_to_number(
            args.cooldown, float, "--cooldown", "AGENT_EVENT_BUS_BRIDGE_COOLDOWN"
        ),
        wake_dir=args.wake_dir,
        hook_url=args.hook_url,
        bind=args.bind,
    )
    # Range checks cover CLI and env alike: an out-of-range port would be a
    # uvicorn traceback naming neither, and a negative cooldown would
    # silently disable the cooldown (now - ts < -5 is never true)
    if not (1 <= config.port <= 65535):
        raise SystemExit(
            f"Invalid port {config.port} (check --port / AGENT_EVENT_BUS_BRIDGE_PORT): "
            "expected 1-65535"
        )
    # isfinite too: nan makes every prune comparison False (cooldown never
    # engages), inf makes it always True (one wake ever, then silence)
    if not math.isfinite(config.cooldown_seconds) or config.cooldown_seconds < 0:
        raise SystemExit(
            f"Invalid cooldown {config.cooldown_seconds} "
            "(check --cooldown / AGENT_EVENT_BUS_BRIDGE_COOLDOWN): "
            "must be finite and >= 0"
        )
    # A daemon's cwd is whatever its supervisor hands it - a relative wake
    # dir would silently relocate the durable path (and its chmod)
    if not config.wake_dir.is_absolute():
        raise SystemExit(
            f"Invalid wake dir {config.wake_dir} "
            "(check --wake-dir / AGENT_EVENT_BUS_WAKE_DIR): must be an absolute path"
        )
    # A scheme-less bus URL ("bus.example:8080/mcp") or hostless one
    # ("http:///mcp") parses with no hostname and would read as loopback,
    # skipping the topology guard below - catch the misconfiguration here
    # instead of in a later connection error
    parsed_bus = urllib.parse.urlsplit(config.bus_url)
    if parsed_bus.scheme not in ("http", "https") or not parsed_bus.hostname:
        raise SystemExit(f"Invalid bus URL {config.bus_url!r}: expected http(s)://host[:port]/path")
    # Same check for the hook URL - it is what BOTH topology guards below
    # read: a scheme-less value parses to hostname None, reads as loopback,
    # skips the guards, and registers a URL the bus can never POST to
    if config.hook_url is not None:
        parsed_hook = urllib.parse.urlsplit(config.hook_url)
        # Require a hostname too: "http:///hook" parses to hostname None,
        # which reads as loopback and would skip every topology guard
        if parsed_hook.scheme not in ("http", "https") or not parsed_hook.hostname:
            raise SystemExit(
                f"Invalid hook URL {config.hook_url!r} "
                "(check --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL): "
                "expected http(s)://host[:port]/path"
            )
    # A loopback hook URL registered on a remote bus makes the bus POST to
    # itself: registration succeeds, /health is green, nothing is ever
    # delivered - and every machine would claim the same URL string, so the
    # startup dedupe could remove a live webhook belonging to the bus host.
    # Refuse the combination instead of failing silently.
    if not _is_loopback(config.bus_url) and _is_loopback(bridge_hook_url(config)):
        raise SystemExit(
            f"Bus at {config.bus_url} is remote but the advertised hook URL "
            f"{bridge_hook_url(config)} is loopback - the bus would POST to itself. "
            "Set --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL to an address the bus "
            "host can reach (and set AGENT_EVENT_BUS_BRIDGE_SECRET). If the bus is "
            "on THIS machine, address it as 127.0.0.1 or localhost - a hostname "
            "that resolves to 127.0.1.1 reads as remote."
        )
    # --bind must be an address uvicorn can actually bind - a typo would
    # otherwise surface as a bare socket.gaierror naming neither the flag
    # nor the env var
    if config.bind is not None:
        try:
            ipaddress.ip_address(config.bind)
        except ValueError:
            if config.bind not in LOOPBACK_HOSTS:
                raise SystemExit(
                    f"Invalid bind address {config.bind!r} "
                    "(check --bind / AGENT_EVENT_BUS_BRIDGE_BIND): expected an IP address"
                ) from None
    # The HMAC is the only authentication once the endpoint is reachable
    # off-box, and exposure is decided by the EFFECTIVE bind (bind_host
    # consults --bind first) as well as the hook URL: a non-loopback bind
    # with no secret would let anyone who reaches the port append
    # attacker-authored lines to a session spool, and a non-loopback hook
    # URL means the hop exists even behind a local TLS terminator. Hard
    # requirement either way - the bus itself defaults to auth-required,
    # and the bridge must not invert that.
    exposed = not _is_host_loopback(bind_host(config)) or not _is_loopback(bridge_hook_url(config))
    if exposed and not config.secret:
        raise SystemExit(
            f"Listener would bind {bind_host(config)} with hook URL "
            f"{bridge_hook_url(config)} - reachable off-box, so set "
            "AGENT_EVENT_BUS_BRIDGE_SECRET (the HMAC signature is the only "
            "authentication on this hop). Check --bind / AGENT_EVENT_BUS_BRIDGE_BIND "
            "and --hook-url."
        )
    # The inverse mismatch is silent inertness, not exposure: a loopback
    # bind under a reachable hook URL means the bus's POSTs are refused at
    # the TCP level. Legitimate behind a same-box TLS terminator, so warn
    # rather than refuse - same policy as the port mismatch below.
    if _is_host_loopback(bind_host(config)) and not _is_loopback(bridge_hook_url(config)):
        logger.warning(
            f"Hook URL {bridge_hook_url(config)} is reachable but the listener binds "
            f"{bind_host(config)} (loopback) - correct only if something forwards between them"
        )
    # The bus POSTs to the hook URL's port while the listener binds --port;
    # a mismatch is legitimate behind a reverse proxy, but name it - it's
    # otherwise the same silent-inertness failure the guards above close.
    # SplitResult.port raises for a malformed port - keep that a named
    # config error like every other input.
    hook_split = urllib.parse.urlsplit(bridge_hook_url(config))
    try:
        hook_port = hook_split.port
    except ValueError:
        raise SystemExit(
            f"Invalid hook URL {bridge_hook_url(config)!r} "
            "(check --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL): bad port"
        ) from None
    if hook_port is None:
        # A missing port is not "no opinion" - the bus POSTs to the scheme
        # default, so a forgotten :8082 is exactly the mismatch this warning
        # names (https behind a TLS terminator forwarding to --port is the
        # legitimate case, same as the other mismatches)
        hook_port = 443 if hook_split.scheme == "https" else 80
    if hook_port != config.port:
        logger.warning(
            f"Hook URL advertises port {hook_port} but the listener binds {config.port} - "
            "correct only if something forwards between them"
        )
    # POST /hook is the only route this listener serves; any other
    # advertised path 404s every dispatch. Legitimate behind a rewriting
    # proxy, so warn-don't-refuse like the port mismatch above.
    hook_path = urllib.parse.urlsplit(bridge_hook_url(config)).path
    if hook_path != "/hook":
        logger.warning(
            f"Hook URL path {hook_path!r} isn't /hook (the only route this "
            "listener serves) - correct only if something rewrites between them"
        )
    # Fourth quadrant: a PINNED non-loopback bind under a loopback hook URL
    # means the bridge advertises 127.0.0.1 while nothing listens there -
    # every dispatch is refused at TCP. Wildcard binds are exempt: 0.0.0.0
    # and :: listen on loopback too, so that combination works exactly as
    # advertised. Same warn-don't-refuse policy as above.
    if (
        not _is_host_loopback(bind_host(config))
        and not _is_host_wildcard(bind_host(config))
        and _is_loopback(bridge_hook_url(config))
    ):
        logger.warning(
            f"Listener binds {bind_host(config)} but the hook URL "
            f"{bridge_hook_url(config)} advertises loopback - the bus can't reach it; "
            "set --hook-url to an address on the bound interface"
        )
    # Address-family mismatch: 0.0.0.0 binds IPv4 only, so an IPv6 hook
    # literal (a Tailscale IPv6 address, say) is refused at TCP while every
    # quadrant warning above stays quiet - both sides can be non-loopback
    # with port and path agreeing. Hostname hooks are exempt: DNS/MagicDNS
    # publishes A records too and happy-eyeballs falls back to v4. Hostname
    # binds ("localhost") are exempt the other way - the resolver decides
    # their family.
    try:
        hook_is_v6 = (
            ipaddress.ip_address(urllib.parse.urlsplit(bridge_hook_url(config)).hostname).version
            == 6
        )
    except ValueError:
        hook_is_v6 = False
    try:
        bind_is_v4 = ipaddress.ip_address(bind_host(config)).version == 4
    except ValueError:
        bind_is_v4 = False
    if hook_is_v6 and bind_is_v4:
        logger.warning(
            f"Hook URL host is an IPv6 address but the listener binds "
            f"{bind_host(config)} (IPv4 only) - the bus can't reach it; "
            "set --bind to '::' (dual-stack) or an IPv6 address"
        )
    return config


def main():
    """Run the bridge daemon."""
    import uvicorn

    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = config_from_args(args)

    state: dict = {}
    stop = threading.Event()
    app = create_bridge_app(config, registration_state=state, registration_stop=stop)
    try:
        uvicorn.run(app, host=bind_host(config), port=config.port)
    finally:
        # Lifespan shutdown already stopped and joined the registration
        # thread AND unregistered (popping the id) - this is pure
        # belt-and-braces for exits that skipped the lifespan
        stop.set()
        if state.get("webhook_id") is not None:
            unregister_from_bus(config, state["webhook_id"])


if __name__ == "__main__":
    main()
