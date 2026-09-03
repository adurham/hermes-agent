"""Regression tests: a subagent row's tool counter must follow the board that
OWNS the row, not the caller's mutable ``_swarm_board`` slot.

Live symptom (2026-09-02): a PM-style orchestrator subagent rendered
``running · 0 tools · thinking`` after ~2h and dozens of real tool calls,
while its note slot kept updating with its own children's completion lines.
The note path uses a board reference captured at dispatch; the tool-count path
re-resolved the board from ``parent_agent._swarm_board`` on every event — a
single-slot attribute that a CONCURRENT sibling ``delegate_task()`` on the same
parent overwrites (and later nulls).  ``SwarmBoard.update()`` on a board that
has no row for that id returns silently, so every ``tool_count=`` write was
discarded.

Styled after tests/tools/test_swarm_board.py: pure state objects, no network,
no subprocesses, no sleeps.
"""
from __future__ import annotations

import unittest

import tools.delegate_tool as dt
from tools.swarm_board import SwarmBoard


class _FakeCli:
    """CLI host stub. ``_find_cli_host`` requires all three hooks, and
    ``_swarm_boards`` is the authoritative list the widget renders from."""

    def __init__(self):
        self._swarm_boards = []

    def _swarm_board_show(self, board):
        self._swarm_boards.append(board)

    def _swarm_board_hide(self, board):
        if board in self._swarm_boards:
            self._swarm_boards.remove(board)

    def _invalidate_app(self):
        pass


class _FakeAgent:
    """Stand-in for AIAgent. Deliberately NOT a MagicMock — the callback
    refuses mock-ish boards via a strict ``is_active is True`` check."""

    def __init__(self, cli_ref=None):
        self._cli_ref = cli_ref
        self._swarm_board = None
        self._delegate_spinner = None

    def tool_progress_callback(self, *_a, **_kw):
        return None


def _row(board, sid):
    return {r.subagent_id: r for r in board.get_rows_snapshot()}[sid]


class TestOrchestratorRowKeepsCountingTools(unittest.TestCase):
    SID = "sa-0-eab5149c"

    def setUp(self):
        self.cli = _FakeCli()
        self.parent = _FakeAgent(cli_ref=self.cli)

        # Board A — the board this subagent's row was registered on.
        self.board_a = SwarmBoard()
        self.cli._swarm_board_show(self.board_a)
        self.board_a.register(
            self.SID, model="claude-opus-5", goal="PM task", status="running"
        )
        self.parent._swarm_board = self.board_a

        self.cb = dt._build_child_progress_callback(
            0, "PM task", self.parent, 1, subagent_id=self.SID
        )
        self.assertIsNotNone(self.cb)

    # -- baseline ---------------------------------------------------------
    def test_counts_normally_when_nothing_steals_the_slot(self):
        self.cb("tool.started", "read_file")
        self.cb("tool.started", "terminal")
        row = _row(self.board_a, self.SID)
        self.assertEqual(row.tool_count, 2)
        self.assertEqual(row.last_tool, "terminal")

    # -- the reported bug -------------------------------------------------
    def test_counts_survive_a_concurrent_sibling_dispatch(self):
        """A second delegate_task() on the SAME parent publishes its own board
        into the single ``_swarm_board`` slot (delegate_tool.py:5773/5856) and
        nulls it when it finishes (:5797). Neither may stop this row counting."""
        self.cb("tool.started", "read_file")

        sibling = SwarmBoard()                    # concurrent PM's board
        self.cli._swarm_board_show(sibling)
        sibling.register("sa-0-sibling", model="m", goal="other PM")
        self.parent._swarm_board = sibling        # slot stolen mid-flight

        self.cb("tool.started", "terminal")
        self.cb("tool.started", "skill_view")

        self.parent._swarm_board = None           # sibling finished, slot nulled
        self.cb("tool.started", "delegate_task")

        row = _row(self.board_a, self.SID)
        self.assertEqual(row.tool_count, 4)
        self.assertEqual(row.last_tool, "delegate_task")
        # A nested dispatch is a distinct state, and it comes from the same
        # update() call the count rides on — so it proves the write landed.
        self.assertEqual(row.status, "waiting_on_children")
        self.assertEqual(_row(sibling, "sa-0-sibling").tool_count, 0)

    def test_counts_survive_the_row_owners_own_nested_delegation(self):
        """The orchestrator shape: while the PM is blocked inside its OWN
        delegate_task, that call overwrites the PM agent's board slot with the
        grandchildren's board and then nulls it. The PM's row lives on board A."""
        self.cb("tool.started", "delegate_task")

        grandchildren = SwarmBoard()
        self.cli._swarm_board_show(grandchildren)
        grandchildren.register("sa-1-worker", model="m", goal="worker")
        self.parent._swarm_board = grandchildren

        self.cb("_thinking", "planning the next phase")
        self.cb("tool.started", "read_file")

        self.cli._swarm_board_hide(grandchildren)
        self.parent._swarm_board = None
        self.cb("tool.started", "execute_code")

        row = _row(self.board_a, self.SID)
        self.assertEqual(row.tool_count, 3)
        self.assertEqual(row.last_tool, "execute_code")

    # -- back-compat ------------------------------------------------------
    def test_headless_parent_still_uses_its_own_slot(self):
        """No CLI host (gateway/library/tests): the slot remains the only
        source, exactly as before."""
        headless = _FakeAgent(cli_ref=None)
        board = SwarmBoard()
        board.register("sa-0-headless", model="m", goal="g")
        headless._swarm_board = board
        cb = dt._build_child_progress_callback(
            0, "g", headless, 1, subagent_id="sa-0-headless"
        )
        cb("tool.started", "read_file")
        self.assertEqual(_row(board, "sa-0-headless").tool_count, 1)

    def test_unknown_row_never_lands_on_someone_elses_board(self):
        """A subagent with no row anywhere must not write onto an unrelated
        board just because it is the only one active."""
        other = SwarmBoard()
        other.register("sa-0-other", model="m", goal="g")
        self.cli._swarm_board_show(other)
        stray = _FakeAgent(cli_ref=self.cli)
        cb = dt._build_child_progress_callback(
            0, "g", stray, 1, subagent_id="sa-0-nowhere"
        )
        cb("tool.started", "read_file")
        self.assertEqual(_row(other, "sa-0-other").tool_count, 0)

    def test_torn_down_board_stops_absorbing_updates(self):
        """Once this row's board is hidden (its delegate_task returned), the
        callback must go quiet rather than keep writing to a dead board."""
        self.cb("tool.started", "read_file")
        self.cli._swarm_board_hide(self.board_a)
        self.parent._swarm_board = None
        self.cb("tool.started", "terminal")
        self.assertEqual(_row(self.board_a, self.SID).tool_count, 1)


if __name__ == "__main__":
    unittest.main()
