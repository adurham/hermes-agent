"""Tests for ``tools.swarm_board`` — the live multi-row subagent board.

These tests exercise the data model and the no-op fallback path.  The
TTY-rendering path is not tested here — its visual correctness is
verified by hand and its integration is exercised by real swarm runs.
"""
from __future__ import annotations

import io
import time
import unittest

from tools.swarm_board import (
    SwarmBoard,
    _NoopBoard,
    _Row,
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

    def test_make_child_print_fn_returns_fallback_for_noop(self):
        captured = []
        b = _NoopBoard()
        fn = make_child_print_fn(b, "x", fallback=lambda *a, **k: captured.append(a))
        # Returned function should be the bare fallback (no wrapping).
        fn("hello")
        assert captured == [("hello",)]


class TestMaybeStartGating(unittest.TestCase):
    """``maybe_start`` is the policy wall — exercise its decision tree."""

    def test_single_child_returns_noop(self):
        # n_children < 2 → no-op regardless of TTY.
        b = SwarmBoard.maybe_start(parent_agent=object(), n_children=1)
        assert isinstance(b, _NoopBoard)

    def test_zero_children_returns_noop(self):
        b = SwarmBoard.maybe_start(parent_agent=object(), n_children=0)
        assert isinstance(b, _NoopBoard)

    def test_env_disable_returns_noop(self, monkeypatch=None):
        # Use os.environ patch directly since unittest.TestCase doesn't
        # carry a monkeypatch fixture.
        import os
        old = os.environ.get("HERMES_SWARM_BOARD")
        os.environ["HERMES_SWARM_BOARD"] = "0"
        try:
            b = SwarmBoard.maybe_start(parent_agent=object(), n_children=5)
            assert isinstance(b, _NoopBoard)
        finally:
            if old is None:
                del os.environ["HERMES_SWARM_BOARD"]
            else:
                os.environ["HERMES_SWARM_BOARD"] = old


class TestPrintFnRouting(unittest.TestCase):
    """The child print interceptor: most lines go to the row's note, but
    error-marker lines pass through to the fallback (so they survive in
    the scrollback)."""

    def setUp(self):
        # Real SwarmBoard — but we won't enter its context (no render
        # thread, no TTY writes).  We just test the data plumbing.
        self.board = SwarmBoard(out=io.StringIO(), refresh_interval=10.0)
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
        b = SwarmBoard(out=io.StringIO())
        b.register("a1", model="m", goal="g")
        b.register("a1", model="m2", goal="")  # update existing
        row = b._rows["a1"]
        assert row.model == "m2"  # updated
        assert row.goal == "g"   # untouched (empty arg = no-op)
        assert b._row_order == ["a1"]  # not duplicated

    def test_update_unknown_id_silently_ignored(self):
        b = SwarmBoard(out=io.StringIO())
        # Updating an unregistered row is a no-op (defensive — children
        # might fire callbacks before register completes).
        b.update("ghost", status="running")  # must not raise

    def test_note_truncates_long_text(self):
        b = SwarmBoard(out=io.StringIO())
        b.register("a1")
        b.note("a1", "x" * 200)
        assert len(b._rows["a1"].last_note) == 60
        assert b._rows["a1"].last_note.endswith("...")

    def test_finish_sets_ended_at_and_status(self):
        b = SwarmBoard(out=io.StringIO())
        b.register("a1")
        b.finish("a1", status="completed", summary="all good")
        row = b._rows["a1"]
        assert row.status == "completed"
        assert row.ended_at is not None
        assert "all good" in row.last_note


if __name__ == "__main__":
    unittest.main()
