"""Regression tests: an agent's ACTIVE BOARDS are a keyed collection, not one slot.

Background
----------
``parent_agent._swarm_board`` was a SINGLE per-agent attribute. Two things
made that structurally wrong rather than merely untidy:

1. **The CLI host went multi-board on 2026-08-09** (``998aba516c``). Before
   that commit ``cli.py`` held a singular ``self._swarm_board`` and the
   widget's visibility filter was ``cli_ref._swarm_board is not None`` — so
   genuinely only ONE board could render, and a per-agent single slot lost
   nothing. Afterwards the host keeps a ``_swarm_boards`` LIST and the widget
   concatenates rows from every board in it. The per-agent slot was never
   revisited.
2. **Nested delegation renders since 2026-08-23** (``f16110bd86``). A nested
   orchestrator reaches the CLI host through the delegation weakref chain, so
   a PM subagent's own ``delegate_task`` publishes a SECOND board onto the
   SAME agent object — deterministically, for the whole time the PM is blocked
   on its children. That is not a race that "might" happen under concurrency;
   it is the guaranteed steady state of every orchestrator dispatch.

These tests pin the invariants of the keyed registry that replaced the slot.
They are hermetic: pure state objects, no network, no subprocesses, no sleeps.
"""
from __future__ import annotations

import threading
import unittest

from tools.swarm_board import (
    SwarmBoard,
    _NoopBoard,
    agent_boards,
    attach_agent_board,
    board_for_row,
    current_agent_board,
    detach_agent_board,
)


class _StubCLI:
    """CLI host stub. ``_find_cli_host`` requires all three hooks, and
    ``_swarm_boards`` is the authoritative list the widget renders from."""

    def __init__(self):
        self._swarm_boards: list = []

    def _swarm_board_show(self, board):
        if board not in self._swarm_boards:
            self._swarm_boards.append(board)

    def _swarm_board_hide(self, board):
        try:
            self._swarm_boards.remove(board)
        except ValueError:
            pass

    def _invalidate_app(self):
        pass


class _StubAgent:
    """Stand-in for AIAgent. Deliberately NOT a MagicMock — board resolution
    rejects mock-ish boards via a strict ``is_active is True`` check."""

    def __init__(self, cli_ref=None, subagent_id=None):
        self._cli_ref = cli_ref
        self._subagent_id = subagent_id


def _row(board, sid):
    return {r.subagent_id: r for r in board.get_rows_snapshot()}[sid]


# ---------------------------------------------------------------------------
# Core registry invariants
# ---------------------------------------------------------------------------


class TestAgentBoardRegistry(unittest.TestCase):
    def setUp(self):
        self.agent = _StubAgent()
        self.a = SwarmBoard()
        self.b = SwarmBoard()

    def test_two_boards_coexist_on_one_agent(self):
        """The invariant the single slot could not express."""
        attach_agent_board(self.agent, self.a)
        attach_agent_board(self.agent, self.b)
        self.assertEqual(agent_boards(self.agent), [self.a, self.b])

    def test_detaching_one_board_leaves_the_sibling_attached(self):
        """A finishing dispatch must never evict a still-running sibling.

        The old teardown did ``parent_agent._swarm_board = None``, which
        blanked whatever board happened to be parked there.
        """
        attach_agent_board(self.agent, self.a)
        attach_agent_board(self.agent, self.b)
        detach_agent_board(self.agent, self.a)
        self.assertEqual(agent_boards(self.agent), [self.b])
        self.assertIs(current_agent_board(self.agent), self.b)

    def test_detach_is_scoped_to_the_named_board_only(self):
        """Detaching a board that was never attached must not disturb others."""
        attach_agent_board(self.agent, self.a)
        detach_agent_board(self.agent, SwarmBoard())
        self.assertEqual(agent_boards(self.agent), [self.a])

    def test_attach_is_idempotent(self):
        attach_agent_board(self.agent, self.a)
        attach_agent_board(self.agent, self.a)
        self.assertEqual(agent_boards(self.agent), [self.a])

    def test_double_detach_is_harmless(self):
        attach_agent_board(self.agent, self.a)
        detach_agent_board(self.agent, self.a)
        detach_agent_board(self.agent, self.a)
        self.assertEqual(agent_boards(self.agent), [])

    def test_empty_agent_reports_no_boards(self):
        self.assertEqual(agent_boards(_StubAgent()), [])
        self.assertIsNone(current_agent_board(_StubAgent()))

    def test_current_board_is_the_most_recent_attachment(self):
        """'Current' means innermost/newest — the board a nested dispatch just
        opened, which is what a caller asking for 'the' board wants."""
        attach_agent_board(self.agent, self.a)
        attach_agent_board(self.agent, self.b)
        self.assertIs(current_agent_board(self.agent), self.b)


# ---------------------------------------------------------------------------
# Backward compatibility with the legacy singular attribute
# ---------------------------------------------------------------------------


class TestLegacySlotCompatibility(unittest.TestCase):
    """``_swarm_board`` (singular) must keep working for any external or
    legacy reader, deriving a single 'current' board rather than breaking."""

    def test_legacy_slot_tracks_the_current_board(self):
        agent = _StubAgent()
        a, b = SwarmBoard(), SwarmBoard()
        attach_agent_board(agent, a)
        self.assertIs(agent._swarm_board, a)
        attach_agent_board(agent, b)
        self.assertIs(agent._swarm_board, b)

    def test_legacy_slot_falls_back_to_the_sibling_on_detach(self):
        """The key behavioral upgrade: teardown reveals the still-live
        sibling instead of nulling the slot outright."""
        agent = _StubAgent()
        a, b = SwarmBoard(), SwarmBoard()
        attach_agent_board(agent, a)
        attach_agent_board(agent, b)
        detach_agent_board(agent, b)
        self.assertIs(agent._swarm_board, a)

    def test_legacy_slot_clears_only_when_nothing_is_left(self):
        agent = _StubAgent()
        a = SwarmBoard()
        attach_agent_board(agent, a)
        detach_agent_board(agent, a)
        self.assertIsNone(agent._swarm_board)

    def test_raw_legacy_assignment_is_still_visible(self):
        """An external caller that sets the singular attribute directly (no
        registry involvement) must still be discoverable."""
        agent = _StubAgent()
        board = SwarmBoard()
        agent._swarm_board = board
        self.assertIn(board, agent_boards(agent))
        self.assertIs(current_agent_board(agent), board)

    def test_detach_of_a_raw_legacy_slot_clears_only_its_own_board(self):
        agent = _StubAgent()
        mine, theirs = SwarmBoard(), SwarmBoard()
        agent._swarm_board = theirs
        detach_agent_board(agent, mine)
        self.assertIs(agent._swarm_board, theirs)


# ---------------------------------------------------------------------------
# Row routing across concurrent / nested dispatch
# ---------------------------------------------------------------------------


class TestBoardForRow(unittest.TestCase):
    """``board_for_row`` follows the ROW, never whichever board is 'current'.

    ``SwarmBoard.update()`` returns silently for an unregistered row id, so a
    misdirected write produces no error — just a frozen row. Every assertion
    below therefore checks the row actually RECEIVED the value.
    """

    def setUp(self):
        self.cli = _StubCLI()
        self.agent = _StubAgent(cli_ref=self.cli)

    def test_finds_the_board_that_owns_the_row(self):
        owner = SwarmBoard()
        owner.register("sa-0-me", model="m", goal="g")
        other = SwarmBoard()
        other.register("sa-0-you", model="m", goal="g")
        attach_agent_board(self.agent, owner)
        attach_agent_board(self.agent, other)
        self.assertIs(board_for_row(self.agent, "sa-0-me"), owner)
        self.assertIs(board_for_row(self.agent, "sa-0-you"), other)

    def test_nested_dispatch_does_not_hide_the_orchestrators_own_row(self):
        """The PM-orchestrator shape. While the PM is blocked inside its own
        delegate_task, the grandchildren's board is the newest attachment on
        the SAME agent — the PM's own row must still resolve."""
        pm_board = SwarmBoard()
        pm_board.register("sa-0-pm", model="m", goal="PM")
        attach_agent_board(self.agent, pm_board)

        grandchildren = SwarmBoard()
        grandchildren.register("sa-1-worker", model="m", goal="worker")
        attach_agent_board(self.agent, grandchildren)

        resolved = board_for_row(self.agent, "sa-0-pm")
        self.assertIs(resolved, pm_board)
        resolved.update("sa-0-pm", tool_count=7)
        self.assertEqual(_row(pm_board, "sa-0-pm").tool_count, 7)
        self.assertEqual(len(grandchildren.get_rows_snapshot()), 1)

    def test_sibling_dispatch_does_not_steal_row_routing(self):
        mine = SwarmBoard()
        mine.register("sa-0-mine", model="m", goal="g")
        attach_agent_board(self.agent, mine)

        sibling = SwarmBoard()
        sibling.register("sa-0-sib", model="m", goal="g")
        attach_agent_board(self.agent, sibling)
        detach_agent_board(self.agent, sibling)  # sibling finishes

        board_for_row(self.agent, "sa-0-mine").update("sa-0-mine", tool_count=3)
        self.assertEqual(_row(mine, "sa-0-mine").tool_count, 3)

    def test_falls_back_to_the_cli_host_list(self):
        """A board published on the CLI host but never attached to this agent
        (e.g. resolved from a different agent in the tree) is still found."""
        board = SwarmBoard()
        board.register("sa-0-elsewhere", model="m", goal="g")
        self.cli._swarm_board_show(board)
        self.assertIs(board_for_row(self.agent, "sa-0-elsewhere"), board)

    def test_unknown_row_resolves_to_nothing(self):
        board = SwarmBoard()
        board.register("sa-0-other", model="m", goal="g")
        attach_agent_board(self.agent, board)
        self.assertIsNone(board_for_row(self.agent, "sa-0-nowhere"))

    def test_empty_subagent_id_resolves_to_nothing(self):
        board = SwarmBoard()
        board.register("sa-0-x", model="m", goal="g")
        attach_agent_board(self.agent, board)
        self.assertIsNone(board_for_row(self.agent, ""))
        self.assertIsNone(board_for_row(self.agent, None))

    def test_noop_board_never_claims_a_row(self):
        """Headless/non-tty must resolve to nothing so callers fall back to
        their normal scrollback path."""
        attach_agent_board(self.agent, _NoopBoard())
        self.assertIsNone(board_for_row(self.agent, "sa-0-anything"))


# ---------------------------------------------------------------------------
# Lifecycle through the real public surface
# ---------------------------------------------------------------------------


class TestBoardLifecyclePublishesToItsOwner(unittest.TestCase):
    def setUp(self):
        self.cli = _StubCLI()
        self.agent = _StubAgent(cli_ref=self.cli)

    def test_context_manager_attaches_and_detaches(self):
        with SwarmBoard.maybe_start(self.agent, 2) as board:
            self.assertIn(board, agent_boards(self.agent))
            self.assertIn(board, self.cli._swarm_boards)
        self.assertNotIn(board, agent_boards(self.agent))
        self.assertNotIn(board, self.cli._swarm_boards)

    def test_nested_context_managers_unwind_in_order(self):
        """Two overlapping dispatches on ONE agent — the exact shape the old
        single slot could not represent."""
        with SwarmBoard.maybe_start(self.agent, 1) as outer:
            with SwarmBoard.maybe_start(self.agent, 1) as inner:
                self.assertEqual(agent_boards(self.agent), [outer, inner])
                self.assertIs(current_agent_board(self.agent), inner)
            # Inner finished; the outer dispatch is still running and must
            # remain both attached and current.
            self.assertEqual(agent_boards(self.agent), [outer])
            self.assertIs(current_agent_board(self.agent), outer)
        self.assertEqual(agent_boards(self.agent), [])

    def test_publish_to_shares_a_board_with_a_child_agent(self):
        child = _StubAgent(subagent_id="sa-0-kid")
        with SwarmBoard.maybe_start(self.agent, 1) as board:
            board.register("sa-0-kid", model="m", goal="g")
            board.publish_to(child)
            self.assertIs(board_for_row(child, "sa-0-kid"), board)
        # Teardown unpublishes from every agent it was published to, not just
        # the owner — otherwise a finished board keeps absorbing writes.
        self.assertEqual(agent_boards(child), [])

    def test_noop_board_publish_is_a_silent_no_op(self):
        headless = _StubAgent(cli_ref=None)
        board = SwarmBoard.maybe_start(headless, 1)
        self.assertIsInstance(board, _NoopBoard)
        with board:
            board.publish_to(headless)  # must not raise
        self.assertEqual(agent_boards(headless), [])


class TestRegistryThreadSafety(unittest.TestCase):
    def test_concurrent_attach_detach_never_loses_a_live_board(self):
        """Sibling dispatches attach/detach from their own worker threads."""
        agent = _StubAgent()
        survivor = SwarmBoard()
        attach_agent_board(agent, survivor)
        errors: list = []
        start = threading.Barrier(9)

        def churn():
            try:
                start.wait(timeout=5)
                for _ in range(200):
                    b = SwarmBoard()
                    attach_agent_board(agent, b)
                    detach_agent_board(agent, b)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=churn) for _ in range(8)]
        for t in threads:
            t.start()
        start.wait(timeout=5)
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        # The long-lived board was never evicted by any sibling's teardown.
        self.assertEqual(agent_boards(agent), [survivor])
        self.assertIs(agent._swarm_board, survivor)


if __name__ == "__main__":
    unittest.main()
