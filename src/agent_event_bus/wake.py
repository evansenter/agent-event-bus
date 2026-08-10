"""The wake-dir contract: multiplexer pane mapping and turn-state markers.

Shared by the two sides that must agree about it - `bridge.py` reads the
mapping on every delivery, and `cli.py` writes it from session hooks. They
lived in separate repos in the RFC #122 prototype (the reader here, the writer
in dotfiles), which is exactly the arrangement that let the contract be fully
specified and never implemented. Keeping both against `parse_target` below
means a writer change that breaks the reader fails in this repo's test suite.

Nothing here imports from `bridge`; `bridge` imports from here.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WAKE_DIR = Path.home() / ".claude" / "contrib" / "agent-event-bus" / "wake"

PANES_FILENAME = "panes.json"
# Sibling of panes.json, not a lock on panes.json itself: the writer replaces
# panes.json by rename, and flock binds to an inode, so locking the file being
# replaced would hand each writer a lock on a different (possibly already
# unlinked) inode and exclude nobody.
PANES_LOCK_FILENAME = "panes.lock"

# Turn-state marker: present between UserPromptSubmit and Stop. Named with a
# suffix outside `<sid>.jsonl*` so the spool-pruning follow-up (whose safe
# target is that glob) can never sweep it.
BUSY_SUFFIX = ".busy"

SUPPORTED_MUXES = ("tmux", "zellij")


class InvalidTargetError(ValueError):
    """A mapping value that is present but unusable as an injection target."""


@dataclass(frozen=True)
class MuxTarget:
    """A validated injection target: which multiplexer, which pane.

    `session` is the multiplexer's own session name, required for zellij
    (`zellij --session X action ...` is the only way to address a pane from
    outside) and unused for tmux, whose pane ids are unique per server.
    """

    mux: str
    pane: str
    session: str | None = None

    def to_json(self) -> dict:
        entry = {"mux": self.mux, "pane": self.pane}
        if self.session is not None:
            entry["session"] = self.session
        return entry

    def describe(self) -> str:
        """Short operator-facing form for log lines."""
        if self.mux == "zellij":
            return f"zellij pane {self.pane} in session {self.session!r}"
        return f"tmux pane {self.pane}"


def _valid_argv_str(value: object) -> bool:
    """Non-empty printable string.

    `isprintable()` is load-bearing rather than cosmetic, and for a specific
    reason: an argv element containing a NUL makes `subprocess.run` raise
    ValueError *before* `check` or `timeout` apply, which is a failure class
    the injector's post-spool handlers do not catch. Rejecting it here keeps
    it out of argv entirely. Real pane ids ("%0", "0", "terminal_3") and real
    session names are printable throughout.
    """
    return isinstance(value, str) and bool(value) and value.isprintable()


def parse_target(value: object) -> MuxTarget:
    """Validate one panes.json value. Raises InvalidTargetError with a reason.

    Two accepted shapes:
      {"mux": "tmux", "pane": "%3"}
      {"mux": "zellij", "pane": "0", "session": "tenacious-lemur"}
    plus a bare string, which means tmux - the shape the contract carried
    before zellij support, kept so pre-existing writers and the tmux contract
    text stay correct.

    A delimited string ("zellij:<session>:<pane>") is deliberately NOT a
    supported shape: zellij accepts ':' in session names, so it cannot be
    split unambiguously.
    """
    if isinstance(value, str):
        if not _valid_argv_str(value):
            raise InvalidTargetError(f"not a pane id ({value!r})")
        return MuxTarget(mux="tmux", pane=value)

    if not isinstance(value, dict):
        # Catches the likeliest bad value in practice: JSON null, which is
        # what `panes[sid] = os.environ.get("TMUX_PANE")` writes outside tmux.
        # Present-but-bad is a different diagnosis and repair than absent, so
        # it must not fold into the quiet unmapped path.
        raise InvalidTargetError(f"not a pane id or mapping object ({value!r})")

    mux = value.get("mux")
    if mux not in SUPPORTED_MUXES:
        raise InvalidTargetError(
            f"unsupported mux {mux!r} (expected one of {', '.join(SUPPORTED_MUXES)})"
        )
    pane = value.get("pane")
    if not _valid_argv_str(pane):
        raise InvalidTargetError(f"pane is not a pane id ({pane!r})")

    session = value.get("session")
    if mux == "zellij":
        if not _valid_argv_str(session):
            raise InvalidTargetError(f"zellij entry needs a session name (got {session!r})")
    elif session is not None and not _valid_argv_str(session):
        raise InvalidTargetError(f"session is not a session name ({session!r})")

    return MuxTarget(mux=mux, pane=pane, session=session)


def detect_target(env: dict | None = None) -> MuxTarget | None:
    """Derive this process's injection target from the environment.

    Returns None when the process is not inside a supported multiplexer, which
    the caller must treat as "omit the entry" rather than "write an empty
    one". That distinction is the whole reason this returns None instead of a
    partially-filled target: an omitted entry takes the quiet absent path,
    while a present-but-empty one warns and asks an operator to repair
    something that is working as intended.

    tmux is checked first only because its variable is unambiguous; the two
    are never both set in practice.
    """
    env = os.environ if env is None else env

    pane = env.get("TMUX_PANE")
    if _valid_argv_str(pane):
        return MuxTarget(mux="tmux", pane=pane)

    pane = env.get("ZELLIJ_PANE_ID")
    session = env.get("ZELLIJ_SESSION_NAME")
    if _valid_argv_str(pane) and _valid_argv_str(session):
        return MuxTarget(mux="zellij", pane=pane, session=session)

    return None


def wake_dir_from_env(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("AGENT_EVENT_BUS_WAKE_DIR") or DEFAULT_WAKE_DIR)


def _ensure_wake_dir(wake_dir: Path) -> None:
    """Create the wake dir 0700 and narrow it if it already exists.

    Mirrors the bridge's own start-up handling: the dir holds full event
    payloads, so the mode is asserted on every use rather than only at
    creation.
    """
    wake_dir.mkdir(parents=True, exist_ok=True)
    wake_dir.chmod(0o700)


def _read_panes(panes_file: Path) -> dict:
    """Best-effort read for the read-modify-write. A damaged file is replaced
    rather than propagated: the writer holds the lock, so it is the one party
    that can restore the file to a well-formed state, and preserving unparseable
    bytes would wedge every future write behind the same failure.
    """
    try:
        with open(panes_file, encoding="utf-8") as f:
            panes = json.load(f)
    except (OSError, ValueError):
        return {}
    return panes if isinstance(panes, dict) else {}


def _write_panes_atomic(wake_dir: Path, panes_file: Path, panes: dict) -> None:
    """Atomic replace, and 0600 before the rename rather than after.

    The temp file MUST live in the same directory as the target: os.replace is
    only atomic within a filesystem, and a temp dir elsewhere would silently
    degrade to a copy the reader can observe half-written. `mkstemp` creates
    at 0600 already; the chmod is explicit so the mode does not depend on that
    staying true.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(wake_dir), prefix=".panes.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(panes, f, indent=2, sort_keys=True, allow_nan=False)
            f.write("\n")
        tmp_path.chmod(0o600)
        os.replace(tmp_path, panes_file)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class _PanesLock:
    """Exclusive flock on the sibling lock file, held across read-modify-write.

    Concurrent SessionStart hooks would otherwise silently lose entries: the
    loser's read predates the winner's write, so its rename drops an entry that
    was legitimately there. Nothing errors on either side - the losing session
    simply reads as unmapped later, which is the documented *normal* outcome
    for a session on another machine. That is what makes the loss invisible and
    the lock non-optional.
    """

    def __init__(self, wake_dir: Path):
        self._path = wake_dir / PANES_LOCK_FILENAME
        self._fd: int | None = None

    def __enter__(self):
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            # flock releases on close; the explicit unlock keeps the ordering
            # obvious and is harmless.
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        return False


def set_pane_entry(wake_dir: Path, session_id: str, target: MuxTarget) -> dict:
    """Map session_id to target, dropping any stale entry on the same pane.

    The stale-entry sweep is not housekeeping. A session killed without its
    SessionEnd hook running leaves a mapping behind, and `docs/BRIDGE.md`'s
    warning about it is concrete: the bridge would type the wake prompt into
    whatever now owns that pane, usually a shell. The next session to occupy
    the pane is the one party that can prove the old mapping is dead, so it
    cleans up.
    """
    _ensure_wake_dir(wake_dir)
    panes_file = wake_dir / PANES_FILENAME
    entry = target.to_json()
    with _PanesLock(wake_dir):
        panes = _read_panes(panes_file)
        evicted = [
            sid for sid, value in panes.items() if sid != session_id and _same_pane(value, target)
        ]
        for sid in evicted:
            del panes[sid]
        panes[session_id] = entry
        _write_panes_atomic(wake_dir, panes_file, panes)
    return {"session_id": session_id, "entry": entry, "evicted": evicted}


def clear_pane_entry(wake_dir: Path, session_id: str, target: MuxTarget | None = None) -> dict:
    """Remove session_id's entry, plus any entry on the same pane.

    Clearing by pane as well as by id matters when the id is unavailable or
    wrong: the pane is the resource that must not be typed into, and it is
    identifiable from the ending session's own environment.
    """
    panes_file = wake_dir / PANES_FILENAME
    if not panes_file.exists():
        return {"session_id": session_id, "removed": [], "existed": False}
    with _PanesLock(wake_dir):
        panes = _read_panes(panes_file)
        removed = [
            sid
            for sid, value in panes.items()
            if sid == session_id or (target is not None and _same_pane(value, target))
        ]
        if removed:
            for sid in removed:
                del panes[sid]
            _write_panes_atomic(wake_dir, panes_file, panes)
    return {"session_id": session_id, "removed": removed, "existed": True}


def _same_pane(value: object, target: MuxTarget) -> bool:
    """Does an existing mapping value point at the same pane as target?

    Unparseable values are never "the same pane" - they are already unusable,
    and evicting them here would silently repair a misconfiguration whose
    warning is the only thing telling an operator to fix their writer.
    """
    try:
        other = parse_target(value)
    except InvalidTargetError:
        return False
    return (other.mux, other.pane, other.session) == (target.mux, target.pane, target.session)


def busy_path(wake_dir: Path, session_id: str) -> Path:
    return wake_dir / f"{session_id}{BUSY_SUFFIX}"


def set_busy(wake_dir: Path, session_id: str) -> None:
    """Mark the session mid-turn. Idempotent."""
    _ensure_wake_dir(wake_dir)
    path = busy_path(wake_dir, session_id)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    os.close(fd)


def clear_busy(wake_dir: Path, session_id: str) -> None:
    """Mark the session idle. Idempotent - missing is the desired end state."""
    busy_path(wake_dir, session_id).unlink(missing_ok=True)


def is_busy(wake_dir: Path, session_id: str) -> bool:
    """Is the session mid-turn?

    Deliberately has no staleness window. A marker outlives its session only
    when SessionEnd never ran (hard kill, crash, reboot) - and then the
    session is gone, so declining to inject is correct rather than a bug: the
    alternative is typing into whatever now owns the pane. A resumed session
    clears it at SessionStart, and a Stop hook that timed out and orphaned one
    on a live session gets it cleared by the next turn's Stop. Both self-heal,
    so a TTL would buy nothing and open a mid-turn injection window on any
    turn that ran longer than it.
    """
    try:
        return busy_path(wake_dir, session_id).exists()
    except OSError:
        # Same never-500 posture as the panes read: a delivery is already
        # spooled by the time this is consulted, and raising would make the
        # bus retry it. An unreadable marker reads as idle, matching the
        # no-marker default rather than wedging the session unwakeable.
        return False
