"""Guardrail: nested subagent dispatches must go through agent_type= role
resolution, never a bare model= string.

Bug (2026-08-30): a subagent (itself a nested child, running as a PM)
dispatched ITS OWN nested child via delegate_task with a bare
``model="claude-sonnet-4-6..."`` and no ``agent_type``. The per-task model
precedence chain in delegate_task is ``task_model_explicit → role_map_model
→ auto-route → config default``, so the bare model= string won outright and
the child ran on an arbitrary model, bypassing the
``delegation.model_by_role`` role map that governs what a given role is
allowed to run on.

The fix is a guardrail gated on the dispatcher being a subagent
(``_delegate_depth >= 1``):

  - a bare ``model=`` with no ``agent_type=`` is REJECTED with a clear error;
  - ``model=`` alongside ``agent_type=`` is DROPPED in favor of role
    resolution (the role map is authoritative for nested children).

Top-level dispatches (depth 0) and the config-driven
``delegation.model_by_role.<role>.fallback`` chains are completely
unaffected — those tests live in test_batch_top_level_model_seeding.py and
test_delegate_role_fallback.py respectively.
"""

import json
from unittest.mock import MagicMock, patch

import tools.async_delegation as ad
import tools.delegate_tool as dt


def _dispatch(tasks, depth=0, **kwargs):
    """Run delegate_task, capturing the model/agent_type each child got.

    Returns (result, captured) where ``result`` is the parsed JSON return
    and ``captured`` is the list of kwargs passed to
    ``_build_child_preserving_parent_tools`` for every child actually
    constructed (empty when the call was rejected before construction).
    """
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
    parent._delegate_depth = depth

    with patch.object(
        dt, "_build_child_preserving_parent_tools", side_effect=_fake_build
    ), patch.object(
        ad,
        "dispatch_async_delegation_batch",
        return_value={"status": "dispatched", "delegation_id": "d"},
    ), patch.object(
        dt, "_get_max_spawn_depth", return_value=3
    ), patch.object(
        dt,
        "_run_single_child",
        return_value={
            "task_index": 0,
            "status": "completed",
            "summary": "ok",
            "api_calls": 1,
            "duration_seconds": 0.1,
        },
    ):
        result = dt.delegate_task(
            tasks=tasks, parent_agent=parent, background=True, **kwargs
        )

    return json.loads(result), captured


def _dispatch_single_goal(depth=0, **kwargs):
    """Run delegate_task via the single-goal form, returning parsed result."""
    parent = MagicMock()
    parent.model = "PARENT-MODEL"
    parent.provider = "anthropic"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = depth

    with patch.object(
        dt, "_build_child_preserving_parent_tools", side_effect=MagicMock()
    ), patch.object(
        dt, "_get_max_spawn_depth", return_value=3
    ):
        result = dt.delegate_task(
            goal="first real task with enough length", parent_agent=parent, **kwargs
        )
    return json.loads(result)


# Goals must clear the batch quality gate's minimum length.
_G0 = "first real task with enough length"
_G1 = "second real task with enough length"


# ── Nested (depth >= 1): the guardrail must fire ──────────────────────────


def test_nested_bare_model_rejected():
    """A subagent dispatching a child with a bare model= and no agent_type=
    must be rejected with a clear error, and no child may be constructed."""
    result, captured = _dispatch(
        [{"goal": _G0, "model": "claude-sonnet-4-6"}, {"goal": _G1}], depth=1
    )
    assert "error" in result, result
    assert "agent_type" in result["error"]
    assert "model" in result["error"]
    assert captured == [], "a child was constructed despite the rejection"


def test_nested_bare_model_rejected_single_goal():
    """Same guardrail through the single-goal form (top-level model=)."""
    result = _dispatch_single_goal(depth=1, model="claude-sonnet-4-6")
    assert "error" in result, result
    assert "agent_type" in result["error"]


def test_nested_model_dropped_when_agent_type_present():
    """model= alongside agent_type= is dropped in favor of role resolution:
    the child is built with agent_type= and NO model, so the role map picks
    the model."""
    result, captured = _dispatch(
        [
            {"goal": _G0, "model": "claude-sonnet-4-6", "agent_type": "coder"},
            {"goal": _G1},
        ],
        depth=1,
    )
    assert len(captured) == 2, captured
    coder = next(c for c in captured if c["agent_type"] == "coder")
    assert coder["model"] is None, (
        "model leaked into the child despite agent_type being set"
    )


def test_nested_agent_type_alone_is_allowed():
    """A nested child with agent_type= and no model= is the sanctioned path
    and must pass through untouched."""
    result, captured = _dispatch(
        [{"goal": _G0, "agent_type": "coder"}, {"goal": _G1}], depth=1
    )
    assert len(captured) == 2, captured
    assert any(c["agent_type"] == "coder" for c in captured)


def test_nested_batch_mixed_rejects_bare_model_task():
    """In a batch, a task with a bare model= is rejected even when a sibling
    task is well-formed — the whole call fails loudly, no partial fan-out."""
    result, captured = _dispatch(
        [
            {"goal": _G0, "agent_type": "coder"},
            {"goal": _G1, "model": "claude-sonnet-4-6"},
        ],
        depth=1,
    )
    assert "error" in result, result
    assert "Task 1" in result["error"]
    assert captured == [], "no child may be constructed on a rejected batch"


def test_nested_batch_model_dropped_per_task():
    """In a batch, model= is dropped per-task when agent_type= is present,
    and a sibling with only agent_type= is untouched."""
    result, captured = _dispatch(
        [
            {"goal": _G0, "model": "claude-sonnet-4-6", "agent_type": "coder"},
            {"goal": _G1, "agent_type": "researcher"},
        ],
        depth=1,
    )
    assert len(captured) == 2, captured
    by_type = {c["agent_type"]: c for c in captured}
    assert by_type["coder"]["model"] is None
    assert by_type["researcher"]["model"] is None


def test_nested_guardrail_does_not_mutate_caller_tasks():
    """The model-drop must copy the task dict, never rewrite the caller's
    own list (the batch branch takes caller dicts verbatim when there is no
    top-level model/agent_type to seed)."""
    tasks = [
        {"goal": _G0, "model": "claude-sonnet-4-6", "agent_type": "coder"},
        {"goal": _G1},
    ]
    _dispatch(tasks, depth=1)
    assert tasks == [
        {"goal": _G0, "model": "claude-sonnet-4-6", "agent_type": "coder"},
        {"goal": _G1},
    ]


# ── Top-level (depth 0): completely unaffected ─────────────────────────────


def test_top_level_bare_model_still_allowed():
    """A top-level dispatcher may still pass a bare model= — the guardrail
    must not touch depth-0 dispatches."""
    result, captured = _dispatch(
        [{"goal": _G0, "model": "claude-opus-5"}, {"goal": _G1}], depth=0
    )
    assert len(captured) == 2, captured
    # The bare model= reaches the child that stated it (task 0); the
    # model-less sibling falls through the normal chain untouched.
    assert captured[0]["model"] == "claude-opus-5"


def test_top_level_model_with_agent_type_keeps_model():
    """At depth 0, model= alongside agent_type= is NOT dropped — the
    existing precedence (per-task model wins) is preserved."""
    result, captured = _dispatch(
        [
            {"goal": _G0, "model": "claude-opus-5", "agent_type": "coder"},
            {"goal": _G1},
        ],
        depth=0,
    )
    assert len(captured) == 2, captured
    coder = next(c for c in captured if c["agent_type"] == "coder")
    assert coder["model"] == "claude-opus-5"
    assert coder["agent_type"] == "coder"
