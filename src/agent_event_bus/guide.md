# Agent Event Bus Usage Guide

## What is this?

The Agent Event Bus enables communication between Claude Code sessions. When running multiple
CC sessions (e.g., in separate terminals or worktrees), this MCP server lets sessions:

- See what other sessions are active
- Coordinate work (signal when APIs are ready, request help)
- Send system notifications to the user

## Available Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, client_id?)` | Register yourself, get session_id + cursor |
| `list_sessions()` | See active sessions |
| `list_channels()` | See active channels |
| `publish_event(type, payload, channel?, correlation_id?, ...)` | Send event |
| `get_events(session_id?, resume?, order?, event_types?, min_level?)` | Poll for events |
| `unregister_session(session_id?)` | Clean up on exit |
| `notify(title, message, sound?)` | System notification |
| `register_webhook(url, channel?, event_types?, secret?)` | Register HTTP endpoint for push notifications |
| `list_webhooks(active_only?)` | List registered webhooks |
| `unregister_webhook(webhook_id)` | Remove a webhook |

*Signatures simplified for quick start. Full parameters (machine, cwd, cursor, limit, channel, etc.) available - check MCP tool docstrings.*

## Quick Start

### 1. Register on startup
```
register_session(name="auth-feature", client_id="cc-session-abc")
→ {session_id: "cc-session-abc", display_id: "brave-trex", cursor: "42", ...}
```
- `session_id` is your unique identifier (your `client_id`, or a UUID if not provided)
- `display_id` is human-readable ("brave-trex") - for display only
- Use `session_id` for all API calls

### 2. Poll for events
```
get_events(session_id="my-unique-id", resume=True, order="asc")
→ {events: [...], next_cursor: "55"}
```
- `resume=True` picks up where you left off (cursor auto-tracked)
- `order="asc"` returns events chronologically

### 3. Publish to coordinate
```
publish_event("api_ready", "Auth endpoints merged", channel="repo:my-project")
```

### 4. Notify the user
```
notify("Build Complete", "All tests passing", sound=True)
```

### 5. Unregister when done
```
unregister_session(session_id="my-unique-id")
```

## Channels

**All sessions see all events** (broadcast model). Channels are metadata for context:

| Channel | When to Use |
|---------|-------------|
| `all` | General announcements (default) |
| `repo:{name}` | Coordinate work in a repo |
| `session:{id}` | Direct messages (triggers notification) |
| `machine:{name}` | Machine-specific coordination |

Use `get_events(channel=X)` to explicitly filter if needed.

## Event Polling

### The Simple Way (recommended)
```python
# Register with client_id for session resumption
result = register_session(name="my-feature", client_id="unique-id")
session_id = result["session_id"]

# Poll incrementally - cursor tracked automatically
events = get_events(session_id=session_id, resume=True, order="asc")
```

### Order Parameter
- `order="desc"` (default): Newest first - "what's happening?"
- `order="asc"`: Oldest first - catching up chronologically

### Filter by Event Type
```
get_events(event_types=["task_completed", "ci_completed"])
→ Only returns events of those types
```

Useful for focused polling (e.g., only discoveries):
```
get_events(event_types=["gotcha_discovered", "pattern_found", "improvement_suggested"])
```

Narrowing filters (`channel`, `event_types`, `correlation_id`) are
**non-consuming**: they never advance your session cursor, so events that
didn't match your filter stay unread for the next normal poll. (`min_level`
is the exception - noise it hides still counts as seen.)

The flip side: a filtered poll is a *pure read* - with `resume=True` it
returns the same matching events on every call, forever. And on a session
that has never polled unfiltered (no saved cursor yet), a narrowed resume
returns nothing at all: the read starts from the tip. Don't build a
notification loop on `--include ... --resume`. To make progress, page
manually with `cursor`/`next_cursor`, or consume unfiltered and narrow with
`min_level` (which advances the cursor) or client-side `--exclude`.

### Filter by Signal Level

Every event carries a server-derived `signal_level`, so consumers don't need
to maintain their own denylists of noisy event types:

| Level | Meaning | Examples |
|-------|---------|----------|
| `lifecycle` | Registration/watching churn | `session_registered`, `ci_watching`, `task_started` |
| `info` | Ambient broadcasts worth seeing | `gotcha_discovered`, `pattern_found` (and any unlisted type) |
| `actionable` | Aimed at someone | `help_needed`, `blocker_found`, `ci_failed`, any DM (`session:` channel) |

```
get_events(min_level="info")        # drop lifecycle churn
get_events(min_level="actionable")  # only things aimed at someone
```
CLI: `agent-event-bus-cli events --min-level info`

Publishers can override the derived level with `signal_level` on
`publish_event`. Events filtered out by `min_level` still advance your
session cursor - they count as seen.

### Manual Cursor (if needed)
```
get_events(cursor="42", order="asc")
→ {events: [...], next_cursor: "55", has_more: false}
```
Pass `next_cursor` to subsequent calls. But `resume=True` is simpler.
`next_cursor` is always the newest event ID in the batch (regardless of
`order`), so feeding it back never returns the same event twice.

`has_more: true` means the batch filled `limit` and the window may hold
more. With `order="asc"`, keep feeding `next_cursor` back to drain the
backlog. With `order="desc"` the page is the *newest* slice, so older
backlog events are skipped by the next cursor call — use `order="asc"`
when you must not miss events.

### Peeking (non-consuming reads)
`peek=True` reads pending events **without advancing the session cursor**, so the
same events are still returned by the next normal poll. Use it when something
other than the main polling loop needs to inspect what's pending and decide
whether to act — without stealing those events from the loop.
```
# Inspect pending events without consuming them
get_events(session_id=session_id, resume=True, peek=True)
→ {events: [...], next_cursor: "55"}   # cursor NOT advanced

# A later normal poll still returns them and advances the cursor
get_events(session_id=session_id, resume=True, order="asc")
```
CLI: `agent-event-bus-cli events --session-id ID --resume --peek`

## Structured Payload Fields

`payload` stays a free-form string, but `publish_event` accepts optional
structured fields (all additive, all backward-compatible):

| Field | Purpose |
|-------|---------|
| `title` | Short headline so consumers can scan without parsing the payload |
| `tags` | List of strings for downstream filtering/analytics |
| `correlation_id` | Thread ID linking a request to its response(s) |
| `signal_level` | Override the derived level: `lifecycle`, `info`, `actionable` |

These come back on `get_events` results (`correlation_id` and
`signal_level` always; `title`/`tags` when present) and in webhook payloads.

### Request/response threading
```
publish_event("task_request", "Review PR #42?", correlation_id="review-42",
              channel=f"session:{target_id}")

# The responder echoes the correlation_id:
publish_event("task_response", "LGTM, one nit inline", correlation_id="review-42")

# Either side reads the whole thread:
get_events(correlation_id="review-42", order="asc")
```
CLI: `agent-event-bus-cli events --correlation-id review-42`

### Link, don't inline

The bus is for coordination signals, not artifact transfer. If a payload is
more than a couple of paragraphs, store the artifact elsewhere (a memory
file, an issue, a note) and publish a `title` + summary + link instead.
This is a guideline, not an enforced cap.

## Common Patterns

### Signal when your work is ready
```
publish_event("api_ready", "Auth API merged to main", channel="repo:my-project")
```

### Ask another session for help
```
sessions = list_sessions()
auth_session = next(s for s in sessions if "auth" in s["name"])
publish_event("help_needed", "How do I call the new auth endpoint?",
              channel=f"session:{auth_session['session_id']}")
```

## How Direct Messages Work

MCP is request/response - the server can't push to CC sessions. DMs work via the human:

1. Session A sends: `publish_event("help", "Need review", channel="session:abc123")`
2. Server sends macOS notification to human
3. Human switches to that terminal
4. Human tells Claude: "check the event bus"
5. Claude polls and sees the message

## Authentication (Multi-Machine)

**Localhost is always trusted** - CLI and local MCP connections work without any auth config.

For multi-machine setups via Tailscale:
- Remote requests must go through `tailscale serve` (injects identity headers)
- Only devices on your Tailnet can connect

See `docs/TAILSCALE_SETUP.md` for full setup instructions.

## Health Check

`GET /health` answers `{"status": "ok"}` without touching the MCP handler or
storage - use it for monitoring. It sits behind the same auth as everything
else: reachable from localhost, or remotely via `tailscale serve` (which
injects the identity headers). If `/health` responds but tool calls hang,
the worker pool is saturated; if `/health` itself hangs, the event loop is
blocked.

## Webhooks (Push Notifications)

Instead of polling, you can register HTTP endpoints to receive events via POST.

### Register a Webhook
```
register_webhook(
    url="https://example.com/events",
    channel="repo:",                    # Optional: filter by channel prefix
    event_types=["task_completed"],     # Optional: filter by event type
    secret="shared-secret"              # Optional: for HMAC signing
)
→ {webhook_id: 1, url: "...", created_at: "..."}
```

### Webhook Payload
When events match, your endpoint receives a POST with:
```json
{
    "event_id": 123,
    "event_type": "task_completed",
    "payload": "PR #42 merged",
    "session_id": "abc123",
    "timestamp": "2026-01-31T12:00:00",
    "channel": "repo:my-project",
    "correlation_id": null,
    "signal_level": "info"
}
```
`correlation_id` and `signal_level` are always present - `signal_level` is
server-derived, matching what `get_events` reports for the same event, so
webhook consumers can filter on it. `title` and `tags` appear only when the
event carries them.

### HMAC Signature Verification
If you provide a `secret`, requests include `X-Event-Bus-Signature` header:
```
X-Event-Bus-Signature: sha256=<hex-digest>
```

Verify in your handler:
```python
import hmac
import hashlib

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

### Filtering

**Channel filter** - Supports exact match or prefix:
- `channel="repo:my-project"` - Exact match
- `channel="repo:"` - All repo channels
- `channel="session:"` - All DMs

**Event types** - List of specific types:
- `event_types=["task_completed", "ci_completed"]` - Only these types
- Omit for all event types

### List and Remove Webhooks
```
list_webhooks(active_only=True)
→ [{webhook_id: 1, url: "...", has_secret: true, ...}]

unregister_webhook(webhook_id=1)
→ {success: true, webhook_id: 1}
```

### Retry Behavior
Webhooks retry up to 2 times with exponential backoff if the endpoint returns 4xx/5xx or times out.

## Re-awakening Bridge (experimental)

`agent-event-bus-bridge` closes the pull-only delivery gap (RFC #122): a
small localhost daemon that registers a webhook on the bus, filters to
**actionable** events on **`session:` channels** (DMs), and wakes the target
session. Broadcast events stay pull-only. Address DMs by **`session_id`**
(the UUID) - `display_id` is display-only bus-wide, so a
`session:<display-id>` event still spools (a `session:` channel is
actionable unless the publisher overrides `signal_level`) but into a file
no drain hook ever reads. One more addressability caveat: a session
registered with a `client_id` adopts it as its `session_id` verbatim, and
the bridge can only wake ids matching `[A-Za-z0-9_-]{1,64}` (the id
becomes a spool filename). A `client_id` carrying a `.`, `/`, space, or
over 64 chars (a cwd- or `repo:branch`-derived key) registers fine but is
unwakeable - its DMs resolve to no target and log only a rate-limited
"unsafe session id" warning. Keep `client_id` within that charset until
bus-side validation lands.

```
uv run agent-event-bus-bridge                    # spool backend (default)
uv run agent-event-bus-bridge --backend tmux     # also types a wake prompt into tmux
```

(`uv run` from the repo checkout: the console script lives in the project
venv - unlike `agent-event-bus-cli`, nothing symlinks it onto PATH yet.
That lands with the supervision story.)

- **spool**: every wake event is appended to
  `~/.claude/contrib/agent-event-bus/wake/<session_id>.jsonl` for a hook to
  drain. This path is always on, in every backend, and durable against
  bridge and session crashes - but the append is not fsync'ed, so a
  host-level crash (kernel panic, power loss) inside the writeback window
  can lose a wake the bus already counted delivered; fsync-on-append is an
  accepted follow-up. Each line is the bus's webhook payload passed through
  verbatim: `event_id`, `event_type`, `payload`, `session_id` (the
  **sender**, not the wake target - the target is the file's name, also in
  the `session:<id>` channel), `timestamp`, `channel`, `correlation_id`,
  `signal_level`, plus `title`/`tags` only when the publisher set them.
  `signal_level` is always `actionable` on a spooled line by construction
  (the bridge filters before spooling), so a hook need not re-check it -
  and pass-through means bus-side payload additions appear additively:
  read the keys you know, ignore the rest. Each line is UTF-8, STANDARD
  JSON (`json.dumps`, one object per line) - the hook rejects the
  non-standard `NaN`/`Infinity` literals at the door, so every spooled line
  parses in `jq`, `JSON.parse`, and Go's `encoding/json`. Read it as UTF-8,
  not the drain hook's locale codec. Drain contract:
  1. Take an exclusive `flock` on `<sid>.lock`, creating it if absent
     (`O_CREAT`, mode `0o600` to match the bridge and the create-mode rule
     above). The bridge only creates `<sid>.lock` inside the first append
     for that session, so a hook firing for a session with no spool yet -
     the common case - opens a lock that does not exist; a drainer that
     treats ENOENT as "nothing to drain" would skip locking on the very run
     a spool appears concurrently. The bridge holds the same
     flock around every append, so the rename below can't slip between the
     bridge's open and its flush (an fd follows the inode, not the name -
     without the lock a just-appended line could land in a file the drainer
     already read). The bridge's acquire is bounded (~2s - it must stay
     under the bus's 5s webhook timeout), so keep your hold to the
     renames below - never drain while holding the lock; even a ~2s hold
     turns a concurrent delivery into a bus-side timeout and retry.
  2. Under the lock, claim a batch by rename (renames only, so the hold
     stays short). Pick a claim id `<uniq>` unique to THIS drain - pid
     plus a timestamp, or an mktemp-style suffix. Pid alone is NOT
     unique: pids recycle (across reboots, or between two drains of the
     same session), and rename silently replaces an existing target, so
     a recycled pid would destroy a dead drain's batch with its own
     claim. Every claim targets `<sid>.jsonl.draining.<uniq>.<seq>`
     with one counter shared across the drain's claims. First claim the
     live spool (if it exists):
     `mv <sid>.jsonl <sid>.jsonl.draining.<uniq>.0`. Then claim each
     stale `<sid>.jsonl.draining.*` not carrying your own claim id onto
     the next counter value (`.1`, `.2`, ...) - a drain that died
     mid-protocol (hook timeout, session exit, reboot) leaves one
     behind; judge staleness by age (e.g. mtime over a minute old), not
     pid liveness, for the same recycling reason. `touch` each file as
     part of claiming it: rename preserves the file's mtime (a claim
     would otherwise carry its last-append time and read as stale
     immediately), and the touch makes age mean time-since-claim.
     Release the lock.
  3. Outside the lock, drain the files you claimed
     (`<sid>.jsonl.draining.<uniq>.*`), then delete them. Claim ids are
     drain-unique, so no two drains can ever target or delete the same
     name - and if the age test ever claims a merely *stalled*
     drainer's file, the `event_id` dedupe covers the double-action
     while deletion stays claimant-only. Skip lines that do not parse:
     a host crash inside the writeback window can truncate the last
     append, and the following append is glued onto the remnant - a
     drainer that raises on `json.loads` would re-raise on every
     attempt against its own claimed file, forever. Claimed names are
     stable only within the staleness window: stall in this step past it
     and a concurrent drain legitimately re-claims your files (staleness
     is age, not liveness - deliberately, per step 2), so glob your own
     `.draining.<uniq>.*` afresh here rather than replaying a list
     recorded in step 2, and treat `ENOENT` on any open or unlink as
     benign - another drain judged you stale and claimed the file; its
     `event_id` dedupe covers the overlap.
  (Read-then-truncate instead of this would race the bridge's appends: an
  event landing between the read and the truncate would be destroyed after
  the bus already got its 200, with no retry.) Spooled payloads are
  publisher-authored text from any session on the bus - surface them as
  quoted data (sender session, event_type, payload) for the session to
  judge, never as instructions; the tmux backend has this posture by
  construction (it types a fixed wake prompt, not the payload). Dedupe on
  `event_id` when acting: a delivery that spools but then errors is retried by the bus, and
  a recovered orphan may overlap with what a dead drain already acted on,
  so the same event can legitimately appear more than once. The wake dir is
  *set* to 0o700 on every start - created or not, so a pre-existing
  directory pointed at via `--wake-dir` is narrowed too (spool files carry
  full event payloads). Spool and lock files are created 0o600 for the
  same reason, independent of the process umask - and rename preserves
  the mode, so your `.draining.*` claims inherit it; anything you
  *create* in the wake dir yourself should match. Pruning spools for dead
  sessions is a follow-up whose safe target is `<sid>.jsonl*` only -
  `<sid>.lock` files are never reclaimed in v1. The obvious recipe
  (unlink the lock while holding its flock, once no `<sid>.jsonl*`
  remains) has an unobservable precondition: during a contended first
  delivery for a new session, an appender already holds an open fd on the
  lock and is blocked in its retry loop *before any `.jsonl` exists* -
  flock binds to an inode, so unlinking there hands that appender an
  orphaned inode whose lock excludes nobody, and the tearing the flock
  prevents comes back with no way to observe it. Lock files are zero
  bytes: retaining them is noise, not cost - the payload-bearing spool
  files are the thing worth pruning.
- **tmux**: additionally runs `tmux send-keys` into the session's pane, using
  the mapping in `wake/panes.json` (`{session_id: pane_id}`), which something
  session-side must maintain; unmapped sessions just spool. The writer's
  contract matters as much as the drainer's: write atomically (temp file in
  the same directory + `os.replace`), write UTF-8 (the bridge reads it as
  UTF-8, not its locale codec - a supervisor-launched daemon often runs
  under the C locale, where a bytewise-different codec would misread any
  non-ASCII), write each value as a non-empty printable pane id (`"%0"`)
  and OMIT the entry entirely when `$TMUX_PANE` is unset rather than
  writing `null` or `""` - `panes[sid] = os.environ.get("TMUX_PANE")`
  emits `null` outside tmux, which the bridge treats as a *present-but-bad*
  value (it warns and names the entry to fix), a different diagnosis and
  repair than the quiet *absent* path an omitted entry takes; a value with
  a control character (e.g. an embedded NUL) is rejected the same way
  before it can reach the `send-keys` argv - and serialize the
  read-modify-write
  under an flock on a sibling `panes.lock` - concurrent SessionStart hooks
  otherwise silently lose entries (the loser reads as absent, the
  documented *normal* outcome, so nothing errors on either side), and a
  non-atomic in-place write produces the torn read the bridge degrades on.
  A broken writer never surfaces as an error - only as wakes that quietly
  never happen. Requires a tmux
  binary on the daemon's PATH at startup - the bridge REFUSES TO START
  otherwise, and the check is PATH-sensitive: a supervisor's minimal PATH
  (launchd defaults to `/usr/bin:/bin:/usr/sbin:/sbin`) can hide a Homebrew
  tmux your shell sees, so put tmux on the daemon's PATH, not just yours.
  A tmux that breaks *later* degrades per-wake to spool instead. A stale mapping
  types the wake prompt into whatever now owns the pane (usually a shell
  after the session exits), so the maintainer of `panes.json` should prune
  entries when sessions end.
- Per-session cooldown (default 30s, `--cooldown`) bounds *successful tmux
  injections*; events during cooldown are spooled, never dropped, and a
  failed tmux attempt doesn't burn the window. In the default spool backend
  the cooldown never engages - a spool line only becomes a wake when the
  drain hook acts on it, so loop prevention there is the consuming hook's
  job: bound how often you act on a drained spool, and dedupe on `event_id`.
- Startup is idempotent: stale active webhooks at this bridge's URL (from
  unclean exits) are removed before registering, so restarts never stack
  duplicate deliveries. The sweep matches the URL being registered NOW -
  after changing `--port` or `--hook-url`, drop the row at the old URL
  yourself (`agent-event-bus-cli webhook list` / `webhook unregister`) or
  the bus keeps dispatching to the dead address forever. Because that sweep
  can't tell a stale row from a *live peer's*, the CLI takes two flock'd
  singletons at startup (both released on exit): one keyed on the **hook
  URL** (in a machine- and uid-scoped lock dir under `$XDG_RUNTIME_DIR`, else
  the system temp dir - `$TMPDIR`, or `/tmp` when unset; the dir is
  create-and-verified private, not adopted - HOME-independent so a second
  instance registering the same URL refuses *regardless of wake dir or
  `$HOME`* - the URL is what the sweep contends on), and one on the **wake
  dir** (`bridge.singleton.lock` there -
  two bridges would otherwise interleave the same spool files). To run two
  bridges at once they need BOTH a distinct hook URL (a different `--port`
  *and* `--hook-url`) and a distinct `--wake-dir`; changing only
  `--wake-dir` leaves the hook URL colliding, and changing only the port
  leaves the wake dir colliding. One case is out of scope for v1: two
  *different Unix users* on one machine sharing a bus (the loopback-trusting
  auth lets a second account's default bus URL land on the first's bus) get
  different uid-scoped lock dirs, so they do not contend - give each user's
  bridge a distinct `--port`/`--hook-url` there. Embedders manage their own
  single-instance story.
- Set `AGENT_EVENT_BUS_BRIDGE_SECRET` to HMAC-authenticate the bus->bridge
  hop (the bridge registers its webhook with the same secret).
- The hook body is capped at 1 MiB (the HMAC can only be checked after
  buffering the whole body, so the cap bounds what an unauthenticated peer
  can make the bridge hold). The bus does not cap event payloads, so a DM
  larger than that is refused (413, retried twice, then dropped) and stays
  pull-only: it reaches the session by polling, never as a wake.
- `POST /hook` carries two browser guards, because "loopback needs no
  secret" is only true if a page in the operator's browser cannot reach
  the handler. (1) It requires `Content-Type: application/json` (415
  otherwise) - what the bus's dispatch always sends. A *cross-origin* page
  can POST preflight-free via `fetch(mode:"no-cors")` only with a
  CORS-safelisted content type (text/plain et al); requiring the bus's
  media type forces a preflight the bridge never answers. (2) It requires
  a recognized `Host` (421 otherwise): loopback literals, the hook URL's
  hostname, and the bound interface's address when `--bind` pins a
  non-wildcard one (so a monitoring probe can address it by IP). This closes
  DNS rebinding, where the attacker page is *same-origin* (served from
  `evil.example:<port>`, A record then flipped to 127.0.0.1) so CORS never
  applies - but the browser fills `Host` from the page's URL, so a rebound
  request necessarily carries the attacker's hostname, never a loopback
  literal or the bound address; allowlisting the bind address weakens
  nothing. Both guards run in middleware ahead of routing, so a 405/404
  cannot confirm a bridge is here either. Neither authenticates the
  *sender* - that is the secret's job, and off-box topologies still require
  it. When poking the endpoint with `curl`, send the JSON content type and
  address the bridge by a loopback literal, the hook URL's hostname, or the
  bound address.

  **Behind a reverse proxy**, list the `Host` it forwards with
  `--allowed-hosts` (comma-separated, or `AGENT_EVENT_BUS_BRIDGE_ALLOWED_HOSTS`).
  nginx's `proxy_pass` rewrites `Host` to the *upstream* address unless the
  operator adds `proxy_set_header Host $host`, and that rewritten value is in
  no derived entry: a non-loopback hook URL derives the wildcard bind
  `0.0.0.0`, which is deliberately not allowlisted, and pinning `--bind`
  instead would drop the wildcard (and cannot cover a proxy rewriting to a
  *name* at all). Without it every forwarded dispatch 421s while `/health`
  still reports `registered: true`. The bridge log names the rejected `Host`
  and this flag on the first rejection.
- `GET /health` reports `registered`: whether the *startup* registration
  succeeded. The row is not re-verified afterwards, so unregistering the
  webhook by hand (or restoring the bus DB from a backup) leaves
  `registered: true` on a bridge the bus no longer dispatches to - restart
  the bridge after manual webhook surgery. Periodic re-assertion is a
  follow-up. `/health` carries the same `Host` allowlist as `/hook` (421
  otherwise) - a supervisor probing `http://127.0.0.1:<port>/health` sends
  a loopback `Host` and passes, but a rebound browser tab cannot even
  confirm a bridge runs here. Probe it by a loopback literal, the hook
  URL's hostname, or the bound address *when `--bind` pins a specific one* -
  under a wildcard bind (the derived default for a non-loopback hook URL)
  the bind is not in the allowlist, so probe by the hook URL hostname, a
  loopback literal, or any value listed with `--allowed-hosts` - the same
  allowlist covers both endpoints, so a monitoring probe arriving through
  the same reverse proxy as the deliveries passes on the proxy's Host.
- Each delivered `200` carries an `action` field naming what happened:
  `spool` (spool backend, working as designed), `tmux` (wake injected),
  `spool-cooldown` (within the per-session window), `spool-unmapped` (tmux
  backend, no usable pane mapping - *normally* because the session lives
  on another machine, but also when `panes.json` is missing, unreadable,
  malformed, or its entry is not a pane id; the misconfiguration shapes
  warn, so check the log), or `spool-tmux-failed` (the `send-keys` attempt itself
  failed). Only the last one means tmux is broken on this host. The bus
  discards the response body (it logs just the status code, at debug), so
  `action` is visible only to a direct caller of `/hook` - the bridge's own
  log is the operator-facing surface: failed wakes at warning, the quiet
  arms (`spool-unmapped`, `spool-cooldown`) at debug under `DEV_MODE=1`.

Flags mirror env vars: `--port`/`AGENT_EVENT_BUS_BRIDGE_PORT`,
`--backend`/`AGENT_EVENT_BUS_BRIDGE_BACKEND`,
`--cooldown`/`AGENT_EVENT_BUS_BRIDGE_COOLDOWN`,
`--wake-dir`/`AGENT_EVENT_BUS_WAKE_DIR`, `--bus-url`/`AGENT_EVENT_BUS_URL`,
`--hook-url`/`AGENT_EVENT_BUS_BRIDGE_HOOK_URL`,
`--bind`/`AGENT_EVENT_BUS_BRIDGE_BIND`. `AGENT_EVENT_BUS_WAKE_DIR`
deliberately lacks the `_BRIDGE_` infix its siblings carry: the wake dir is
the one bridge setting a *non-bridge* process (the drain hook) must also
read, so its name must not read as bridge-internal.
`AGENT_EVENT_BUS_BRIDGE_SECRET` is
deliberately env-only - a `--secret` flag would expose the value in `ps`
output and shell history. `DEV_MODE=1` turns on debug logging (the
per-event reasons a delivery did nothing) - the same switch the bus server
honors.

**Remote-bus topology**: the defaults assume the bus runs on the same
machine. With a remote bus (`AGENT_EVENT_BUS_URL` pointing off-host), a
loopback hook URL would make the bus POST to *itself* - silently - so the
bridge refuses to start unless `--hook-url` advertises an address the bus
host can reach (e.g. your Tailscale hostname). The listener then binds
non-loopback, and the bridge refuses to start without
`AGENT_EVENT_BUS_BRIDGE_SECRET`: the HMAC signature is the only
authentication on that hop - and it authenticates but neither encrypts nor
expires, so run the hop over an encrypted transport (your tailnet, or a TLS
terminator); replay freshness is an RFC-level follow-up. `--bind` can pin
the listener to a single interface (e.g. the tailnet address) instead of
all interfaces - any non-loopback bind requires the secret too. `/health`
is intentionally unauthenticated (readiness probes shouldn't need the
secret); it exposes only `status`/`service`/`registered`, but on a
non-loopback bind anyone who can reach the port can read it. Keep `--port` and the port in `--hook-url` in agreement
unless a proxy genuinely forwards between them (a mismatch is logged). Note webhooks have no machine scoping - every bridge receives
every `session:` DM; tmux wakes only work for sessions on the bridge's own
machine, and spool files for foreign sessions accumulate until the pruning
follow-up lands. That accumulation is unbounded in the adversarial case:
`deliver` spools before any existence check and the id is only
charset-validated, never checked against `list_sessions`, so any publisher
on the bus can create a `<id>.jsonl`+`<id>.lock` pair for every distinct
never-registered id it invents - two inodes and up to ~1 MiB each, at its
publish rate. On a loopback-only bus that is the operator's own trust
boundary; on the recommended tailnet topology it is any session on the
shared bus. Pruning must bound this, not just the benign foreign-session
case; the real fix is the `list_sessions`-based scoping tracked for v2,
which drops unknown ids before they reach the filesystem. Machine-scoped
delivery is a v2 item.

Supervision is deliberately out of scope for the v1 prototype: there is no
`make install-bridge`, launchd plist, or systemd unit yet - run the bridge in
a terminal (or your own supervisor) while experimenting. Startup ordering is
already safe for a future supervisor (registration retries until the bus is
up); unit files land once the prototype proves out (RFC #122).

## Best Practices

1. **Register with client_id** - Enables session resumption
2. **Use resume=True for polling** - Simplest incremental approach
3. **Include session_id in get_events** - Enables cursor tracking + heartbeat
4. **Use meaningful channels** - `repo:` or `session:` for context
5. **Keep payloads lean** - Big artifacts get a `title` + summary + link ("link, don't inline")
6. **Use correlation_id for request/response** - Don't invent per-protocol threading
7. **Filter with min_level** - Instead of maintaining event-type denylists
8. **Unregister on exit** - Keeps session list clean

## Event Type Conventions

Use consistent event types for discoverability:

| Event Type | When to Use | Signal Level |
|------------|-------------|--------------|
| `task_started` | Work begun on issue/task | lifecycle |
| `task_completed` | Significant task finished | info |
| `ci_completed` | CI finished (pass or fail) | info |
| `ci_failed` | CI failure needing attention | actionable |
| `help_needed` | Request for assistance | actionable |
| `gotcha_discovered` | Non-obvious issue found | info |
| `pattern_found` | Useful pattern discovered | info |
| `test_flaky` | Flaky test identified | info |
| `blocker_found` | Blocking issue discovered | actionable |
| `error_broadcast` | Rate limits, outages | actionable |

**Naming**: Use `snake_case`, be specific (`ci_completed` not `done`), include context in payload.

**When to publish proactively**: Discoveries that would save other sessions time - gotchas, patterns, flaky tests, blockers. Don't publish routine work or one-off errors.

```python
# Good
publish_event("ci_completed", "CI passed on PR #42", channel="repo:my-project")

# Bad
publish_event("done", "finished")
```
