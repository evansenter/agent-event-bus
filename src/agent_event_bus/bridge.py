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

Loop prevention: a per-session cooldown (default 30s) bounds injections; an
event that arrives during cooldown is spooled instead, so nothing is lost.

Run:  agent-event-bus-bridge [--backend tmux] [--port 8082] ...
The bridge registers its own webhook on the bus at startup (HMAC-signed when
AGENT_EVENT_BUS_BRIDGE_SECRET is set) and unregisters it on clean shutdown.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_event_bus.cli import DEFAULT_URL, call_tool

logger = logging.getLogger("agent-event-bus-bridge")

DEFAULT_BRIDGE_PORT = 8082
DEFAULT_COOLDOWN_SECONDS = 30.0
DEFAULT_WAKE_DIR = Path.home() / ".claude" / "contrib" / "agent-event-bus" / "wake"

# tmux send-keys is bounded like the notifier subprocesses in helpers.py -
# a hung tmux must not wedge the bridge
TMUX_TIMEOUT = 5.0

# Bound on joining the registration thread at shutdown: long enough for an
# in-flight register call to commit, short enough not to hang exit
REGISTRATION_JOIN_TIMEOUT = 10.0

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


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Check the bus's X-Event-Bus-Signature HMAC against the raw body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


# Session ids are UUIDs (36 chars) or display ids ("brave-trex"); nothing
# else. The channel string is publisher-controlled wire input and the id
# becomes a spool filename, so path separators, "..", and any other
# unexpected byte must never reach the filesystem - the bus warns on
# malformed channels but does not reject them. The length bound keeps a
# too-long name from turning into an unretryable OSError out of the spool
# open (and caps spool-file blast radius).
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def resolve_target_session(event: dict) -> str | None:
    """Which session should this event wake?

    v1 wakes only for direct messages (session:<id> channels) - the
    unambiguous "aimed at exactly this session" case. Broadcast actionable
    events (help_needed on a repo channel, ...) have no single target and
    are left for normal polling.
    """
    channel = event.get("channel") or ""
    if not channel.startswith("session:"):
        return None
    target = channel.split(":", 1)[1]
    if not SESSION_ID_PATTERN.fullmatch(target):
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
        self.config.wake_dir.mkdir(parents=True, exist_ok=True)
        self.config.wake_dir.chmod(0o700)

    def deliver(self, session_id: str, event: dict) -> str:
        """Wake session_id for event. Returns the action taken:
        "tmux", "spool", or "spool-cooldown".

        The cooldown bounds successful injections only: spool writes are
        durable bookkeeping, not wakes, and a failed tmux attempt must not
        burn the window - the next event should retry (e.g. after a
        SessionStart hook repairs the pane mapping).
        """
        with self._lock:
            # Locked append: concurrent webhook deliveries would otherwise
            # interleave buffered writes and tear a JSON line
            self._spool(session_id, event)

            if self.config.backend != "tmux":
                return "spool"

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

        # tmux runs outside the lock (bounded at 5s, but other sessions'
        # deliveries shouldn't wait on it)
        if self._tmux_wake(session_id):
            return "tmux"
        with self._lock:
            # Roll back the reservation so the failure doesn't burn the
            # window - unless a later delivery already re-claimed it. There
            # is no previous value to restore: any entry that survived the
            # prune returned spool-cooldown above.
            if self._last_wake.get(session_id) == now:
                del self._last_wake[session_id]
        return "spool"

    def _spool(self, session_id: str, event: dict) -> None:
        """Always-on durable path: one JSON line per event, per session.

        Callers must hold self._lock.
        """
        spool_file = self.config.wake_dir / f"{session_id}.jsonl"
        # Defense in depth behind resolve_target_session's charset check: a
        # traversal or absolute component in a wire-supplied id must never
        # produce a write outside the wake dir
        if spool_file.resolve().parent != self.config.wake_dir.resolve():
            raise ValueError(f"Spool path escapes wake dir: {session_id!r}")
        with spool_file.open("a") as f:
            f.write(json.dumps(event) + "\n")

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

    def _tmux_wake(self, session_id: str) -> bool:
        """Type the wake prompt into the session's pane. False on any miss."""
        pane = self._tmux_pane(session_id)
        if pane is None:
            logger.info(f"No tmux pane mapping for {session_id[:8]}...; spooled only")
            return False
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
    injector: Injector | None = None,
    registration_state: dict | None = None,
    registration_stop: threading.Event | None = None,
) -> Starlette:
    """Build the ASGI app: POST /hook (webhook receiver) + GET /health.

    When registration_state/registration_stop are given, the app's lifespan
    owns bus registration: startup launches register_with_retry in a
    background thread (so the listener binds first and a down bus can't kill
    the daemon), and shutdown stops it and joins - stop can't interrupt a
    register call already inside its HTTP POST, so without the join a clean
    exit could read webhook_id before the thread writes it and leak the row.
    """
    injector = injector or Injector(config)
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
        yield
        if registration_thread is not None:
            registration_stop.set()
            registration_thread.join(timeout=REGISTRATION_JOIN_TIMEOUT)

    def process(body: bytes, signature: str | None) -> tuple[dict, int]:
        """Sync webhook handling (runs in a worker thread - the file appends
        and tmux subprocess must stay off the event loop, same invariant as
        the bus server's #112 fix)."""
        if config.secret and not verify_signature(body, signature, config.secret):
            return {"error": "bad signature"}, 401

        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            return {"error": "invalid JSON"}, 400

        # The bus always sends the derived signal_level; only actionable
        # events (DMs, help_needed, blockers, CI failures) justify a wake
        if event.get("signal_level") != "actionable":
            return {"status": "ignored", "reason": "below actionable"}, 200

        target = resolve_target_session(event)
        if target is None:
            return {"status": "ignored", "reason": "no target session"}, 200

        action = injector.deliver(target, event)
        return {"status": "delivered", "action": action, "session_id": target}, 200

    async def hook_endpoint(request: Request) -> JSONResponse:
        from starlette.concurrency import run_in_threadpool

        body = await request.body()
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

    return Starlette(
        routes=[
            Route("/hook", hook_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def bridge_hook_url(config: BridgeConfig) -> str:
    """The URL the bus POSTs to. Loopback by default (bus on this machine);
    --hook-url / AGENT_EVENT_BUS_BRIDGE_HOOK_URL overrides it with an
    address the bus host can reach when the bus is remote."""
    return config.hook_url or f"http://127.0.0.1:{config.port}/hook"


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    return host is None or host in LOOPBACK_HOSTS


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
    if isinstance(existing, list):
        for wh in existing:
            if wh.get("url") == hook_url and wh.get("webhook_id") is not None:
                call_tool(
                    "unregister_webhook", {"webhook_id": wh["webhook_id"]}, url=config.bus_url
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
        call_tool("unregister_webhook", {"webhook_id": webhook_id}, url=config.bus_url)
        logger.info(f"Unregistered bridge webhook #{webhook_id}")
    except SystemExit:
        # call_tool exits on connection errors; shutdown must not care
        logger.warning(f"Could not unregister webhook #{webhook_id} (bus unreachable)")


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
        except SystemExit as e:
            logger.warning(
                f"Registration on {config.bus_url} failed ({e}); retrying in {delay:.0f}s"
            )
        if stop.wait(delay):
            return
        delay = min(delay * 2, max_delay)


def _env_number(name: str, default, cast):
    """Read a numeric env var, turning a typo into a config error instead of
    a bare ValueError traceback out of build_parser()."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        raise SystemExit(f"Invalid {name}={raw!r}: expected a number") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agent-event-bus re-awakening bridge (RFC #122)")
    parser.add_argument(
        "--bus-url",
        default=os.environ.get("AGENT_EVENT_BUS_URL", DEFAULT_URL),
        help="Bus MCP endpoint to register the webhook on",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_number("AGENT_EVENT_BUS_BRIDGE_PORT", DEFAULT_BRIDGE_PORT, int),
        help=f"Localhost port to listen on (default: {DEFAULT_BRIDGE_PORT})",
    )
    parser.add_argument(
        "--backend",
        choices=["spool", "tmux"],
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_BACKEND", "spool"),
        help="Wake mechanism: spool file only, or tmux send-keys + spool",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=_env_number("AGENT_EVENT_BUS_BRIDGE_COOLDOWN", DEFAULT_COOLDOWN_SECONDS, float),
        help="Minimum seconds between wakes per session (loop prevention)",
    )
    parser.add_argument(
        "--wake-dir",
        type=Path,
        default=Path(os.environ.get("AGENT_EVENT_BUS_WAKE_DIR", str(DEFAULT_WAKE_DIR))),
        help="Directory for spool files and panes.json",
    )
    parser.add_argument(
        "--hook-url",
        default=os.environ.get("AGENT_EVENT_BUS_BRIDGE_HOOK_URL") or None,
        help="URL the bus POSTs events to (default: http://127.0.0.1:<port>/hook; "
        "must be an address the bus host can reach when the bus is remote)",
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
        port=args.port,
        backend=args.backend,
        # `or None`: an accidentally empty env var must not put registration
        # (which would skip the secret, so unsigned payloads) and verification
        # (which would demand signatures) on opposite sides - that 401s every
        # delivery silently
        secret=os.environ.get("AGENT_EVENT_BUS_BRIDGE_SECRET") or None,
        cooldown_seconds=args.cooldown,
        wake_dir=args.wake_dir,
        hook_url=args.hook_url,
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
            "host can reach (and set AGENT_EVENT_BUS_BRIDGE_SECRET)."
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
    # Loopback hook URL -> bind localhost only (the local bus is the sole
    # caller). A non-loopback hook URL means a remote bus must reach us, so
    # bind wide - at which point the HMAC secret is the only authentication.
    if _is_loopback(bridge_hook_url(config)):
        host = "127.0.0.1"
    else:
        host = "0.0.0.0"  # noqa: S104 - deliberate, guarded by warning below
        if not config.secret:
            logger.warning(
                "Hook URL is non-loopback and AGENT_EVENT_BUS_BRIDGE_SECRET is unset - "
                "anyone who can reach this port can inject wake events"
            )
    try:
        uvicorn.run(app, host=host, port=config.port)
    finally:
        # Lifespan shutdown already stopped and joined the registration
        # thread; the stop here only covers exits that skipped the lifespan
        stop.set()
        if state.get("webhook_id") is not None:
            unregister_from_bus(config, state["webhook_id"])


if __name__ == "__main__":
    main()
