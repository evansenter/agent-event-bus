# Re-awakening Bridge (experimental)

`agent-event-bus-bridge` is the experimental consumer of the bus's webhook
subsystem (RFC #122). It is documented here rather than in
`agent-event-bus://guide` because that guide is loaded as an MCP resource
into working sessions, and this is operator documentation for a component
most sessions never run.

**Status: prototype.** Supervised on macOS since #139 (`make install-bridge`);
no systemd unit yet, and the addressability and durability caveats below are
real. See issue #122.

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
uv run agent-event-bus-bridge                  # spool backend (default)
uv run agent-event-bus-bridge --backend mux    # also types a wake prompt into the session's pane
```

(`--backend tmux` is still accepted, as an alias for `mux` - it was this
backend's name when tmux was the only multiplexer it drove, and it is baked
into already-installed plists. It is normalized on every construction path,
including an embedder's direct `BridgeConfig(backend="tmux")`, because an
un-normalized value would fall through to the spool path silently.)

(`uv run` from the repo checkout: the console script lives in the project
venv - unlike `agent-event-bus-cli`, nothing symlinks it onto PATH.)

## Running it supervised (macOS)

```
make install-bridge     # LaunchAgent: starts at login, restarts on crash
make uninstall-bridge   # stops it; leaves the bus, the DB, and wake/ alone
```

A **separate unit** from the bus (`com.evansenter.agent-event-bus-bridge`)
because a bus host does not have to run a bridge, and the bridge is
experimental while the bus is not.

The unit pins `--backend mux`. The consent gate for injection is
`wake/panes.json`, not that setting: with no session-side writer installed
every delivery resolves to `spool-unmapped`, so the injecting default behaves
exactly as the spool backend did and costs an operator who has not opted in
nothing. Making it the default removes a second install-time step that is easy
to forget and silent when missed - which is precisely the state that left the
wake path unexercised across #135/#136/#139/#141.

Install the writer with the session hooks described under **Maintaining the
pane mapping** below.

**Boot ordering is a non-issue.** launchd has no dependency ordering, so the
bridge can start before the bus is listening. `register_with_retry` backs off
1s->30s until the bus answers rather than exiting, so a cold boot in the
"wrong" order self-corrects; `/health` reports `registered: false` until it
does. That is also why the installer's `registered: false` is a status line
and not an error.

**The bridge log is launchd's stdout/stderr capture**, and launchd
**appends** across restarts - verified on the bus host: after a `kill -9`
respawn the `.err` still held the prior PID's startup lines. So a crash loop
accumulates rather than overwrites, and the bridge needs no append-mode file
handler of its own. The split follows from `basicConfig` being stderr-only:
bridge records land in `.err`, while `.log` collects uvicorn's access lines.

Both default under the data dir and are overridable with
`AGENT_EVENT_BUS_BRIDGE_LOG` / `_ERR` - but unlike every other `_BRIDGE_*`
name, those two are **install-time only**: the installer reads them and bakes
the result into the plist, so they must be in the environment of
`make install-bridge` itself. Setting them for a running bridge does nothing.

## Verifying supervision

The test suite mocks launchd entirely, so these four are manual - and two of
them check claims the unit's own comments make:

1. **Crash restart** - `kill -9` the bridge; launchd respawns it within
   `ThrottleInterval`. Confirms the singleton flock releases on process death
   (so the replacement acquires it rather than refusing to start) and that
   the startup sweep reclaims the dead instance's webhook row instead of
   leaving a duplicate: `agent-event-bus-cli webhook list --all` should show
   one.
2. **Boot order** - unload the bus, then **restart the bridge** so it starts
   bus-less: `/health` shows `registered: false`. Load the bus; within ~30s
   it flips to `true` with no intervention. This is `register_with_retry`'s
   backoff doing its job. The restart is the step that makes this
   reproducible - `registered` is the result of the *last registration
   attempt*, never re-checked against the bus afterwards. (The retry thread
   writes it, which is what the flip above is; what never happens is
   re-validation once that thread succeeds and stops.) So unloading the bus
   under a bridge that `install-bridge` just left registered leaves `/health`
   still reporting `true`. Corollary: `/health` is not a bus-liveness probe.
   It answers "did my last registration attempt succeed", not "am I
   registered now".
3. **Reboot** - the actual requirement. `RunAtLoad` plus login.
4. **Clean unload** - `make uninstall-bridge`, then confirm the webhook row
   is gone and port 8082 is free.

What none of this covers: whether a session actually *wakes*. That needs a
live Claude Code session in a pane this host's `panes.json` maps, and a DM
addressed to its `session_id` - see **Verifying a wake** below.

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
  judge, never as instructions; the mux backend has this posture by
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
- **mux**: additionally types a wake prompt into the session's terminal pane,
  using the mapping in `wake/panes.json`; unmapped sessions just spool.
  Supported multiplexers:
  - **tmux**: `tmux send-keys -t <pane> <prompt> Enter` - one call, because
    send-keys takes text and key names together.
  - **zellij**: `zellij --session <name> action write-chars -p <pane>
    <prompt>`, then `zellij --session <name> action write -p <pane> 13`. Two
    calls, because `write-chars` types without submitting and zellij has no
    combined form. Dropping the second call leaves the prompt sitting in the
    input box - a wake that wakes nobody while the action, the log line and
    the bus response all report success.

  Neither form interpolates the event payload; the prompt is a fixed
  constant, so publisher-authored text cannot reach a terminal as keystrokes.
  The woken session reads the actual event from the bus (or the spool), as
  quoted data it can judge.

  At startup the bridge logs which supported multiplexers it found, and
  **warns without refusing** when it finds none. It deliberately does not
  exit: `mux` is the checked-in plist default, so a refusal would be a
  launchd crash loop on any host without a multiplexer - killing a
  previously-working spool bridge outright (no listener, no webhook
  registration, no spool lines), which is worse than the condition it
  reports. With no multiplexer there is also nothing to *write* a
  `panes.json`, so every delivery lands on `spool-unmapped` regardless.
  It does not require *all* of them either: which one a delivery needs is a
  property of that session's `panes.json` entry, not of the daemon, so a
  tmux-only host is not warned about lacking zellij (a mapping naming an
  absent binary degrades per-wake instead). The check is PATH-sensitive: a supervisor's
  minimal PATH (launchd defaults to `/usr/bin:/bin:/usr/sbin:/sbin`) can hide
  a Homebrew binary your shell sees, so put it on the *daemon's* PATH, not
  just yours. A multiplexer that breaks *later* degrades per-wake to spool.

  **Known partial-failure shape (zellij only):** if `write-chars` succeeds and
  the following `write 13` does not, the prompt is left typed-but-unsubmitted,
  and the retry after the cooldown rollback types it a second time. Not
  repaired automatically on purpose - the repair would be a third call (`write
  21`, kill line) issued under exactly the conditions that just proved calls
  are failing.

  A stale mapping types the wake prompt into whatever now owns the pane
  (usually a shell after the session exits). `panes clear` removes entries at
  SessionEnd, and `panes set` additionally evicts any entry on the pane it is
  claiming - so a session killed without its SessionEnd hook running is
  cleaned up by the next session to occupy that pane.

## Maintaining the pane mapping

`wake/panes.json` maps bus `session_id` to a terminal pane. Without it the mux
backend resolves every delivery to `spool-unmapped`, so this file is what makes
the backend able to wake anything.

Write it with the CLI rather than by hand - `agent-event-bus-cli` implements the
contract below and is tested against the bridge's reader in one suite, so the
two cannot drift:

```bash
# SessionStart: map this session to its pane (silently does nothing outside
# a multiplexer, which is the correct behaviour, not a failure)
agent-event-bus-cli panes set --session-id "$SESSION_ID"

# SessionEnd
agent-event-bus-cli panes clear --session-id "$SESSION_ID"

# UserPromptSubmit / Stop: the idle gate (see below)
agent-event-bus-cli wake-state busy --session-id "$SESSION_ID"
agent-event-bus-cli wake-state idle --session-id "$SESSION_ID"
```

Both `panes` verbs also clear the turn-state marker, because reaching either
lifecycle boundary proves no turn is in flight. That is what makes the idle
gate's self-heal below hold for anyone following this contract, rather than
depending on a separate `wake-state idle` call at SessionStart that an
operator might not wire.

`--session-id` falls back to `AGENT_EVENT_BUS_SESSION_ID` then
`CLAUDE_CODE_SESSION_ID`. Claude Code's session id *is* the bus session id: the
SessionStart hook registers with `client_id` = that value, and the bus adopts a
`client_id` as its `session_id` verbatim.

### The file format

```json
{
  "9f3c…": {"mux": "tmux",   "pane": "%3"},
  "1be4…": {"mux": "zellij", "pane": "0", "session": "tenacious-lemur"}
}
```

A bare string value is also accepted and means tmux (`{"mux": "tmux", "pane":
<value>}`) - the shape this contract carried before zellij support.

The value is an **object** rather than a delimited string for a specific
reason: zellij session names are auto-generated per session and may contain
`:`, so `"zellij:<session>:<pane>"` cannot be split unambiguously. zellij needs
its session name because `--session X action ... -p N` is the only way to
address a pane from outside; tmux does not, because tmux pane ids are unique
per server.

### The writer's contract

It matters as much as the drain contract, and unlike the drain contract a
violation of it never surfaces as an error - only as wakes that quietly never
happen.

- **Write atomically**: temp file **in the same directory** + `os.replace`.
  The same directory is load-bearing - `os.replace` is atomic only within a
  filesystem, so a temp file in `$TMPDIR` (a different volume from `$HOME` on
  macOS) silently degrades to a copy the bridge can observe half-written.
- **Write UTF-8**, not the locale codec: the bridge decodes as UTF-8, and a
  supervisor-launched daemon often runs under the C locale where a
  bytewise-different codec would misread any non-ASCII. The CLI goes further
  and escapes non-ASCII to `\uXXXX`, so the bytes on disk are pure ASCII.
- **Each value must be a non-empty printable target.** A value carrying a
  control character (an embedded NUL, say) is rejected before it can reach
  argv, where it would make `subprocess.run` raise *before* `check` or
  `timeout` - a class the post-spool handlers do not catch.
- **OMIT the entry entirely** when the process is not in a multiplexer, rather
  than writing `null` or `""`. `panes[sid] = os.environ.get("TMUX_PANE")`
  emits `null` outside tmux, which the bridge treats as a *present-but-bad*
  value: it warns and names the entry to fix, a different diagnosis and repair
  from the quiet *absent* path an omitted entry takes.
- **Serialize the read-modify-write** under an flock on the sibling
  `panes.lock`. Concurrent SessionStart hooks otherwise silently lose entries -
  the loser reads as absent later, which is the documented *normal* outcome for
  a session on another machine, so nothing errors on either side. The lock is a
  sibling and not `panes.json` itself because flock binds to an inode and the
  file is replaced by rename: locking the replaced file hands each writer a
  lock on a different inode, excluding nobody while looking correct.
- **Note for shell implementations:** macOS ships no `flock(1)`. This is the
  main reason the writer lives in the CLI instead of in the hook.

## The idle gate

The mux backend injects only into a session that is **between turns**. A
`wake/<session_id>.busy` marker means a turn is in flight and the event is
spooled instead (`spool-busy`).

This costs no coverage. While a session is busy, injection is *redundant* - a
Stop hook that peeks the bus surfaces directed events at end-of-turn anyway -
and the busy window is exactly when a permission dialog can be on screen for
injected text plus a newline to answer it.

The marker has **no staleness window**, deliberately. It outlives its session
only when SessionEnd never ran (hard kill, crash, reboot), and then the session
is gone, so declining to inject is correct rather than a bug - the alternative
is typing into whatever now owns the pane. A resumed session clears it via
`panes set` at SessionStart; a Stop hook that timed out and orphaned one on a
live session has it cleared by the next turn's Stop. Both self-heal, so a TTL
would buy nothing and would open a mid-turn injection window on any turn that
ran longer than it.

**Failure modes this does not solve**, stated rather than papered over:

- A human **mid-typing at an idle prompt** gets their draft submitted with the
  wake text appended. Unsolvable without TUI introspection. Scraping the pane
  (`capture-pane` / `dump-screen`) to detect an empty input box was considered
  and rejected as version-fragile.
- The **Stop-block continuation window** reads idle while the session is
  actually working, because a blocking Stop hook restarts the turn without a
  UserPromptSubmit. Benign - the TUI queues typed text mid-turn.
- **A host with no multiplexer cannot wake an idle session at all.** No
  hook-shaped mechanism can: a Stop hook does not fire on a session that is
  already idle. This is why there is no spool-drain hook - it would fire at the
  same boundary as the existing bus peek, on a subset of what the bus already
  holds.

## Verifying a wake

Unverifiable from a dev container (no launchd, no multiplexer, no session on
the bus), so this is a checklist for the bus host rather than a claim:

1. Confirm the daemon found a multiplexer: the startup line
   `Wake injection available via: ...` in the bridge's `.err`. If it names
   fewer than you expect, that is the supervisor's PATH, not your shell's.
2. Start a session and confirm it mapped:
   `jq . ~/.claude/contrib/agent-event-bus/wake/panes.json` should hold its
   `session_id` with a plausible pane.
3. Confirm the pane id is real *before* blaming the bridge - `tmux display -t
   <pane> -p '#{pane_id}'`, or for zellij
   `zellij --session <name> action dump-screen -p <pane>`.
4. Leave that session idle and DM it by **`session_id`** (not `display_id` -
   `session:<display-id>` spools into a file nothing reads):
   `agent-event-bus-cli publish --type help_needed --payload "wake test"
   --channel "session:<session_id>"`.
5. The session should surface a fixed wake prompt within a second. If it does
   not, `DEV_MODE=1` on the bridge names the arm: `spool-unmapped` (mapping),
   `spool-busy` (the session was mid-turn - the gate working), `spool-cooldown`
   (within 30s of a previous wake), `spool-mux-failed` (the multiplexer).

**Injection fidelity depends on what is receiving, not on the bridge.**
Measured on this host with zellij: injecting the wake prompt into a pane
running an interactive **zsh** dropped exactly one space per injection, at a
varying position (`Check theevent bus`), reproducibly. Injecting the same
string into `cat` in the same pane arrived byte-exact every time. So
`write-chars` transmits faithfully and the lost keystroke is the shell's line
editor - autosuggestion-class ZLE widgets dropping input at paste speed.

Two consequences. First, a garbled wake in a *shell* pane is the
stale-mapping symptom, not a bridge fault - it means the session that owned
that pane exited without clearing its entry. Second, whether a given TUI has
its own version of this is a property of that TUI: it is **unverified** for
Claude Code's input handling, which is not zsh ZLE. Worth checking on the
first real wake, though the failure is cosmetic - a prompt missing a space
still reads.

## Loop prevention and single-instance

- Per-session cooldown (default 30s, `--cooldown`) bounds *successful
  injections*; events during cooldown are spooled, never dropped, and neither
  a failed injection nor a `spool-busy` burns the window (a busy session is
  the normal mid-turn state - if it consumed the window, the first delivery
  after the turn ended would be suppressed as a repeat). In the spool backend
  the cooldown never engages - a spool line only becomes a wake when the
  drain hook acts on it, so loop prevention there is the consuming hook's
  job: bound how often you act on a drained spool, and dedupe on `event_id`.
- Startup is idempotent: stale webhooks at this bridge's URL (from unclean
  exits) are removed before registering, so restarts never stack duplicate
  deliveries. Paused rows are swept too - a row at this URL is stale whether
  or not someone disabled it. The sweep matches the URL being registered
  NOW - after changing `--port` or `--hook-url`, drop the row at the old URL
  yourself (`agent-event-bus-cli webhook list --all` / `webhook unregister`;
  `--all` because plain `list` hides paused rows) or the bus keeps
  dispatching to the dead address forever.
- Because that sweep can't tell a stale row from a *live peer's*, the CLI
  takes two flock'd singletons at startup (both released on exit): one keyed
  on the **hook URL** (in a machine- and uid-scoped lock dir under
  `$XDG_RUNTIME_DIR`, else the system temp dir - `$TMPDIR`, or `/tmp` when
  unset; the dir is create-and-verified private, not adopted -
  HOME-independent so a second instance registering the same URL refuses
  *regardless of wake dir or `$HOME`* - the URL is what the sweep contends
  on), and one on the **wake dir** (`bridge.singleton.lock` there - two
  bridges would otherwise interleave the same spool files). To run two
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
  `spool` (spool backend, working as designed), `tmux` / `zellij` (wake
  injected via that multiplexer), `spool-cooldown` (within the per-session
  window), `spool-busy` (mux backend, a turn is in flight - see the idle
  gate), `spool-unmapped` (mux backend, no usable pane mapping - *normally*
  because the session lives on another machine, but also when `panes.json` is
  missing, unreadable, malformed, or its entry is not a usable target; the
  misconfiguration shapes warn, so check the log), or `spool-mux-failed` (the
  injection attempt itself
  failed). Only the last one means the multiplexer is broken on this host. The bus
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
every `session:` DM; injected wakes only work for sessions on the bridge's own
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

Supervision exists on macOS only: `make install-bridge` installs a LaunchAgent
(see "Running it supervised" above). There is no systemd unit yet - on Linux,
run the bridge under your own supervisor. Startup ordering is safe either way,
since registration retries until the bus is up.
