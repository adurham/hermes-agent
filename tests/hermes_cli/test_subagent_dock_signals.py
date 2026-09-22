"""The subagent dock renders every live signal, driven by the REAL progress path.

These are the parity tests for the 2026-09-22 consolidation that retired
``tools/swarm_board.py`` onto upstream's ``hermes_cli/cli_subagent_monitor``
dock. Each signal below existed ONLY on the swarm board before that change; the
dock rendered a flat ``goal · Ns · last: tool`` line. Retiring the board without
these would have silently dropped user-visible state.

Deliberately end-to-end rather than unit-mocked: every test drives the real
``_ChildProgressRelay`` against the real registry and renders through the real
``dock_text``/``roster_text``, because the whole bug class being guarded against
is "the producer and the widget disagree about where the data lives".
"""
from __future__ import annotations

import time

import pytest

from hermes_cli.cli_subagent_monitor import (
    SubagentMonitor,
    format_elapsed,
    order_rows_for_display,
    row_prefix,
    shorten_model,
)
from tools.delegate_tool_progress import _ChildProgressRelay
from tools.delegate_tool_registry import (
    _active_subagents,
    _register_subagent,
    _unregister_subagent,
)


class _Agent:
    """Weakref-able agent stand-in with the failover surface live reads use."""

    def __init__(self, model="claude-opus-5", provider="anthropic"):
        self.model = model
        self.provider = provider
        self.session_id = "sess-parent"

    def fail_over(self, model, provider="anthropic"):
        """Exactly what agent/chat_completion_helpers::try_activate_fallback does."""
        self._primary_runtime = {"model": self.model, "provider": self.provider}
        self.model = model
        self.provider = provider
        self._fallback_activated = True


class _CLI:
    def __init__(self, agent):
        self.agent = agent

    def _invalidate(self):
        pass


@pytest.fixture
def swarm():
    """A parent with two children: a nested orchestrator and its grandchild."""
    parent = _Agent()
    children = {
        "sa-0-parent01": _Agent(),
        "sa-1-child001": _Agent("glm-5.3", "ollama-cloud"),
    }
    started = time.time() - 65  # >60s so the MmSSs rollover applies
    for sid, (cid, child) in enumerate(children.items()):
        _register_subagent({
            "subagent_id": cid,
            "parent_id": None if cid.startswith("sa-0") else "sa-0-parent01",
            "depth": 0 if cid.startswith("sa-0") else 1,
            "goal": f"Goal for {cid}", "model": child.model,
            "started_at": started, "status": "running", "tool_count": 0,
            "agent": child, "owner_agent_session_id": "sess-parent",
        })
    try:
        yield parent, children
    finally:
        for cid in children:
            _unregister_subagent(cid)
        _active_subagents.pop("sa-2-extra001", None)


def _relay(parent, sid, child):
    return _ChildProgressRelay(
        task_index=0, goal=f"Goal for {sid}", spinner=None, parent_cb=None,
        task_count=2, subagent_id=sid, parent_id=None, depth=0,
        model=child.model, toolsets=None, session_ref={},
        agent_ref={"agent": child}, parent_agent=parent,
    )


def _dock(parent, columns=120):
    monitor = SubagentMonitor(_CLI(parent))
    monitor.refresh()
    return monitor, monitor.dock_text(columns=columns, rows=40)


class TestDockRendersPortedSignals:
    """One test per signal the swarm board uniquely carried."""

    def test_tool_count_and_last_tool(self, swarm):
        parent, children = swarm
        relay = _relay(parent, "sa-0-parent01", children["sa-0-parent01"])
        relay("tool.started", "search_files")
        relay("tool.started", "read_file")
        _, text = _dock(parent)
        assert "2 tools" in text
        assert "last: read_file" in text

    def test_tool_count_singular_at_one(self, swarm):
        parent, children = swarm
        _relay(parent, "sa-0-parent01", children["sa-0-parent01"])("tool.started", "read_file")
        _, text = _dock(parent)
        assert "1 tool ·" in text, "singular 'tool' expected at a count of one"

    def test_waiting_on_children_status_and_glyph(self, swarm):
        """A row blocked in its own nested delegate_task is distinct from
        'running' -- the signal that told a supervisor a PM row was idle."""
        parent, children = swarm
        _relay(parent, "sa-0-parent01", children["sa-0-parent01"])("tool.started", "delegate_task")
        _, text = _dock(parent)
        assert "waiting_on_children" in text
        assert "👥" in text

    def test_model_identity(self, swarm):
        parent, children = swarm
        for sid, child in children.items():
            _relay(parent, sid, child)("tool.started", "read_file")
        _, text = _dock(parent)
        assert "claude-opus-5" in text
        assert "glm-5.3" in text

    def test_fallback_indicator(self, swarm):
        """A child that silently failed over is marked, with BOTH halves of the
        label shortened past their provider prefix."""
        parent, children = swarm
        child = children["sa-0-parent01"]
        child.fail_over("claude-opus-5")
        child._primary_runtime = {"model": "ollama-cloud/glm-5.3", "provider": "ollama-cloud"}
        _relay(parent, "sa-0-parent01", child)("tool.started", "read_file")
        _, text = _dock(parent)
        assert "⚠" in text
        assert "glm-5.3→claude-opus-5" in text
        assert "ollama-cloud/" not in text, "provider prefix should be stripped"

    def test_lineage_indentation(self, swarm):
        """A grandchild renders indented under the orchestrator that spawned
        it, not as a flat sibling."""
        parent, children = swarm
        for sid, child in children.items():
            _relay(parent, sid, child)("tool.started", "read_file")
        _, text = _dock(parent)
        child_line = next(ln for ln in text.splitlines() if "sa-1-child001" in ln)
        parent_line = next(ln for ln in text.splitlines() if "sa-0-parent01" in ln)
        assert "└─" in child_line
        assert "└─" not in parent_line
        assert text.index(parent_line) < text.index(child_line), "parent renders first"

    def test_elapsed_rolls_over_to_mmsss(self, swarm):
        parent, children = swarm
        _relay(parent, "sa-0-parent01", children["sa-0-parent01"])("tool.started", "read_file")
        _, text = _dock(parent)
        assert "1m05s" in text
        assert "65s" not in text


class TestDockSurfaceInvariants:
    def test_empty_dock_renders_nothing(self):
        monitor = SubagentMonitor(_CLI(_Agent()))
        monitor.refresh()
        assert monitor.dock_text(columns=120, rows=40) == ''

    def test_narrow_terminal_clips_every_line_to_budget(self, swarm):
        """Rows must be trimmed to the terminal, not overflow it."""
        parent, children = swarm
        for sid, child in children.items():
            _relay(parent, sid, child)("tool.started", "read_file")
        _, text = _dock(parent, columns=46)
        from prompt_toolkit.utils import get_cwidth
        assert all(get_cwidth(line) <= 46 for line in text.splitlines())

    def test_collapsed_mode_still_one_line(self, swarm):
        parent, children = swarm
        _relay(parent, "sa-0-parent01", children["sa-0-parent01"])("tool.started", "read_file")
        monitor, _ = _dock(parent)
        monitor.collapsed = True
        assert len(monitor.dock_text(columns=120, rows=40).splitlines()) == 1

    def test_overflow_summary_reports_how_many_are_still_running(self, swarm):
        """'+N more, all finished' and '+N more, all running' are very
        different situations for someone watching a live dock."""
        parent, _ = swarm
        for i in range(6):
            _register_subagent({
                "subagent_id": f"sa-x-{i:06d}", "parent_id": None, "depth": 0,
                "goal": f"Extra {i}", "model": "claude-opus-5",
                "started_at": time.time(), "status": "running", "tool_count": 0,
                "agent": _Agent(), "owner_agent_session_id": "sess-parent",
            })
        try:
            _, text = _dock(parent)
            summary = next(ln for ln in text.splitlines() if "more" in ln)
            assert "running" in summary
        finally:
            for i in range(6):
                _unregister_subagent(f"sa-x-{i:06d}")


class TestPureRenderHelpers:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"), (5, "5s"), (59, "59s"), (60, "1m00s"), (65, "1m05s"), (729, "12m09s"),
    ])
    def test_elapsed_format(self, seconds, expected):
        assert format_elapsed(seconds) == expected

    def test_elapsed_tolerates_junk(self):
        assert format_elapsed(None) == "0s"
        assert format_elapsed("nope") == "0s"

    def test_shorten_model_strips_provider_prefix(self):
        assert shorten_model("ollama-cloud/glm-5.3") == "glm-5.3"
        assert shorten_model("claude-opus-5") == "claude-opus-5"
        assert shorten_model(None) == ""

    def test_orphan_renders_as_root_not_floating_indent(self):
        """A child whose parent already finished and left the registry must not
        render indented under nothing."""
        rows = [{"subagent_id": "b", "parent_id": "gone-a"}]
        assert order_rows_for_display(rows) == [(rows[0], 0)]

    def test_every_row_renders_exactly_once_under_a_parent_cycle(self):
        rows = [
            {"subagent_id": "a", "parent_id": "b"},
            {"subagent_id": "b", "parent_id": "a"},
        ]
        out = order_rows_for_display(rows)
        assert len(out) == 2
        assert {r["subagent_id"] for r, _ in out} == {"a", "b"}

    def test_indent_is_capped_so_deep_trees_cannot_march_off_screen(self):
        deep = row_prefix(99)
        assert deep == row_prefix(4)
        assert len(deep) < 20
