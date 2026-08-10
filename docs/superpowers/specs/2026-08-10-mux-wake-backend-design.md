# Waking an idle session: the mux backend (RFC #122)

Design record for closing the last gap in RFC #122. Status at the time of
writing: the bridge is built, supervised (#139), and hardened (#135/#136/#141),
and has never woken anything.

## Why the wake path is inert

Three independent reasons, each sufficient on its own. Recorded in full on
issue #122; summarized here because the third one shapes the design.

1. `scripts/com.evansenter.agent-event-bus-bridge.plist` pins
   `AGENT_EVENT_BUS_BRIDGE_BACKEND=spool`. Only the tmux backend injects.
2. Nothing writes `wake/panes.json`. Every reference in this repo is a reader,
   a test fixture, or prose; `dotfiles` has no reference to `panes.json` or the
   wake dir at all.
3. **The bus host does not run tmux.** `mac-mini.local` runs zellij. `$TMUX`
   and `$TMUX_PANE` are unset, no tmux server runs, and `hooks/tmux-status.sh`
   is not wired into `settings.json` — `zj-status.sh` holds the
   `UserPromptSubmit`/`Stop` slots.

(3) is what makes a literal reading of the brief insufficient. A writer built
exactly to the documented contract — which correctly says to **omit** the entry
when `$TMUX_PANE` is unset — would omit *every* entry on this host, producing a
syntactically perfect and permanently empty mapping. It would fail silently,
because an omitted entry is the documented quiet-absent path that deliberately
does not warn.

## Decisions

### Value shape: objects, not delimited strings

`panes.json` values become objects:

```json
{
  "9f3c…": {"mux": "tmux",   "pane": "%3"},
  "1be4…": {"mux": "zellij", "pane": "0", "session": "tenacious-lemur"}
}
```

A string encoding (`"zellij:<session>:<pane>"`) was rejected on evidence:
zellij session names are auto-generated per session and **may contain `:`** —
verified by creating and successfully targeting a session named `has:colon` —
so the encoding is ambiguous by construction.

A bare string value remains accepted, meaning `{"mux": "tmux", "pane": <value>}`.
There is no deployed writer to be compatible with; the affordance costs ~3 lines
and keeps the existing contract text and its tmux test coverage meaningful.

Validation, all failures degrading to "unmapped" via the existing warn-once
paths: `mux ∈ {tmux, zellij}`; `pane` a non-empty printable string; `session`
required, non-empty and printable, when `mux == "zellij"`. The `isprintable()`
check stays load-bearing for the same reason as before — an argv element with an
embedded NUL makes `subprocess.run` raise before `check` or `timeout`, a class
the post-spool arms do not catch.

Everything else in the writer contract is unchanged: atomic temp-file +
`os.replace` in the same directory, UTF-8 rather than the locale codec, flock on
a sibling `panes.lock`, omit the entry when no pane id is available, `0600`.

### Injection commands

- tmux: `tmux send-keys -t <pane> <WAKE_PROMPT> Enter` (unchanged).
- zellij: `zellij --session <session> action write-chars -p <pane> <WAKE_PROMPT>`
  followed by `zellij --session <session> action write -p <pane> 13`.

Two calls because `write-chars` does not submit; `write 13` sends the carriage
return separately. This preserves the existing security posture by construction:
a fixed prompt is typed, never the publisher-authored payload.

Verified on the bus host, under an env stripped to launchd's
(`env -i PATH=/opt/homebrew/bin:… HOME=…`, no `ZELLIJ*` vars, no TTY): the text
landed in the targeted pane of a detached background session and executed.
Cross-tab addressing verified too — pane 0 in tab 1 accepted a write while tab 2
was focused.

### Actions

`tmux` and `zellij` on success — the existing `tmux` value is preserved rather
than renamed. `spool-tmux-failed` becomes `spool-mux-failed`. New: `spool-busy`.

### Startup preflight

The current preflight refuses to start when `--backend tmux` and no `tmux`
binary is on PATH. Generalized: refuse only when **neither** `tmux` nor `zellij`
is on PATH, and log which are present. A per-entry missing binary lands on
`spool-mux-failed`, which is already the "this box's mux is broken" arm.

### Idle gate

`wake/<sid>.busy`, created at `UserPromptSubmit` and removed at `Stop`. The
bridge injects only when the marker is absent; otherwise `spool-busy`.

The argument is that gating costs **zero coverage**. While a session is busy,
injection is redundant — `drain-directed-events.sh` (Stop hook) already peeks
the bus at end-of-turn and surfaces directed events — and the busy window is
exactly when a permission dialog can be on screen for injected text plus a
newline to answer.

**No TTL on the marker, deliberately.** A stale marker requires `SessionEnd` not
to fire (hard kill, crash, reboot). In that case the session is gone, so
declining to inject is the *correct* outcome — the alternative is typing into
whatever now owns the pane. If the session resumes under the same id,
`SessionStart` clears the marker. If a `Stop` hook times out and orphans a marker
on a live session, the next turn's `Stop` clears it. Both paths self-heal, so a
TTL would add a mid-long-turn injection window in exchange for nothing.

### Failure modes not solved

Stated rather than papered over, per the verification-boundaries rule:

- **A human mid-typing at an idle prompt** gets their draft submitted with the
  wake text appended. Unsolvable without TUI introspection. Pane scraping
  (`dump-screen` / `capture-pane` matched against the prompt box) was considered
  and rejected: it is exactly the version-fragile guess about host behavior that
  #141 punished twice.
- **The Stop-block continuation window** reads idle while the session is
  actually working, because a blocking Stop hook restarts the turn without a
  `UserPromptSubmit`. Benign — the TUI queues typed text mid-turn.
- **A mux-less host cannot wake an idle session at all.** No hook-shaped
  mechanism can: a `Stop` hook does not fire on a session that is already idle.

### Writer lives in the CLI

`agent-event-bus-cli panes set --session-id X --auto` / `panes clear`, and
`wake-state busy|idle`. The dotfiles hooks become guarded one-liners.

Two reasons this beats implementing it in bash:

1. **macOS has no `flock(1)`** — util-linux's is Linux-only. The alternative, a
   `mkdir` lock, needs stale-holder recovery whose failure mode (deadlock on a
   crashed holder) is worse than the race it prevents.
2. The contract is specified in this repo and read by this repo's bridge.
   Implementing the writer here means writer and reader are tested against each
   other in one suite and cannot drift.

`--auto` detects the multiplexer from `$TMUX_PANE` / `$ZELLIJ_PANE_ID` +
`$ZELLIJ_SESSION_NAME` and exits 0 silently when neither is set, which moves the
"omit the entry" rule out of untested bash into tested Python.

### Keying and stale-entry sweeping

`session-start.sh` keys on the `session_id` from the register response, falling
back to Claude Code's `.session_id` from stdin. The fallback is safe because the
two are equal by construction (`server.py:313` — `session_id = client_id if
client_id else uuid4()`, and registration passes `--client-id <CC session_id>`),
and it keeps the mapping correct when the bus is down.

Both `panes set` and `panes clear` also drop any *other* entry targeting the
same pane. This addresses the stale-mapping hazard `docs/BRIDGE.md` flags: a
session hard-killed in a pane leaves an entry that would otherwise type into
whatever now owns it, and the next session to occupy that pane cleans it up.

### Plist default

Flips to the injecting backend. The mapping file is the consent gate: with no
writer installed, an injecting backend resolves every delivery to
`spool-unmapped` and behaves exactly as the pinned spool backend does today. So
the default costs an unrelated operator nothing, while removing a second
install-time step that is easy to forget and silent when missed.

### No spool-drain hook

The idle gate closes the argument. A busy session's events are surfaced from the
bus at `Stop`; an idle session's are injected. Neither path has a hole a drain
would fill, and the spool is a subset of what the bus already holds. The
"portable fallback" framing does not survive contact either: a `Stop` hook cannot
fire on an already-idle session, so on a mux-less host nothing hook-shaped can
wake anything, and a drain does not change that.

## Delivery

One PR per repo. `agent-event-bus`: value shape, zellij support, idle gate, CLI
writer, tests, `docs/BRIDGE.md`, plist. `dotfiles`: `session-start.sh`,
`session-end.sh`, and a new `hooks/wake-state.sh` wired last in
`UserPromptSubmit` and last in `Stop` — last in `Stop` keeps it clear of the
load-bearing block ordering between `drain-directed-events.sh` and
`enforce-insight-publish.sh`.

## What cannot be verified from here

Per the verification-boundaries rule in `CLAUDE.md`. Hand to the bus host:

- An actual Claude Code session waking. Needs a live session in a mapped pane.
- `make install-bridge` picking up the new backend, and the supervised bridge
  reading a real `panes.json`.
- `ZELLIJ_PANE_ID` uniqueness across a session's full pane set in daily use
  (single-pane and cross-tab addressing were verified directly).
