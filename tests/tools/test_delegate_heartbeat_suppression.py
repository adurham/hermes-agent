"""Regression tests for duplicate scrollback heartbeat lines while the live
subagent dock is rendering.

Root cause (see ``tools/delegate_tool_registry.py::subagent_dock_active``): the
suppression gate must read the state the WIDGET actually renders from, not a
per-agent slot a concurrent sibling dispatch can clear out from under it. The
original bug read ``parent_agent._swarm_board`` — a SINGLE per-agent slot that
concurrent ``delegate_task()`` calls overwrite and clear — so a sibling batch's
still-rendering rows would silently open the heartbeat emit gate and the user
got duplicate output.

Ported from the swarm-board era (2026-09-22): the board those tests gated on was
retired in favour of upstream's ``hermes_cli/cli_subagent_monitor`` dock, which
now renders the same per-child model/status/tool/elapsed state. The invariants
are unchanged — only the widget (and therefore the authoritative collection,
``cli._subagent_monitor.entries``) is different.

These tests are hermetic: no network, no subprocesses, no real sleeps beyond a
monkeypatched ~20ms heartbeat interval.
"""
from __future__ import annotations

import threading
import time
import weakref
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeMonitor:
    """Stand-in for ``hermes_cli.cli_subagent_monitor.SubagentMonitor``.

    Only ``entries`` matters to the gate — it is the list the dock widget
    renders from, and the same list ``install_dock``'s visibility filter reads.
    """

    def __init__(self):
        self.entries: list = []


class FakeCLI:
    """Minimal stand-in for the CLI host carrying a live dock."""

    def __init__(self):
        self._subagent_monitor = _FakeMonitor()
        self.invalidate_calls = 0

    def _invalidate(self):
        self.invalidate_calls += 1


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
    a real AIAgent: unset attributes aren't magically truthy."""

    class _Parent:
        pass

    p = _Parent()
    p._cli_ref = cli
    p._touch_activity = MagicMock()
    p._current_task_id = None
    p._emit_status = MagicMock()
    return p


def _heartbeat_lines(parent):
    return [
        line
        for line in (c.args[0] for c in parent._emit_status.call_args_list if c.args)
        if "🔀" in line and "elapsed" in line
    ]


def _patch_heartbeat(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.02)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 10_000)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IN_TOOL", 10_000)
    return delegate_tool


class TestHeartbeatSuppressionDuringSiblingRace:
    def test_no_duplicate_heartbeat_lines_while_dock_rendering(self, monkeypatch):
        """THE REGRESSION TEST.

        A sibling dispatch mutating per-agent state mid-run must NOT reopen the
        emit gate while the dock still has rows on screen. The gate reads the
        dock's own entry list, so a racing mutation of anything else on the
        parent cannot affect it.
        """
        delegate_tool = _patch_heartbeat(monkeypatch)

        cli = FakeCLI()
        # The dock is rendering a row for this tree the whole time.
        cli._subagent_monitor.entries = [{"subagent_id": "sa-0-fakeXY"}]
        parent = _make_parent(cli)
        child = _StubChild(hang_seconds=0.35)

        def _sibling_teardown_race():
            time.sleep(0.05)
            # A concurrent sibling dispatch finishing mid-run: it tears down its
            # OWN per-agent display state. The dock's entries — the thing
            # actually on screen for this tree — are untouched, so the gate must
            # stay closed. Reading per-agent state here instead was the bug.
            parent._sibling_dispatch_done = True

        racer = threading.Thread(target=_sibling_teardown_race, daemon=True)
        racer.start()
        result = delegate_tool._run_single_child(
            task_index=0, goal="test goal", child=child, parent_agent=parent,
        )
        racer.join(timeout=2.0)

        assert result["status"] == "completed"
        assert _heartbeat_lines(parent) == [], (
            "expected zero heartbeat lines while the dock is rendering, got: "
            f"{_heartbeat_lines(parent)!r}"
        )

    def test_heartbeat_emits_when_dock_empty(self, monkeypatch):
        """Sanity control: with no rows on the dock the heartbeat must resume --
        this proves the gate isn't just permanently stuck closed."""
        delegate_tool = _patch_heartbeat(monkeypatch)

        cli = FakeCLI()  # monitor present but no entries -- nothing on screen
        parent = _make_parent(cli)
        child = _StubChild(hang_seconds=0.15)

        result = delegate_tool._run_single_child(
            task_index=0, goal="test goal", child=child, parent_agent=parent,
        )
        assert result["status"] == "completed"
        assert _heartbeat_lines(parent), (
            "expected heartbeat lines to emit when the dock shows nothing"
        )


# ---------------------------------------------------------------------------
# Unit tests for subagent_dock_active
# ---------------------------------------------------------------------------


class TestSubagentDockActive:
    def test_true_via_cli_monitor_on_agent(self):
        from tools.delegate_tool_registry import subagent_dock_active

        cli = FakeCLI()
        cli._subagent_monitor.entries = [{"subagent_id": "sa-0"}]
        agent = type("A", (), {"_cli_ref": cli})()
        assert subagent_dock_active(agent) is True

    def test_true_via_delegate_parent_ref_weakref_chain(self):
        """A nested orchestrator subagent has no ``_cli_ref`` of its own -- the
        gate must walk the delegation weakref chain to reach the CLI host."""
        from tools.delegate_tool_registry import subagent_dock_active

        cli = FakeCLI()
        cli._subagent_monitor.entries = [{"subagent_id": "sa-0"}]

        class _Root:
            pass

        root = _Root()
        root._cli_ref = cli

        class _Child:
            pass

        child = _Child()
        child._delegate_parent_ref = weakref.ref(root)

        assert subagent_dock_active(child) is True

    def test_false_when_dock_has_no_rows(self):
        from tools.delegate_tool_registry import subagent_dock_active

        agent = type("A", (), {"_cli_ref": FakeCLI()})()
        assert subagent_dock_active(agent) is False

    def test_false_when_no_cli_ref_headless_contract(self):
        """THE HEADLESS CONTRACT: headless/gateway runs with no reachable CLI
        host must still emit heartbeats."""
        from tools.delegate_tool_registry import subagent_dock_active

        agent = type("A", (), {})()  # no _cli_ref at all
        assert subagent_dock_active(agent) is False

    def test_false_for_magicmock_parent(self):
        """LOAD-BEARING: a bare MagicMock() auto-creates every attribute, so a
        duck-typed truthiness check would see a 'monitor' with 'entries' and
        wrongly suppress heartbeats for real headless test doubles. The
        isinstance(list) check must reject it."""
        from tools.delegate_tool_registry import subagent_dock_active

        assert subagent_dock_active(MagicMock()) is False

    def test_false_for_dead_weakref_in_chain(self):
        """A parent that has been garbage-collected ends the walk rather than
        raising -- the child then reports 'no dock' and keeps its heartbeats."""
        from tools.delegate_tool_registry import subagent_dock_active

        class _Root:
            pass

        root = _Root()
        ref = weakref.ref(root)
        child = type("C", (), {})()
        child._delegate_parent_ref = ref
        del root

        assert subagent_dock_active(child) is False
