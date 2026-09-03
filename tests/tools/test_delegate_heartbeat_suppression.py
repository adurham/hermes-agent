"""Regression tests for Bug 1: duplicate scrollback heartbeat lines while a
swarm board is rendering.

Root cause (see ``tools/swarm_board.py::any_board_active`` docstring): the
old suppression gate in ``delegate_tool.py`` read
``parent_agent._swarm_board`` — a SINGLE per-agent slot. Concurrent
``delegate_task()`` calls on the same parent overwrite and clear that slot
(the single-child teardown at the bottom of ``_execute_and_aggregate``
unconditionally did ``parent_agent._swarm_board = None``), so a sibling
batch's still-rendering board would silently open the heartbeat emit gate.
The fix reads the CLI host's ``_swarm_boards`` LIST instead — the same
collection the widget actually renders from.

These tests are hermetic: no network, no subprocesses, no real sleeps beyond
a monkeypatched ~20ms heartbeat interval.
"""
from __future__ import annotations

import threading
import time
import weakref
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeCLI:
    """Minimal stand-in for the CLI host, modeled on
    ``tests/tools/test_swarm_board.py::_StubCLI``. Carries the authoritative
    ``_swarm_boards`` LIST the real widget renders from.
    """

    def __init__(self):
        self._swarm_boards: list = []
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


class _FakeBoard:
    """Stand-in for a live SwarmBoard row-source; only ``is_active`` matters
    to the (pre-fix) single-slot gate."""

    is_active = True


class _StubChild:
    """Minimal AIAgent stand-in that hangs in run_conversation() for a
    controllable duration so the heartbeat loop gets several ticks."""

    def __init__(self, *, hang_seconds: float, subagent_id: str = "sa-0-fakeXY"):
        self._subagent_id = subagent_id
        self._delegate_depth = 1
        self._delegate_role = "leaf"
        self.model = "test/model"
        self.provider = "testprov"
        self.api_mode = "chat_completions"
        self.base_url = "https://example.test/v1"
        self.max_iterations = 30
        self.quiet_mode = True
        self.skip_memory = True
        self.skip_context_files = True
        self.platform = "cli"
        self.ephemeral_system_prompt = "sys prompt"
        self.enabled_toolsets = ["web"]
        self.valid_tool_names = {"web_search"}
        self.tools = [{"name": "web_search", "description": "search"}]
        self._hang = threading.Event()
        self._hang_seconds = hang_seconds

    def get_activity_summary(self):
        return {
            "api_call_count": 1,
            "max_iterations": self.max_iterations,
            "current_tool": "web_search",
            "seconds_since_activity": 0,
        }

    def run_conversation(self, user_message, task_id=None, stream_callback=None):
        self._hang.wait(self._hang_seconds)
        return {"final_response": "done", "completed": True, "api_calls": 1}

    def interrupt(self):
        self._hang.set()


def _make_parent(cli):
    """A plain object (not MagicMock) so getattr-based probing behaves like
    a real AIAgent: unset attributes raise/aren't magically truthy."""

    class _Parent:
        pass

    p = _Parent()
    p._cli_ref = cli
    p._touch_activity = MagicMock()
    p._current_task_id = None
    p._emit_status = MagicMock()
    p._swarm_board = None
    return p


def _run_child_with_race(monkeypatch, *, hang_seconds, clear_slot_after):
    """Drive the real ``_run_single_child`` while simulating the sibling
    teardown race: the board is published (both on the CLI list and the
    parent's single slot), then partway through the run the slot alone is
    cleared -- exactly what the old unconditional
    ``parent_agent._swarm_board = None`` teardown did to a SIBLING batch's
    still-active board.
    """
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 10_000)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IN_TOOL", 10_000)

    cli = FakeCLI()
    board = _FakeBoard()
    cli._swarm_boards.append(board)  # board is still rendering on the widget

    parent = _make_parent(cli)
    parent._swarm_board = board  # this call's own slot, initially set

    child = _StubChild(hang_seconds=hang_seconds)

    def _clear_slot_midflight():
        time.sleep(clear_slot_after)
        # Simulate the sibling race: null the SLOT only. The board stays
        # live in cli._swarm_boards the whole time.
        parent._swarm_board = None

    racer = threading.Thread(target=_clear_slot_midflight, daemon=True)
    racer.start()

    result = delegate_tool._run_single_child(
        task_index=0,
        goal="test goal",
        child=child,
        parent_agent=parent,
    )
    racer.join(timeout=2.0)
    return parent, cli, result


class TestHeartbeatSuppressionDuringSiblingRace:
    def test_no_duplicate_heartbeat_lines_while_sibling_board_active(
        self, monkeypatch
    ):
        """THE REGRESSION TEST. Fails on pre-fix code (single-slot gate),
        passes after the fix (list-based any_board_active gate)."""
        parent, cli, result = _run_child_with_race(
            monkeypatch, hang_seconds=0.35, clear_slot_after=0.05
        )

        assert result["status"] == "completed"
        # The board never left cli._swarm_boards for the CLI's list, so the
        # heartbeat gate must have stayed CLOSED the entire time, even after
        # the slot got nulled by the "sibling" teardown race.
        emitted_lines = [
            call.args[0]
            for call in parent._emit_status.call_args_list
            if call.args
        ]
        heartbeat_lines = [
            line for line in emitted_lines if "🔀" in line and "elapsed" in line
        ]
        assert heartbeat_lines == [], (
            f"expected zero heartbeat lines while sibling board active, got: "
            f"{heartbeat_lines!r}"
        )

    def test_heartbeat_emits_when_board_truly_gone(self, monkeypatch):
        """Sanity control: when the board is ACTUALLY torn down (removed
        from cli._swarm_boards too, not just the slot), the heartbeat must
        resume -- this proves the gate isn't just permanently stuck closed."""
        from tools import delegate_tool

        monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.02)
        monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
        monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 10_000)
        monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IN_TOOL", 10_000)

        cli = FakeCLI()  # no board ever added -- headless-equivalent state
        parent = _make_parent(cli)
        child = _StubChild(hang_seconds=0.15)

        result = delegate_tool._run_single_child(
            task_index=0, goal="test goal", child=child, parent_agent=parent,
        )
        assert result["status"] == "completed"
        emitted_lines = [
            call.args[0]
            for call in parent._emit_status.call_args_list
            if call.args
        ]
        heartbeat_lines = [
            line for line in emitted_lines if "🔀" in line and "elapsed" in line
        ]
        assert heartbeat_lines, "expected heartbeat lines to emit when no board is active"


# ---------------------------------------------------------------------------
# Unit tests for any_board_active
# ---------------------------------------------------------------------------


class TestAnyBoardActive:
    def test_true_via_cli_list_on_agent(self):
        from tools.swarm_board import any_board_active

        cli = FakeCLI()
        cli._swarm_boards.append(_FakeBoard())
        agent = type("A", (), {"_cli_ref": cli})()
        assert any_board_active(agent) is True

    def test_true_via_delegate_parent_ref_weakref_chain(self):
        from tools.swarm_board import any_board_active

        cli = FakeCLI()
        cli._swarm_boards.append(_FakeBoard())

        class _Root:
            pass

        root = _Root()
        root._cli_ref = cli

        class _Child:
            pass

        child = _Child()
        child._delegate_parent_ref = weakref.ref(root)

        assert any_board_active(child) is True

    def test_false_when_no_cli_ref_and_empty_slot_headless_contract(self):
        """THE HEADLESS CONTRACT: headless/gateway runs with no reachable
        CLI host and no board slot must still emit heartbeats."""
        from tools.swarm_board import any_board_active

        agent = type("A", (), {})()  # no _cli_ref, no _swarm_board
        assert any_board_active(agent) is False

    def test_false_for_noop_board_in_slot(self):
        from tools.swarm_board import _NoopBoard, any_board_active

        agent = type("A", (), {"_swarm_board": _NoopBoard()})()
        assert any_board_active(agent) is False

    def test_false_for_magicmock_parent(self):
        """LOAD-BEARING: a bare MagicMock() auto-creates every attribute,
        so a naive gate would see a truthy '_swarm_boards' / 'is_active'
        and wrongly suppress heartbeats for real headless test doubles.
        The isinstance(list) / `is True` checks must reject it."""
        from tools.swarm_board import any_board_active

        assert any_board_active(MagicMock()) is False
