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

Run:  uv run agent-event-bus-bridge [--backend tmux] [--port 8082] ...
(from the repo checkout - the console script lives in the project venv;
nothing puts it on PATH yet, that lands with the supervision story)
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

# The header name is a wire contract with the bus - import it rather than
# re-spelling it, the same coupling discipline as the tests building their
# signatures from the bus's _compute_signature. From helpers, NOT server:
# server.py opens/migrates the bus database and attaches its log handler at
# import time, none of which this pure HTTP client of the bus may trigger
# (see test_bridge_import_does_not_pull_in_the_bus_server).
from agent_event_bus.helpers import SIGNATURE_HEADER, WEBHOOK_CONTENT_TYPE

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
    backend: str = "spool"  # "spool" | "tmux"
    secret: str | None = None
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
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


class BridgeConfigError(ValueError):
    """An invalid BridgeConfig. A ValueError subclass so EMBEDDERS can catch
    it with a normal `except Exception` around app assembly - SystemExit is
    a BaseException and would tear through that. config_from_args translates
    it into SystemExit for the CLI path, which keeps main() printing the
    message and exiting without a traceback."""


def _now() -> float:
    """Monotonic-clock seam: tests freeze THIS, not time.monotonic itself -
    patching the stdlib module attribute would freeze the clock for every
    caller in the process (portal threads included), not just the code
    under test."""
    return time.monotonic()


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
# Same dead end round 26 documented for display_id, reached from the
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
        """Wake session_id for event. Returns the action taken: "tmux",
        "spool" (spool backend working as designed), "spool-cooldown",
        "spool-unmapped" (tmux backend, no usable pane mapping - normally
        because the session lives on another machine, since webhooks have
        no machine scoping, but also when panes.json is missing,
        unreadable, malformed, or its entry is not a pane id; the
        misconfiguration shapes warn, so check the log), or
        "spool-tmux-failed" (tmux backend, the send-keys
        attempt itself failed - the arm that means tmux on this box is
        broken). The action value is in-band for a direct caller of /hook
        only - the bus discards the response body - so operator-facing
        visibility is this module's log: failed wakes at warning, the quiet
        arms at debug under DEV_MODE.

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
        # noise - matches the below-actionable filter arm and _tmux_pane's
        # silent missing-file case.
        pane = self._tmux_pane(session_id)
        if pane is None:
            logger.debug(f"No tmux pane mapping for {session_id[:8]}...; spooled only")
            return "spool-unmapped"

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

        Durable against bridge and session crashes, not host crashes: the
        file is flushed and closed before the 200, but never fsync'ed, so a
        kernel panic or power loss inside the writeback window can lose a
        wake the bus already counted delivered. fsync-on-append is an
        accepted follow-up - it would eat into the SPOOL_LOCK deadline
        budget that must stay under the bus's WEBHOOK_TIMEOUT.

        Serialized by the per-session flock below - against other threads in
        this process and against the drain hook alike.

        A serialization failure (RecursionError out of json.dumps on a
        pathologically nested payload - the same recursion sibling the
        json.loads arms catch, one screen up in process()) is raised BEFORE
        any file is created or lock taken, and process() maps it to the
        named 400 every other wire-input path returns. Every OTHER raise
        here is retry-meaningful: nothing is durably stored, so the bus
        retry is real (unlike a post-spool error).
        """
        spool_file = self.config.wake_dir / f"{session_id}.jsonl"
        # Defense in depth behind resolve_target_session's charset check: a
        # traversal or absolute component in a wire-supplied id must never
        # produce a write outside the wake dir
        if spool_file.resolve().parent != self.config.wake_dir.resolve():
            raise ValueError(f"Spool path escapes wake dir: {session_id!r}")
        # Serialize BEFORE the lock/open: json.dumps can raise RecursionError
        # on a payload nested just under the parse limit (loads and dumps
        # spend their recursion budgets independently, and this call sits a
        # few frames deeper than the loads that admitted the body), and a
        # failure must create no file and take no lock. process() catches it.
        line = json.dumps(event) + "\n"
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
            # encoding="utf-8" explicitly, NOT the locale codec read_text()
            # defaults to: a supervisor hands the daemon no LC_ALL/LANG,
            # glibc resolves the C locale, and the default codec becomes
            # ASCII - a healthy panes.json with any byte >0x7f would then
            # raise UnicodeDecodeError and wrongly route into the
            # unparseable arm, leaving every session on the box permanently
            # spool-unmapped. The writer contract (guide) says UTF-8.
            panes = json.loads(panes_file.read_text(encoding="utf-8"))
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
        except (ValueError, RecursionError) as e:
            # Parse failures (JSONDecodeError, UnicodeDecodeError from a
            # torn write) - typically transient, self-healing on the
            # writer's next write. RecursionError is the non-ValueError
            # sibling, same as process()'s json.loads arm: deep nesting
            # blows the interpreter limit before any JSONDecodeError
            # exists, and an escape here 500s a delivery whose spool line
            # is already committed. Separate reason from the OSError arm: a
            # torn write escalating into a permanently unreadable file must
            # WARN again, not be demoted as a repeat of the same condition.
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
        if not isinstance(panes, dict):
            self._warn_panes_once(
                "not-an-object", "panes.json is not an object; treating as unmapped"
            )
            return None
        self._disarm_file_keys()  # the FILE parsed - file-level conditions cleared
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
        pane = panes[session_id]
        if not (isinstance(pane, str) and pane and pane.isprintable()):
            # Present but wrong-shaped (0 instead of "%0", "", null, or a
            # control character - JSON encodes NUL as \u0000): a
            # misconfiguration whose repair is nothing like "the mapping is
            # absent", so it must not fold into the unmapped debug line.
            # isprintable() is load-bearing, not cosmetic: an argv element
            # with an embedded NUL makes subprocess.run raise ValueError
            # BEFORE check or timeout - a class _tmux_wake's post-spool arms
            # don't catch - so it must be rejected here, where the warning
            # names the entry to repair, and never reach argv. Real pane ids
            # ("%0", "%12") are printable ASCII throughout.
            # Keyed per session: the condition is per entry, unlike the
            # file-level failures above.
            self._warn_panes_once(
                entry_key,
                f"panes.json entry for {session_id[:8]}... is not a pane id "
                f"({pane!r}); treating as unmapped",
            )
            return None
        self._warned_panes_keys.discard(entry_key)  # healthy entry re-arms it
        return pane

    _PANES_FILE_KEYS = frozenset({"unparseable", "unreadable", "not-an-object"})

    def _disarm_file_keys(self) -> None:
        """A healthy file read (or the normal missing-file state) clears the
        file-level conditions - and ONLY those; per-entry keys are cleared
        by their own session's healthy or absent reads."""
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
            # A working wake re-arms THIS session's failure conditions only -
            # clearing globally would oscillate a broken session's warning
            # under interleaved healthy deliveries (the round-38 shape).
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
            # OSError covers a missing or non-executable tmux binary. An
            # exception escaping here would 500 the webhook and make the bus
            # retry an event that is already durably spooled (duplicate
            # lines). Bounded like the panes guard - keyed per (session,
            # exception type): a persistent condition warns once per session
            # and then debugs, a different failure type warns fresh, and a
            # second broken session is never silenced by the first's key.
            key = f"{session_id}:{type(e).__name__}"
            message = f"tmux wake failed for {session_id[:8]}... ({e}); spooled only"
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
    # The /hook Host allowlist (see hook_endpoint): loopback literals plus
    # the hook URL's hostname - exactly the names a legitimate caller can
    # arrive under (the bus fills Host from the URL it was told to POST to;
    # local tools use a loopback literal). urlsplit().hostname already
    # lowercases and strips IPv6 brackets.
    hook_host = urllib.parse.urlsplit(bridge_hook_url(config)).hostname
    allowed_hook_hosts = {"127.0.0.1", "localhost", "::1"}
    if hook_host:
        allowed_hook_hosts.add(hook_host)
    registration_thread: threading.Thread | None = None
    # Version-skew line bound (a closure cell: the arm lives in process(),
    # not on the injector). The condition - a bus predating derived levels -
    # is persistent and per-deployment, so it gets the same first-sighting
    # treatment as every other stuck-condition line; re-armed by the first
    # delivery that DOES carry a level, which is what an upgraded bus sends.
    skew_state = {"warned": False}

    @asynccontextmanager
    async def lifespan(app):
        nonlocal registration_thread
        if registration_state is not None and registration_stop is not None:
            # A re-entered lifespan must register again: the previous
            # cycle's shutdown set() this event, and register_with_retry
            # checks it before its first attempt - without the clear, a
            # second cycle on the same app binds, serves /health, and never
            # registers (the silent shape the pair check above closes for
            # half-wired calls). main() constructs the event unset, so this
            # is a no-op on the first cycle.
            registration_stop.clear()
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
        # ValueError but not a JSONDecodeError). RecursionError is the
        # sibling case OUTSIDE ValueError: ~500k nested brackets fit well
        # under MAX_BODY_BYTES and blow the interpreter limit before any
        # JSONDecodeError can be raised. The bus retries any >=400
        # identically, so a 400 buys a clean named error in both logs
        # instead of a traceback per attempt - not fewer retries.
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
        except RecursionError:
            # json.dumps in _spool has the same non-ValueError recursion
            # sibling the json.loads arm above catches - and it runs a few
            # frames deeper, so a payload nested just under the parse limit
            # is admitted here and then fails to encode. Deterministic
            # (all three bus retries would raise identically), so answer
            # with the named 400 every wire-input path gives, not three
            # tracebacks. _spool serializes before it locks, so nothing is
            # stored and no lock is held when this fires.
            return {"error": "invalid JSON"}, 400
        return {"status": "delivered", "action": action, "session_id": target}, 200

    def _host_rejection(request: Request) -> JSONResponse | None:
        # DNS-rebinding guard, shared by /hook and /health: a page served
        # from evil.example:<our port> whose A record then flips to
        # 127.0.0.1 is SAME-origin with the bridge, so CORS never applies -
        # no preflight, arbitrary headers, the request reaches the handler.
        # What rebinding cannot forge is the Host header: the browser fills
        # it from the page's URL, so a rebound request necessarily carries
        # the attacker's hostname, never a loopback literal or the hook URL
        # host the bus was told to POST to. Both routes need it - /hook so
        # the write is refused, /health so a rebound tab cannot even
        # CONFIRM a bridge is running here (the signal that the probe is
        # worth the round trip). A supervisor probing 127.0.0.1/health
        # sends a loopback Host and still passes. Hand-rolled rather than
        # TrustedHostMiddleware: its host.split(":")[0] mangles bracketed
        # IPv6 ("[::1]:8082" -> "["), and --bind ::1 is a supported shape.
        # 421 Misdirected Request.
        raw_host = request.headers.get("host", "")
        if raw_host.startswith("["):  # bracketed IPv6, with or without port
            host = raw_host.split("]", 1)[0].lstrip("[").lower()
        else:
            host = raw_host.rsplit(":", 1)[0].lower()
        if host not in allowed_hook_hosts:
            return JSONResponse({"error": f"unexpected Host {raw_host!r}"}, status_code=421)
        return None

    async def hook_endpoint(request: Request) -> JSONResponse:
        rejected = _host_rejection(request)
        if rejected is not None:
            return rejected
        # The other half of the browser guard, for CROSS-origin pages:
        # fetch(mode:"no-cors")
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
        # Starlette header lookup is case-insensitive, so the canonical-case
        # constant works directly
        signature = request.headers.get(SIGNATURE_HEADER)
        payload, status = await run_in_threadpool(process, body, signature)
        return JSONResponse(payload, status_code=status)

    async def health(request: Request) -> JSONResponse:
        # Same DNS-rebinding Host guard as /hook: a rebound tab must not
        # even confirm a bridge runs here. A supervisor's loopback probe
        # passes; the readiness-probe story is intact for the callers that
        # matter.
        rejected = _host_rejection(request)
        if rejected is not None:
            return rejected
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
    interfaces. validate_config requires the HMAC secret whenever the
    effective bind OR the hook URL is non-loopback - on every path, not
    just the CLI (the refusal moved there in round 41 so it travels with a
    hand-built config onto the embedding paths)."""
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


def register_with_bus(config: BridgeConfig) -> int:
    """Register this bridge's webhook on the bus. Returns webhook_id;
    raises on any failure - SystemExit for this module's own named checks
    and for call_tool's connection-error arm, the original transport
    exception (debug=True) for everything else. register_with_retry treats
    both shapes as retry-with-backoff.

    Idempotent: an unclean exit (SIGKILL, crash, reboot) skips main()'s
    finally, leaving a stale active webhook at this URL - and the bus neither
    dedupes by URL nor deactivates failing hooks, so each stale row would
    duplicate every wake. Remove matching URLs before registering.
    """
    hook_url = bridge_hook_url(config)

    # debug=True: call_tool would otherwise funnel every failure into a bare
    # SystemExit(1) with the real cause printed to stderr - a stream a
    # supervisor may discard - leaving the retry loop logging
    # "SystemExit(1)" for a 401, timeout, and bad body alike. With the flag,
    # those re-raise and their repr reaches the daemon's own log; the one
    # failure still funneled into a bare exit is a connection error, which
    # the retry loop renders as exactly that.
    existing = call_tool("list_webhooks", {"active_only": True}, url=config.bus_url, debug=True)
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
                "unregister_webhook",
                {"webhook_id": wh["webhook_id"]},
                url=config.bus_url,
                debug=True,
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
        debug=True,
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
        result = call_tool(
            "unregister_webhook", {"webhook_id": webhook_id}, url=config.bus_url, debug=True
        )
    except SystemExit:
        # With debug=True the only failure call_tool still funnels into
        # SystemExit is its connection-error arm - so this log line now
        # reports a diagnosis it actually observed
        logger.warning(f"Could not unregister webhook #{webhook_id} (bus unreachable)")
        return
    except Exception as e:
        # Everything else re-raises with its cause (401, timeout, bad
        # body); shutdown stays best-effort, but the one log line an
        # operator has when a row leaks should carry the real reason
        logger.warning(f"Could not unregister webhook #{webhook_id} ({e!r})")
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
        # SystemExit is call_tool's failure shape; Exception covers the
        # transport errors debug=True re-raises and anything unexpected
        # inside register_with_bus - either way the thread must back off
        # and retry, never die silently
        except (SystemExit, Exception) as e:
            # With debug=True at the call sites, a BARE SystemExit(1) means
            # exactly one thing - call_tool's connection-error arm - so name
            # it instead of logging "SystemExit(1)" for every cause alike
            # (the real reason went to stderr, a stream a supervisor may
            # discard). The bridge's own SystemExits carry their message.
            reason = (
                "bus unreachable (connection error)"
                if isinstance(e, SystemExit) and e.code == 1
                else repr(e)
            )
            logger.warning(
                f"Registration on {config.bus_url} failed ({reason}); retrying in {delay:.0f}s"
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

    # argparse `choices` only guards command-line values, and an embedder
    # skips argparse entirely - an unknown backend would silently mean
    # "spool" (deliver only tests != "tmux")
    if config.backend not in ("spool", "tmux"):
        raise BridgeConfigError(
            f"Invalid backend {config.backend!r} (check AGENT_EVENT_BUS_BRIDGE_BACKEND): "
            "expected 'spool' or 'tmux'"
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
                    "(check --bind / AGENT_EVENT_BUS_BRIDGE_BIND): expected an IP address"
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
        wake_dir=args.wake_dir,
        hook_url=args.hook_url,
        bind=args.bind,
    )
    # Translate the embedder-catchable error into the CLI's exit shape -
    # main() prints the message and exits without a traceback
    try:
        validate_config(config)
    except BridgeConfigError as e:
        raise SystemExit(str(e)) from None
    # Preflight the binary (CLI path only - embedders manage their own
    # runtime env): a tmux backend on a box without tmux would degrade
    # every wake to spool-tmux-failed for the daemon's lifetime - one
    # startup message beats discovering it per-DM, and the check is
    # PATH-sensitive, so it names the fix. A tmux that breaks LATER is
    # still handled (and bounded) by the wake-failure guard.
    if config.backend == "tmux" and shutil.which("tmux") is None:
        raise SystemExit(
            "Backend is tmux but no tmux binary is on PATH of THIS process "
            "(a supervisor's minimal PATH can hide a tmux your shell sees; "
            "check --backend / AGENT_EVENT_BUS_BRIDGE_BACKEND)"
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
