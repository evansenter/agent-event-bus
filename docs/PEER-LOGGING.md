# Peer logging — "which process is registering?"

Operator instrumentation for [#145](https://github.com/evansenter/agent-event-bus/issues/145):
something registers and immediately unregisters a session ~1.5 times a minute
on the bus host, and nothing recorded *where* the calls came from.

Kept out of `CLAUDE.md` (and out of `guide.md`) for the same reason as
`BRIDGE.md`: CLAUDE.md loads into every session, and this is operator material
with a stated removal condition.

## Removing it

**Temporary by intent.** Once the process is named, delete all of the
following — a partial removal leaves a dead helper, a vestigial parameter, or
a dangling pointer:

`src/agent_event_bus/middleware.py`
- `PEER_LOGGED_TOOLS`
- `_peer_logging_enabled()`
- `_peer_label()`
- the `peer` plumbing: `peer = _peer_label(scope)` in
  `RequestLoggingMiddleware.__call__`, the extra argument to
  `_log_tool_call(..., peer)`, that function's `peer` parameter, and the
  `peer_suffix` it builds

  Leave the `(scope.get("client") or ...)` / `(scope.get("headers") or ...)`
  reads in `TailscaleAuthMiddleware` alone — those fix a real crash on an
  explicit `None` and are not part of this instrumentation.

`tests/test_middleware.py`
- `TestPeerLogging` in full
- `TestTailscaleAuthMiddleware::test_explicit_null_client_does_not_raise` and
  `::test_explicit_null_headers_does_not_raise` **stay** — they pin the
  crash fix above, not the peer label

`CLAUDE.md` — several references, not one (no count here: the number is the
part that goes stale when the next one is added):
- the `AGENT_EVENT_BUS_LOG_PEER` block in Operations
- the `(also turns on peer logging below)` parenthetical on the `DEV_MODE=1`
  line — it dangles twice over, since "below" points at a block that is gone
  and the side effect it describes disappears with `_peer_logging_enabled()`
- `_LOG_PEER` in the environment-variable list (Naming Conventions)
- the `docs/PEER-LOGGING.md` line in the Architecture tree
- the See Also entry

`docs/PEER-LOGGING.md`
- this file

Then, last, grep for every name. This step is self-erasing — it runs after
this file is gone, because deleting it changes the output — so copy the
command out first. To recover it afterwards, mind which side of the commit
you are on: `git show HEAD:docs/PEER-LOGGING.md` while the deletion is still
uncommitted, `git show HEAD~1:docs/PEER-LOGGING.md` once it has landed.

The identifiers match neither `LOG_PEER` nor `PEER-LOGGING`, so a narrower
grep comes back clean over a half-finished removal and reads as confirmation:

```bash
git grep -E "LOG_PEER|PEER-LOGGING|_peer_label|_peer_logging_enabled|PEER_LOGGED_TOOLS|peer_suffix|_log_tool_call"
```

`git grep`, not `grep -r`: the latter descends into ignored artifacts, and
`htmlcov/` is a rendered copy of the source — so a stale coverage report keeps
reporting `_peer_label` long after a correct removal, and the operator goes
hunting for a leftover that is not there. Tracked files are exactly the set
the checklist above enumerates, and stay so as new ignored artifacts appear.

`_log_tool_call` is in the list to put its **signature** in view: if a removal
takes `_peer_label` and `peer_suffix` but leaves the `peer` parameter behind,
the only surviving token is the bare word `peer`, which is useless to grep —
so that one leftover is visible here or not at all. Its handful of hits are
readable by eye.

This, not the bullet list, is the part that does not rot.

## Turning it on

```bash
AGENT_EVENT_BUS_LOG_PEER=1 agent-event-bus
```

Appends the caller's peer to the `register_session` log line:

```
register_session(name="x") → session=brave-trex from 127.0.0.1:54321
```

Then, while the calls are live:

```bash
lsof -i :54321
```

The **port** is the identifying half of a *direct* connection — the bus
listens on loopback, so the address alone rarely narrows anything.

To turn it back off, **unset it**. Any non-empty value is truthy, so `=0`
still enables it. That is the repo-wide idiom (see `helpers.py`, `server.py`,
`bridge.py`), but this is the one documented with an explicit `=1`, which
invites reaching for `=0`.

### Why not `DEV_MODE=1`

`DEV_MODE` also enables peer logging, but it is not a *quiet* switch: it fires
a desktop notification per tool call (`_dev_notify`) and drops the logger to
DEBUG. On a host with the #145 churn that is ~1.5 notifications per minute for
as long as you watch, plus every real session.

The trade runs both ways — `DEV_MODE` buys you console output and charges you
the notifications. Neither switch is on by default.

### Where to watch

`agent-event-bus.log` (`make logs`) — **not** the console. The console handler
is created only under `DEV_MODE` (`server.py`), so a foreground run with
`AGENT_EVENT_BUS_LOG_PEER` alone prints nothing to the terminal. The lines are
there, in the file, at INFO.

### Scope

Only `register_session` carries the peer. `get_events` runs every few seconds
per session and would drown the log you are reading it from.

## Reading the label

| Form | Meaning |
|------|---------|
| `127.0.0.1:54321` | Direct — the port names the caller |
| `... via tailscale` | Proxied; see the caveat below |
| `unknown peer` | The server reported no peer — nothing to chase |
| `unknown peer (unparseable)` | A peer **exists** and the label could not render it — a bug on that line, not a dead end |
| `... - headers unreadable` | The port is good, the identity check failed — so a *missing* `via tailscale` here is **not** evidence of a local caller |
| *no `from …` at all* | Check the tool name first: only `register_session` carries a peer, so unsuffixed `get_events` lines are normal and mean nothing is wrong (see [Scope](#scope)). Otherwise the switch is not reaching the process — either you are watching the console instead of the log (see [Where to watch](#where-to-watch)), or the bus is supervised and never saw the variable (see [the supervised-bus caveat](#the-supervised-bus-does-not-see-either-switch--on-either-platform)) |

`unknown peer` is a prefix of `unknown peer (unparseable)` and the two carry
opposite meanings, so match the longer string first.

IPv6 hosts are **bracketed** — `[::1]:54321`, `[fd7a:115c:a1e0::1]:54321` —
which is the form `lsof`/`ss` print back. Unbracketed, there would be no way
to see where the address ends and the port begins, and the port is the half
you came for.

## Caveats

### `tailscale serve`

A proxied request is terminated by the **local** tailscaled, so the peer is
tailscaled's socket: `lsof` names tailscaled while the real caller is
somewhere on the tailnet. Those lines are marked:

```
register_session(name="x") → session=brave-trex from 127.0.0.1:54321 via tailscale
```

Tailscale's identity header is the only thing in the ASGI scope that
distinguishes them, and loopback bypasses auth — so the marker narrows the
candidates, it does not authenticate. An unmarked loopback peer is a local
process as far as anything in the scope can tell: `tailscale serve` is the
only proxy this deployment puts in front of the bus, but nothing here proves
the absence of another.

### The supervised bus does not see either switch — on either platform

Both service templates carry the same short set of variables (`PYTHONPATH`,
`AGENT_EVENT_BUS_ICON`, `_LOG`, `_ERR`; the plist adds `PATH`), so neither
`AGENT_EVENT_BUS_LOG_PEER` nor `DEV_MODE` reaches a supervised server.

The simplest route is to **not use the service at all**: stop it and run the
bus in the foreground for the duration of the diagnosis.

To keep it supervised, note that a reload acts on the **installed** unit, not
on the repo template — editing `scripts/…` alone changes nothing until an
install regenerates the copy the service manager actually reads:

| | macOS (launchd) | Linux (systemd) |
|---|---|---|
| **Template** (in-repo) | `scripts/com.evansenter.agent-event-bus.plist` | `scripts/agent-event-bus.service` |
| **Installed** (what runs) | `~/Library/LaunchAgents/com.evansenter.agent-event-bus.plist` | `~/.config/systemd/user/agent-event-bus.service` |
| **What to add** | a key in `EnvironmentVariables` | `Environment=AGENT_EVENT_BUS_LOG_PEER=1`, under `[Service]` — beside the other `Environment=` lines |
| **Apply an installed-unit edit** | `launchctl unload` + `load` (restarts the process) | `systemctl --user daemon-reload` **and** `systemctl --user restart agent-event-bus` |

Two paths, and they differ in what a reinstall does to them:

- **Edit the template, then `make install-server`** — regenerates the
  installed unit and restarts. Slower, but the change survives future
  reinstalls, which matters if the diagnosis runs long.
- **Edit the installed unit, then apply as above** — faster, and wiped the
  next time `make install-server` regenerates from the template.

On systemd, `daemon-reload` alone is not enough: it re-reads unit files while
the running service keeps its old environment, so the log never grows a
`from …` suffix and the switch looks broken. `make restart` covers both
platforms and is equivalent to the restart half.
