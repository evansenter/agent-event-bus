# Peer logging — "which process is registering?"

Operator instrumentation for [#145](https://github.com/evansenter/agent-event-bus/issues/145):
something registers and immediately unregisters a session ~1.5 times a minute
on the bus host, and nothing recorded *where* the calls came from.

**Temporary by intent.** Once the process is named, this can be removed —
delete this file and the `AGENT_EVENT_BUS_LOG_PEER` block in CLAUDE.md's
Operations section along with `_peer_label` / `PEER_LOGGED_TOOLS` in
`middleware.py`. Kept out of `CLAUDE.md` (and out of `guide.md`) for the same
reason as `BRIDGE.md`: CLAUDE.md loads into every session, and this is
operator material with a stated removal condition.

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

`unknown peer` is a prefix of `unknown peer (unparseable)` and the two carry
opposite meanings, so match the longer string first.

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

### The supervised bus does not see either switch

`scripts/com.evansenter.agent-event-bus.plist` templates only `PATH`,
`PYTHONPATH`, `AGENT_EVENT_BUS_ICON`, `_LOG` and `_ERR`, so neither
`AGENT_EVENT_BUS_LOG_PEER` nor `DEV_MODE` reaches a launchd-managed server.
On the bus host, either:

- add it to the plist's `EnvironmentVariables` and reload the LaunchAgent, or
- stop the service and run the bus in the foreground for the duration of the
  diagnosis.

Note that `make install-server` regenerates the plist from the template, so a
hand-edit does not survive a reinstall.
