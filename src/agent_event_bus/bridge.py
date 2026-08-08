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


def verify_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Check the bus's X-Event-Bus-Signature HMAC against the raw body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


# Session ids are UUIDs or display ids ("brave-trex"); nothing else. The
# channel string is publisher-controlled wire input and the id becomes a spool
# filename, so path separators, "..", and any other unexpected byte must never
# reach the filesystem - the bus warns on malformed channels but does not
# reject them.
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


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
        if target:
            logger.warning(f"Ignoring event with unsafe session id in channel {channel!r}")
        return None
    return target


class Injector:
    """Delivers wake-ups, bounded by a per-session cooldown."""

    def __init__(self, config: BridgeConfig):
        self.config = config
        self._lock = threading.Lock()
        self._last_wake: dict[str, float] = {}

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
            last = self._last_wake.get(session_id)
            if last is not None and (now - last) < self.config.cooldown_seconds:
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
            # window - unless a later delivery already re-claimed it
            if self._last_wake.get(session_id) == now:
                if last is None:
                    del self._last_wake[session_id]
                else:
                    self._last_wake[session_id] = last
        return "spool"

    def _spool(self, session_id: str, event: dict) -> None:
        """Always-on durable path: one JSON line per event, per session.

        Callers must hold self._lock. The wake dir is kept private (0o700):
        spool files carry full event payloads.
        """
        self.config.wake_dir.mkdir(parents=True, exist_ok=True)
        self.config.wake_dir.chmod(0o700)
        spool_file = self.config.wake_dir / f"{session_id}.jsonl"
        # Defense in depth behind resolve_target_session's charset check: a
        # traversal or absolute component in a wire-supplied id must never
        # produce a write outside the wake dir
        if spool_file.resolve().parent != self.config.wake_dir.resolve():
            raise ValueError(f"Spool path escapes wake dir: {session_id!r}")
        with spool_file.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def _tmux_pane(self, session_id: str) -> str | None:
        """Look up the session's tmux pane from <wake_dir>/panes.json."""
        panes_file = self.config.wake_dir / "panes.json"
        try:
            panes = json.loads(panes_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
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


def create_bridge_app(config: BridgeConfig, injector: Injector | None = None) -> Starlette:
    """Build the ASGI app: POST /hook (webhook receiver) + GET /health."""
    injector = injector or Injector(config)

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
        return JSONResponse({"status": "ok", "service": "agent-event-bus-bridge"})

    return Starlette(
        routes=[
            Route("/hook", hook_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ]
    )


def bridge_hook_url(config: BridgeConfig) -> str:
    return f"http://127.0.0.1:{config.port}/hook"


def register_with_bus(config: BridgeConfig) -> int | None:
    """Register this bridge's webhook on the bus. Returns webhook_id.

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
    webhook_id = result.get("webhook_id")
    if webhook_id is not None:
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
        except SystemExit:
            logger.warning(f"Bus unreachable at {config.bus_url}; retrying in {delay:.0f}s")
        if stop.wait(delay):
            return
        delay = min(delay * 2, max_delay)


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
        default=int(os.environ.get("AGENT_EVENT_BUS_BRIDGE_PORT", DEFAULT_BRIDGE_PORT)),
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
        default=float(os.environ.get("AGENT_EVENT_BUS_BRIDGE_COOLDOWN", DEFAULT_COOLDOWN_SECONDS)),
        help="Minimum seconds between wakes per session (loop prevention)",
    )
    parser.add_argument(
        "--wake-dir",
        type=Path,
        default=Path(os.environ.get("AGENT_EVENT_BUS_WAKE_DIR", str(DEFAULT_WAKE_DIR))),
        help="Directory for spool files and panes.json",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> BridgeConfig:
    """Build the runtime config from parsed args plus environment."""
    return BridgeConfig(
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
    )


def main():
    """Run the bridge daemon."""
    import uvicorn

    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = config_from_args(args)

    state: dict = {}
    stop = threading.Event()
    app = create_bridge_app(config)
    # Register from a startup hook: uvicorn binds right after startup
    # completes, so the thread's first attempt races the bind by microseconds
    # at most (and the bus retries deliveries anyway)
    app.add_event_handler(
        "startup",
        lambda: threading.Thread(
            target=register_with_retry, args=(config, state, stop), daemon=True
        ).start(),
    )
    try:
        # Localhost only: the bus is the sole intended caller, and HMAC (when
        # a secret is set) authenticates it
        uvicorn.run(app, host="127.0.0.1", port=config.port)
    finally:
        stop.set()
        if state.get("webhook_id") is not None:
            unregister_from_bus(config, state["webhook_id"])


if __name__ == "__main__":
    main()
