"""Swarm-board rows surface a subagent's failover state.

``register()`` stamped a row's model once at dispatch and ``update()`` had no
model parameter at all, so a child that silently failed over kept rendering
under the model it started on for the life of the board.

``format_row`` must stay a PURE function — the CLI's prompt_toolkit widget
getter calls it without the board's lock — so the failover state travels on
``RowSnapshot``/``_Row`` rather than being resolved from a live agent at
render time. These tests pin both halves.
"""
from __future__ import annotations

import unittest

from tools.swarm_board import (
    RowSnapshot,
    SwarmBoard,
    _Row,
    _shorten_model,
    format_row,
)


def _snapshot(**overrides) -> RowSnapshot:
    base = dict(
        subagent_id="sa-1",
        model="claude-opus-5",
        goal="do the thing",
        status="running",
        tool_count=3,
        last_tool="read_file",
        last_note="",
        elapsed_seconds=12.0,
    )
    base.update(overrides)
    return RowSnapshot(**base)


class TestFormatRowFallbackIndicator(unittest.TestCase):
    def test_no_fallback_renders_the_bare_model(self):
        """A healthy row is byte-identical to the pre-fix rendering."""
        line = format_row(_snapshot())
        assert "claude-opus-5" in line
        assert "⚠" not in line
        assert "→" not in line

    def test_fallback_renders_the_compact_swap(self):
        line = format_row(
            _snapshot(
                model="claude-opus-5",
                fallback_active=True,
                primary_model="glm-5.3",
            )
        )
        assert "⚠ glm-5.3→claude-opus-5" in line

    def test_fallback_without_a_primary_degrades(self):
        line = format_row(_snapshot(model="claude-opus-5", fallback_active=True))
        assert "⚠ claude-opus-5" in line
        assert "→" not in line

    def test_both_halves_are_shortened(self):
        """A namespaced primary must not blow the row's width budget."""
        line = format_row(
            _snapshot(
                model="anthropic/claude-opus-5",
                fallback_active=True,
                primary_model="ollama-cloud/glm-5.3",
            )
        )
        assert "⚠ glm-5.3→claude-opus-5" in line
        assert "ollama-cloud" not in line
        assert "anthropic" not in line

    def test_rest_of_the_row_is_unchanged(self):
        line = format_row(
            _snapshot(fallback_active=True, primary_model="glm-5.3")
        )
        assert "🔀" in line
        assert "running" in line
        assert "3 tools" in line
        assert "read_file" in line
        assert "12s" in line

    def test_still_pure_no_lock_no_agent(self):
        """Callable from the widget getter with a bare snapshot."""
        line = format_row(
            RowSnapshot(
                subagent_id="sa-2",
                model="claude-opus-5",
                goal="",
                status="running",
                tool_count=0,
                last_tool="",
                last_note="",
                elapsed_seconds=1.0,
                fallback_active=True,
                primary_model="glm-5.3",
            )
        )
        assert "⚠ glm-5.3→claude-opus-5" in line

    def test_indentation_still_applies_to_a_fallback_row(self):
        line = format_row(
            _snapshot(fallback_active=True, primary_model="glm-5.3", depth=1)
        )
        assert line.startswith("  └─ ")
        assert "⚠ glm-5.3→claude-opus-5" in line


class TestShortenModel(unittest.TestCase):
    def test_strips_the_provider_prefix(self):
        assert _shorten_model("anthropic/claude-opus-5") == "claude-opus-5"

    def test_leaves_a_bare_slug_alone(self):
        assert _shorten_model("claude-opus-5") == "claude-opus-5"

    def test_empty_inputs(self):
        assert _shorten_model("") == ""
        assert _shorten_model(None) == ""

    def test_only_the_first_separator_is_split(self):
        assert _shorten_model("a/b/c") == "b/c"


class TestRowFallbackDefaults(unittest.TestCase):
    def test_row_defaults_to_not_in_fallback(self):
        row = _Row(subagent_id="sa-1")
        assert row.fallback_active is False
        assert row.primary_model is None

    def test_snapshot_carries_the_state_through(self):
        row = _Row(subagent_id="sa-1", fallback_active=True, primary_model="glm-5.3")
        snap = row.snapshot()
        assert snap.fallback_active is True
        assert snap.primary_model == "glm-5.3"


class TestBoardModelResync(unittest.TestCase):
    """``update()`` gained model params so a row can be re-synced post-dispatch."""

    def _board(self) -> SwarmBoard:
        return SwarmBoard(on_change=lambda: None)

    def test_update_rewrites_a_registered_rows_model(self):
        board = self._board()
        board.register("sa-1", model="glm-5.3", goal="g")
        board.update(
            "sa-1",
            model="claude-opus-5",
            fallback_active=True,
            primary_model="glm-5.3",
        )
        row = board.get_rows_snapshot()[0]
        assert row.model == "claude-opus-5"
        assert row.fallback_active is True
        assert row.primary_model == "glm-5.3"
        assert "⚠ glm-5.3→claude-opus-5" in format_row(row)

    def test_update_without_model_kwargs_leaves_the_marker_alone(self):
        """fallback_active is tri-state: None means "no opinion"."""
        board = self._board()
        board.register(
            "sa-1", model="claude-opus-5", fallback_active=True,
            primary_model="glm-5.3",
        )
        board.update("sa-1", tool_count=5)
        row = board.get_rows_snapshot()[0]
        assert row.fallback_active is True
        assert row.primary_model == "glm-5.3"
        assert row.tool_count == 5

    def test_update_can_clear_the_marker_on_restore(self):
        """restore_primary_runtime makes failover reversible."""
        board = self._board()
        board.register(
            "sa-1", model="claude-opus-5", fallback_active=True,
            primary_model="glm-5.3",
        )
        board.update("sa-1", model="glm-5.3", fallback_active=False)
        row = board.get_rows_snapshot()[0]
        assert row.fallback_active is False
        assert format_row(row).count("⚠") == 0

    def test_register_seeds_a_child_already_in_fallback(self):
        """init_agent can activate a fallback during build, before any event."""
        board = self._board()
        board.register(
            "sa-1", model="claude-opus-5", goal="g",
            fallback_active=True, primary_model="glm-5.3",
        )
        row = board.get_rows_snapshot()[0]
        assert row.fallback_active is True
        assert "⚠ glm-5.3→claude-opus-5" in format_row(row)

    def test_update_on_an_unknown_row_is_still_a_silent_drop(self):
        board = self._board()
        board.update("nope", model="claude-opus-5", fallback_active=True)
        assert board.get_rows_snapshot() == []

    def test_existing_update_callers_are_unaffected(self):
        board = self._board()
        board.register("sa-1", model="glm-5.3", goal="g")
        board.update("sa-1", status="running", tool_count=2, last_tool="grep")
        row = board.get_rows_snapshot()[0]
        assert row.model == "glm-5.3"
        assert row.fallback_active is False
        assert row.status == "running"


if __name__ == "__main__":
    unittest.main()
