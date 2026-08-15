"""Webhook-to-injection bridge: re-awaken idle Claude Code sessions (RFC #122).

EXPERIMENTAL prototype. The bus has a push subsystem (webhooks) that nothing
consumes, while delivery to sessions is pull-only - a DM to an idle session
sits unread until its human happens to prompt. This daemon closes the loop:

    publish_event(channel="session:X", ...)
        -> bus webhook POST -> bridge (localhost)
        -> filter to actionable signal
        -> inject a wake-up for session X

Injection backends:
  spool  append the event as a JSON line to <wake_dir>/<session_id>.jsonl -
         portable; a Stop/UserPromptSubmit hook can drain the spool at its
         next opportunity. This path is always on, in every backend.
  mux    additionally type a wake prompt into the session's terminal pane
         (tmux `send-keys`, zellij `action write-chars`), falling back to
         spool when no pane mapping exists. Mappings live in
         <wake_dir>/panes.json and are written by `agent-event-bus-cli panes
         set` from a SessionStart hook. `tmux` is accepted as an alias for
         this backend's original, tmux-only name.

Idle gate: the mux backend injects only into a session that is BETWEEN turns.
A <wake_dir>/<session_id>.busy marker (written by `agent-event-bus-cli
wake-state`, from UserPromptSubmit and Stop hooks) means a turn is in flight,
and the event is spooled instead. This costs no coverage - a Stop hook already
surfaces directed events at end-of-turn - and it keeps injected keystrokes away
from the one window where a permission dialog can be on screen to consume them.

Loop prevention: in the mux backend a per-session cooldown (default 30s)
bounds injections; an event that arrives during cooldown is spooled instead,
so nothing is lost. In the default spool backend the cooldown never engages -
a spool line only becomes a wake when the drain hook acts on it, so bounding
that belongs to the drain hook.

Run:  uv run agent-event-bus-bridge [--backend mux] [--port 8082] ...
(from the repo checkout - the console script lives in the project venv and
nothing puts it on PATH; on macOS `make install-bridge` supervises it as a
LaunchAgent instead)
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
import shutil
import stat
import subprocess
import tempfile
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
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_event_bus.cli import DEFAULT_URL, BusUnreachableError, call_tool

# The header name is a wire contract with the bus - import it rather than
# re-spelling it, the same coupling discipline as the tests building their
# signatures from the bus's _compute_signature. From helpers, NOT server:
# server.py opens/migrates the bus database and attaches its log handler at
# import time, none of which this pure HTTP client of the bus may trigger
# (see test_bridge_import_does_not_pull_in_the_bus_server).
from agent_event_bus.helpers import SIGNATURE_HEADER, WEBHOOK_CONTENT_TYPE

# The wake-dir contract - pane mapping shape, turn-state markers, and the
# validation both sides run. The writer (`agent-event-bus-cli panes`) is built
# on the same module, so a change that breaks this reader breaks a test here
# rather than silently producing wakes that never happen.
from agent_event_bus.wake import (
    DEFAULT_BUSY_TTL_SECONDS,
    DEFAULT_WAKE_DIR,
    PANES_FILENAME,
    SUPPORTED_MUXES,
    InvalidTargetError,
    MuxTarget,
    is_busy,
    parse_target,
)

logger = logging.getLogger("agent-event-bus-bridge")

DEFAULT_BRIDGE_PORT = 8082
DEFAULT_COOLDOWN_SECONDS = 30.0
# Home for the hook-URL singleton lock. MACHINE-scoped and uid-scoped, NOT
# HOME-anchored: the webhook row it guards is global to the bus, so the lock
# must contend for every process on this machine that could register the
# same URL - a Path.home()-based path only contends within one HOME, so the
# same uid under two HOMEs (a systemd unit vs a login shell) would both
# acquire and both sweep. XDG_RUNTIME_DIR (per-user tmpfs) when set, else the
# system temp dir (tempfile.gettempdir() - $TMPDIR/$TEMP/$TMP, else /tmp;
# macOS resolves this to a per-user /var/folders/.../T). The uid-named subdir
# makes it HOME-independent; on a box that falls back to a SHARED temp dir the
# dir is create-and-VERIFIED before use (_ensure_private_lock_dir), not
# adopted. Cross-USER on one loopback bus (a different uid reaching the same
# bus, which the loopback-trusting auth allows) stays out of scope for v1 - a
# different uid gets a different dir - and is called out in the guide.
DEFAULT_LOCK_DIR = (
    Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    / f"agent-event-bus-bridge-{os.getuid()}"
)

# The injection subprocesses are bounded like the notifier subprocesses in
# helpers.py - a hung multiplexer must not wedge the bridge. Sized under the
# bus's WEBHOOK_TIMEOUT (5s per attempt, server.py): the bus's clock starts
# before ours, so a bound at or above its timeout means the bus never
# sees the response this bound exists to produce - just a timeout plus
# retries (and a duplicate spool line per retry).
# A TOTAL budget for the injection, not a per-call one. zellij needs two
# calls (type, then submit), and a per-call bound would make the worst case
# 2 x this - which at 2.0 puts spool_deadline + injection at 6.0, over the
# bus's 5s and straight into the timeout-plus-retry this constant exists to
# prevent. _mux_wake spends it against a monotonic deadline instead, so the
# invariant stays "spool deadline + MUX_TIMEOUT < WEBHOOK_TIMEOUT"
# regardless of how many calls a backend needs.
MUX_TIMEOUT = 2.0

# Floor for the per-call slice of that budget. A slice that has decayed to
# ~0 would make the LAST call (zellij's submit) time out instantly on a
# merely-slow host, leaving text typed into a pane and never submitted -
# strictly worse than not having injected at all.
MUX_CALL_MIN_TIMEOUT = 0.25

# Bound on joining the registration thread at shutdown. register_with_bus
# makes 2 + N sequential call_tool requests (list, one unregister per
# stale row, register), each under the CLI's 10s timeout - 35s covers the
# common N <= 1 case. A longer sweep (several unclean exits) or a slower
# bus can still leak a row; the startup dedupe reclaims it.
REGISTRATION_JOIN_TIMEOUT = 35.0

# Real bus payloads are a few KB; the body must be read before the HMAC can
# be checked, so bound what an unauthenticated peer can make us buffer
MAX_BODY_BYTES = 1_048_576  # 1 MiB

# panes.json is a small {session_id: pane_id} map maintained by an external
# component, read on the delivery path for every tmux-backend DM. Bound the
# read so a runaway or malicious file can't pull unbounded data into memory
# per DM (a real mapping is orders of magnitude smaller); a truncated read
# is almost certainly invalid JSON and lands on the unparseable arm. This is
# a CHARACTER cap - the read is text-mode UTF-8, so the bytes pulled off disk
# are up to ~4x this for multibyte content (~1 MiB here), still bounded and
# still far above any real mapping.
MAX_PANES_CHARS = 262_144  # 256 Ki characters

# Bound the wait for the per-session spool flock. The drain contract keeps
# the drainer's hold down to a couple of renames, so seconds of contention
# means a stuck drainer (SIGSTOP, network mount) - and an unbounded block
# here parks a threadpool worker per pending event until /hook starves.
# Raising after the deadline is correct: nothing is durably stored yet, so
# the bus retry is meaningful (unlike post-spool errors). Like MUX_TIMEOUT,
# the deadline (attempts x retry) must stay under the bus's WEBHOOK_TIMEOUT
# (5s per attempt) - and their SUM must too, or a slow-lock-then-slow-tmux
# request times out bus-side even though each bound individually held.
SPOOL_LOCK_ATTEMPTS = 20
SPOOL_LOCK_RETRY_SECONDS = 0.1

# Cap on the warn-once key sets (wake failures and panes conditions): a
# session that ends while broken never sheds its key - only a later
# delivery for that same session can - so on a weeks-long daemon the sets
# are pure retention for dead sessions. Housekeeping, not correctness:
# clearing costs one repeat warning per still-live condition.
_WARN_KEYS_CAP = 256

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
    backend: str = "spool"  # "spool" | "mux" ("tmux" is a legacy alias)
    secret: str | None = None
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    # How long a <sid>.busy marker is believed without a refresh. See
    # wake.DEFAULT_BUSY_TTL_SECONDS - this is an ageing window on a REFRESHED
    # marker, not a static TTL: a turn that keeps marking itself busy holds
    # the gate for as long as it runs.
    busy_ttl_seconds: float = DEFAULT_BUSY_TTL_SECONDS
    wake_dir: Path = field(default_factory=lambda: DEFAULT_WAKE_DIR)
    # URL the bus POSTs to; None means loopback (bus on this machine)
    hook_url: str | None = None
    # Interface to bind; None derives it from the hook URL (see bind_host)
    bind: str | None = None
    # Embedder-only opt-in for the exposure invariant: under
    # `uvicorn --factory --host ...` or an ASGI mount the HOSTING server
    # owns the real bind - this config never sees it, so bind_host reads
    # the (None) bind as loopback and the exposed-listener secret
    # requirement in validate_config cannot fire. Set True when the
    # hosting server is not loopback-only to get the same hard refusal
    # the CLI gives a wide --bind. The CLI path never needs it: there the
    # bridge owns the bind, so the derived check is already real.
    assume_exposed: bool = False
    # Extra Host values the /hook and /health allowlist accepts, beyond the
    # loopback literals, the hook URL hostname, and a pinned --bind. A
    # forwarding proxy commonly rewrites Host to its upstream address (the
    # nginx default `proxy_set_header Host $proxy_host`) or a name, which
    # nothing else adds under the derived wildcard bind - so a reverse-proxy
    # deployment must list that value here or every dispatch is 421'd.
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        # Normalize the legacy backend spelling HERE, not in validate_config,
        # so it holds for every construction path. An embedder building a
        # BridgeConfig directly - and every test that does - skips validation
        # entirely, and there `backend="tmux"` would fall through deliver()'s
        # `!= "mux"` check and silently behave as the spool backend: no error,
        # no log line, just a bridge that never injects. That is the exact
        # failure shape this whole change exists to remove, so the alias has
        # to be resolved at the type's boundary rather than on one path into
        # it.
        if self.backend == "tmux":
            self.backend = "mux"


class BridgeConfigError(ValueError):
    """An invalid BridgeConfig. A ValueError subclass so EMBEDDERS can catch
    it with a normal `except Exception` around app assembly - SystemExit is
    a BaseException and would tear through that. config_from_args translates
    it into SystemExit for the CLI path, which keeps main() printing the
    message and exiting without a traceback."""


class _UnserializablePayloadError(Exception):
    """_spool's json.dumps could not produce a standard-JSON line - the
    payload was nested past the recursion limit, or carried an inf/nan value
    (json.dumps(allow_nan=False) rejects those). Raised BEFORE any file is
    created or lock taken, so process() can map it to a named 400 with a
    STRUCTURAL guarantee that nothing durable happened - rather than catching
    a bare error around all of deliver(), which also runs the tmux steps
    AFTER the spool line is committed (a 400 there would make the bus retry
    an event whose line already landed, duplicating it)."""


def _now() -> float:
    """Monotonic-clock seam: tests freeze THIS, not time.monotonic itself -
    patching the stdlib module attribute would freeze the clock for every
    caller in the process (portal threads included), not just the code
    under test."""
    return time.monotonic()


def _reject_json_constant(literal: str):
    """json.loads parse_constant hook: reject NaN/Infinity/-Infinity so
    nothing non-standard reaches a spool line. Raises ValueError, which the
    hook's named-400 arm catches."""
    raise ValueError(f"non-standard JSON constant {literal!r}")


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


# PATH SAFETY, not addressability: the channel string is
# publisher-controlled wire input and the id becomes a spool filename, so
# path separators, "..", and any other unexpected byte must never reach
# the filesystem - the bus warns on malformed channels but does not reject
# them. The length bound keeps a too-long name from turning into an
# unretryable OSError out of the spool open (and caps spool-file blast
# radius). The charset covers display ids ("brave-trex") and the UUIDs the
# bus GENERATES - but not necessarily the id a session is addressed by: a
# session registered with a client_id becomes that string verbatim
# (server.py register_session sets session_id = client_id or uuid4(), and
# nothing validates it), so a cwd- or repo:branch-derived client_id with a
# ".", "/", space, or >64 chars gets a session_id this pattern REJECTS. A
# DM to such a session then resolves to no target and spools nowhere, with
# only the rate-limited "unsafe session id" WARNING below - which reads as
# a hostile publisher, not a legitimate own registration. Loosening the
# pattern is not the fix (the id still becomes a filename); validating
# client_id bus-side at registration is, and belongs in its own change.
# Same dead end already documented for display_id, reached from the
# client_id side.
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

# The unsafe-id rejection below is publisher-drivable (the bus warns on
# malformed channels but never rejects them), and the channel string is
# publisher-CHOSEN - so a bound keyed on the value (a dedup set) still lets
# a publisher who varies the id force one warning per event. Bound by time
# instead: at most one WARNING per interval regardless of content, repeats
# at debug (DEV_MODE surfaces every offending string). A persistent
# condition re-warns each interval instead of going dark forever. Unlocked -
# a rare race just duplicates a warning. Deliberately PROCESS-WIDE module
# state, unlike the per-Injector panes guard: resolve_target_session is a
# module function that runs before any Injector exists, and the log the
# bound protects is per-process anyway. (Two apps mounted in one process
# therefore share the window - acceptable for v1.)
_UNSAFE_WARN_INTERVAL_SECONDS = 60.0
_unsafe_warn_state = {"last": -math.inf}

# Every ACCEPTED event has its disposition logged somewhere (the skew line,
# the filter arms, the cooldown/unmapped debug lines, the spool breadcrumb),
# but a REJECTED request (bad signature, wrong media type, oversized,
# unparseable, unserializable, foreign Host) returned only a status the bus
# discards - so a secret mismatch, a media-type change, or a Host-rewriting
# proxy behind a 421 was diagnosable only by reproducing it with curl.
# _log_rejection closes that asymmetry. Rate-limited PER REASON on the
# MONOTONIC clock (_now, like every other window in this module - so an
# NTP step cannot stretch or collapse it): the values (Host, signature)
# are attacker/proxy-controlled and would otherwise spam. Process-wide
# like _unsafe_warn_state, same v1 caveat about mounted apps sharing the
# window.
_reject_warn_state: dict[str, float] = {}


def _log_rejection(reason: str, detail: str) -> None:
    now = _now()
    if now - _reject_warn_state.get(reason, -math.inf) >= _UNSAFE_WARN_INTERVAL_SECONDS:
        _reject_warn_state[reason] = now
        logger.warning(f"Rejected request ({reason}): {detail}")
    else:
        logger.debug(f"Rejected request ({reason}): {detail}")


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
        now = _now()
        if now - _unsafe_warn_state["last"] >= _UNSAFE_WARN_INTERVAL_SECONDS:
            _unsafe_warn_state["last"] = now
            logger.warning(f"Ignoring event with unsafe session id in channel {channel!r}")
        else:
            logger.debug(f"Ignoring event with unsafe session id in channel {channel!r}")
        return None
    return target


class Injector:
    """Delivers wake-ups, bounded by a per-session cooldown."""

    def __init__(self, config: BridgeConfig):
        self.config = config
        # Resolve the wake dir ONCE: validate_config normalized it to an
        # absolute path and nothing reassigns it, so its resolved target is
        # fixed for this Injector's life. _spool's containment check runs on
        # every delivery - including the many foreign-session DMs a
        # machine-unscoped bus delivers here just to conclude "not ours" - so
        # keeping this off the per-event path drops one readlink/lstat chain
        # per event. (The FileNotFoundError self-heal recreates the dir BY
        # NAME, matching this by-name identity; a wake dir whose path
        # components are re-symlinked mid-run is compared against the cached
        # target, which is strictly less exposure than the per-call resolve.)
        self._wake_dir_resolved = config.wake_dir.resolve()
        self._lock = threading.Lock()
        self._last_wake: dict[str, float] = {}
        # Armed panes.json warning KEYS (not messages - a torn write varies
        # its parse position per read). A set, not a single slot: the
        # file-level conditions and each session's bad-entry condition are
        # independent, and a single slot would let a healthy read for one
        # session re-arm another's warning - interleaved deliveries (the
        # NORMAL multi-machine steady state, where most DMs are unmapped)
        # would oscillate that into one WARNING per DM.
        self._warned_panes_keys: set[str] = set()
        # Same idea for tmux wake failures - and the same per-condition
        # keying lesson as the panes guard: the condition is per (session,
        # exception type), so a single slot would let a second broken
        # session go silent behind the first's warning while a healthy
        # session's success re-armed a broken one into warn-per-DM. The
        # truly global case (no tmux at all) is the startup preflight's job.
        self._warned_wake_fail_keys: set[str] = set()
        # First-sighting bound for the spool backend's happy-path INFO -
        # see the log site in deliver() for why repeats demote to debug
        self._spool_breadcrumb_logged = False
        # Once at construction, not per delivery: keeps the lock hold to the
        # append itself, and a deliberate later permission change by the
        # operator isn't silently reverted on the next event - though it IS
        # re-asserted on the next restart (and by the self-heal path), on a
        # directory the bridge did not necessarily create: spool files carry
        # full event payloads, so the private mode is worth asserting every
        # start rather than trusting whatever was there.
        try:
            self.config.wake_dir.mkdir(parents=True, exist_ok=True)
            self.config.wake_dir.chmod(0o700)
        except OSError as e:
            # The one startup filesystem precondition: a named config error
            # like every other operator-facing input, not a bare traceback.
            # BridgeConfigError, not SystemExit: Injector is constructed by
            # create_bridge_app, so this raise crosses the embedding surface
            # too - main() translates it for the CLI, same as validate_config
            raise BridgeConfigError(
                f"Cannot prepare wake dir {self.config.wake_dir} "
                f"(check --wake-dir / AGENT_EVENT_BUS_WAKE_DIR): {e}"
            ) from None

    def deliver(self, session_id: str, event: dict) -> str:
        """Wake session_id for event. Returns the action taken: "tmux" or
        "zellij" (wake injected via that multiplexer),
        "spool" (spool backend working as designed), "spool-cooldown",
        "spool-busy" (mux backend, a turn is in flight - see the idle gate
        below), "spool-unmapped" (mux backend, no usable pane mapping -
        normally because the session lives on another machine, since
        webhooks have no machine scoping, but also when panes.json is
        missing, unreadable, malformed, or its entry is not a usable
        target; the misconfiguration shapes warn, so check the log), or
        "spool-mux-failed" (mux backend, the injection attempt itself
        failed - the arm that means the multiplexer on this box is
        broken). The action value is in-band for a direct caller of /hook
        only - the bus discards the response body - so operator-facing
        visibility is this module's log: failed wakes at warning, the quiet
        arms at debug under DEV_MODE.

        The cooldown bounds successful injections only: spool writes are
        durable bookkeeping, not wakes, and a failed injection must not
        burn the window - the next event should retry (e.g. after a
        SessionStart hook repairs the pane mapping).
        """
        # The per-session flock inside _spool serializes appends on its own
        # (flock contends per open file description, in-process included),
        # and it can block on an external drainer holding the drain lock -
        # so it must NOT run under the global lock, or one stalled drain
        # would stall every session's delivery
        self._spool(session_id, event)

        if self.config.backend != "mux":
            # The happy-path breadcrumb: without it the spool backend's
            # terminal shows the registration line and then permanent
            # silence, indistinguishable from a bus that stopped
            # dispatching. FIRST delivery only: the volume driver is
            # webhooks having no machine scoping (every bridge receives
            # every session: DM, most for sessions this host will never
            # wake), so per-DM INFO here is the same unbounded noise the
            # unmapped arm's debug demotion avoids - one line proves the
            # whole chain works, repeats land at debug under DEV_MODE.
            # Unlocked: a rare race just duplicates the INFO.
            message = f"Spooled event {event.get('event_id')!r} for {session_id[:8]}..."
            if self._spool_breadcrumb_logged:
                logger.debug(message)
            else:
                self._spool_breadcrumb_logged = True
                logger.info(f"{message} (first delivery; further spools log at debug)")
            return "spool"

        # Pane lookup BEFORE the cooldown machinery: on a multi-machine bus
        # the unmapped arm is the normal outcome, not a failure, so there is
        # no reservation to take or roll back. Debug, not info: per-event
        # INFO on an arm every foreign-machine DM lands on is permanent
        # noise - matches the below-actionable filter arm and _mux_target's
        # silent missing-file case.
        target = self._mux_target(session_id)
        if target is None:
            logger.debug(f"No pane mapping for {session_id[:8]}...; spooled only")
            return "spool-unmapped"

        # The idle gate, and it comes before the cooldown for the same reason
        # the pane lookup does: a busy session is the normal mid-turn state,
        # not a failure, so it must not consume the cooldown window that a
        # later - genuinely idle - delivery needs. Injecting mid-turn is
        # redundant anyway (a Stop hook surfaces directed events at
        # end-of-turn) and it is the one window where a permission dialog can
        # be on screen to swallow the keystrokes.
        if is_busy(self.config.wake_dir, session_id, self.config.busy_ttl_seconds):
            logger.debug(f"Turn in flight for {session_id[:8]}...; spooled only")
            return "spool-busy"

        with self._lock:
            now = _now()
            # Housekeeping: entries past the cooldown can never gate a wake
            # again, and sessions are ephemeral - don't retain them forever
            self._last_wake = {
                sid: ts
                for sid, ts in self._last_wake.items()
                if (now - ts) < self.config.cooldown_seconds
            }
            in_cooldown = self._last_wake.get(session_id) is not None
            if not in_cooldown:
                # Reserve the window before releasing the lock: two
                # concurrent deliveries for the same session must not both
                # pass the check and double-inject
                self._last_wake[session_id] = now
        if in_cooldown:
            # Logged outside the lock (a debug handler write is IO). Every
            # other arm names itself somewhere; this one must too - it is
            # the only case where the bridge chose not to wake a session it
            # could have, which is exactly what a DEV_MODE operator asking
            # "why didn't my session wake?" needs to see.
            logger.debug(f"Cooldown active for {session_id[:8]}...; spooled only")
            return "spool-cooldown"

        # The multiplexer runs outside the lock (bounded at MUX_TIMEOUT, but
        # other sessions' deliveries shouldn't wait on it). The pane can go
        # stale between the lookup above and here - the injection just fails,
        # which is the arm below.
        if self._mux_wake(session_id, target):
            return target.mux
        with self._lock:
            # Roll back the reservation so the failure doesn't burn the
            # window - unless a later delivery already re-claimed it. There
            # is no previous value to restore: any entry that survived the
            # prune returned spool-cooldown above.
            if self._last_wake.get(session_id) == now:
                del self._last_wake[session_id]
        return "spool-mux-failed"

    def _spool(self, session_id: str, event: dict) -> None:
        """Always-on durable path: one JSON line per event, per session.

        Durable against bridge and session crashes, not host crashes: the
        file is flushed and closed before the 200, but never fsync'ed, so a
        kernel panic or power loss inside the writeback window can lose a
        wake the bus already counted delivered. fsync-on-append is an
        accepted follow-up - it would eat into the SPOOL_LOCK deadline
        budget that must stay under the bus's WEBHOOK_TIMEOUT.

        Serialized by the per-session flock below - against other threads in
        this process and against the drain hook alike.

        A serialization failure (a RecursionError on a pathologically nested
        payload, or the ValueError json.dumps(allow_nan=False) raises on an
        inf/nan that json.loads' parse_float admitted - e.g. 1e400) is
        re-raised as _UnserializablePayloadError BEFORE any file is created or
        lock taken, and process() maps THAT to the named 400 every other
        wire-input path returns. Every OTHER raise here is retry-meaningful:
        nothing is durably stored, so the bus retry is real (unlike a
        post-spool error).
        """
        spool_file = self.config.wake_dir / f"{session_id}.jsonl"
        # The id is charset-clean by here (resolve_target_session validated
        # it), so a containment failure means a SYMLINK was planted in the
        # wake dir, not a hostile id: name that so an operator runs `ls -l`
        # on the wake dir instead of hunting a publisher. O_NOFOLLOW on the
        # open below is the kernel-enforced backstop that catches the same
        # thing at open time (ELOOP); this is the readable message, and it
        # never follows the link. A hard raise, not a degrade: a symlink in
        # the wake dir is local tampering that will not self-heal.
        if spool_file.is_symlink():
            raise ValueError(
                f"Spool file {spool_file} is a symlink (-> {os.readlink(spool_file)!r}); "
                "refusing to follow it - remove it from the wake dir"
            )
        # Defense in depth behind resolve_target_session's charset check: a
        # traversal or absolute component in a wire-supplied id must never
        # produce a write outside the wake dir
        if spool_file.resolve().parent != self._wake_dir_resolved:
            raise ValueError(f"Spool path escapes wake dir: {session_id!r}")
        # Serialize BEFORE the lock/open: a failure must create no file and
        # take no lock. Two non-standard-JSON producers are rejected here, so
        # the "every spooled line is standard JSON" invariant holds by
        # construction (not by the bus's good behavior):
        #  - allow_nan=False: an overflowing numeric literal (payload: 1e400)
        #    parses through json.loads' parse_float=float, which returns inf
        #    WITHOUT raising - so process()'s parse_constant never sees it -
        #    and default json.dumps would write it back as bare Infinity, a
        #    line jq / JSON.parse / Go's encoding/json all reject (the drain
        #    hook then skips it and the wake is silently lost). allow_nan=False
        #    raises ValueError on inf/nan here instead. (parse_constant still
        #    rejects the bare NaN/Infinity TOKENS earlier, for events that
        #    never reach the spool too.)
        #  - RecursionError: a payload nested just under the parse limit
        #    (loads and dumps spend their recursion budgets independently, and
        #    this call sits a few frames deeper than the loads that admitted
        #    the body).
        # Both re-raise as the dedicated type so process() maps ONLY this
        # pre-durable failure to a 400 - never a post-spool error from
        # deliver's later tmux steps.
        try:
            line = json.dumps(event, allow_nan=False) + "\n"
        except (RecursionError, ValueError) as e:
            raise _UnserializablePayloadError from e
        # flock a sibling lock file around the append: flock contends per
        # open file description, so it serializes writers in this process and
        # the drain hook (another process) alike - without it, the drainer's
        # rename could slip between our open and our flush and the line would
        # land in a file the drainer already read (and will delete)
        lock_file = self.config.wake_dir / f"{session_id}.lock"
        # ONE deadline shared across the self-heal retry below: a per-call
        # attempt counter would let contended-then-vanished-dir double the
        # worst case past the sum-under-WEBHOOK_TIMEOUT invariant that
        # TestBusTimingContract pins on the constants. time.monotonic
        # directly, NOT the _now() seam: the seam exists for the cooldown's
        # semantics and tests freeze it - a frozen clock here would turn a
        # held flock into an unbounded spin instead of the deadline raise.
        # (ATTEMPTS x RETRY defines the budget; the loop is wall-clock
        # gated, so ATTEMPTS is a budget multiplier, not a literal count.)
        deadline = time.monotonic() + SPOOL_LOCK_ATTEMPTS * SPOOL_LOCK_RETRY_SECONDS

        def _open_append_0600(path: Path):
            # Explicit create mode, not the process umask (a plain append
            # open would land 0o644 under the usual one): spool lines carry
            # full publisher-authored payloads, and the directory's 0o700 is
            # the only other guard - --wake-dir can point at a pre-existing
            # shared path, and the documented manual workflows invite a
            # later chmod on it. Create-time only; the append path pays
            # nothing once the file exists.
            # O_NOFOLLOW covers BOTH the spool and lock opens: --wake-dir may
            # have been group/world-writable before the daemon first ran,
            # and Injector.__init__'s chmod narrows the dir but does not
            # remove a pre-planted <sid>.lock / <sid>.jsonl SYMLINK already
            # inside it - a plain open would follow it and create/open the
            # target as the operator. The spool path's resolve() check
            # catches its own link but is TOCTOU-racy by construction;
            # O_NOFOLLOW is kernel-enforced at open time and guards the lock
            # sibling (which has no resolve() check) too. Neither file is
            # ever legitimately a symlink, so the happy path pays nothing;
            # a symlinked final component fails ELOOP - an OSError, already
            # the retryable arm.
            # encoding="utf-8" explicitly: fdopen(..., "a") otherwise picks
            # the locale codec (ASCII under a C-locale daemon), and the
            # spool line is a UTF-8-JSON cross-process contract the drain
            # hook reads. Safe today only because json.dumps defaults to
            # ensure_ascii=True; pinning it here keeps a later
            # ensure_ascii=False from raising UnicodeEncodeError mid-append
            # under the held flock, 500ing an already-committed delivery.
            return os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600),
                "a",
                encoding="utf-8",
            )

        def _append() -> None:
            with _open_append_0600(lock_file) as lock_fd:
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise OSError(
                                f"Could not lock spool for {session_id} within "
                                f"{SPOOL_LOCK_ATTEMPTS * SPOOL_LOCK_RETRY_SECONDS:.0f}s "
                                "(stuck drainer?)"
                            ) from None
                        time.sleep(SPOOL_LOCK_RETRY_SECONDS)
                try:
                    with _open_append_0600(spool_file) as f:
                        f.write(line)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)

        try:
            _append()
        except FileNotFoundError:
            # The wake dir can vanish at runtime - hand-clearing spools is
            # the documented interim workflow while pruning is a follow-up -
            # and without recovery every later delivery 500s until restart
            # (bus retries exhausted, wakes lost, /health still green).
            # Recreate once and retry the WHOLE critical section: the dir
            # can also disappear between the lock open and the spool open,
            # so wrapping only the first open would leave the same 500.
            # mkdir/chmod stay off the happy path, and the resolve() check
            # above already ran, so this cannot widen the path-safety
            # surface.
            self.config.wake_dir.mkdir(parents=True, exist_ok=True)
            self.config.wake_dir.chmod(0o700)
            _append()

    def _mux_target(self, session_id: str) -> MuxTarget | None:
        """Look up the session's injection target from <wake_dir>/panes.json.

        The mapping is maintained by an external session-side component, so
        every failure shape - unreadable file, non-UTF-8 bytes from a torn
        write, valid JSON that isn't an object - must degrade to "unmapped":
        an exception escaping here would 500 the webhook and make the bus
        retry an already-spooled event.
        """
        panes_file = self.config.wake_dir / PANES_FILENAME
        try:
            # os.open with O_NOFOLLOW + O_NONBLOCK, not read_text():
            #  - O_NOFOLLOW: panes.json shares the wake dir's history (may have
            #    been group/world-writable before the daemon narrowed it to
            #    0o700), so a symlink planted at the name must not be followed -
            #    the same guard _open_append_0600 carries for the spool/lock
            #    files. ELOOP is an OSError, landing on the unreadable arm.
            #  - O_NONBLOCK: a planted FIFO would otherwise block a threadpool
            #    worker forever inside a deliver() that already committed its
            #    spool line - each tmux-backend delivery parks one and /hook
            #    starves once they are all consumed. This path has no
            #    MUX_TIMEOUT/deadline of its own. On a no-writer FIFO the read
            #    returns EOF (empty -> unparseable); a writer-attached FIFO
            #    with nothing buffered surfaces as a TypeError from the text
            #    layer (raw read returns None), caught below; on a regular file
            #    O_NONBLOCK is a no-op.
            #  - encoding="utf-8", NOT the locale codec: a supervisor hands the
            #    daemon no LC_ALL/LANG, glibc resolves C -> ASCII, and a
            #    healthy file with any byte >0x7f would wrongly hit the
            #    unparseable arm. The writer contract (guide) says UTF-8.
            #  - read(MAX_PANES_CHARS): bound the per-DM buffer against a
            #    runaway file; a truncated read is invalid JSON -> unparseable.
            fd = os.open(panes_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as f:
                panes = json.loads(f.read(MAX_PANES_CHARS))
        except FileNotFoundError:
            # A missing file IS this session's absent read: clear the
            # file-level keys AND this session's entry key, matching the
            # absent-key arm below (a full delete-and-recreate clean-state
            # cycle must re-arm the bad-entry warning - it is the
            # repair-didn't-take signal). Still only THIS session's key, so
            # no cross-session re-arm.
            self._disarm_file_keys()
            self._warned_panes_keys.discard(f"bad-pane-value:{session_id}")
            return None
        except (ValueError, RecursionError, TypeError) as e:
            # Parse failures - the read produced something json.loads can't
            # take. ValueError: JSONDecodeError, or UnicodeDecodeError from a
            # torn write. RecursionError: deep nesting blows the interpreter
            # limit before any JSONDecodeError. TypeError: a writer-attached
            # FIFO with nothing buffered makes the text layer's read return
            # None (raw EAGAIN surfaces as None, not an OSError). All
            # typically transient/self-healing; separate reason from the
            # OSError arm so a torn write escalating into a permanently
            # unreadable file WARNs again rather than demoting.
            self._warn_panes_once(
                "unparseable", f"Unparseable panes.json ({e}); treating as unmapped"
            )
            return None
        except OSError as e:
            # I/O failures (PermissionError, IsADirectoryError) - typically
            # persistent, and there may never be a healthy read to re-arm
            # the guard, so this class carries its own reason key
            self._warn_panes_once(
                "unreadable", f"Unreadable panes.json ({e}); treating as unmapped"
            )
            return None
        except Exception as e:
            # Never-500 backstop by SHAPE, not enumeration: this method's
            # contract is that no failure reading an externally-maintained,
            # hostile-adjacent file escapes to 500 an already-spooled
            # delivery (which the bus would retry, duplicating lines). The
            # specific arms above stay for reason selection; this catches
            # anything they miss so the invariant can't be broken again by a
            # new exception shape (it has been widened three times). Logged
            # in full on first sighting so a genuine bug is not masked.
            self._warn_panes_once(
                "unexpected", f"Unexpected error reading panes.json ({e!r}); treating as unmapped"
            )
            return None
        # A successful json.loads proves the READ and PARSE conditions
        # cleared, whatever the shape below is - so clear those two keys
        # NOW, before the shape check. Otherwise a valid-but-non-dict file
        # (a JSON list) returns via the not-an-object arm with unparseable /
        # unreadable still armed from an earlier condition, and a
        # genuinely-new later parse or read failure demotes to debug -
        # exactly the outcome the re-arm design exists to prevent. Only the
        # read/parse keys, NOT not-an-object: that is a SHAPE condition, so
        # clearing it here would defeat its own warn-once (it would re-warn
        # every read of a persistently non-dict file).
        self._warned_panes_keys -= self._PANES_READ_KEYS
        if not isinstance(panes, dict):
            self._warn_panes_once(
                "not-an-object", "panes.json is not an object; treating as unmapped"
            )
            return None
        # Shape is a dict too - the last file-level condition clears
        self._warned_panes_keys.discard("not-an-object")
        # Membership, not .get(): a JSON null value and an absent key both
        # come back None from .get(), and null is the LIKELIEST bad value in
        # practice (panes[sid] = os.environ.get("TMUX_PANE") emits exactly
        # that outside tmux) - it must land in the bad-value arm below, not
        # read as healthy-absent and re-arm the guard
        entry_key = f"bad-pane-value:{session_id}"
        if session_id not in panes:
            # Genuinely absent re-arms THIS session's entry condition only -
            # discarding more would let the (normal) unmapped arm re-arm a
            # different session's warning and oscillate the bound
            self._warned_panes_keys.discard(entry_key)
            return None
        try:
            # parse_target is the SHARED validator - literally the call the
            # writer (`agent-event-bus-cli panes set`) runs - so what this
            # reader accepts and what the session hook writes cannot drift
            # apart. That drift has no loud failure mode here: it is wakes
            # that quietly never happen.
            target = parse_target(panes[session_id])
        except InvalidTargetError as e:
            # Present but wrong-shaped (0 instead of "%0", "", null, a zellij
            # entry carrying no session name, or a
            # control character - JSON encodes NUL as \u0000): a
            # misconfiguration whose repair is nothing like "the mapping is
            # absent", so it must not fold into the unmapped debug line.
            # isprintable() is load-bearing, not cosmetic: an argv element
            # with an embedded NUL makes subprocess.run raise ValueError
            # BEFORE check or timeout - a class _mux_wake's post-spool arms
            # don't catch - so it must be rejected here, where the warning
            # names the entry to repair, and never reach argv. Real pane ids
            # ("%0", "%12" for tmux, "0" for zellij) are printable ASCII.
            # Keyed per session: the condition is per entry, unlike the
            # file-level failures above.
            self._warn_panes_once(
                entry_key,
                f"panes.json entry for {session_id[:8]}... is unusable ({e}); treating as unmapped",
            )
            return None
        except Exception as e:
            # The same never-500 backstop BY SHAPE that guards the read above,
            # extended over the one call that INTERPRETS the externally
            # maintained data. parse_target raises only InvalidTargetError
            # today, but it now lives in a shared module cli.py also evolves,
            # and this method's contract - that no failure reading a
            # hostile-adjacent file escapes to 500 an already-spooled delivery
            # - must not rest on a sibling module's exception surface staying
            # put. Distinct reason key so a genuine bug is not filed under
            # "misconfigured entry".
            self._warn_panes_once(
                f"parse-error:{session_id}",
                f"Unexpected error validating the panes.json entry for "
                f"{session_id[:8]}... ({e!r}); treating as unmapped",
            )
            return None
        # Both per-entry keys re-arm on a healthy entry. Missing the
        # parse-error one would silence it for that session for the lifetime
        # of the daemon, including if the condition came back - the opposite
        # of every other per-entry key here.
        self._warned_panes_keys.discard(entry_key)
        self._warned_panes_keys.discard(f"parse-error:{session_id}")
        return target

    _PANES_FILE_KEYS = frozenset({"unparseable", "unreadable", "not-an-object", "unexpected"})
    # The subset a successful read+parse clears regardless of the shape it
    # yields; not-an-object is left out because it tracks the shape, cleared
    # only when the shape is actually a dict. "unexpected" (the never-500
    # catch-all) is a read/parse-stage condition, so a healthy read re-arms it.
    _PANES_READ_KEYS = frozenset({"unparseable", "unreadable", "unexpected"})

    def _disarm_file_keys(self) -> None:
        """The normal missing-file state clears ALL file-level conditions -
        a delete-and-recreate is a full clean-state cycle. (A healthy read
        clears the read/parse keys inline, and not-an-object only when the
        shape is a dict.) Per-entry keys are cleared by their own session's
        healthy or absent reads."""
        self._warned_panes_keys -= self._PANES_FILE_KEYS

    def _warn_panes_once(self, key: str, message: str) -> None:
        """A persistently broken panes.json must not emit one WARNING per
        delivered DM - the same unbounded-volume shape as the unsafe-channel
        warning, driven by DM rate against a stuck local condition instead
        of a hostile publisher. Keyed, not message-matched: the message
        embeds str(e), and a torn non-atomic write yields a different parse
        position on every read. Armed keys live in a SET so independent
        conditions bound independently - file-level keys are cleared by a
        healthy file read, per-entry keys by that session's own healthy or
        absent read; a single slot would oscillate under interleaved
        deliveries. The full exception text still reaches the log on the
        first sighting (and every repeat at debug under DEV_MODE).
        Unlocked - a rare race just duplicates a warning."""
        if key in self._warned_panes_keys:
            logger.debug(message)
        else:
            if len(self._warned_panes_keys) >= _WARN_KEYS_CAP:
                # Same dead-session retention bound as the wake-failure set
                self._warned_panes_keys.clear()
            self._warned_panes_keys.add(key)
            logger.warning(message)

    @staticmethod
    def _wake_argv(target: MuxTarget) -> list[list[str]]:
        """The command(s) that type the wake prompt into target's pane.

        A LIST of argvs because the two multiplexers differ in whether
        submitting is separable from typing:

        - tmux takes text and key names in one send-keys, so WAKE_PROMPT is
          one argument followed by "Enter". Passing it WITHOUT -l is why
          WAKE_PROMPT must stay a fixed multi-word constant - a value that
          happened to match a key name (Enter, C-c, Escape) would be sent as
          that keystroke instead of typed.
        - zellij has no equivalent: `write-chars` types and never submits, so
          the carriage return is a second call (`write 13`). Dropping it would
          leave the prompt sitting in the input box - a wake that wakes
          nobody while every status here stays green.

        Neither form interpolates the event payload. The prompt is fixed, so
        publisher-authored text cannot reach a terminal as keystrokes; the
        payload is available to the woken session through the bus and the
        spool, as quoted data it can judge.
        """
        if target.mux == "zellij":
            base = ["zellij", "--session", target.session, "action"]
            return [
                [*base, "write-chars", "-p", target.pane, WAKE_PROMPT],
                # 13 = carriage return. `write` takes decimal bytes.
                [*base, "write", "-p", target.pane, "13"],
            ]
        return [["tmux", "send-keys", "-t", target.pane, WAKE_PROMPT, "Enter"]]

    def _mux_wake(self, session_id: str, target: MuxTarget) -> bool:
        """Type the wake prompt into the given pane. False on failure."""
        try:
            # time.monotonic directly, NOT the _now() seam: that seam exists
            # for the cooldown's benefit and tests freeze it, which would turn
            # this bound into an infinite one in exactly the tests most likely
            # to exercise a hanging multiplexer.
            deadline = time.monotonic() + MUX_TIMEOUT
            for argv in self._wake_argv(target):
                subprocess.run(
                    argv,
                    check=True,
                    capture_output=True,
                    timeout=max(MUX_CALL_MIN_TIMEOUT, deadline - time.monotonic()),
                )
            logger.info(f"Woke {session_id[:8]}... via {target.describe()}")
            # A working wake re-arms THIS session's failure conditions only -
            # clearing globally would oscillate a broken session's warning
            # under interleaved healthy deliveries.
            # Under the lock: the comprehension iterates the set, and a
            # concurrent delivery's add would otherwise raise RuntimeError
            # out of the one method whose exceptions must never 500 the
            # webhook. The tmux subprocess stays outside the lock.
            with self._lock:
                self._warned_wake_fail_keys = {
                    k for k in self._warned_wake_fail_keys if not k.startswith(f"{session_id}:")
                }
            return True
        except (subprocess.SubprocessError, OSError) as e:
            # SubprocessError covers CalledProcessError/TimeoutExpired;
            # OSError covers a missing or non-executable multiplexer binary
            # (which is now per-entry rather than fatal at startup, since the
            # preflight only requires that SOME supported mux exists). An
            # exception escaping here would 500 the webhook and make the bus
            # retry an event that is already durably spooled (duplicate
            # lines). Bounded like the panes guard - keyed per (session,
            # exception type): a persistent condition warns once per session
            # and then debugs, a different failure type warns fresh, and a
            # second broken session is never silenced by the first's key.
            #
            # Known partial-failure shape, zellij only: if write-chars
            # succeeds and the following `write 13` does not, the prompt is
            # left typed-but-unsubmitted in the pane, and the retry after the
            # cooldown rollback types it a second time. Not repaired here on
            # purpose - the repair would be a third call (`write 21`, kill
            # line) issued under precisely the conditions that just proved
            # calls are failing. Named in docs/BRIDGE.md instead.
            key = f"{session_id}:{type(e).__name__}"
            # Include the multiplexer's OWN stderr, not just argv and an exit
            # code. capture_output=True means the reason is sitting on
            # e.stderr while `{e}` renders only "Command [...] returned
            # non-zero exit status N" - and the likeliest zellij failure is a
            # CLI-surface mismatch (a build that does not take
            # `action write-chars -p`), which zellij explains on stderr and
            # nothing else here would. Truncated because a usage dump can run
            # long, and decoded defensively: this path must not raise.
            detail = ""
            stderr = getattr(e, "stderr", None)
            if stderr:
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                detail = f": {' '.join(str(stderr).split())[:300]}"
            message = f"Wake injection failed for {session_id[:8]}... ({e}{detail}); spooled only"
            # Same lock as the success-arm comprehension: an unsynchronized
            # add would race its iteration. Logging stays outside the hold.
            with self._lock:
                first_sighting = key not in self._warned_wake_fail_keys
                if first_sighting:
                    if len(self._warned_wake_fail_keys) >= _WARN_KEYS_CAP:
                        # Housekeeping, not correctness: a session whose pane
                        # never recovers never sheds its key (only success
                        # discards), and sessions are ephemeral - so clear at
                        # a cap; one repeat warning per condition after that
                        # many distinct ones is the lesser noise
                        self._warned_wake_fail_keys.clear()
                    self._warned_wake_fail_keys.add(key)
            if first_sighting:
                logger.warning(message)
            else:
                logger.debug(message)
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

    On those embeddings the HOSTING server owns the listener's bind, which
    validate_config cannot see - its exposure check reads the config's
    (None) bind as loopback. When the host is not loopback-only, set
    assume_exposed=True on the config (opts into the CLI's hard refusal)
    or just set the secret.

    The host also owns the port and any mount prefix, and both are derived
    from config.port into bridge_hook_url(config) - which is BOTH the URL
    the lifespan registers on the bus AND the Host allowlist /hook and
    /health enforce. So set hook_url whenever the hosting server's address,
    port, or mount path differs from the derived http://127.0.0.1:<port>/hook
    default: otherwise the bus registers and POSTs to a URL the outer app
    404s (or nothing listens on), the Host guard rejects the real caller,
    and /health reports registered:true forever - none of the port/path/
    quadrant warnings fire, because those live in config_from_args, off the
    embedding path.
    """
    # A half-wired pair would bind and serve /health but never register,
    # with nothing logged and registered:false forever - the silent failure
    # mode the lifespan wiring exists to prevent, reachable through the
    # embedding surface this docstring advertises. main() always passes
    # both, so a partial call is unambiguously a programming error.
    if (registration_state is None) != (registration_stop is None):
        raise ValueError(
            "registration_state and registration_stop come as a pair - "
            "pass both to enable bus registration, or neither"
        )
    # Embedders build configs by hand - the invariants (including the
    # exposed-listener secret requirement) must hold on this path too
    validate_config(config)
    injector = Injector(config)
    # The Host allowlist (see _HostAllowlistMiddleware): loopback literals,
    # the hook URL's hostname, and the effective bind address - exactly the
    # names a legitimate caller can arrive under (the bus fills Host from the
    # URL it was told to POST to; local tools use a loopback literal; a
    # monitoring probe may address the bound interface by IP). urlsplit()
    # .hostname already lowercases and strips IPv6 brackets. The bind
    # address is safe to allow: a DNS-rebinding page's Host carries the
    # attacker hostname by construction, never the bound literal.
    hook_host = urllib.parse.urlsplit(bridge_hook_url(config)).hostname
    allowed_hook_hosts = {"127.0.0.1", "localhost", "::1"}
    if hook_host:
        # Normalize an IPv6 literal the same way the bind address is below:
        # urlsplit lowercases and de-brackets but does NOT compress, so an
        # expanded hook-URL literal ([FD7A:...:0:0:0:0:1]) would otherwise
        # miss the compressed Host a probe sends. A hostname compares
        # verbatim (urlsplit already lowercased it).
        try:
            allowed_hook_hosts.add(str(ipaddress.ip_address(hook_host)))
        except ValueError:
            allowed_hook_hosts.add(hook_host)
    bind = bind_host(config)
    try:
        bind_ip = ipaddress.ip_address(bind)
        # Skip the wildcards (0.0.0.0 / ::): nobody probes an unspecified
        # address, and "localhost" is already covered above. Store the
        # NORMALIZED form - str(ip_address) renders IPv6 lowercase and
        # compressed, exactly what _host_from_header yields - so an uppercase
        # or expanded --bind (FE80::1, 0:0:0:0:0:0:0:1) still matches the
        # Host a probe sends. IPv4 is unaffected.
        if not bind_ip.is_unspecified:
            allowed_hook_hosts.add(str(bind_ip))
    except ValueError:
        pass  # "localhost" - already present; the resolver decides its family
    # Operator-configured extras (--allowed-hosts): the ONLY way to accept a
    # reverse proxy's rewritten Host, which the derived wildcard bind above
    # never adds (and a name-rewriting proxy can't be covered by --bind at
    # all). Added VERBATIM: validate_config (called above, so this holds for
    # embedders too) already stripped, dropped blanks, and canonicalized each
    # entry. Re-applying _host_from_header here would be actively wrong, not
    # merely redundant - it is not idempotent on IPv6, since the unbracketed
    # branch splits on the last colon ("fd7a::1" -> "fd7a:").
    allowed_hook_hosts.update(config.allowed_hosts)
    # Version-skew line bound (a closure cell: the arm lives in process(),
    # not on the injector). The condition - a bus predating derived levels -
    # is persistent and per-deployment, so it gets the same first-sighting
    # treatment as every other stuck-condition line; re-armed by the first
    # delivery that DOES carry a level, which is what an upgraded bus sends.
    skew_state = {"warned": False}

    @asynccontextmanager
    async def lifespan(app):
        # registration_thread and cycle_stop are LOCALS, deliberately not
        # nonlocal/shared: @asynccontextmanager makes a fresh generator per
        # startup/shutdown cycle, so each cycle gets its own stop event and
        # thread handle. The old design cleared ONE shared event on
        # re-entry, which could resurrect a prior cycle's thread that
        # outlived REGISTRATION_JOIN_TIMEOUT - it would see the shared event
        # un-set and resume its retry loop, and both threads would then
        # register and race state["webhook_id"], leaking the loser row until
        # a later startup sweep reclaimed it. A per-cycle event can never be
        # un-set by a later startup.
        registration_thread: threading.Thread | None = None
        cycle_stop: threading.Event | None = None
        if registration_state is not None and registration_stop is not None:
            # The per-cycle cycle_stop is what the thread actually waits on;
            # the caller's registration_stop is only the observable "stopped"
            # flag (mirrored in the finally). Old code cleared the shared
            # event on startup, so a pre-set one was already ignored - a
            # fresh cycle here matches that while never sharing an event a
            # stale thread could be woken through.
            cycle_stop = threading.Event()
            registration_thread = threading.Thread(
                target=register_with_retry,
                args=(config, registration_state, cycle_stop),
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
            if registration_thread is not None and cycle_stop is not None:
                cycle_stop.set()
                # Mirror onto the caller's event: the thread waits on the
                # per-cycle cycle_stop, but registration_stop stays the
                # observable "stopped" flag main()'s belt-and-braces finally
                # and external code read.
                if registration_stop is not None:
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
            # parse_constant rejects the bare NaN/Infinity/-Infinity TOKENS
            # json.loads accepts by DEFAULT, early and for every event
            # (including ones that never reach the spool). It is NOT the whole
            # guard: an overflowing number (1e400) reaches parse_float=float,
            # which returns inf without raising, so it slips past here - that
            # producer is caught at the write side by _spool's
            # allow_nan=False, which is where the standard-JSON invariant
            # actually holds. The rejector's ValueError folds into the
            # named-400 arm below.
            event = json.loads(body, parse_constant=_reject_json_constant)
        # ValueError, not just JSONDecodeError: json.loads(bytes) DECODES
        # before parsing, and invalid UTF-8 raises UnicodeDecodeError (a
        # ValueError but not a JSONDecodeError). RecursionError is the
        # sibling case OUTSIDE ValueError: ~500k nested brackets fit well
        # under MAX_BODY_BYTES and blow the interpreter limit before any
        # JSONDecodeError can be raised. The bus retries any >=400
        # identically, so a 400 is not fewer retries - it is a clean named
        # status instead of a traceback per attempt. The named reason
        # reaches THIS daemon's log via _log_rejection at the hook_endpoint
        # boundary; the bus discards the response body, so its log carries
        # only the status code.
        except (ValueError, RecursionError):
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
        if level is not None:
            skew_state["warned"] = False  # the bus sends levels - re-arm the skew line
        if level != "actionable":
            if level is None:
                # A bus predating derived levels (#129) sends none at all -
                # every delivery would land here forever, so make the
                # version skew visible instead of filtering silently -
                # bounded: INFO on the first sighting, debug repeats (the
                # volume is the DM rate against a persistent condition)
                message = (
                    f"Event {event.get('event_id')!r} carries no signal_level "
                    "(bus predates derived levels?); filtering it"
                )
                if skew_state["warned"]:
                    logger.debug(message)
                else:
                    skew_state["warned"] = True
                    logger.info(message)
            else:
                logger.debug(f"Ignoring event {event.get('event_id')!r}: level {level!r}")
            return {"status": "ignored", "reason": "below actionable"}, 200

        target = resolve_target_session(event)
        if target is None:
            logger.debug(
                f"Ignoring event {event.get('event_id')!r}: "
                f"channel {event.get('channel')!r} has no target session"
            )
            return {"status": "ignored", "reason": "no target session"}, 200

        try:
            action = injector.deliver(target, event)
        except _UnserializablePayloadError:
            # json.dumps in _spool has the same non-ValueError recursion
            # sibling the json.loads arm above catches - and it runs a few
            # frames deeper, so a payload nested just under the parse limit
            # is admitted here and then fails to encode. Deterministic (all
            # three bus retries would raise identically), so answer with the
            # named 400 every wire-input path gives, not three tracebacks.
            # Catching the DEDICATED type, not a bare RecursionError around
            # deliver: _spool raises it before it opens/locks anything, so
            # this 400 structurally cannot fire after a spool line has
            # landed (which would make the bus retry and duplicate it) - a
            # RecursionError from deliver's later tmux steps stays a 500.
            # A DISTINCT string from the parse-failure 400 above: the body
            # parsed fine, it just cannot re-serialize to a standard-JSON
            # spool line (deep nesting, or an inf/nan allow_nan=False
            # rejects) - a different producer and fix than "not JSON". The
            # response body is a direct-caller-only surface, so this is the
            # only place the distinction can surface.
            return {"error": "payload not serializable to a spool line"}, 400
        return {"status": "delivered", "action": action, "session_id": target}, 200

    async def hook_endpoint(request: Request) -> JSONResponse:
        # The Host allowlist (DNS-rebinding guard) runs in middleware ahead
        # of routing - see _HostAllowlistMiddleware. This is the OTHER half
        # of the browser guard, for CROSS-origin pages: fetch(mode:"no-cors")
        # from any web page the operator has open can POST to 127.0.0.1 as
        # long as its Content-Type stays CORS-safelisted (a string body
        # defaults to text/plain) - no preflight is sent, the opaque
        # response doesn't matter, the write would already have happened:
        # attacker-authored spool lines from a background tab. Requiring
        # the bus's actual media type (single-sourced in helpers.py)
        # forces a preflight the bridge never answers, so the browser
        # never sends the POST at all; the bus is unaffected. Parameters
        # are tolerated ("application/json; charset=utf-8") - the guard is
        # about which media types a browser can send preflight-free, not
        # about strictness for its own sake.
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != WEBHOOK_CONTENT_TYPE:
            _log_rejection("content-type", f"Content-Type {content_type!r} (need JSON)")
            return JSONResponse(
                {"error": f"Content-Type must be {WEBHOOK_CONTENT_TYPE}"}, status_code=415
            )
        # Precheck the honest case cheaply; the streamed count below covers
        # a missing or lying content-length (e.g. chunked encoding).
        # isdecimal(), NOT isdigit(): isdigit() is True for compatibility
        # digits int() rejects (U+00B2 superscript two, which is exactly
        # latin-1 byte 0xB2 - and Starlette decodes headers as latin-1), so
        # an isdigit()+int() pair would 500 on a header value that h11
        # rejects for the CLI but a mounting app might pass through.
        # isdecimal() matches what int() actually accepts.
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdecimal() and int(content_length) > MAX_BODY_BYTES:
            _log_rejection("too-large", f"Content-Length {content_length} > {MAX_BODY_BYTES}")
            return JSONResponse({"error": "body too large"}, status_code=413)
        # Stream with a running count, not request.body(): body() concatenates
        # every chunk unconditionally, so a chunked POST (no content-length to
        # precheck) could make the daemon buffer arbitrarily many bytes before
        # a post-read check ever ran - the bound must hold WHILE reading.
        # ClientDisconnect (the peer aborts mid-body - uvicorn delivers
        # http.disconnect) is the one wire-driven path that would otherwise
        # escape as an unnamed 500 traceback; every sibling failure returns a
        # named 4xx so the log carries a diagnosis, not a stack. Nothing is
        # written and no lock is taken, so a 400 is honest.
        chunks: list[bytes] = []
        received = 0
        try:
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_BODY_BYTES:
                    _log_rejection("too-large", f"streamed body exceeded {MAX_BODY_BYTES}")
                    return JSONResponse({"error": "body too large"}, status_code=413)
                chunks.append(chunk)
        except ClientDisconnect:
            _log_rejection("disconnect", "client aborted mid-body")
            return JSONResponse({"error": "client disconnected mid-body"}, status_code=400)
        body = b"".join(chunks)
        # Starlette header lookup is case-insensitive, so the canonical-case
        # constant works directly
        signature = request.headers.get(SIGNATURE_HEADER)
        payload, status = await run_in_threadpool(process, body, signature)
        # The bus discards the response body, so a reject is otherwise a bare
        # status in the uvicorn access line with no diagnosis - every ACCEPTED
        # event's disposition is logged, so log the rejects too (rate-limited
        # per reason). 401/400 come from process(); 415/413/disconnect above
        # and 421 in the middleware log at their own sites.
        if status >= 400:
            _log_rejection(payload.get("error", "reject"), f"status {status}")
        return JSONResponse(payload, status_code=status)

    async def health(request: Request) -> JSONResponse:
        # The Host guard runs in middleware ahead of routing (see
        # _HostAllowlistMiddleware), so a rebound tab cannot confirm a
        # bridge runs here even via a 405/404. A supervisor's loopback
        # probe passes.
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
        # Ahead of the router: the Host guard must answer before a method or
        # path mismatch (405/404) can confirm a bridge is here.
        middleware=[Middleware(_HostAllowlistMiddleware, allowed=allowed_hook_hosts)],
        lifespan=lifespan,
    )
    # POST /hook/ must be a loud 404 (bus retries and logs it), not a 307:
    # the bus's httpx client doesn't follow redirects and counts any status
    # under 400 as delivered, so the default slash-redirect would make a
    # trailing-slash hook URL read as a perfectly healthy webhook while the
    # bridge processes nothing.
    app.router.redirect_slashes = False
    return app


def _host_from_header(raw_host: str) -> str:
    """Strip the port from a Host header value, handling bracketed IPv6
    ("[::1]:8082" -> "::1"), and normalize an IP literal to its canonical
    form. Normalizing HERE (both the incoming Host and the allowlist entries
    go through the same canonicalization) collapses every wire spelling of
    an IPv6 address - compressed, expanded, upper/lower - onto one key, so
    the guard does not depend on which spelling the bus's httpx client
    happens to render the Host as. A hostname passes through lowercased."""
    if raw_host.startswith("["):  # bracketed IPv6, with or without a port
        host = raw_host.split("]", 1)[0].lstrip("[").lower()
    else:
        host = raw_host.rsplit(":", 1)[0].lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host  # a hostname (or garbage) - compare verbatim


class _HostAllowlistMiddleware:
    """DNS-rebinding guard applied AHEAD of routing, so it covers method and
    path mismatches too. A page served from evil.example:<our port> whose A
    record then flips to 127.0.0.1 is SAME-origin with the bridge, so CORS
    never applies and the request reaches us - but the browser fills Host
    from the page's URL, so a rebound request never carries a loopback
    literal or the hook URL host the bus was told to POST to. Enforcing this
    inside the endpoints (per-route) left GET /hook -> 405 and
    /anything -> 404 answerable BEFORE the endpoint ran, and 405-vs-404 still
    confirms a bridge is here - the exact existence signal extending the
    guard to /health was meant to deny. As middleware it runs before the
    router. Hand-rolled rather than TrustedHostMiddleware, whose
    host.split(':')[0] mangles bracketed IPv6 (--bind ::1 is supported).
    421 Misdirected Request."""

    def __init__(self, app, allowed: set[str]):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw_host = ""
            for key, value in scope.get("headers") or ():
                if key == b"host":
                    raw_host = value.decode("latin-1")
                    break
            if _host_from_header(raw_host) not in self.allowed:
                # A rebound tab's Host is attacker-chosen, but so is a
                # reverse proxy's rewritten Host - which is a legitimate
                # deployment 421'd silently under the derived wildcard bind.
                # Name the rejected Host and the fix so it's recoverable.
                _log_rejection(
                    "host",
                    f"Host {raw_host!r} not in the allowlist {sorted(self.allowed)} - if a "
                    "reverse proxy forwards here, add its forwarded Host via --allowed-hosts / "
                    "AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS",
                )
                response = JSONResponse({"error": f"unexpected Host {raw_host!r}"}, status_code=421)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
    interfaces. validate_config requires the HMAC secret whenever the
    effective bind OR the hook URL is non-loopback - on every path, not
    just the CLI (the refusal lives here so it travels with a hand-built
    config onto the embedding paths, not only through argparse)."""
    if config.bind:
        return config.bind
    hook_host = urllib.parse.urlsplit(bridge_hook_url(config)).hostname
    if hook_host is None:
        # A hostless hook URL reads as loopback; validate_config refuses it
        # upstream, so this is only reachable via a direct bind_host call
        return "127.0.0.1"
    if _is_host_loopback(hook_host):
        # Bind the SPECIFIC loopback address the hook URL names, not a
        # hardcoded 127.0.0.1: loopback is all of 127.0.0.0/8 plus ::1, so
        # a hook on 127.0.1.1 (Debian's own-hostname address), a 127.0.0.2
        # alias, or [::1] all read as loopback here - and the old hardcode
        # bound an interface the bus could never reach (ECONNREFUSED on
        # every dispatch) while both sides still counted as loopback, so
        # every exposure and topology guard stayed silent. urlsplit lowered
        # the host and stripped IPv6 brackets, so ip_address parses it.
        try:
            return str(ipaddress.ip_address(hook_host))  # 127.0.1.1, ::1, ...
        except ValueError:
            return "127.0.0.1"  # "localhost" - no single literal to bind
    return "0.0.0.0"  # noqa: S104 - deliberate; secret enforced at config time


class BridgeRegistrationError(Exception):
    """A registration step failed in a way that should be retried.

    Raised for the bus answering with something this module cannot act on (a
    non-list webhook listing, a missing webhook_id, a refused removal). Every
    such case is retryable, so register_with_retry backs off on it exactly as
    it does on a BusUnreachableError - but it carries its own message, so the log
    line names which step failed.
    """


def register_with_bus(config: BridgeConfig) -> int:
    """Register this bridge's webhook on the bus. Returns webhook_id.

    Raises on any failure: BusUnreachableError when the bus is not up, whatever
    call_tool lets through for a real transport fault (401, timeout, bad
    body), and BridgeRegistrationError for this module's own named checks.
    register_with_retry backs off on all of them.

    Idempotent: an unclean exit (SIGKILL, crash, reboot) skips main()'s
    finally, leaving a stale webhook at this URL - and the bus neither dedupes
    by URL nor deactivates failing hooks, so each stale row would duplicate
    every wake. Remove matching URLs before registering.
    """
    hook_url = bridge_hook_url(config)

    # active_only=False: a row at THIS bridge's hook URL is stale whether or
    # not someone paused it, and the removal is a delete either way. Sweeping
    # active-only would skip a paused row and add a second row at the same
    # URL - and re-enabling the paused one then makes the bus dispatch every
    # DM here twice. (Harmless until set_webhook_active existed, since
    # `active` could never be 0; pausing a noisy hook is exactly the case
    # that feature is for, and this endpoint fits it.)
    existing = call_tool("list_webhooks", {"active_only": False}, url=config.bus_url)
    if not isinstance(existing, list):
        # Proceeding without the dedupe would stack the duplicate deliveries
        # this sweep exists to prevent - retryable failure, like the no-id
        # case below
        raise BridgeRegistrationError(f"list_webhooks returned unexpected result: {existing!r}")
    for wh in existing:
        # Guard the element shape too: an AttributeError here would
        # escape register_with_retry and kill the registration thread
        if not isinstance(wh, dict):
            continue
        if wh.get("url") == hook_url and wh.get("webhook_id") is not None:
            removal = call_tool(
                "unregister_webhook",
                {"webhook_id": wh["webhook_id"]},
                url=config.bus_url,
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
                raise BridgeRegistrationError(
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
        # not silent success
        raise BridgeRegistrationError(f"register_webhook returned no webhook_id: {result!r}")
    logger.info(f"Registered bridge webhook #{webhook_id} on {config.bus_url}")
    return webhook_id


def unregister_from_bus(config: BridgeConfig, webhook_id: int) -> None:
    """Best-effort webhook cleanup on shutdown."""
    try:
        result = call_tool("unregister_webhook", {"webhook_id": webhook_id}, url=config.bus_url)
    except BusUnreachableError:
        logger.warning(f"Could not unregister webhook #{webhook_id} (bus unreachable)")
        return
    except Exception as e:
        # 401, timeout, bad body. Shutdown stays best-effort, but the one log
        # line an operator has when a row leaks should carry the real reason
        logger.warning(f"Could not unregister webhook #{webhook_id} ({e!r})")
        return
    # Same accept-shape as the startup sweep: the bus reports logical
    # failure in-band (success-False dict), and already-gone is the goal
    # state. Shutdown stays best-effort, so a surprise is a warning here
    # rather than the sweep's retryable BridgeRegistrationError - but the log
    # must not assert a removal it never checked; the next startup sweep
    # reclaims.
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
    "bus not up yet" is the normal case at boot - a BusUnreachableError must not
    kill the daemon. Runs in a background thread started from the app's
    startup hook, so the listener binds (essentially) first and webhook
    deliveries have a live port to hit.
    """
    delay = initial_delay
    while not stop.is_set():
        try:
            state["webhook_id"] = register_with_bus(config)
            return
        # Every failure shape - unreachable bus, transport fault, this
        # module's own named checks - is retryable here; the thread must
        # back off, never die silently. Each carries its own message, so
        # the log line names the real cause without inspecting exit codes.
        except Exception as e:
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
        raise BridgeConfigError(
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
        help=f"Port to listen on (default: {DEFAULT_BRIDGE_PORT}; see --bind for the interface)",
    )
    parser.add_argument(
        "--backend",
        # "tmux" is kept as an accepted spelling of "mux": it was this
        # backend's name when tmux was the only multiplexer it drove, and it
        # is baked into installed plists. config_from_args normalizes it.
        choices=["spool", "mux", "tmux"],
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_BACKEND") or "spool",
        help="Wake mechanism: spool file only, or terminal injection "
        "(tmux/zellij) + spool. 'tmux' is a legacy alias for 'mux'",
    )
    parser.add_argument(
        "--cooldown",
        # Raw string default, cast in config_from_args - see _to_number
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_COOLDOWN") or str(DEFAULT_COOLDOWN_SECONDS),
        help="Minimum seconds between wake injections per session (mux "
        "backend only; the spool backend's loop prevention belongs to the "
        "drain hook)",
    )
    parser.add_argument(
        "--busy-ttl",
        # Raw string default, cast in config_from_args - see _to_number
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_BUSY_TTL") or str(DEFAULT_BUSY_TTL_SECONDS),
        help="Seconds a <session_id>.busy marker is believed without a refresh "
        "(mux backend only). Lower this only if something refreshes the marker "
        "during a turn; with the default wiring it must exceed a long turn",
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
    parser.add_argument(
        "--allowed-hosts",
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS") or "",
        help="Comma-separated extra Host values the /hook & /health allowlist "
        "accepts, beyond the loopback literals, the hook URL host, and a pinned "
        "--bind. Needed when a reverse proxy rewrites Host to its upstream "
        "address or a name (else every dispatch is 421'd)",
    )
    return parser


def validate_config(config: BridgeConfig) -> None:
    """Enforce the hard invariants on a BridgeConfig, wherever it came from.

    config_from_args calls this on the CLI path, and create_bridge_app
    calls it again for embedders (uvicorn --factory, an ASGI mount) that
    build a BridgeConfig by hand and never pass through argparse - the
    security posture (an exposed listener requires the HMAC secret) must
    travel with the config, not with the entry point. One caveat travels
    the other way: exposure is derived from bind_host(config) and the hook
    URL, and on embedding paths the HOSTING server owns the real bind
    (uvicorn's --host, the enclosing app's) - invisible here, so the
    bind-derived half of the check only binds on the CLI path. Embedders
    whose hosting server is not loopback-only must set assume_exposed=True
    (same refusal) or just set the secret. Raises
    BridgeConfigError (a ValueError - embedder-catchable, unlike
    SystemExit); config_from_args translates it for the CLI. NORMALIZES
    port / cooldown_seconds / wake_dir on the config IN PLACE before
    checking (int/float/Path - note int() truncates a float port rather
    than rejecting it), so a caller keeping the config as its source of
    truth reads back the coerced values. Error messages name flags and env
    vars because the CLI is the common path; the invariants are runtime
    ones. The advisory topology WARNINGS stay in config_from_args - they
    are startup diagnostics, not invariants.
    """
    # Normalize BEFORE checking: the CLI hands strings through argparse
    # defaults, and a hand-built config may carry raw env values - a wrong
    # type must become the named config error these checks exist to give,
    # not a bare TypeError out of a comparison or an AttributeError off a
    # str that was annotated Path
    config.port = _to_number(config.port, int, "--port", "AGENT_EVENT_BUS_BRIDGE_PORT")
    config.cooldown_seconds = _to_number(
        config.cooldown_seconds, float, "--cooldown", "AGENT_EVENT_BUS_BRIDGE_COOLDOWN"
    )
    config.busy_ttl_seconds = _to_number(
        config.busy_ttl_seconds, float, "--busy-ttl", "AGENT_EVENT_BUS_BRIDGE_BUSY_TTL"
    )
    try:
        config.wake_dir = Path(config.wake_dir)
    except (TypeError, ValueError):
        # The one normalization Path() doesn't cover with a named error -
        # Path(None), Path(123), Path(b"...") raise bare TypeErrors, the
        # exact shape this block's comment promises to prevent
        raise BridgeConfigError(
            f"Invalid wake dir {config.wake_dir!r} "
            "(check --wake-dir / AGENT_EVENT_BUS_WAKE_DIR): expected a filesystem path"
        ) from None
    # The three string inputs get no numeric coercion, so a hand-built
    # config carrying a non-str slips through until it raises a bare
    # AttributeError out of urlsplit (bus_url, hook_url) - or, for bind,
    # validates as a bogus address (ipaddress.ip_address(123) is 0.0.0.123)
    # and fails only later inside uvicorn.run. Name them here like every
    # other config error. Embedder-only: argparse hands strings.
    for value, label in (
        (config.bus_url, "--bus-url / AGENT_EVENT_BUS_URL"),
        (config.hook_url, "--hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL"),
        (config.bind, "--bind / AGENT_EVENT_BUS_BRIDGE_BIND"),
        # secret is truthy when bytes, so it satisfies the exposed-listener
        # requirement and then fails at RUNTIME instead: register_with_bus'
        # json= serialization raises TypeError (retried forever, /health
        # registered:false), and verify_signature's secret.encode() would
        # AttributeError into a 500. HMAC keys are conventionally bytes, so
        # b"..." is a plausible embedder slip.
        (config.secret, "AGENT_EVENT_BUS_BRIDGE_SECRET / secret="),
    ):
        if value is not None and not isinstance(value, str):
            raise BridgeConfigError(f"Invalid value {value!r} (check {label}): expected a string")

    # allowed_hosts is the one field whose ENTRIES feed a security guard, so
    # it is canonicalized HERE rather than in config_from_args: the allowlist
    # is built in create_bridge_app, which every embedder reaches without
    # passing through argparse. Three inputs a hand-built config makes easy,
    # each of which silently breaks the guard if it survives to that loop:
    #   ("",)      - the `os.environ.get(..., "").split(",")` idiom. Empty
    #                string canonicalizes to itself, and the middleware
    #                defaults raw_host to "" when the Host header is absent
    #                or blank (h11 permits both), so the entry MATCHES those
    #                requests and turns the rebinding guard off entirely -
    #                for /health too, while it still reports registered.
    #   " host"    - never stripped, so it can never match: escape hatch
    #                silently inert, the mirror image of the above.
    #   "host"     - a bare str is iterable, so the loop would add one entry
    #                PER CHARACTER (including "" for the empty string).
    # A bare IPv6 literal is accepted too: _host_from_header's unbracketed
    # branch splits on the last colon ("fd7a::1" -> "fd7a:"), but --bind
    # takes bare literals, so that is the spelling an operator arrives with.
    if isinstance(config.allowed_hosts, str):
        config.allowed_hosts = (config.allowed_hosts,)
    canonical_hosts = []
    for entry in config.allowed_hosts:
        if not isinstance(entry, str):
            raise BridgeConfigError(
                f"Invalid allowed host {entry!r} (check --allowed-hosts / "
                "AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS): expected a string"
            )
        stripped = entry.strip()
        if not stripped:
            continue  # blank entry (trailing comma, empty env var) - drop it
        try:  # bare IPv6, which the port-splitting path would mangle
            canonical = str(ipaddress.ip_address(stripped))
        except ValueError:
            canonical = _host_from_header(stripped)
        # Emptiness is tested on the CANONICAL value, not the stripped input:
        # _host_from_header can PRODUCE "" from an entry that is not blank -
        # ":8082" (no bracket, so the rsplit on the last colon leaves nothing
        # before it) and "[]" / "[]:8082" (bracketed branch, nothing between
        # the brackets). Checking only the input would let those through to
        # the allowlist and reopen the Host-less bypass above by another door.
        # A named refusal rather than the silent `continue`: a trailing comma
        # is a formatting artifact, but ":8082" is an operator reaching for
        # "any host on this port" - a wish this flag cannot grant, and one
        # they need told rather than dropped.
        if not canonical:
            raise BridgeConfigError(
                f"Invalid allowed host {entry!r} (check --allowed-hosts / "
                "AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS): no hostname in it. "
                "Entries are Host values (name, IP literal, or either with a "
                "port) - a port alone matches nothing."
            )
        canonical_hosts.append(canonical)
    config.allowed_hosts = tuple(canonical_hosts)

    # The legacy "tmux" spelling is already normalized to "mux" by
    # BridgeConfig.__post_init__, so it cannot reach this check.
    # argparse `choices` only guards command-line values, and an embedder
    # skips argparse entirely - an unknown backend would silently mean
    # "spool" (deliver only tests != "mux")
    if config.backend not in ("spool", "mux"):
        raise BridgeConfigError(
            f"Invalid backend {config.backend!r} (check AGENT_EVENT_BUS_BRIDGE_BACKEND): "
            "expected 'spool' or 'mux'"
        )
    # Range checks cover CLI and env alike: an out-of-range port would be a
    # uvicorn traceback naming neither, and a negative cooldown would
    # silently disable the cooldown (now - ts < -5 is never true)
    if not (1 <= config.port <= 65535):
        raise BridgeConfigError(
            f"Invalid port {config.port} (check --port / AGENT_EVENT_BUS_BRIDGE_PORT): "
            "expected 1-65535"
        )
    # isfinite too: nan makes every prune comparison False (cooldown never
    # engages), inf makes it always True (one wake ever, then silence)
    if not math.isfinite(config.cooldown_seconds) or config.cooldown_seconds < 0:
        raise BridgeConfigError(
            f"Invalid cooldown {config.cooldown_seconds} "
            "(check --cooldown / AGENT_EVENT_BUS_BRIDGE_COOLDOWN): "
            "must be finite and >= 0"
        )
    # nan makes every age comparison False, so the gate would never engage and
    # every mid-turn DM would inject. 0 is a legitimate value meaning "ignore
    # the marker entirely"; inf is legitimate too, restoring the never-expire
    # behaviour for a deployment that would rather latch than risk a mid-turn
    # wake - so only nan and negatives are refused.
    if math.isnan(config.busy_ttl_seconds) or config.busy_ttl_seconds < 0:
        raise BridgeConfigError(
            f"Invalid busy TTL {config.busy_ttl_seconds} "
            "(check --busy-ttl / AGENT_EVENT_BUS_BRIDGE_BUSY_TTL): "
            "must not be negative or nan"
        )
    # A daemon's cwd is whatever its supervisor hands it - a relative wake
    # dir would silently relocate the durable path (and its chmod)
    if not config.wake_dir.is_absolute():
        raise BridgeConfigError(
            f"Invalid wake dir {config.wake_dir} "
            "(check --wake-dir / AGENT_EVENT_BUS_WAKE_DIR): must be an absolute path"
        )
    # A scheme-less bus URL ("bus.example:8080/mcp") or hostless one
    # ("http:///mcp") parses with no hostname and would read as loopback,
    # skipping the topology guard below - catch the misconfiguration here
    # instead of in a later connection error
    # urlsplit itself raises ValueError("Invalid IPv6 URL") on an unbalanced
    # bracket - one call earlier than the port guard below, same class of
    # input, so it gets the same named-config-error treatment
    try:
        parsed_bus = urllib.parse.urlsplit(config.bus_url)
    except ValueError as e:
        raise BridgeConfigError(f"Invalid bus URL {config.bus_url!r}: {e}") from None
    if parsed_bus.scheme not in ("http", "https") or not parsed_bus.hostname:
        raise BridgeConfigError(
            f"Invalid bus URL {config.bus_url!r}: expected http(s)://host[:port]/path"
        )
    # Same check for the hook URL - it is what BOTH topology guards below
    # read: a scheme-less value parses to hostname None, reads as loopback,
    # skips the guards, and registers a URL the bus can never POST to
    if config.hook_url is not None:
        try:
            parsed_hook = urllib.parse.urlsplit(config.hook_url)
        except ValueError as e:
            raise BridgeConfigError(
                f"Invalid hook URL {config.hook_url!r} "
                f"(check --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL): {e}"
            ) from None
        # Require a hostname too: "http:///hook" parses to hostname None,
        # which reads as loopback and would skip every topology guard
        if parsed_hook.scheme not in ("http", "https") or not parsed_hook.hostname:
            raise BridgeConfigError(
                f"Invalid hook URL {config.hook_url!r} "
                "(check --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL): "
                "expected http(s)://host[:port]/path"
            )
        # SplitResult.port parses lazily - urlsplit itself accepts
        # "http://host:80o82/hook" (scheme and hostname above read fine),
        # and the bus stores the registered URL verbatim with no validation
        # of its own, so a malformed port would register cleanly and then
        # fail EVERY dispatch bus-side (httpx.InvalidURL) with /health
        # green - the silent-inertness shape the scheme refusal above
        # closes. Refuse it here so the embedding path gets the same named
        # error as the CLI.
        try:
            parsed_hook.port
        except ValueError:
            raise BridgeConfigError(
                f"Invalid hook URL {config.hook_url!r} "
                "(check --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL): bad port"
            ) from None
    hook = bridge_hook_url(config)
    # A loopback hook URL registered on a remote bus makes the bus POST to
    # itself: registration succeeds, /health is green, nothing is ever
    # delivered - and every machine would claim the same URL string, so the
    # startup dedupe could remove a live webhook belonging to the bus host.
    # Refuse the combination instead of failing silently.
    if not _is_loopback(config.bus_url) and _is_loopback(hook):
        raise BridgeConfigError(
            f"Bus at {config.bus_url} is remote but the advertised hook URL "
            f"{hook} is loopback - the bus would POST to itself. "
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
                raise BridgeConfigError(
                    f"Invalid bind address {config.bind!r} "
                    "(check --bind / AGENT_EVENT_BUS_BRIDGE_BIND): expected an IP "
                    "address or localhost"
                ) from None
    bind = bind_host(config)
    # The HMAC is the only authentication once the endpoint is reachable
    # off-box, and exposure is decided by the EFFECTIVE bind (bind_host
    # consults --bind first) as well as the hook URL: a non-loopback bind
    # with no secret would let anyone who reaches the port append
    # attacker-authored lines to a session spool, and a non-loopback hook
    # URL means the hop exists even behind a local TLS terminator. Hard
    # requirement either way - the bus itself defaults to auth-required,
    # and the bridge must not invert that. assume_exposed ORs in because
    # the bind-derived half only binds on the CLI path: an embedder's
    # hosting server owns the real bind, invisibly to this check.
    exposed = not _is_host_loopback(bind) or not _is_loopback(hook) or config.assume_exposed
    if exposed and not config.secret:
        if config.assume_exposed and _is_host_loopback(bind) and _is_loopback(hook):
            # Name the actual lever: the generic message below would claim
            # a loopback bind is "reachable off-box"
            raise BridgeConfigError(
                "assume_exposed is set but no secret is configured - the hosting "
                "server's listener is reachable off-box and the HMAC signature is "
                "the only authentication on this hop. Set "
                "AGENT_EVENT_BUS_BRIDGE_SECRET (or secret= on the config)."
            )
        raise BridgeConfigError(
            f"Listener would bind {bind} with hook URL "
            f"{hook} - reachable off-box, so set "
            "AGENT_EVENT_BUS_BRIDGE_SECRET or secret= on the config - the env "
            "var is only read on the CLI path (the HMAC signature is the only "
            "authentication on this hop). Check --bind / AGENT_EVENT_BUS_BRIDGE_BIND "
            "and --hook-url."
        )


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    """Build the runtime config from parsed args plus environment."""
    config = BridgeConfig(
        bus_url=args.bus_url,
        port=args.port,  # raw; validate_config coerces and range-checks
        backend=args.backend,
        # `or None`: an accidentally empty env var must not put registration
        # (which would skip the secret, so unsigned payloads) and verification
        # (which would demand signatures) on opposite sides - that 401s every
        # delivery silently
        secret=os.environ.get("AGENT_EVENT_BUS_BRIDGE_SECRET") or None,
        cooldown_seconds=args.cooldown,  # raw; validate_config coerces
        busy_ttl_seconds=args.busy_ttl,  # raw; validate_config coerces
        wake_dir=args.wake_dir,
        hook_url=args.hook_url,
        bind=args.bind,
        # Comma-separated -> tuple; blank entries dropped so a trailing comma
        # or an empty env var yields ().
        # Split only; validate_config does the stripping, blank-dropping, and
        # canonicalization, so the embedder path gets identical treatment.
        allowed_hosts=tuple(args.allowed_hosts.split(",")),
    )
    # Translate the embedder-catchable error into the CLI's exit shape -
    # main() prints the message and exits without a traceback
    try:
        validate_config(config)
    except BridgeConfigError as e:
        raise SystemExit(str(e)) from None
    # Preflight the binaries (CLI path only - embedders manage their own
    # runtime env). PATH-sensitive, so the message names the fix: a
    # supervisor's minimal PATH can hide a binary your shell sees.
    #
    # ANY supported mux satisfies it, not all of them: which one a given
    # delivery needs is a property of that session's panes.json entry, not of
    # the daemon, so a tmux-only host must not be warned about lacking zellij.
    # A mapping naming an absent binary is a per-entry OSError on the
    # wake-failure arm.
    #
    # WARNS rather than refuses, and that is deliberate. The tmux-only
    # ancestor of this check raised SystemExit, which was defensible when the
    # backend was something an operator typed - but `mux` is now the CHECKED-IN
    # plist default, so a refusal here is a launchd crash loop (KeepAlive,
    # ThrottleInterval 10) on any host without a multiplexer. That would take
    # out a previously-working spool bridge entirely: no listener, no webhook
    # registration, no spool lines. Strictly worse than the condition it
    # reports, and it would contradict this default's whole justification -
    # that an operator who has not opted into injection pays nothing for it.
    #
    # Nothing is lost by warning. The value of the old refusal was "one startup
    # message beats discovering it per-DM", and a startup WARNING is that
    # message. Meanwhile the daemon keeps binding, registering, spooling and
    # answering /health - and with no multiplexer there is also nothing to
    # WRITE a panes.json, so every delivery lands on spool-unmapped anyway.
    if config.backend == "mux":
        available = [mux for mux in SUPPORTED_MUXES if shutil.which(mux) is not None]
        if available:
            # Which ones are present is the first thing to check when wakes
            # fail for one mux and not the other, and PATH differences between
            # a supervisor and a shell are exactly why that happens.
            logger.info(f"Wake injection available via: {', '.join(available)}")
        else:
            logger.warning(
                f"Backend is mux but none of {', '.join(SUPPORTED_MUXES)} is on PATH "
                "of THIS process (a supervisor's minimal PATH can hide a binary "
                "your shell sees; check --backend / AGENT_EVENT_BUS_BRIDGE_BACKEND). "
                "Continuing: events are still spooled, but nothing will be woken."
            )
    # One derivation for the warning block below - the refusals above
    # already validated both URLs, so these cannot raise
    hook = bridge_hook_url(config)
    bind = bind_host(config)
    # The inverse mismatch is silent inertness, not exposure: a loopback
    # bind under a reachable hook URL means the bus's POSTs are refused at
    # the TCP level. Legitimate behind a same-box TLS terminator, so warn
    # rather than refuse - same policy as the port mismatch below.
    if _is_host_loopback(bind) and not _is_loopback(hook):
        logger.warning(
            f"Hook URL {hook} is reachable but the listener binds "
            f"{bind} (loopback) - correct only if something forwards between them"
        )
    # The bus POSTs to the hook URL's port while the listener binds --port;
    # a mismatch is legitimate behind a reverse proxy, but name it - it's
    # otherwise the same silent-inertness failure the guards above close.
    # Cannot raise: validate_config already refused a malformed hook-URL
    # port (the refusal lives there so the embedding path gets it too),
    # and the derived loopback default is well-formed - only the advisory
    # COMPARISON lives here, per the docstring split.
    hook_split = urllib.parse.urlsplit(hook)
    hook_port = hook_split.port
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
    # The listener only ever speaks plain HTTP (uvicorn.run gets no TLS
    # config), so an https hook URL needs a terminator in front. A SAME-BOX
    # terminator fronts one port while forwarding to another, so https with
    # the ports agreeing usually means no terminator exists - every dispatch
    # dies in the TLS handshake with every other guard quiet. An OFF-HOST
    # terminator (an LB at the same port number, forwarding here) is the
    # legitimate shape the warning text names; it can't be told apart
    # locally - the bind is a wildcard in the common case, and hook
    # hostnames don't compare against bind addresses - so warn-don't-refuse
    # like the port and path mismatches. The mismatched-port terminator
    # shape is already named by the check above.
    if hook_split.scheme == "https" and hook_port == config.port:
        logger.warning(
            f"Hook URL {hook} is https but this listener serves "
            "plain HTTP on that same port - correct only if a TLS terminator "
            "(e.g. an off-host proxy) forwards between them"
        )
    # POST /hook is the only route this listener serves; any other
    # advertised path 404s every dispatch. Legitimate behind a rewriting
    # proxy, so warn-don't-refuse like the port mismatch above.
    hook_path = hook_split.path
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
    if not _is_host_loopback(bind) and not _is_host_wildcard(bind) and _is_loopback(hook):
        logger.warning(
            f"Listener binds {bind} but the hook URL "
            f"{hook} advertises loopback - the bus can't reach it; "
            "set --hook-url to an address on the bound interface"
        )
    # The missing member of the quadrant family: two DIFFERENT loopback
    # literals of the SAME family. A PINNED --bind 127.0.0.1 under a
    # 127.0.1.1 hook URL (a 127.0.0.2 alias, ...) is all-loopback so the
    # checks above stay quiet, same-family so the family checks below stay
    # quiet, and exposed is False so no secret is demanded - yet the bus
    # POSTs to an address nothing listens on (ECONNREFUSED every dispatch).
    # A DERIVED bind can't reach this (bind_host binds the hook's own
    # literal); only a pinned --bind contradicting the hook can. localhost
    # is exempt on either side (no single literal to compare); the
    # cross-family case is left to the family checks below to avoid a
    # double warning. Warn-don't-refuse like the rest of the family.
    if _is_host_loopback(bind) and _is_loopback(hook):
        try:
            bind_lit = ipaddress.ip_address(bind)
            hook_lit = ipaddress.ip_address(hook_split.hostname)
        except ValueError:
            bind_lit = hook_lit = None  # "localhost" on a side - no literal
        if (
            bind_lit is not None
            and hook_lit is not None
            and bind_lit.version == hook_lit.version
            and bind_lit != hook_lit
        ):
            logger.warning(
                f"Listener binds loopback {bind} but the hook URL advertises a "
                f"different loopback address {hook_split.hostname} - the bus "
                "can't reach it; align --bind and --hook-url"
            )
    # Address-family mismatch: 0.0.0.0 binds IPv4 only, so an IPv6 hook
    # literal (a Tailscale IPv6 address, say) is refused at TCP while every
    # quadrant warning above stays quiet - both sides can be non-loopback
    # with port and path agreeing. Hostname hooks are exempt: DNS/MagicDNS
    # publishes A records too and happy-eyeballs falls back to v4. Hostname
    # binds ("localhost") are exempt the other way - the resolver decides
    # their family.
    try:
        hook_family = ipaddress.ip_address(hook_split.hostname).version
    except ValueError:
        hook_family = None  # hostname - the resolver decides its family
    try:
        bind_ip = ipaddress.ip_address(bind)
    except ValueError:
        bind_ip = None  # "localhost" - the resolver decides its family
    if hook_family == 6 and bind_ip is not None and bind_ip.version == 4:
        logger.warning(
            f"Hook URL host is an IPv6 address but the listener binds "
            f"{bind} (IPv4 only) - the bus can't reach it; "
            "set --bind to '::' (dual-stack) or an IPv6 address"
        )
    # The mirror direction: a v4 hook literal with a PINNED v6 bind (::1, a
    # tailnet v6 address) is refused at TCP the same way, with every other
    # guard quiet - e.g. --bind ::1 under the default loopback hook URL is
    # all-loopback, so neither the exposure nor the quadrant checks fire.
    # :: stays quiet: dual-stack picks up v4 too (_is_host_wildcard is what
    # distinguishes it from a pinned address).
    if (
        hook_family == 4
        and bind_ip is not None
        and bind_ip.version == 6
        and not _is_host_wildcard(bind)
    ):
        logger.warning(
            f"Hook URL host is an IPv4 address but the listener binds "
            f"{bind} (IPv6 only) - the bus can't reach it; "
            "bind an IPv4 or dual-stack ('::') address, or advertise an "
            "IPv6 hook URL"
        )
    return config


def _ensure_private_lock_dir(path: Path) -> None:
    """Create `path` as a private (0o700) directory this process owns, or
    SystemExit with a named message - create-and-VERIFY, not adopt. The
    hook-lock dir sits at a uid-derived, guessable name under
    tempfile.gettempdir(); on a box without XDG_RUNTIME_DIR that resolves to
    a SHARED temp dir (containers, cron, launchd, su/sudo, any login without
    pam_systemd), where a local user can pre-plant it. mkdir(exist_ok)+chmod
    would then either EPERM into a bare traceback (foreign-owned) or
    silently narrow-and-write-through (a symlink). This is the
    directory-level counterpart of the O_NOFOLLOW that already guards the
    lock FILE. lstat (not stat) so a symlink fails closed. Only the hook-lock
    dir is routed here; the wake dir is Injector.__init__'s job."""
    try:
        os.mkdir(path, 0o700)
        return  # we just created it - unambiguously ours
    except FileExistsError:
        pass
    except OSError as e:
        # read-only/full temp dir, a missing XDG_RUNTIME_DIR parent, ... -
        # a named message, not the bare traceback every other config-time
        # failure in this module avoids
        raise SystemExit(
            f"Cannot create bridge lock dir {path} ({e}); set XDG_RUNTIME_DIR "
            "to a writable per-user directory."
        ) from None
    info = os.lstat(path)  # lstat: a symlink must NOT be followed here
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit(
            f"Bridge lock path {path} is not a directory (a symlink or file is "
            "planted there); refusing to use it. Remove it, or set XDG_RUNTIME_DIR."
        )
    if info.st_uid != os.getuid():
        raise SystemExit(
            f"Bridge lock dir {path} is owned by uid {info.st_uid}, not this "
            f"process ({os.getuid()}); refusing to use it. Remove it, or set "
            "XDG_RUNTIME_DIR to a per-user directory."
        )
    if info.st_mode & 0o077:
        raise SystemExit(
            f"Bridge lock dir {path} is group/world-accessible "
            f"(mode {oct(info.st_mode & 0o777)}); refusing to use it. "
            "chmod 700 it, or set XDG_RUNTIME_DIR."
        )


def _flock_or_exit(lock_path: Path, conflict_message: str) -> int:
    """Take an exclusive, non-blocking advisory flock on lock_path, or
    SystemExit(conflict_message) if another holder has it. Returns the held
    fd - the caller keeps it for the process's lifetime (the lock releases
    when the fd closes). O_NOFOLLOW so a symlink planted at the FILE is not
    followed (the hook-lock DIR is verified by _ensure_private_lock_dir; the
    wake dir is Injector's). A failed open becomes a named SystemExit rather
    than the bare traceback every other failure here avoids (e.g. EACCES on
    a foreign lock file, ELOOP on a symlinked one)."""
    # mode=0o700, matching _ensure_private_lock_dir's create mode: normally a
    # no-op (both callers reach here with the parent present), but if the
    # hook-lock dir vanished between the verify and this call, a default-mode
    # re-create (0o777 & ~umask) would make the NEXT start's verify refuse
    # our own dir as group/world-accessible.
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise SystemExit(f"Cannot open bridge lock {lock_path} ({e})") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(conflict_message) from None
    return fd


def _acquire_singleton_locks(config: BridgeConfig) -> list[int]:
    """Refuse a SECOND bridge that would collide on either resource a
    running instance owns, before it ever touches the bus. Two INDEPENDENT
    hazards, so two locks:

    - HOOK URL: the startup sweep removes any active webhook at this hook
      URL and cannot tell a stale row (unclean exit) from a LIVE PEER's, so
      a second instance registering the same URL unregisters the first's
      row, then fails to bind (EADDRINUSE) and unregisters its own on exit -
      leaving the running bridge listening, reporting registered:true, and
      deaf. This is keyed on the hook URL (not the wake dir) because that is
      what the sweep contends on: two instances with different --wake-dir
      and the same port still collide. The lock lives in DEFAULT_LOCK_DIR
      (machine + uid scoped, HOME-independent - see its definition).
    - WAKE DIR: two bridges sharing a wake dir would interleave spool and
      lock files regardless of their hook URLs.

    Acquired SEQUENTIALLY with cleanup: if the second lock refuses, the
    first is closed before the SystemExit propagates. Returned fds are kept
    for the process's lifetime. CLI-only: embedders own their process model.
    Each lock name carries a "." so it can never collide with a
    <session_id>.lock spool file (the id charset forbids ".")."""
    hook = bridge_hook_url(config)
    digest = hashlib.sha256(hook.encode()).hexdigest()[:16]
    _ensure_private_lock_dir(DEFAULT_LOCK_DIR)  # create-and-verify, not adopt
    hook_lock = DEFAULT_LOCK_DIR / f"hook.{digest}.lock"
    wake_lock = config.wake_dir / "bridge.singleton.lock"
    specs = [
        (
            hook_lock,
            f"Another agent-event-bus-bridge is already registered for hook URL "
            f"{hook} ({hook_lock} is locked). Refusing to start: a second instance "
            "would unregister the running bridge's webhook and leave it deaf. Stop "
            "the other instance, or give this one a distinct hook URL - a different "
            "--port AND --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL (a different "
            "--wake-dir alone does NOT change the hook URL, so it would not help).",
        ),
        (
            wake_lock,
            f"Another agent-event-bus-bridge is already running on wake dir "
            f"{config.wake_dir} ({wake_lock} is locked). Refusing to start: two "
            "bridges would interleave the same spool and lock files. Stop the other "
            "instance, or run this one with a different --wake-dir / "
            "AGENT_EVENT_BUS_WAKE_DIR.",
        ),
    ]
    fds: list[int] = []
    for lock_path, message in specs:
        try:
            fds.append(_flock_or_exit(lock_path, message))
        except SystemExit:
            # Close what we already hold, so a partial acquisition (e.g. the
            # wake lock refuses after the hook lock succeeded) does not leak
            # an fd - in-process that would hold the lock forever and 421 a
            # later legitimate start on that hook URL.
            for fd in fds:
                os.close(fd)
            raise
    return fds


def main():
    """Run the bridge daemon."""
    import uvicorn

    args = build_parser().parse_args()
    # DEV_MODE=1 turns on the debug diagnostics - the per-event reasons a
    # delivery did nothing - matching server.py and the switch CLAUDE.md
    # documents for this package. Without a level path every logger.debug
    # in this module is dead code in the shipped daemon.
    level = logging.DEBUG if os.environ.get("DEV_MODE") else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")
    # basicConfig is a no-op when the root logger already has handlers
    # (e.g. under a test harness), so pin the module logger too
    logger.setLevel(level)
    config = config_from_args(args)

    state: dict = {}
    stop = threading.Event()
    # Same translation as config_from_args: app construction raises the
    # embedder-catchable BridgeConfigError (Injector's wake-dir check runs
    # in there), and the CLI turns it into a clean message-and-exit
    try:
        app = create_bridge_app(config, registration_state=state, registration_stop=stop)
    except BridgeConfigError as e:
        raise SystemExit(str(e)) from None
    # After create_bridge_app (the Injector created + chmodded the wake dir)
    # and BEFORE uvicorn.run triggers the lifespan's registration sweep: a
    # second instance must refuse here, upstream of the bus mutation. Held
    # for the whole run; released when the fds close in the finally (process
    # exit in production - the finally also lets a re-entered main() in the
    # same process, e.g. tests, re-acquire).
    singleton_fds = _acquire_singleton_locks(config)
    try:
        uvicorn.run(app, host=bind_host(config), port=config.port)
    finally:
        # Lifespan shutdown already stopped and joined the registration
        # thread AND unregistered (popping the id) - this is pure
        # belt-and-braces for exits that skipped the lifespan
        stop.set()
        if state.get("webhook_id") is not None:
            unregister_from_bus(config, state["webhook_id"])
        for fd in singleton_fds:
            os.close(fd)  # release the singleton locks


if __name__ == "__main__":
    main()
