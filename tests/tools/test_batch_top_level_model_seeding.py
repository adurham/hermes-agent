"""Regression tests: top-level model/agent_type must reach BATCH children.

Bug (2026-08-25): ``delegate_task``'s batch branch assigned
``task_list = tasks`` verbatim, so a top-level ``model`` or ``agent_type``
was silently DROPPED for every fan-out. The single-``goal`` branch injects
both into its synthetic task, and the precedence comment above the
per-child resolution explicitly documents "top-level model" as a step in
the chain -- but the batch path never seeded it, so the chain's
``task_model_explicit`` was always None for a caller who set the model once
at the top level. Those children silently ran on
``delegation.by_provider.<p>.model`` instead, with nothing in the result
indicating the request had been ignored.

This is the failure recorded on 2026-08-21: a batch dispatched with a
top-level ``model`` override ran entirely on the config default.

The fix seeds via ``setdefault`` so an explicit per-task value still wins.
These tests assert both halves of that contract -- the default reaches
children, AND it never clobbers a per-task choice.
"""

from unittest.mock import MagicMock, patch

import tools.async_delegation as ad
import tools.delegate_tool as dt


def _dispatch(tasks, **kwargs):
    """Run delegate_task, capturing the model/agent_type each child got."""
    captured = []

    def _fake_build(**kw):
        captured.append(
            {
                "task_index": kw.get("task_index"),
                "model": kw.get("model"),
                "agent_type": kw.get("agent_type"),
            }
        )
        child = MagicMock()
        child.model = kw.get("model")
        return child

    parent = MagicMock()
    parent.model = "PARENT-MODEL"
    parent.provider = "anthropic"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = 0

    with patch.object(
        dt, "_build_child_preserving_parent_tools", side_effect=_fake_build
    ), patch.object(
        ad,
        "dispatch_async_delegation_batch",
        return_value={"status": "dispatched", "delegation_id": "d"},
    ):
        dt.delegate_task(
            tasks=tasks, parent_agent=parent, background=True, **kwargs
        )

    return sorted(captured, key=lambda c: c["task_index"])


# Goals must clear the batch quality gate's minimum length.
_G0 = "first real task with enough length"
_G1 = "second real task with enough length"


def test_top_level_model_reaches_batch_children():
    """The reported bug: top-level model was dropped for the whole fan-out."""
    got = _dispatch([{"goal": _G0}, {"goal": _G1}], model="claude-opus-5")
    assert [c["model"] for c in got] == ["claude-opus-5", "claude-opus-5"]


def test_top_level_agent_type_reaches_batch_children():
    """agent_type was dropped by the same code path."""
    got = _dispatch([{"goal": _G0}, {"goal": _G1}], agent_type="reviewer")
    assert [c["agent_type"] for c in got] == ["reviewer", "reviewer"]


def test_per_task_model_still_overrides_top_level():
    """Seeding must not invert precedence -- a stated per-task choice wins."""
    got = _dispatch(
        [{"goal": _G0}, {"goal": _G1, "model": "claude-haiku-4-5"}],
        model="claude-opus-5",
    )
    assert got[0]["model"] == "claude-opus-5"  # inherited the default
    assert got[1]["model"] == "claude-haiku-4-5"  # kept its own


def test_per_task_agent_type_still_overrides_top_level():
    got = _dispatch(
        [{"goal": _G0}, {"goal": _G1, "agent_type": "coder"}],
        agent_type="reviewer",
    )
    assert got[0]["agent_type"] == "reviewer"
    assert got[1]["agent_type"] == "coder"


def test_no_top_level_defaults_leaves_tasks_untouched():
    """Without top-level values the batch path must behave exactly as before."""
    got = _dispatch([{"goal": _G0, "model": "claude-haiku-4-5"}, {"goal": _G1}])
    assert got[0]["model"] == "claude-haiku-4-5"
    # Task 1 falls through to the normal chain (role map / config default),
    # never to a leaked value from task 0.
    assert got[1]["model"] != "claude-haiku-4-5"


def test_caller_task_dicts_are_not_mutated():
    """Seeding copies -- the caller's own list must not be rewritten."""
    tasks = [{"goal": _G0}, {"goal": _G1}]
    _dispatch(tasks, model="claude-opus-5", agent_type="reviewer")
    assert tasks == [{"goal": _G0}, {"goal": _G1}]
