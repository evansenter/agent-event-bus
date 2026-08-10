"""Tests for the issue #112 hardening: event-loop offloading, bounded
notification subprocesses, SQLite concurrency pragmas, loop-safe webhook
dispatch, and the /health liveness route."""

import asyncio
import inspect
import subprocess
import threading
from datetime import datetime

from agent_event_bus import helpers, server
from agent_event_bus.storage import BUSY_TIMEOUT_MS, Event


class TestAsyncToolWrappers:
    """Tool functions must not block the server's event loop."""

    def _registered_tools(self):
        """Every @mcp.tool()-decorated function, taken from the registry.

        Enumerated rather than hand-listed: a hand-list silently stops
        covering the next tool someone adds, which is how set_webhook_active
        arrived uncovered. The registry cannot fall behind the code.

        Matched by TYPE, anchored on one known tool, rather than by duck-typing
        on .fn/.name - conftest's autouse fixture patches a MagicMock into this
        module, and a MagicMock answers hasattr for every name.
        """
        tool_type = type(server.register_session)
        tools = [v for v in vars(server).values() if isinstance(v, tool_type)]
        assert tools, "found no registered tools - has FastMCP's tool wrapper changed shape?"
        return tools

    def test_all_tools_are_async(self):
        """The #112 invariant: a blocking tool body freezes the whole server."""
        for tool in self._registered_tools():
            assert inspect.iscoroutinefunction(tool.fn), f"{tool.name} is not async"

    def test_tool_coverage_includes_every_known_tool(self):
        """Guard the guard: if the registry scan ever silently matches
        nothing (or stops matching some tools), the loop above passes
        vacuously. Pin the roster so a shape change fails loudly."""
        found = {tool.name for tool in self._registered_tools()}
        assert found == {
            "register_session",
            "list_sessions",
            "list_channels",
            "publish_event",
            "get_events",
            "unregister_session",
            "notify",
            "register_webhook",
            "list_webhooks",
            "set_webhook_active",
            "unregister_webhook",
        }, f"tool roster changed: {sorted(found)} - update this list and guide.md together"

    def test_wrappers_pass_through_end_to_end(self):
        async def main():
            registered = await server.register_session.fn(
                name="async-e2e",
                machine="test-machine",
                cwd="/home/user/project",
                client_id="async-e2e-client",
            )
            sid = registered["session_id"]
            published = await server.publish_event.fn(
                event_type="task_completed", payload="done", session_id=sid
            )
            polled = await server.get_events.fn(
                cursor=registered["cursor"], session_id=sid, order="asc"
            )
            return registered, published, polled

        registered, published, polled = asyncio.run(main())

        assert registered["name"] == "async-e2e"
        assert published["event_id"] is not None
        assert any(e["id"] == published["event_id"] for e in polled["events"])
        # The wrapper captured the loop for cross-thread webhook dispatch
        assert server._server_loop is not None


class TestNotificationTimeout:
    """A hung notifier subprocess must not wedge the caller."""

    def test_timeout_returns_false(self, monkeypatch):
        monkeypatch.setattr(helpers.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(helpers.shutil, "which", lambda name: "/usr/bin/terminal-notifier")

        def fake_run(cmd, **kwargs):
            assert kwargs.get("timeout") == helpers.NOTIFY_TIMEOUT
            raise subprocess.TimeoutExpired(cmd, helpers.NOTIFY_TIMEOUT)

        monkeypatch.setattr(helpers.subprocess, "run", fake_run)

        assert helpers.send_notification("title", "message") is False

    def test_all_subprocess_calls_pass_timeout(self, monkeypatch):
        """Every notification path (terminal-notifier, osascript, notify-send)
        must bound its subprocess."""
        seen_timeouts = []

        def fake_run(cmd, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))

            class Result:
                returncode = 0

            return Result()

        monkeypatch.setattr(helpers.subprocess, "run", fake_run)
        monkeypatch.setattr(helpers.shutil, "which", lambda name: f"/usr/bin/{name}")

        monkeypatch.setattr(helpers.platform, "system", lambda: "Darwin")
        helpers.send_notification("t", "m")

        monkeypatch.setattr(helpers.shutil, "which", lambda name: None)
        helpers.send_notification("t", "m")  # osascript fallback

        monkeypatch.setattr(helpers.platform, "system", lambda: "Linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
        helpers.send_notification("t", "m")

        assert len(seen_timeouts) == 3
        assert all(t == helpers.NOTIFY_TIMEOUT for t in seen_timeouts)


class TestSQLiteConcurrencyPragmas:
    def test_wal_and_busy_timeout_set(self, storage):
        with storage._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == BUSY_TIMEOUT_MS


class TestLoopSafeWebhookDispatch:
    def test_webhook_client_recreated_on_new_loop(self):
        """Reusing an AsyncClient across event loops hangs; each loop must get
        a fresh client."""

        async def get_client():
            return server._get_webhook_client()

        client1 = asyncio.run(get_client())
        client2 = asyncio.run(get_client())
        assert client1 is not client2

    def test_webhook_client_reused_on_same_loop(self):
        async def get_twice():
            return server._get_webhook_client(), server._get_webhook_client()

        client1, client2 = asyncio.run(get_twice())
        assert client1 is client2

    def test_dispatch_from_worker_thread_runs_on_server_loop(self, monkeypatch):
        """publish_event runs in a worker thread; its webhook dispatch must
        land on the server loop, not spawn a throwaway thread."""
        ran: dict = {}
        done = threading.Event()

        async def fake_dispatch(event):
            ran["loop"] = asyncio.get_running_loop()
            done.set()

        monkeypatch.setattr(server, "_dispatch_webhooks", fake_dispatch)
        event = Event(id=1, event_type="t", payload="p", session_id="s", timestamp=datetime.now())

        async def main():
            loop = asyncio.get_running_loop()
            # _run_sync captures the loop, then runs the schedule call in a
            # worker thread - exactly the publish_event code path.
            await server._run_sync(lambda: server._schedule_webhook_dispatch(event))
            for _ in range(500):
                if done.is_set():
                    break
                await asyncio.sleep(0.01)
            return loop

        loop = asyncio.run(main())
        assert done.is_set()
        assert ran["loop"] is loop


class TestHealthEndpoint:
    def test_health_bypasses_mcp(self):
        from starlette.testclient import TestClient

        with TestClient(server.create_app()) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["service"] == "agent-event-bus"


class TestDispatchStorageOffLoop:
    def test_webhook_lookup_runs_off_the_loop_thread(self, monkeypatch):
        """_dispatch_webhooks runs on the server loop; its SQLite lookup must
        be offloaded to a worker thread per the #112 invariant."""
        seen = {}

        def fake_matching(event):
            seen["thread"] = threading.current_thread()
            return []

        monkeypatch.setattr(server.storage, "get_matching_webhooks", fake_matching)
        event = Event(id=1, event_type="t", payload="p", session_id="s", timestamp=datetime.now())

        asyncio.run(server._dispatch_webhooks(event))

        # asyncio.run drove the loop on this thread; the lookup must not have
        # run there
        assert seen["thread"] is not threading.current_thread()


class TestHealthEndpointAuth:
    def test_health_sits_behind_tailscale_auth(self, monkeypatch):
        """With auth enabled, /health requires localhost or Tailscale identity
        headers - it is not an openly probeable endpoint."""
        from starlette.testclient import TestClient

        monkeypatch.delenv("AGENT_EVENT_BUS_AUTH_DISABLED", raising=False)

        with TestClient(server.create_app()) as client:
            # TestClient's IP is "testclient", not a trusted localhost address
            assert client.get("/health").status_code == 401
            resp = client.get("/health", headers={"tailscale-user-login": "user@example.com"})
            assert resp.status_code == 200
