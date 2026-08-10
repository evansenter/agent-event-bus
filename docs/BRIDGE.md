# Re-awakening Bridge (experimental)

`agent-event-bus-bridge` is the experimental consumer of the bus's webhook
subsystem (RFC #122). It is documented here rather than in
`agent-event-bus://guide` because that guide is loaded as an MCP resource
into working sessions, and this is operator documentation for a component
most sessions never run.

**Status: prototype.** No install target, no supervision story, and the
addressability and durability caveats below are real. See issue #122.

---

It closes the pull-only delivery gap: a small localhost daemon that
registers a webhook on the bus, filters to **actionable** events on
**`session:` channels** (DMs), and wakes the target session. Broadcast events stay pull-only. Address DMs by **`session_id`**
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

## Running it

```
uv run agent-event-bus-bridge                    # spool backend (default)
uv run agent-event-bus-bridge --backend tmux     # also types a wake prompt into tmux
```

(`uv run` from the repo checkout: the console script lives in the project
venv - unlike `agent-event-bus-cli`, nothing symlinks it onto PATH yet.
That lands with the supervision story.)

## Backends

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

## Loop prevention and single-instance

- Per-session cooldown (default 30s, `--cooldown`) bounds *successful tmux
  injections*; events during cooldown are spooled, never dropped, and a
  failed tmux attempt doesn't burn the window. In the default spool backend
  the cooldown never engages - a spool line only becomes a wake when the
  drain hook acts on it, so loop prevention there is the consuming hook's
  job: bound how often you act on a drained spool, and dedupe on `event_id`.
- Startup is idempotent: stale webhooks at this bridge's URL (from unclean
  exits) are removed before registering, so restarts never stack duplicate
  deliveries. Paused rows are swept too - a row at this URL is stale whether
  or not someone disabled it. The sweep matches the URL being registered
  NOW - after changing `--port` or `--hook-url`, drop the row at the old URL
  yourself (`agent-event-bus-cli webhook list --all` / `webhook unregister`)
  or the bus keeps dispatching to the dead address forever. Use `--all`:
  plain `webhook list` hides paused rows, so a disabled one at the old URL
  would look like nothing to clean up. Because that sweep
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

## Security and operation

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

## Delivery outcomes

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

## Flags and environment variables

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

## Remote-bus topology

The defaults assume the bus runs on the same
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

## Supervision

Supervision is deliberately out of scope for the v1 prototype: there is no
`make install-bridge`, launchd plist, or systemd unit yet - run the bridge in
a terminal (or your own supervisor) while experimenting. Startup ordering is
already safe for a future supervisor (registration retries until the bus is
up); unit files land once the prototype proves out (RFC #122).
