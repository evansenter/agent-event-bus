"""Tests for the wake-dir contract: pane mapping and turn-state markers.

The mapping's failure mode is the reason these are thorough out of proportion
to the module's size: a writer that violates the contract produces no error on
either side, just wakes that quietly never happen. Every test here pins
something whose breakage is silent.
"""

import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from agent_event_bus import wake
from agent_event_bus.wake import InvalidTargetError, MuxTarget


class TestParseTarget:
    """parse_target is the single validator BOTH the bridge's reader and the
    CLI's writer run, so these cases define the contract for both."""

    def test_object_shapes_round_trip(self):
        tmux = wake.parse_target({"mux": "tmux", "pane": "%3"})
        assert (tmux.mux, tmux.pane, tmux.session) == ("tmux", "%3", None)

        zellij = wake.parse_target({"mux": "zellij", "pane": "0", "session": "tenacious-lemur"})
        assert (zellij.mux, zellij.pane, zellij.session) == ("zellij", "0", "tenacious-lemur")

    def test_bare_string_means_tmux(self):
        """The shape the contract carried before zellij support. Kept so an
        already-deployed writer, and docs/BRIDGE.md's tmux examples, stay
        correct rather than becoming a silent unmapped."""
        assert wake.parse_target("%7") == MuxTarget(mux="tmux", pane="%7")

    def test_round_trips_through_json(self):
        """to_json/parse_target must compose: the writer serializes with one
        and the reader validates with the other, through a file."""
        for target in (
            MuxTarget(mux="tmux", pane="%1"),
            MuxTarget(mux="zellij", pane="3", session="has:colon"),
        ):
            assert wake.parse_target(json.loads(json.dumps(target.to_json()))) == target

    @pytest.mark.parametrize(
        "value",
        [
            None,  # what `panes[sid] = os.environ.get("TMUX_PANE")` writes outside tmux
            "",
            0,
            [],
            {"mux": "screen", "pane": "1"},
            {"mux": "tmux"},
            {"mux": "tmux", "pane": ""},
            {"mux": "tmux", "pane": 3},
            {"pane": "%1"},
        ],
    )
    def test_unusable_values_raise(self, value):
        with pytest.raises(InvalidTargetError):
            wake.parse_target(value)

    def test_zellij_without_session_is_rejected(self):
        """zellij can only be addressed as `--session X action ... -p N`, so
        an entry with no session name is unusable however valid its pane is.
        Rejecting it here means the bridge WARNS and names the entry, instead
        of building an argv that would fail per-DM."""
        with pytest.raises(InvalidTargetError, match="session name"):
            wake.parse_target({"mux": "zellij", "pane": "0"})

    @pytest.mark.parametrize("bad", ["%0\x00", "pane\nid", "\x1b[31m"])
    def test_control_characters_are_rejected(self, bad):
        """Load-bearing, not cosmetic: an argv element containing a NUL makes
        subprocess.run raise ValueError BEFORE check or timeout - a class the
        injector's post-spool handlers do not catch, so it would escape as a
        500 on an already-spooled event."""
        with pytest.raises(InvalidTargetError):
            wake.parse_target({"mux": "tmux", "pane": bad})
        with pytest.raises(InvalidTargetError):
            wake.parse_target({"mux": "zellij", "pane": "0", "session": bad})

    def test_delimited_string_is_not_a_supported_shape(self):
        """The reason the value is an object at all. zellij accepts ':' in
        session names (verified against a live session named "has:colon"), so
        "zellij:<session>:<pane>" cannot be split unambiguously. It must fail
        loudly rather than parse into a plausible-but-wrong target."""
        with pytest.raises(InvalidTargetError):
            wake.parse_target({"mux": "zellij:tenacious-lemur", "pane": "0"})
        # As a bare string it is accepted as a tmux pane id - garbage in the
        # tmux namespace, which send-keys rejects loudly - but it must never
        # be silently understood as a zellij target.
        assert wake.parse_target("zellij:tenacious-lemur:0").mux == "tmux"


class TestDetectTarget:
    def test_tmux(self):
        assert wake.detect_target({"TMUX_PANE": "%4"}) == MuxTarget(mux="tmux", pane="%4")

    def test_zellij(self):
        env = {"ZELLIJ_PANE_ID": "0", "ZELLIJ_SESSION_NAME": "tenacious-lemur"}
        assert wake.detect_target(env) == MuxTarget(
            mux="zellij", pane="0", session="tenacious-lemur"
        )

    def test_outside_any_multiplexer_returns_none(self):
        """None means OMIT the entry. The caller must not turn this into a
        null or empty value: an omitted entry takes the bridge's quiet absent
        path, while a present-but-bad one warns and asks an operator to repair
        something that is working exactly as intended."""
        assert wake.detect_target({}) is None

    def test_partial_zellij_env_is_not_a_target(self):
        """A pane id with no session name cannot address anything. Treating it
        as absent is right; writing it would produce the warn-and-repair path
        for a session that simply has nowhere to be woken."""
        assert wake.detect_target({"ZELLIJ_PANE_ID": "0"}) is None
        assert wake.detect_target({"ZELLIJ_SESSION_NAME": "x"}) is None

    def test_empty_env_values_are_not_targets(self):
        """Unset and set-to-empty must behave identically - a supervisor or a
        `TMUX_PANE=` in a profile should not produce a mapping to "" that the
        bridge then has to warn about."""
        assert wake.detect_target({"TMUX_PANE": "", "ZELLIJ_PANE_ID": ""}) is None


class TestPaneEntries:
    def test_set_then_read_back_through_the_bridges_validator(self, tmp_path):
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="zellij", pane="0", session="s"))
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert wake.parse_target(panes["sid-1"]).pane == "0"

    def test_set_preserves_siblings(self, tmp_path):
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        wake.set_pane_entry(tmp_path, "sid-2", MuxTarget(mux="tmux", pane="%2"))
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert set(panes) == {"sid-1", "sid-2"}

    def test_set_evicts_a_stale_entry_on_the_same_pane(self, tmp_path):
        """docs/BRIDGE.md's stale-mapping hazard, made self-healing. A session
        killed without its SessionEnd hook leaves a mapping that would have
        the bridge type the wake prompt into whatever now owns the pane. The
        next session to occupy it is the one party that can prove the old
        mapping is dead."""
        pane = MuxTarget(mux="tmux", pane="%1")
        wake.set_pane_entry(tmp_path, "dead-session", pane)
        result = wake.set_pane_entry(tmp_path, "live-session", pane)

        assert result["evicted"] == ["dead-session"]
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert set(panes) == {"live-session"}

    def test_eviction_does_not_touch_a_different_pane(self, tmp_path):
        wake.set_pane_entry(tmp_path, "other", MuxTarget(mux="tmux", pane="%9"))
        result = wake.set_pane_entry(tmp_path, "mine", MuxTarget(mux="tmux", pane="%1"))
        assert result["evicted"] == []

    def test_eviction_distinguishes_muxes_with_the_same_pane_id(self, tmp_path):
        """ "0" is a plausible pane id in both namespaces, and they are not the
        same pane. Evicting across muxes would unmap a live session."""
        wake.set_pane_entry(tmp_path, "tmux-session", MuxTarget(mux="tmux", pane="0"))
        result = wake.set_pane_entry(
            tmp_path, "zellij-session", MuxTarget(mux="zellij", pane="0", session="s")
        )
        assert result["evicted"] == []

    def test_eviction_distinguishes_zellij_sessions(self, tmp_path):
        """Pane ids restart per zellij session, so pane 0 of two different
        zellij sessions are different panes."""
        wake.set_pane_entry(tmp_path, "a", MuxTarget(mux="zellij", pane="0", session="alpha"))
        result = wake.set_pane_entry(
            tmp_path, "b", MuxTarget(mux="zellij", pane="0", session="beta")
        )
        assert result["evicted"] == []

    def test_unparseable_neighbours_are_not_evicted(self, tmp_path):
        """An unusable entry is already degraded to unmapped AND warned about
        by the bridge. Silently deleting it here would remove the only signal
        telling an operator their writer is broken."""
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "panes.json").write_text(json.dumps({"broken": None}), encoding="utf-8")
        wake.set_pane_entry(tmp_path, "mine", MuxTarget(mux="tmux", pane="%1"))
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert "broken" in panes

    def test_clear_removes_only_the_named_session(self, tmp_path):
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        wake.set_pane_entry(tmp_path, "sid-2", MuxTarget(mux="tmux", pane="%2"))
        wake.clear_pane_entry(tmp_path, "sid-1")
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert set(panes) == {"sid-2"}

    def test_clear_also_drops_other_entries_on_the_same_pane(self, tmp_path):
        pane = MuxTarget(mux="tmux", pane="%1")
        wake.set_pane_entry(tmp_path, "stale", pane)
        result = wake.clear_pane_entry(tmp_path, "mine", pane)
        assert result["removed"] == ["stale"]

    def test_clear_on_a_missing_file_is_not_an_error(self, tmp_path):
        """SessionEnd runs for every session, including ones that never had a
        pane. It must not fail, and must not create the file."""
        result = wake.clear_pane_entry(tmp_path / "absent", "sid-1")
        assert result["existed"] is False
        assert not (tmp_path / "absent" / "panes.json").exists()

    def test_damaged_file_is_replaced_rather_than_propagated(self, tmp_path):
        """The writer holds the lock, so it is the only party that can restore
        a well-formed file. Preserving unparseable bytes would wedge every
        future write behind the same failure - and the bridge is already
        degrading to unmapped meanwhile."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "panes.json").write_text("{not json", encoding="utf-8")
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert set(panes) == {"sid-1"}


class TestWriteContract:
    """The mechanical half of the contract, all of which fails silently."""

    def test_non_ascii_survives_any_reader_codec(self, tmp_path):
        """The reader's codec hazard, closed from the writer's side.

        The bridge decodes panes.json as UTF-8 explicitly because a supervisor
        hands the daemon no LC_ALL and a C locale resolves to ASCII. This
        writer goes further: json.dump escapes non-ASCII to \\uXXXX, so the
        bytes on disk are pure ASCII and decode identically under UTF-8, the
        C locale, or anything else. A writer switched to ensure_ascii=False
        would still be correct against the current reader and would silently
        stop being correct against any other consumer of the file - hence the
        byte-level assertion rather than a round-trip one.
        """
        session = "süß-lemur"
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="zellij", pane="0", session=session))
        raw = (tmp_path / "panes.json").read_bytes()

        assert raw.decode("ascii")  # no byte above 0x7f reached the file
        for codec in ("utf-8", "ascii", "latin-1"):
            assert json.loads(raw.decode(codec))["sid-1"]["session"] == session

    def test_no_temp_files_survive(self, tmp_path):
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []

    def test_temp_file_is_created_in_the_target_directory(self, tmp_path, monkeypatch):
        """os.replace is atomic only WITHIN a filesystem. A temp file in
        $TMPDIR (a different volume from $HOME on macOS) would silently
        degrade the replace into a copy the bridge can observe half-written -
        exactly the torn read it is documented to degrade on. Pinning the
        `dir=` argument is the only way to catch that reintroduced by a
        defaulted argument."""
        seen = {}
        real_mkstemp = wake.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen["dir"] = kwargs.get("dir")
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(wake.tempfile, "mkstemp", spy)
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        assert Path(seen["dir"]) == tmp_path

    def test_modes_are_narrow(self, tmp_path):
        """The wake dir holds full event payloads; panes.json names every live
        session's terminal. Both are set explicitly rather than left to the
        process umask, matching the bridge's own handling."""
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        assert stat.S_IMODE((tmp_path / "panes.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700

    def test_preexisting_wide_directory_is_narrowed(self, tmp_path):
        wide = tmp_path / "wide"
        wide.mkdir()
        wide.chmod(0o755)
        wake.set_pane_entry(wide, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        assert stat.S_IMODE(wide.stat().st_mode) == 0o700


def _writer(args):
    """Module-level so it is picklable for the process pool below."""
    wake_dir, sid, pane = args
    for _ in range(20):
        wake.set_pane_entry(Path(wake_dir), sid, MuxTarget(mux="tmux", pane=pane))


class TestConcurrentWriters:
    def test_concurrent_writers_do_not_lose_entries(self, tmp_path):
        """The reason the lock is not optional. Without it the loser's read
        predates the winner's write, so its rename drops an entry that was
        legitimately there - and NOTHING errors on either side: the losing
        session simply reads as unmapped later, which is the documented
        NORMAL outcome for a session on another machine. That is what makes
        the loss invisible.

        Separate processes, not threads: flock is advisory per open file
        description, and a thread-only test would pass against an
        implementation that holds no lock at all if the GIL happened to
        serialize the short critical section.
        """
        sessions = [(str(tmp_path), f"sid-{i}", f"%{i}") for i in range(8)]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(len(sessions)) as pool:
            pool.map(_writer, sessions)

        panes = json.loads((tmp_path / "panes.json").read_text(encoding="utf-8"))
        assert set(panes) == {f"sid-{i}" for i in range(8)}

    def test_lock_is_a_sibling_file(self, tmp_path):
        """flock binds to an INODE and the writer replaces panes.json by
        rename, so a lock taken on panes.json itself would give each writer a
        lock on a different (often already unlinked) inode - a lock that
        excludes nobody while looking entirely correct."""
        wake.set_pane_entry(tmp_path, "sid-1", MuxTarget(mux="tmux", pane="%1"))
        assert (tmp_path / "panes.lock").exists()


class TestBusyMarker:
    def test_set_and_clear(self, tmp_path):
        assert wake.is_busy(tmp_path, "sid-1") is False
        wake.set_busy(tmp_path, "sid-1")
        assert wake.is_busy(tmp_path, "sid-1") is True
        wake.clear_busy(tmp_path, "sid-1")
        assert wake.is_busy(tmp_path, "sid-1") is False

    def test_both_operations_are_idempotent(self, tmp_path):
        """Hooks fire more than once per state in practice (SessionStart on
        resume and compact, a Stop that blocks and re-fires), and neither
        repeat may fail."""
        wake.set_busy(tmp_path, "sid-1")
        wake.set_busy(tmp_path, "sid-1")
        wake.clear_busy(tmp_path, "sid-1")
        wake.clear_busy(tmp_path, "sid-1")
        assert wake.is_busy(tmp_path, "sid-1") is False

    def test_marker_is_per_session(self, tmp_path):
        wake.set_busy(tmp_path, "sid-1")
        assert wake.is_busy(tmp_path, "sid-2") is False

    def test_marker_name_is_outside_the_spool_prune_glob(self, tmp_path):
        """The spool-pruning follow-up's safe target is `<sid>.jsonl*`. A
        marker inside that glob would be swept by a future prune, silently
        unwedging the idle gate for a dead session."""
        wake.set_busy(tmp_path, "sid-1")
        assert wake.busy_path(tmp_path, "sid-1").name == "sid-1.busy"
        assert not wake.busy_path(tmp_path, "sid-1").match("*.jsonl*")

    def test_marker_is_0600(self, tmp_path):
        wake.set_busy(tmp_path, "sid-1")
        assert stat.S_IMODE(wake.busy_path(tmp_path, "sid-1").stat().st_mode) == 0o600

    def test_unreadable_marker_reads_as_idle(self, tmp_path, monkeypatch):
        """Same never-500 posture as the panes read: the delivery is already
        spooled by the time this is consulted, so raising would make the bus
        retry it. Reading as idle matches the no-marker default rather than
        wedging the session permanently unwakeable."""

        def boom(self):
            raise OSError("nope")

        monkeypatch.setattr(Path, "exists", boom)
        assert wake.is_busy(tmp_path, "sid-1") is False


class TestWakeDirFromEnv:
    def test_env_override(self, tmp_path):
        assert wake.wake_dir_from_env({"AGENT_EVENT_BUS_WAKE_DIR": str(tmp_path)}) == tmp_path

    def test_empty_env_value_falls_back_to_the_default(self):
        """An empty env var would make Path("") the CWD, scattering wake files
        wherever a hook happened to run."""
        assert wake.wake_dir_from_env({"AGENT_EVENT_BUS_WAKE_DIR": ""}) == wake.DEFAULT_WAKE_DIR

    def test_default_matches_the_documented_path(self):
        assert wake.DEFAULT_WAKE_DIR == Path(
            os.path.expanduser("~/.claude/contrib/agent-event-bus/wake")
        )
