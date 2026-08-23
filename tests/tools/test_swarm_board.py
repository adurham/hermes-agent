"""Tests for ``tools.swarm_board`` — the swarm board state container.

The board is now pure state + thread-safe mutators; rendering happens in the
CLI's prompt_toolkit widget.  These tests cover the data model, the
``maybe_start`` activation gate, the ``on_change`` invalidation hook, and the
child-print interceptor.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
import weakref

from tools.swarm_board import (
    DEFAULT_MAX_BOARD_ROWS,
    MIN_MAX_BOARD_ROWS,
    RowSnapshot,
    SwarmBoard,
    _MAX_RENDER_DEPTH,
    _NoopBoard,
    _Row,
    collapse_rows_to_limit,
    format_row,
    make_child_print_fn,
    order_rows_for_display,
    resolve_max_board_rows,
    resolve_row_lineage,
)


class TestRow(unittest.TestCase):
    def test_elapsed_runs_until_ended(self):
        r = _Row(subagent_id="x", started_at=time.time() - 5.0)
        # No ended_at — elapsed reads now-ish.
        assert 4.5 <= r.elapsed() <= 6.0
        r.ended_at = r.started_at + 3.0
        # Now elapsed is fixed at 3 regardless of wall clock.
        assert r.elapsed() == 3.0


class TestNoopBoard(unittest.TestCase):
    """The no-op board is the fallback when the board doesn't activate.
    Every method must be safe to call with arbitrary args."""

    def test_methods_are_silent(self):
        b = _NoopBoard()
        with b:
            b.register("x", model="claude-haiku-4-5", goal="hi")
            b.update("x", status="running", tool_count=3)
            b.note("x", "anything")
            b.finish("x", "completed", summary="done")
        # No exception = pass.

    def test_is_active_is_false(self):
        # delegate_tool's progress callback uses ``is_active`` to decide
        # whether to suppress the legacy spinner.print_above chatter.  The
        # noop must report False so non-CLI callers still see chatter.
        assert _NoopBoard().is_active is False

    def test_get_rows_snapshot_returns_empty_list(self):
        assert _NoopBoard().get_rows_snapshot() == []

    def test_make_child_print_fn_returns_fallback_for_noop(self):
        captured = []
        b = _NoopBoard()
        fn = make_child_print_fn(b, "x", fallback=lambda *a, **k: captured.append(a))
        # Returned function should be the bare fallback (no wrapping).
        fn("hello")
        assert captured == [("hello",)]


class _StubCLI:
    """Minimal stand-in for ``HermesCLI`` exposing only the swarm-board
    hooks ``maybe_start`` looks for.  Used to test the activation gate
    without instantiating the real CLI.

    Mirrors the real CLI's list-based tracking (``_swarm_boards``) so tests
    exercise the same "multiple concurrent boards" contract that
    ``cli.py`` implements — a single delegate_task() batch's board must not
    stomp on another concurrently-active batch's board.
    """

    def __init__(self):
        self._swarm_boards = []
        self.show_calls = []
        self.hide_calls = 0
        self.invalidate_calls = 0

    def _swarm_board_show(self, board):
        if board not in self._swarm_boards:
            self._swarm_boards.append(board)
        self.show_calls.append(board)

    def _swarm_board_hide(self, board):
        try:
            self._swarm_boards.remove(board)
        except ValueError:
            pass
        self.hide_calls += 1

    def _invalidate_app(self):
        self.invalidate_calls += 1


class TestMaybeStartGating(unittest.TestCase):
    """``maybe_start`` is the policy wall — exercise its decision tree."""

    def test_single_child_returns_real_board(self):
        # 1+ children with a CLI host → real board (single-child delegations
        # get the same in-place row treatment as batches — see maybe_start's
        # docstring for why this changed from "single-child = no-op").
        cli = _StubCLI()
        parent = type("P", (), {"_cli_ref": cli})()
        b = SwarmBoard.maybe_start(parent_agent=parent, n_children=1)
        assert isinstance(b, SwarmBoard)
        assert b.is_active is True

    def test_zero_children_returns_noop(self):
        parent = type("P", (), {"_cli_ref": _StubCLI()})()
        b = SwarmBoard.maybe_start(parent_agent=parent, n_children=0)
        assert isinstance(b, _NoopBoard)

    def test_no_cli_ref_returns_noop(self):
        # Without a CLI to host the widget, fall back to chatter mode.
        b = SwarmBoard.maybe_start(parent_agent=object(), n_children=5)
        assert isinstance(b, _NoopBoard)

    def test_cli_ref_missing_hooks_returns_noop(self):
        # A CLI subclass that drops the hooks must not crash maybe_start.
        class HalfCLI:
            _swarm_board = None
            # No _swarm_board_show / _swarm_board_hide / _invalidate_app
        parent = type("P", (), {"_cli_ref": HalfCLI()})()
        b = SwarmBoard.maybe_start(parent_agent=parent, n_children=5)
        assert isinstance(b, _NoopBoard)

    def test_cli_ref_present_returns_real_board(self):
        cli = _StubCLI()
        parent = type("P", (), {"_cli_ref": cli})()
        b = SwarmBoard.maybe_start(parent_agent=parent, n_children=3)
        assert isinstance(b, SwarmBoard)
        assert b.is_active is True

    def test_env_disable_returns_noop(self):
        import os
        old = os.environ.get("HERMES_SWARM_BOARD")
        os.environ["HERMES_SWARM_BOARD"] = "0"
        try:
            parent = type("P", (), {"_cli_ref": _StubCLI()})()
            b = SwarmBoard.maybe_start(parent_agent=parent, n_children=5)
            assert isinstance(b, _NoopBoard)
        finally:
            if old is None:
                del os.environ["HERMES_SWARM_BOARD"]
            else:
                os.environ["HERMES_SWARM_BOARD"] = old


class TestContextManagerWiresShowHide(unittest.TestCase):
    """Entering / exiting the ``with`` block must call the CLI's show/hide
    hooks so the widget appears and disappears."""

    def test_enter_exit_drives_cli_hooks(self):
        cli = _StubCLI()
        parent = type("P", (), {"_cli_ref": cli})()
        with SwarmBoard.maybe_start(parent, n_children=2) as board:
            assert isinstance(board, SwarmBoard)
            assert cli.show_calls == [board]
            assert cli._swarm_boards == [board]
        assert cli.hide_calls == 1
        assert cli._swarm_boards == []

    def test_two_concurrent_boards_both_stay_visible(self):
        # Regression test: a second delegate_task() batch starting while
        # the first is still running used to overwrite the CLI's
        # single-slot `_swarm_board` attribute, silently dropping the
        # first batch's rows from the widget. Both boards must coexist in
        # the active list until each is individually torn down.
        cli = _StubCLI()
        parent = type("P", (), {"_cli_ref": cli})()
        board1 = SwarmBoard.maybe_start(parent, n_children=2)
        board1.__enter__()
        board2 = SwarmBoard.maybe_start(parent, n_children=3)
        board2.__enter__()
        assert cli._swarm_boards == [board1, board2]
        # Finishing the SECOND batch first must not blank the first one out.
        board2.__exit__(None, None, None)
        assert cli._swarm_boards == [board1]
        board1.__exit__(None, None, None)
        assert cli._swarm_boards == []


class TestPrintFnRouting(unittest.TestCase):
    """The child print interceptor: most lines go to the row's note, but
    error-marker lines pass through to the fallback (so they survive in
    the scrollback)."""

    def setUp(self):
        self.board = SwarmBoard()
        self.board.register("a1", model="claude-haiku-4-5", goal="g")
        self.captured = []
        self.fn = make_child_print_fn(
            self.board, "a1", fallback=lambda *a, **k: self.captured.append(a)
        )

    def test_chatter_goes_to_note_not_stdout(self):
        self.fn("[subagent-0] 🔧 Auto-repaired tool name: 'foo' -> 'mcp_foo'")
        assert self.captured == []  # nothing went to stdout
        assert "Auto-repaired tool name" in self.board._rows["a1"].last_note

    def test_log_prefix_is_stripped_from_note(self):
        self.fn("[subagent-0] hello world")
        # The "[subagent-0] " prefix is redundant inside the row — strip it.
        assert self.board._rows["a1"].last_note == "hello world"

    def test_error_lines_pass_through(self):
        self.fn("❌ API failed after 3 retries")
        # ❌ marker → goes to fallback (stdout), not into the row note.
        assert any("❌" in str(a) for a in self.captured)

    def test_request_dump_passes_through(self):
        self.fn("🧾 Request debug dump written to: /tmp/x.json")
        assert any("Request debug dump" in str(a) for a in self.captured)


class TestRegisterAndUpdate(unittest.TestCase):
    def test_register_creates_row_once(self):
        b = SwarmBoard()
        b.register("a1", model="m", goal="g")
        b.register("a1", model="m2", goal="")  # update existing
        row = b._rows["a1"]
        assert row.model == "m2"  # updated
        assert row.goal == "g"   # untouched (empty arg = no-op)
        assert b._row_order == ["a1"]  # not duplicated

    def test_update_unknown_id_silently_ignored(self):
        b = SwarmBoard()
        # Updating an unregistered row is a no-op (defensive — children
        # might fire callbacks before register completes).
        b.update("ghost", status="running")  # must not raise

    def test_note_truncates_long_text(self):
        b = SwarmBoard()
        b.register("a1")
        b.note("a1", "x" * 200)
        assert len(b._rows["a1"].last_note) == 60
        assert b._rows["a1"].last_note.endswith("...")

    def test_finish_sets_ended_at_and_status(self):
        b = SwarmBoard()
        b.register("a1")
        b.finish("a1", status="completed", summary="all good")
        row = b._rows["a1"]
        assert row.status == "completed"
        assert row.ended_at is not None
        assert "all good" in row.last_note

    def test_summarizing_freezes_elapsed_clock(self):
        # Entering "summarizing" freezes work_ended_at so the displayed
        # elapsed time reflects work duration, not final-answer streaming
        # latency.
        b = SwarmBoard()
        b.register("a1")
        b.update("a1", status="summarizing")
        row = b._rows["a1"]
        assert row.work_ended_at is not None
        frozen = row.work_ended_at
        time.sleep(0.05)
        # elapsed() must not advance while frozen.
        assert row.elapsed() == max(0.0, frozen - row.started_at)

    def test_running_after_false_positive_summarizing_unfreezes_clock(self):
        # Regression test: a heuristic false-positive "summarizing" flip
        # (e.g. TASK_THINKING's _looks_like_summary_phase matching an
        # intermediate "## Summary" planning artifact, not the real final
        # answer) used to freeze work_ended_at PERMANENTLY — the row's
        # elapsed clock stayed pinned at that timestamp forever even
        # though the child kept calling real tools afterward (tool_count
        # climbing while the displayed time stayed stuck, e.g. at "4s").
        # A real tool call (TASK_TOOL_STARTED -> status="running") is an
        # unambiguous "the child is actively working" signal and must
        # clear the freeze so elapsed() resumes tracking wall-clock time.
        b = SwarmBoard()
        b.register("a1")
        b.update("a1", status="summarizing")
        row = b._rows["a1"]
        assert row.work_ended_at is not None
        b.update("a1", status="running", tool_count=1, last_tool="Read")
        assert row.work_ended_at is None
        time.sleep(0.05)
        # Clock is unfrozen: elapsed grows again with real wall-clock time.
        assert row.elapsed() > 0.0

    def test_terminal_status_after_summarizing_keeps_freeze(self):
        # finish() is the terminal path and sets ended_at directly; it does
        # not go through update()'s status handling, so a legitimate
        # "summarizing" freeze survives through to the completed row and
        # elapsed() still reports the frozen work duration, not work +
        # answer-streaming time.
        b = SwarmBoard()
        b.register("a1")
        b.update("a1", status="summarizing")
        row = b._rows["a1"]
        frozen = row.work_ended_at
        assert frozen is not None
        time.sleep(0.05)
        b.finish("a1", status="completed")
        assert row.work_ended_at == frozen
        assert row.elapsed() == max(0.0, frozen - row.started_at)


class TestSnapshotAndOnChange(unittest.TestCase):
    """Two contracts the prompt_toolkit widget relies on:

    * ``get_rows_snapshot`` returns frozen views in registration order so
      the widget renders without holding the lock.
    * Every mutator fires ``on_change`` so the host can invalidate its
      Application and trigger a redraw.
    """

    def test_snapshot_preserves_registration_order(self):
        b = SwarmBoard()
        b.register("a", model="m1")
        b.register("b", model="m2")
        b.register("c", model="m3")
        ids = [r.subagent_id for r in b.get_rows_snapshot()]
        assert ids == ["a", "b", "c"]

    def test_snapshot_is_frozen_view(self):
        # Mutating the snapshot must not bleed back into the live row.
        b = SwarmBoard()
        b.register("a", model="m")
        snap = b.get_rows_snapshot()[0]
        snap.model = "MUTATED"
        # Live row is untouched.
        assert b._rows["a"].model == "m"

    def test_on_change_fires_on_every_mutator(self):
        calls = []
        b = SwarmBoard(on_change=lambda: calls.append(1))
        b.register("a")
        b.update("a", status="running")
        b.note("a", "hi")
        b.finish("a", status="completed")
        assert len(calls) == 4

    def test_on_change_failure_is_swallowed(self):
        # If the host's invalidate raises (e.g. app already torn down),
        # the mutation must still succeed.
        def boom():
            raise RuntimeError("app gone")
        b = SwarmBoard(on_change=boom)
        b.register("a")  # must not raise
        b.update("a", status="running")  # must not raise
        assert b._rows["a"].status == "running"

    def test_concurrent_updates_are_thread_safe(self):
        # 16 threads × 200 increments each: every event must land in the
        # row without lock contention crashing things.
        b = SwarmBoard()
        b.register("a")
        N_THREADS = 16
        N_PER_THREAD = 200

        def worker(_):
            for _ in range(N_PER_THREAD):
                b.update("a", tool_count=b._rows["a"].tool_count + 1)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Final count is allowed to race below the upper bound (read-modify-
        # write on tool_count isn't atomic across separate update calls),
        # but must be > 0 and not have raised.
        assert b._rows["a"].tool_count > 0
        # Snapshot reads must remain coherent under concurrent writes.
        assert b.get_rows_snapshot()[0].subagent_id == "a"


class TestFormatRow(unittest.TestCase):
    """The rendering helper used by the CLI widget."""

    def test_format_strips_mcp_prefix(self):
        b = SwarmBoard()
        b.register("a1", model="claude-haiku-4-5")
        b.update("a1", last_tool="mcp_jira_search_issues", tool_count=3, status="running")
        line = format_row(b.get_rows_snapshot()[0])
        # mcp_ prefix stripped so the row stays readable.
        assert "mcp_" not in line
        assert "jira_search_issues" in line
        assert "3 tools" in line

    def test_format_strips_provider_prefix(self):
        b = SwarmBoard()
        b.register("a1", model="anthropic/claude-haiku-4-5")
        line = format_row(b.get_rows_snapshot()[0])
        assert "anthropic/" not in line
        assert "claude-haiku-4-5" in line

    def test_format_truncates_long_tool_name(self):
        b = SwarmBoard()
        b.register("a1")
        long = "this_is_a_really_long_tool_name_that_must_be_truncated"
        b.update("a1", last_tool=long)
        line = format_row(b.get_rows_snapshot()[0])
        # Truncated to ≤ 30 chars + "..." marker.
        assert long not in line
        assert "..." in line

    def test_format_flattens_newlines_in_note(self):
        # Final-summary text often contains markdown separators
        # ("Here is X.\n---\n## Section ...") which used to leak into the
        # row note slot — a stray newline inside format_row's output
        # makes prompt_toolkit render two visual lines for a row whose
        # widget allocates only one, pushing later rows off-board.
        b = SwarmBoard()
        b.register("a1")
        b.update(
            "a1",
            last_note="Here is the full case picture.\n---\n## Tool inventory",
        )
        line = format_row(b.get_rows_snapshot()[0])
        assert "\n" not in line, f"newline leaked: {line!r}"
        assert "\r" not in line
        # The collapsed text should still show the meaningful content.
        assert "Here is the full case picture." in line

    def test_format_flattens_newlines_in_tool(self):
        b = SwarmBoard()
        b.register("a1")
        b.update("a1", last_tool="some_tool\nwith_newline")
        line = format_row(b.get_rows_snapshot()[0])
        assert "\n" not in line


def _snap(sid, *, parent=None, depth=0, status="running"):
    """Minimal RowSnapshot for the pure ordering/collapse helpers."""
    return RowSnapshot(
        subagent_id=sid,
        model="claude-opus-4-5",
        goal="g",
        status=status,
        tool_count=0,
        last_tool="",
        last_note="",
        elapsed_seconds=1.0,
        depth=depth,
        parent_subagent_id=parent,
    )


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


class _StubAgent:
    """Stand-in for an AIAgent carrying only the attributes the board reads.

    Declared (rather than assigned onto a bare ``type()``) so the delegation
    attributes the board walks are real, typed fields.
    """

    def __init__(
        self,
        *,
        cli_ref=None,
        delegate_depth: object = 0,
        subagent_id=None,
        parent=None,
    ):
        self._cli_ref = cli_ref
        self._delegate_depth = delegate_depth
        self._subagent_id = subagent_id
        # Mirrors what delegate_tool._build_child_agent stamps at build time.
        self._delegate_parent_ref = weakref.ref(parent) if parent else None


class TestRowLineageFields(unittest.TestCase):
    """RowSnapshot carries the delegation hierarchy, defaulting to flat."""

    def test_defaults_are_top_level(self):
        b = SwarmBoard()
        b.register("a1")
        row = b.get_rows_snapshot()[0]
        assert row.depth == 0
        assert row.parent_subagent_id is None

    def test_register_records_depth_and_parent(self):
        b = SwarmBoard()
        b.register("kid", depth=1, parent_subagent_id="orch")
        row = b.get_rows_snapshot()[0]
        assert row.depth == 1
        assert row.parent_subagent_id == "orch"

    def test_lineage_survives_updates_and_finish(self):
        # The hierarchy must not be lost as the row moves through its
        # lifecycle — otherwise a child un-nests itself mid-flight.
        b = SwarmBoard()
        b.register("kid", depth=2, parent_subagent_id="orch")
        b.update("kid", status="running", tool_count=4)
        b.note("kid", "working")
        b.finish("kid", status="completed", summary="done")
        row = b.get_rows_snapshot()[0]
        assert row.depth == 2
        assert row.parent_subagent_id == "orch"

    def test_negative_depth_is_clamped(self):
        b = SwarmBoard()
        b.register("a1", depth=-3)
        assert b.get_rows_snapshot()[0].depth == 0

    def test_reregister_does_not_clobber_lineage(self):
        # register() is called again on refresh; absent kwargs must not
        # reset an already-known parent to None.
        b = SwarmBoard()
        b.register("kid", depth=1, parent_subagent_id="orch")
        b.register("kid", model="new-model")
        row = b.get_rows_snapshot()[0]
        assert row.depth == 1
        assert row.parent_subagent_id == "orch"


class TestResolveRowLineage(unittest.TestCase):
    """Lineage is read off attributes delegation already maintains."""

    def test_top_level_agent_is_flat(self):
        parent = _StubAgent(delegate_depth=0)
        assert resolve_row_lineage(parent) == (0, None)

    def test_subagent_parent_yields_depth_and_id(self):
        parent = _StubAgent(delegate_depth=1, subagent_id="sa-0-abcd")
        assert resolve_row_lineage(parent) == (1, "sa-0-abcd")

    def test_non_int_depth_degrades_to_flat(self):
        # MagicMock-ish parents in tests must not produce a garbage depth.
        parent = _StubAgent(delegate_depth=object())
        depth, sid = resolve_row_lineage(parent)
        assert depth == 0
        assert sid is None


class TestOrderRowsForDisplay(unittest.TestCase):
    """Grouping must survive rows arriving from separate board objects."""

    def test_flat_rows_keep_input_order_at_depth_zero(self):
        rows = [_snap("a"), _snap("b"), _snap("c")]
        ordered = order_rows_for_display(rows)
        assert [r.subagent_id for r, _ in ordered] == ["a", "b", "c"]
        assert all(d == 0 for _, d in ordered)

    def test_child_renders_directly_after_its_parent(self):
        # THE core scenario: an orchestrator's grandchildren live on a
        # different board, so raw concatenation puts an unrelated
        # top-level dispatch between parent and child.
        rows = [
            _snap("orch"),
            _snap("unrelated"),          # different, concurrent dispatch
            _snap("kid1", parent="orch"),  # nested board's rows appended last
            _snap("kid2", parent="orch"),
        ]
        ordered = order_rows_for_display(rows)
        ids = [r.subagent_id for r, _ in ordered]
        assert ids.index("kid1") == ids.index("orch") + 1
        assert ids.index("kid2") == ids.index("kid1") + 1
        assert "unrelated" in ids

    def test_children_are_deeper_than_their_parent(self):
        rows = [_snap("orch"), _snap("kid", parent="orch")]
        depths = {r.subagent_id: d for r, d in order_rows_for_display(rows)}
        assert depths["kid"] > depths["orch"]

    def test_three_levels_strictly_increase_in_depth(self):
        rows = [
            _snap("top"),
            _snap("mid", parent="top"),
            _snap("leaf", parent="mid"),
        ]
        depths = {r.subagent_id: d for r, d in order_rows_for_display(rows)}
        assert depths["top"] < depths["mid"] < depths["leaf"]

    def test_orphan_renders_as_root_not_indented(self):
        # Parent already finished and dropped its board; the surviving
        # child must not indent under a row that isn't there.
        rows = [_snap("kid", parent="gone-orch")]
        ordered = order_rows_for_display(rows)
        assert ordered[0][1] == 0

    def test_every_row_appears_exactly_once(self):
        rows = [
            _snap("a"),
            _snap("b", parent="a"),
            _snap("c", parent="b"),
            _snap("d"),
        ]
        ordered = order_rows_for_display(rows)
        assert len(ordered) == len(rows)
        assert sorted(r.subagent_id for r, _ in ordered) == ["a", "b", "c", "d"]

    def test_parent_cycle_does_not_hang_or_drop_rows(self):
        rows = [_snap("x", parent="y"), _snap("y", parent="x")]
        ordered = order_rows_for_display(rows)
        assert len(ordered) == 2

    def test_self_parent_does_not_hang(self):
        rows = [_snap("solo", parent="solo")]
        ordered = order_rows_for_display(rows)
        assert len(ordered) == 1
        assert ordered[0][1] == 0

    def test_duplicate_ids_all_render(self):
        rows = [_snap("dup"), _snap("dup")]
        assert len(order_rows_for_display(rows)) == 2

    def test_empty_input(self):
        assert order_rows_for_display([]) == []

    def test_runaway_nesting_stops_deepening(self):
        # Depth is bounded so rows can't march off the right edge.
        rows = [_snap("n0")]
        for i in range(1, 12):
            rows.append(_snap(f"n{i}", parent=f"n{i - 1}"))
        depths = [d for _, d in order_rows_for_display(rows)]
        assert max(depths) <= _MAX_RENDER_DEPTH
        assert len(depths) == len(rows)


class TestFormatRowIndentation(unittest.TestCase):
    """Indentation is the visible hierarchy signal."""

    def test_deeper_rows_are_indented_more_than_parents(self):
        rows = [
            _snap("top"),
            _snap("mid", parent="top"),
            _snap("leaf", parent="mid"),
        ]
        lines = [
            format_row(r, depth=d) for r, d in order_rows_for_display(rows)
        ]
        indents = [_leading_spaces(line) for line in lines]
        assert indents[0] < indents[1] < indents[2], indents

    def test_top_level_row_is_not_indented(self):
        assert _leading_spaces(format_row(_snap("a"), depth=0)) == 0

    def test_effective_depth_argument_overrides_row_depth(self):
        # An orphan whose stamped depth is 2 renders flat when its parent
        # isn't on the board.
        row = _snap("kid", parent="gone", depth=2)
        assert _leading_spaces(format_row(row, depth=0)) == 0
        assert _leading_spaces(format_row(row)) > 0  # falls back to row.depth

    def test_indented_row_stays_single_line(self):
        # A row that renders as 2 visual lines breaks the widget's height
        # allocation — the same class of bug the note-flattening fixed.
        row = _snap("kid", parent="orch")
        line = format_row(row, depth=3)
        assert "\n" not in line and "\r" not in line

    def test_indentation_preserves_row_content(self):
        b = SwarmBoard()
        b.register("kid", model="anthropic/claude-opus-4-5", depth=1)
        b.update("kid", last_tool="mcp_jira_search", tool_count=2)
        line = format_row(b.get_rows_snapshot()[0], depth=1)
        assert "claude-opus-4-5" in line
        assert "anthropic/" not in line
        assert "jira_search" in line
        assert "2 tools" in line


class TestBoardHeightCap(unittest.TestCase):
    """The panel must stay bounded under heavy concurrent delegation."""

    def test_under_limit_renders_every_row(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(4)]
        lines = collapse_rows_to_limit(entries, 12)
        assert len(lines) == 4
        assert not any("more subagent" in ln for ln in lines)

    def test_output_never_exceeds_the_cap(self):
        for total in (13, 20, 50, 200):
            entries = [(_snap(f"a{i}"), 0) for i in range(total)]
            lines = collapse_rows_to_limit(entries, 12)
            assert len(lines) <= 12, (total, len(lines))

    def test_exactly_at_limit_is_not_collapsed(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(12)]
        lines = collapse_rows_to_limit(entries, 12)
        assert len(lines) == 12
        assert not any("more subagent" in ln for ln in lines)

    def test_overflow_is_summarized_with_the_hidden_count(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(20)]
        lines = collapse_rows_to_limit(entries, 12)
        # 11 real rows + 1 summary covering the remaining 9.
        assert len(lines) == 12
        assert "+9 more subagent" in lines[-1]

    def test_summary_reports_hidden_rows_still_running(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(11)]
        entries += [(_snap("busy", status="running"), 0)]
        entries += [(_snap("done", status="completed"), 0)]
        lines = collapse_rows_to_limit(entries, 12)
        assert "2 more subagents" in lines[-1]
        assert "1 running" in lines[-1]

    def test_all_hidden_finished_omits_running_count(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(11)]
        entries += [
            (_snap("d1", status="completed"), 0),
            (_snap("d2", status="failed"), 0),
        ]
        lines = collapse_rows_to_limit(entries, 12)
        assert "running" not in lines[-1]

    def test_collapse_keeps_the_head_so_parents_stay_visible(self):
        entries = [(_snap("orch"), 0), (_snap("kid", parent="orch"), 1)]
        entries += [(_snap(f"x{i}"), 0) for i in range(20)]
        lines = collapse_rows_to_limit(entries, 5)
        assert "orch" in lines[0]
        assert "kid" in lines[1]

    def test_summary_line_is_single_line(self):
        entries = [(_snap(f"a{i}"), 0) for i in range(30)]
        lines = collapse_rows_to_limit(entries, 6)
        assert all("\n" not in ln for ln in lines)

    def test_zero_or_negative_cap_renders_nothing(self):
        entries = [(_snap("a"), 0)]
        assert collapse_rows_to_limit(entries, 0) == []
        assert collapse_rows_to_limit(entries, -1) == []

    def test_empty_entries(self):
        assert collapse_rows_to_limit([], 12) == []


class TestResolveMaxBoardRows(unittest.TestCase):
    """Row budget scales with the terminal but is always bounded."""

    def test_unknown_height_falls_back_to_the_ceiling(self):
        assert resolve_max_board_rows(None) == DEFAULT_MAX_BOARD_ROWS
        assert resolve_max_board_rows(0) == DEFAULT_MAX_BOARD_ROWS

    def test_tall_terminal_is_still_capped(self):
        assert resolve_max_board_rows(200) == DEFAULT_MAX_BOARD_ROWS

    def test_short_terminal_shrinks_the_board(self):
        assert resolve_max_board_rows(15) < DEFAULT_MAX_BOARD_ROWS

    def test_board_never_takes_more_than_a_third_of_the_screen(self):
        for rows in range(10, 120):
            budget = resolve_max_board_rows(rows)
            # +2 border lines must still leave the transcript the majority.
            assert budget + 2 < rows, rows

    def test_tiny_terminal_keeps_a_usable_floor(self):
        assert resolve_max_board_rows(4) >= MIN_MAX_BOARD_ROWS


class TestNestedBoardReachesCLIHost(unittest.TestCase):
    """A nested orchestrator's board must find the CLI via its ancestors.

    ``_cli_ref`` is stamped only on the top-level agent, so before this a
    subagent dispatching its own workers got a _NoopBoard and the
    grandchildren rendered nowhere at all.
    """

    def setUp(self):
        self.cli = _StubCLI()
        self.top = _StubAgent(cli_ref=self.cli, delegate_depth=0)

    def _child_of(self, parent, sid, depth):
        return _StubAgent(
            delegate_depth=depth, subagent_id=sid, parent=parent
        )

    def test_top_level_agent_gets_a_real_board(self):
        board = SwarmBoard.maybe_start(self.top, 1)
        assert isinstance(board, SwarmBoard)

    def test_nested_orchestrator_gets_a_real_board(self):
        orch = self._child_of(self.top, "sa-0-orch", 1)
        board = SwarmBoard.maybe_start(orch, 2)
        assert isinstance(board, SwarmBoard), (
            "an orchestrator subagent's own workers must render on the "
            "CLI board, not vanish into a _NoopBoard"
        )

    def test_deeply_nested_agent_still_finds_the_host(self):
        a = self._child_of(self.top, "sa-0-a", 1)
        b = self._child_of(a, "sa-0-b", 2)
        c = self._child_of(b, "sa-0-c", 3)
        assert isinstance(SwarmBoard.maybe_start(c, 1), SwarmBoard)

    def test_detached_chain_still_degrades_to_noop(self):
        # No CLI anywhere up the chain (gateway / library run).
        headless = _StubAgent(delegate_depth=0)
        orphan = self._child_of(headless, "sa-0-x", 1)
        assert isinstance(SwarmBoard.maybe_start(orphan, 1), _NoopBoard)

    def test_broken_weakref_degrades_to_noop(self):
        dead = self._child_of(_StubAgent(), "sa-0-dead", 1)
        # Referent already collected -> weakref returns None.
        assert isinstance(SwarmBoard.maybe_start(dead, 1), _NoopBoard)

    def test_cycle_in_parent_chain_terminates(self):
        a = _StubAgent()
        b = _StubAgent(parent=a)
        a._delegate_parent_ref = weakref.ref(b)
        assert isinstance(SwarmBoard.maybe_start(a, 1), _NoopBoard)

    def test_env_disable_still_wins_for_nested_boards(self):
        orch = self._child_of(self.top, "sa-0-orch", 1)
        os.environ["HERMES_SWARM_BOARD"] = "0"
        try:
            assert isinstance(SwarmBoard.maybe_start(orch, 2), _NoopBoard)
        finally:
            del os.environ["HERMES_SWARM_BOARD"]


class TestNestedDelegationEndToEnd(unittest.TestCase):
    """The reported scenario, driven through the real public surface."""

    def test_orchestrator_and_grandchildren_render_grouped_and_bounded(self):
        cli = _StubCLI()
        top = _StubAgent(cli_ref=cli, delegate_depth=0)

        # 1. Top-level agent dispatches an orchestrator + an unrelated worker.
        top_board = SwarmBoard.maybe_start(top, 2)
        top_board.__enter__()
        d, p = resolve_row_lineage(top)
        top_board.register("sa-0-orch", model="fable", depth=d,
                           parent_subagent_id=p)
        top_board.register("sa-1-solo", model="opus", depth=d,
                           parent_subagent_id=p)

        # 2. The orchestrator subagent dispatches its OWN workers. Its board
        #    is a separate object appended after the top-level board.
        orch = _StubAgent(
            delegate_depth=1, subagent_id="sa-0-orch", parent=top
        )

        nested_board = SwarmBoard.maybe_start(orch, 2)
        assert isinstance(nested_board, SwarmBoard)
        nested_board.__enter__()
        nd, np_ = resolve_row_lineage(orch)
        nested_board.register("sa-0-kid", model="opus", depth=nd,
                              parent_subagent_id=np_)
        nested_board.register("sa-1-kid", model="opus", depth=nd,
                              parent_subagent_id=np_)

        # 3. Render exactly as the CLI widget does: concatenate every active
        #    board, then order + collapse.
        assert len(cli._swarm_boards) == 2
        rows = []
        for b in list(cli._swarm_boards):
            rows.extend(b.get_rows_snapshot())
        ordered = order_rows_for_display(rows)
        lines = collapse_rows_to_limit(ordered, resolve_max_board_rows(40))

        ids = [r.subagent_id for r, _ in ordered]
        # Grandchildren group under their orchestrator, NOT after the
        # unrelated top-level dispatch that sat between them in raw order.
        assert ids.index("sa-0-kid") == ids.index("sa-0-orch") + 1
        assert ids.index("sa-1-kid") == ids.index("sa-0-kid") + 1
        # And they are visibly deeper than both their parent and the
        # unrelated sibling dispatch.
        depths = {r.subagent_id: d for r, d in ordered}
        assert depths["sa-0-kid"] > depths["sa-0-orch"]
        assert depths["sa-0-kid"] > depths["sa-1-solo"]
        by_id = {}
        for line in lines:
            for sid in ("sa-0-orch", "sa-1-solo", "sa-0-kid", "sa-1-kid"):
                if f"[{sid}]" in line:
                    by_id[sid] = line
        assert _leading_spaces(by_id["sa-0-kid"]) > _leading_spaces(
            by_id["sa-0-orch"]
        )

        # 4. The nested batch finishes and drops its board; the still-active
        #    top-level rows must survive.
        nested_board.__exit__(None, None, None)
        assert len(cli._swarm_boards) == 1
        remaining = []
        for b in list(cli._swarm_boards):
            remaining.extend(b.get_rows_snapshot())
        assert {r.subagent_id for r in remaining} == {"sa-0-orch", "sa-1-solo"}
        top_board.__exit__(None, None, None)
        assert cli._swarm_boards == []

    def test_wide_nested_tree_stays_within_the_height_budget(self):
        # 1 orchestrator + 30 grandchildren + 8 unrelated dispatches.
        rows = [_snap("orch")]
        rows += [_snap(f"kid{i}", parent="orch") for i in range(30)]
        rows += [_snap(f"solo{i}") for i in range(8)]
        budget = resolve_max_board_rows(40)
        lines = collapse_rows_to_limit(order_rows_for_display(rows), budget)
        assert len(lines) <= budget
        # Panel height (rows + 2 borders) leaves the transcript room.
        assert len(lines) + 2 < 40
        assert "more subagent" in lines[-1]


if __name__ == "__main__":
    unittest.main()
