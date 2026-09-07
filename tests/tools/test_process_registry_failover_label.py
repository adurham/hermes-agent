"""Async-delegation completion notifications name the model that actually ran.

``_format_async_delegation`` renders the block that re-enters the parent's
conversation when a background subagent finishes. It reported the model from
the completion event verbatim, which was the dispatch-time value — so a child
that silently failed over had its work attributed to a model it never used.

The result entry now carries live failover state (resolved off the child at
completion in ``delegate_tool``'s result build), and these tests pin that the
formatter renders it — with and without an active fallback, on both the
single and the batch path.
"""
from __future__ import annotations

import unittest

from tools.process_registry import _format_async_delegation, _result_model_label


def _single_evt(**overrides) -> dict:
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_abc123",
        "goal": "fix the thing",
        "role": "leaf",
        "model": "glm-5.3",
        "status": "completed",
        "summary": "Did the thing.",
        "api_calls": 4,
        "duration_seconds": 12.5,
        "dispatched_at": 1000.0,
        "completed_at": 1012.5,
    }
    evt.update(overrides)
    return evt


def _batch_evt(results, **overrides) -> dict:
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_batch1",
        "is_batch": True,
        "goals": [r.get("goal", "g") for r in results],
        "role": "leaf",
        "model": "glm-5.3",
        "status": "completed",
        "results": results,
        "total_duration_seconds": 30.0,
        "dispatched_at": 1000.0,
        "completed_at": 1030.0,
    }
    evt.update(overrides)
    return evt


class TestResultModelLabel(unittest.TestCase):
    def test_no_fallback_is_the_bare_slug(self):
        assert _result_model_label({"model": "glm-5.3"}) == "glm-5.3"

    def test_fallback_names_the_primary(self):
        label = _result_model_label(
            {
                "model": "claude-opus-5",
                "fallback_active": True,
                "primary_model": "glm-5.3",
            }
        )
        assert label == "⚠ claude-opus-5 (fallback from glm-5.3)"

    def test_legacy_entry_without_fallback_keys(self):
        """A completion recovered from a pre-upgrade async_delegations row."""
        assert _result_model_label({"model": "glm-5.3"}) == "glm-5.3"

    def test_missing_model(self):
        assert _result_model_label({}) == "?"


class TestSingleCompletionNotification(unittest.TestCase):
    def test_no_fallback_reads_exactly_as_before(self):
        block = _format_async_delegation(_single_evt())
        assert "Role: leaf   Model: glm-5.3" in block
        assert "⚠" not in block

    def test_fallback_renders_the_marker_and_the_primary(self):
        block = _format_async_delegation(
            _single_evt(
                model="claude-opus-5",
                fallback_active=True,
                primary_model="glm-5.3",
                primary_provider="ollama-cloud",
            )
        )
        assert (
            "Role: leaf   Model: ⚠ claude-opus-5 (fallback from glm-5.3)" in block
        )

    def test_fallback_without_a_primary_degrades(self):
        block = _format_async_delegation(
            _single_evt(model="claude-opus-5", fallback_active=True)
        )
        assert "Model: ⚠ claude-opus-5 (fallback)" in block

    def test_the_rest_of_the_block_is_untouched(self):
        block = _format_async_delegation(
            _single_evt(
                model="claude-opus-5", fallback_active=True, primary_model="glm-5.3"
            )
        )
        assert "[ASYNC DELEGATION COMPLETE — deleg_abc123]" in block
        assert "Original goal: fix the thing" in block
        assert "API calls: 4" in block
        assert "Did the thing." in block


class TestBatchCompletionNotification(unittest.TestCase):
    def test_no_fallback_reads_exactly_as_before(self):
        block = _format_async_delegation(
            _batch_evt(
                [
                    {"task_index": 0, "model": "glm-5.3", "status": "completed",
                     "summary": "a"},
                    {"task_index": 1, "model": "glm-5.3", "status": "completed",
                     "summary": "b"},
                ]
            )
        )
        assert "Role: leaf   Model: glm-5.3" in block
        assert "⚠" not in block

    def test_a_uniformly_failed_over_batch_shows_the_marker(self):
        block = _format_async_delegation(
            _batch_evt(
                [
                    {
                        "task_index": i,
                        "model": "claude-opus-5",
                        "fallback_active": True,
                        "primary_model": "glm-5.3",
                        "status": "completed",
                        "summary": "s",
                    }
                    for i in range(2)
                ]
            )
        )
        assert "⚠ claude-opus-5 (fallback from glm-5.3)" in block

    def test_a_mixed_batch_still_reports_varies(self):
        """One child failed over, one didn't — the header must not claim
        either one spoke for the whole batch."""
        block = _format_async_delegation(
            _batch_evt(
                [
                    {"task_index": 0, "model": "glm-5.3", "status": "completed",
                     "summary": "a"},
                    {
                        "task_index": 1,
                        "model": "claude-opus-5",
                        "fallback_active": True,
                        "primary_model": "glm-5.3",
                        "status": "completed",
                        "summary": "b",
                    },
                ]
            )
        )
        assert "batch default; per-task varies" in block


if __name__ == "__main__":
    unittest.main()
