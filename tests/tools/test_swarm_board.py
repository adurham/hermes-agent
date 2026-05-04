"""Tests for ``tools.swarm_board`` — the swarm board state container.

The board is now pure state + thread-safe mutators; rendering happens in the
CLI's prompt_toolkit widget.  These tests cover the data model, the
``maybe_start`` activation gate, the ``on_change`` invalidation hook, and the
child-print interceptor.
"""
from __future__ import annotations

import threading
import time
import unittest

from tools.swarm_board import (
    SwarmBoard,
    _NoopBoard,
    _Row,
    format_row,
    make_child_print_fn,
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
    without instantiating the real CLI."""

    def __init__(self):
        self._swarm_board = None
        self.show_calls = []
        self.hide_calls = 0
        self.invalidate_calls = 0

    def _swarm_board_show(self, board):
        self._swarm_board = board
        self.show_calls.append(board)

    def _swarm_board_hide(self):
        self._swarm_board = None
        self.hide_calls += 1

    def _invalidate_app(self):
        self.invalidate_calls += 1


class TestMaybeStartGating(unittest.TestCase):
    """``maybe_start`` is the policy wall — exercise its decision tree."""

    def test_single_child_returns_noop(self):
        # n_children < 2 → no-op regardless of CLI.
        parent = type("P", (), {"_cli_ref": _StubCLI()})()
        b = SwarmBoard.maybe_start(parent_agent=parent, n_children=1)
        assert isinstance(b, _NoopBoard)

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
            assert cli._swarm_board is board
        assert cli.hide_calls == 1
        assert cli._swarm_board is None


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


if __name__ == "__main__":
    unittest.main()
