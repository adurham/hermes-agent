"""Per-role delegation settings must reach the children they configure.

``delegation.reasoning_effort_by_role`` and ``delegation.max_iterations_by_role``
are read by ``hermes_cli.personas`` (lookup_reasoning_for_role /
lookup_max_iterations_for_role). A merge once dropped the delegate_tool call
sites, so both maps silently stopped doing anything while the config readers
(and their unit tests) kept passing. These tests write a real config.yaml into
the sandboxed HERMES_HOME and drive the real delegation entry points, asserting
on what the child is actually constructed with.

Precedence under test:
  reasoning: by_role[agent_type] > by_role[role] > delegation.reasoning_effort > parent
  max_iterations: by_role[agent_type] > by_role[role] > by_role[top_role] >
                  delegation.max_iterations > DEFAULT_MAX_ITERATIONS
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_yaml as yaml

import tools.async_delegation as ad
import tools.delegate_tool as dt
from hermes_constants import get_hermes_home

_PARENT_REASONING = {"enabled": True, "effort": "medium"}


def _write_delegation_cfg(delegation: dict) -> None:
    get_hermes_home().joinpath("config.yaml").write_text(
        yaml.safe_dump({"delegation": delegation}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# reasoning_effort_by_role — driven through _build_child_agent (the real caller
# of _resolve_child_runtime), capturing the kwargs AIAgent is constructed with.
# ---------------------------------------------------------------------------


def _parent() -> SimpleNamespace:
    return SimpleNamespace(
        model="parent-model",
        provider="anthropic",
        base_url="https://api.anthropic.invalid",
        api_key="sk-test",
        api_mode="anthropic_messages",
        capabilities={},
        fallback_model=None,
        request_overrides={},
        reasoning_config=dict(_PARENT_REASONING),
        acp_command=None,
        acp_args=[],
        enabled_toolsets=["terminal", "file"],
        disabled_toolsets=[],
        session_id=None,
        _session_db=None,
        _delegate_depth=0,
    )


def _child_reasoning(*, agent_type=None, role="leaf"):
    captured = {}

    def _fake_agent(**kw):
        captured.update(kw)
        return MagicMock()

    # _build_child_agent does ``from run_agent import AIAgent`` at call time; a stub
    # module keeps the real (bootstrap-heavy) run_agent import out of this test.
    stub = SimpleNamespace(AIAgent=_fake_agent)
    with patch.dict(sys.modules, {"run_agent": stub}):
        dt._build_child_agent(
            task_index=0, goal="do the thing", context=None, toolsets=None,
            model=None, max_iterations=10, task_count=1, parent_agent=_parent(),
            role=role, agent_type=agent_type,
        )
    assert captured, "AIAgent was never constructed"
    return captured["reasoning_config"]


def test_role_map_entry_beats_global_reasoning_effort():
    _write_delegation_cfg({
        "reasoning_effort": "low",
        "reasoning_effort_by_role": {"orchestrator": "xhigh"},
    })
    assert _child_reasoning(role="orchestrator") == {"enabled": True, "effort": "xhigh"}


def test_agent_type_entry_beats_spawn_role_entry():
    _write_delegation_cfg({
        "reasoning_effort": "low",
        "reasoning_effort_by_role": {"coder": "minimal", "orchestrator": "xhigh"},
    })
    assert _child_reasoning(agent_type="coder", role="orchestrator") == {
        "enabled": True, "effort": "minimal",
    }


def test_no_role_entry_falls_back_to_global_and_false_disables_thinking():
    # Map present but no entry for this child -> global value applies.
    _write_delegation_cfg({
        "reasoning_effort": "high",
        "reasoning_effort_by_role": {"reviewer": "xhigh"},
    })
    assert _child_reasoning(agent_type="coder", role="leaf") == {"enabled": True, "effort": "high"}

    # Global YAML ``false`` must disable thinking, not inherit the parent's level.
    _write_delegation_cfg({"reasoning_effort": False})
    assert _child_reasoning(agent_type="coder", role="leaf") == {"enabled": False}

    # Nothing configured at all -> parent inherit.
    _write_delegation_cfg({})
    assert _child_reasoning(agent_type="coder", role="leaf") == _PARENT_REASONING


# ---------------------------------------------------------------------------
# max_iterations_by_role — driven through delegate_task, capturing the
# max_iterations each child is built with.
# ---------------------------------------------------------------------------

_G0 = "first real task with enough length"
_G1 = "second real task with enough length"


def _dispatch_max_iters(tasks, **kwargs):
    captured = []

    def _fake_build(**kw):
        captured.append((kw.get("task_index"), kw.get("max_iterations")))
        child = MagicMock()
        child.model = kw.get("model")
        return child

    parent = MagicMock()
    parent.model = "PARENT-MODEL"
    parent.provider = "anthropic"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = 0

    with patch.object(dt, "_build_child_preserving_parent_tools", side_effect=_fake_build), \
            patch.object(ad, "dispatch_async_delegation_batch",
                         return_value={"status": "dispatched", "delegation_id": "d"}):
        dt.delegate_task(tasks=tasks, parent_agent=parent, background=True, **kwargs)
    return [n for _, n in sorted(captured)]


def test_max_iterations_role_entries_beat_global_cap():
    _write_delegation_cfg({
        "max_spawn_depth": 2,
        "max_iterations": 40,
        "max_iterations_by_role": {"orchestrator": 300, "coder": 7},
    })
    # Child 0: agent_type entry wins over the top-level role entry.
    # Child 1: no agent_type entry -> top-level role (orchestrator) entry wins over global.
    got = _dispatch_max_iters(
        [{"goal": _G0, "agent_type": "coder"}, {"goal": _G1, "agent_type": "researcher"}],
        role="orchestrator",
    )
    assert got == [7, 300]


def test_max_iterations_without_role_entries_falls_back_to_global():
    _write_delegation_cfg({
        "max_iterations": 40,
        "max_iterations_by_role": {"reviewer": 5},
    })
    assert _dispatch_max_iters([{"goal": _G0, "agent_type": "coder"}, {"goal": _G1}]) == [40, 40]

    _write_delegation_cfg({})
    assert _dispatch_max_iters([{"goal": _G0}, {"goal": _G1}]) == [
        dt.DEFAULT_MAX_ITERATIONS, dt.DEFAULT_MAX_ITERATIONS,
    ]
