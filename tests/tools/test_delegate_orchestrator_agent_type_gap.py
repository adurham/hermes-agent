"""Guardrail: dispatching role='orchestrator' with no agent_type= (and no
explicit model=) must surface a visible warning, not silently inherit the
parent's own model.

Bug (2026-08-30): a supervisor session dispatched a PM-tier subagent with
``role='orchestrator'`` intending it to run on ``delegation.model_by_role.pm``
(claude-opus-5), but omitted ``agent_type='pm'``. ``role`` grants CAPABILITY
(can this child spawn its own children) and is entirely independent of
``agent_type``, which is what actually routes the child through
``delegation.model_by_role.<agent_type>`` to pick its model. With no
agent_type, no per-task model, and no auto-route hit, the child silently
inherited the PARENT's own model/provider (claude-sonnet-5, an expensive
Anthropic session model) instead of the intended pm-tier model — no error, no
warning. The subagent ran ~110s on the wrong model before the mistake was
caught by manual inspection.

The fix adds this exact shape (role='orchestrator', agent_type=None,
model=None) to the existing ``_roster_warnings`` channel used by
test_delegate_model_roster_guardrail.py, so the gap is surfaced through the
SAME visible path (both the immediate tool-call response's
``model_roster_warnings`` and the async completion event) rather than being
silent.
"""

import json
from unittest.mock import MagicMock, patch

import hermes_cli.ruflo_agents as ruflo
import tools.async_delegation as ad
import tools.delegate_tool as dt


ROSTER_CFG = {
    "model": "claude-sonnet-5",
    "by_provider": {
        "anthropic": {"model": "claude-opus-5"},
        "ollama": {"model": "glm-5.3"},
    },
}
ROSTER_ENTRY_MAP = {
    # No provider pin here on purpose: a provider-pinned entry triggers real
    # credential resolution (_resolve_role_credentials), which this test
    # deliberately doesn't mock — matching
    # test_delegate_model_roster_guardrail.py's own unpinned 'coder' entry,
    # which only exercises the model_by_role -> role_map_model lookup.
    "pm": {"model": "claude-opus-5"},
    "coder": {"model": "claude-fable-5"},
}
ROSTER_CREDS = {
    "model": "claude-sonnet-5",
    "provider": "anthropic",
    "base_url": None,
    "api_key": "sk-test",
    "api_mode": None,
    "command": None,
    "args": None,
}

_G0 = "first real task with enough length"
_G1 = "second real task with enough length"


def _dispatch_roster(
    tasks,
    depth=0,
    roster_cfg=None,
    entry_map=None,
    creds_model="claude-sonnet-5",
    parent_model="PARENT-MODEL",
    **kwargs,
):
    """Mirrors test_delegate_model_roster_guardrail._dispatch_roster."""
    captured = []

    def _fake_build(**kw):
        captured.append(
            {
                "task_index": kw.get("task_index"),
                "model": kw.get("model"),
                "agent_type": kw.get("agent_type"),
                "role": kw.get("role"),
            }
        )
        child = MagicMock()
        child.model = kw.get("model")
        return child

    parent = MagicMock()
    parent.model = parent_model
    parent.provider = "anthropic"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = depth

    creds = dict(ROSTER_CREDS)
    creds["model"] = creds_model

    with patch.object(
        dt, "_load_config", return_value=dict(roster_cfg if roster_cfg is not None else {})
    ), patch.object(
        ruflo, "get_role_entry_map", return_value=dict(entry_map or {})
    ), patch.object(
        dt, "_resolve_delegation_credentials", return_value=creds
    ), patch.object(
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


def test_orchestrator_without_agent_type_warns_and_inherits_parent_model():
    """The incident shape: role='orchestrator', no agent_type, no model.
    Must surface a visible model_roster_warnings entry naming the gap, and
    (documenting existing — now warned-about — behavior) the child still
    inherits the parent/batch default model since nothing else pins one."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "role": "orchestrator"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    orch = next(c for c in captured if c["role"] == "orchestrator")
    assert orch["agent_type"] is None
    # Documents the pre-existing fallback behavior this guardrail warns
    # about, not a new restriction: the child still gets the batch/parent
    # default model. The warning is the fix, not a behavior change.
    assert orch["model"] == "claude-sonnet-5"
    warnings = result.get("model_roster_warnings")
    assert warnings, f"no warning surfaced for orchestrator-without-agent_type: {result}"
    assert any("agent_type" in w and "orchestrator" in w for w in warnings), warnings


def test_orchestrator_with_agent_type_no_warning():
    """Positive control: role='orchestrator' + agent_type='pm' set together
    must NOT trigger the gap warning — this is the correct, intended usage."""
    result, captured = _dispatch_roster(
        [
            {"goal": _G0, "role": "orchestrator", "agent_type": "pm"},
            {"goal": _G1},
        ],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    pm = next(c for c in captured if c["agent_type"] == "pm")
    assert pm["agent_type"] == "pm"
    warnings = result.get("model_roster_warnings") or []
    assert not any("agent_type" in w and "orchestrator" in w for w in warnings), warnings


def test_orchestrator_with_explicit_model_no_warning():
    """role='orchestrator' + an explicit roster-valid model= (no agent_type)
    is a deliberate pin, not the silent-inheritance gap — no warning."""
    result, captured = _dispatch_roster(
        [
            {"goal": _G0, "role": "orchestrator", "model": "claude-opus-5"},
            {"goal": _G1},
        ],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    orch = next(c for c in captured if c["role"] == "orchestrator")
    assert orch["model"] == "claude-opus-5"
    warnings = result.get("model_roster_warnings") or []
    assert not any("agent_type" in w and "orchestrator" in w for w in warnings), warnings


def test_leaf_role_without_agent_type_no_warning():
    """A plain leaf dispatch (the default role) with no agent_type is normal,
    common usage — must NOT trigger this orchestrator-specific warning."""
    result, captured = _dispatch_roster(
        [{"goal": _G0}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    warnings = result.get("model_roster_warnings") or []
    assert not any("orchestrator" in w for w in warnings), warnings
