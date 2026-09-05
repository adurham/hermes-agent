"""Guardrail: caller-supplied model= strings are validated against the live
config model roster at EVERY delegation depth, closing the depth-0 gap.

Bug (2026-08-30): the TOP-LEVEL session (depth 0) dispatched delegate_task
with ``model="claude-opus-4-7"`` — a stale string typed from assistant
memory — and the harness silently accepted it and ran a real subagent on it.
The nested-only guardrail (depth >= 1) did not protect depth 0.

The fix is a single unified validation pass over the task list at every
depth, backed by a 'known current models' roster built once per call from:

  (a) delegation.by_provider.<p>.model (every provider block)
  (b) top-level delegation.model (legacy)
  (c) delegation.model_by_role entries (get_role_entry_map) — each entry's
      model plus its nested ``fallback`` dict's model
  (d) creds["model"] (the resolved batch default)
  (e) parent_agent.model (the live running model)

Matching is case-insensitive and tolerant of a provider-prefix segment
(``"anthropic/claude-opus-5"`` matches roster entry ``"claude-opus-5"``).

FAIL-OPEN: when the config roster is empty (no config, fresh install,
sandboxed test home — conftest sandboxes HERMES_HOME to a per-test tempdir,
so the roster is empty unless a test seeds it), the depth-0 roster-validity
check is skipped and delegation behaves exactly as before. The nested
(depth >= 1) role-governance rules are NOT gated on the roster.

Semantics per task with a non-empty model=:

  - bare model= (no agent_type=):
      depth >= 1  -> REJECT (role governance, unchanged)
      depth 0     -> REJECT if STALE (not in roster); allow if roster-valid
  - model= alongside agent_type=:
      depth >= 1  -> DROP model (role resolution wins, unchanged); surface a
                     warning in the result when STALE
      depth 0     -> DROP model + surface a warning when STALE; keep model
                     (explicit pin wins) when roster-valid
"""

import json
from unittest.mock import MagicMock, patch

import hermes_cli.ruflo_agents as ruflo
import tools.async_delegation as ad
import tools.delegate_tool as dt


# ── Gen-5 roster (mirrors the live user config roster shape) ────────────────
# claude-opus-4-7 (the incident string) matches NONE of these.
ROSTER_CFG = {
    "model": "claude-sonnet-5",
    "by_provider": {
        "anthropic": {"model": "claude-opus-5"},
        "ollama": {"model": "glm-5.3"},
    },
}
ROSTER_ENTRY_MAP = {
    "coder": {"model": "claude-fable-5"},
    "researcher": {
        "model": "claude-haiku-4-5-20251001",
        "fallback": {"model": "gemma4:31b"},
    },
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

# Goals must clear the batch quality gate's minimum length.
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
    """Run delegate_task with a seeded model roster, capturing the
    model/agent_type each child got.

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


def _dispatch_single_goal_roster(
    depth=0,
    roster_cfg=None,
    entry_map=None,
    creds_model="claude-sonnet-5",
    parent_model="PARENT-MODEL",
    **kwargs,
):
    """Run delegate_task via the single-goal form with a seeded roster,
    returning the parsed result (synchronous path)."""
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
        dt, "_build_child_preserving_parent_tools", side_effect=MagicMock()
    ), patch.object(
        dt, "_get_max_spawn_depth", return_value=3
    ):
        result = dt.delegate_task(
            goal="first real task with enough length", parent_agent=parent, **kwargs
        )
    return json.loads(result)


# ── depth 0: bare STALE model rejected ──────────────────────────────────────


def test_depth0_bare_stale_model_rejected_batch():
    """A top-level bare model= that is NOT in the roster must be rejected
    with an error naming the model and task index, and no child constructed."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "claude-opus-4-7"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert "error" in result, result
    assert "claude-opus-4-7" in result["error"]
    assert "Task 0" in result["error"]
    assert "roster" in result["error"]
    assert captured == [], "a child was constructed despite the rejection"


def test_depth0_bare_stale_model_rejected_single_goal():
    """Same rejection through the single-goal form (top-level model=)."""
    result = _dispatch_single_goal_roster(
        depth=0,
        model="claude-opus-4-7",
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert "error" in result, result
    assert "claude-opus-4-7" in result["error"]
    assert "roster" in result["error"]


# ── depth 0: STALE model + agent_type -> dropped with a visible warning ─────


def test_depth0_stale_model_with_agent_type_dropped_and_warned():
    """At depth 0, a STALE model= alongside agent_type= is dropped in favor
    of role resolution, and a warning naming the dropped model is surfaced in
    the result JSON."""
    result, captured = _dispatch_roster(
        [
            {"goal": _G0, "model": "claude-opus-4-7", "agent_type": "coder"},
            {"goal": _G1},
        ],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    coder = next(c for c in captured if c["agent_type"] == "coder")
    assert coder["model"] != "claude-opus-4-7", (
        "stale model leaked into the child despite agent_type being set"
    )
    assert coder["agent_type"] == "coder"
    warnings = result.get("model_roster_warnings")
    assert warnings, f"no roster warning surfaced in result: {result}"
    assert any("claude-opus-4-7" in w for w in warnings), warnings


# ── depth 0: roster-VALID models pass through unchanged ─────────────────────


def test_depth0_roster_valid_bare_model_allowed():
    """Positive control: a bare model= that IS in the roster passes through
    unchanged (proves validation matches, not rejects-everything)."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "claude-opus-5"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "claude-opus-5"
    assert "error" not in result


def test_depth0_roster_valid_model_with_agent_type_kept():
    """At depth 0, a roster-valid model= alongside agent_type= is KEPT
    (explicit pin wins — existing precedence, unchanged)."""
    result, captured = _dispatch_roster(
        [
            {"goal": _G0, "model": "claude-opus-5", "agent_type": "coder"},
            {"goal": _G1},
        ],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    coder = next(c for c in captured if c["agent_type"] == "coder")
    assert coder["model"] == "claude-opus-5"
    assert coder["agent_type"] == "coder"
    # Narrowed 2026-09-04 from a blanket `"model_roster_warnings" not in
    # result`: this test's subject is task 0 (roster-valid model= alongside
    # agent_type= must NOT be dropped), but the batch's filler task 1
    # ({"goal": _G1}) states neither model= nor agent_type= and now
    # legitimately draws the silent-omission notice. Assert the actual
    # intent — no drop/ignore warning about task 0's model — instead of the
    # absence of the shared channel, matching the `any(...)` style the
    # sibling assertions in this file already use.
    warnings = result.get("model_roster_warnings") or []
    assert not any("IGNORED" in w for w in warnings), warnings
    assert not any("Task 0" in w for w in warnings), warnings


# ── depth 0: EMPTY roster -> fail-open ──────────────────────────────────────


def test_depth0_empty_roster_fails_open_bare_model_allowed():
    """With no config roster (empty cfg, empty entry map), a bare model= is
    allowed through — fail-open, exactly as before the guardrail."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "claude-opus-4-7"}, {"goal": _G1}],
        depth=0,
        roster_cfg={},
        entry_map={},
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "claude-opus-4-7"
    assert "error" not in result


# ── roster-source coverage ──────────────────────────────────────────────────


def test_depth0_model_in_fallback_dict_counts_as_known():
    """A model present ONLY in a model_by_role entry's nested fallback dict
    counts as known current."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "gemma4:31b"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "gemma4:31b"
    assert "error" not in result


def test_depth0_model_in_other_provider_block_counts_as_known():
    """A model present ONLY in a DIFFERENT provider's by_provider block counts
    as known current."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "glm-5.3"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "glm-5.3"
    assert "error" not in result


def test_depth0_parent_model_counts_as_known():
    """parent_agent.model counts as known current — a task model matching the
    parent's own running model is clearly not stale."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "PARENT-MODEL"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
        parent_model="PARENT-MODEL",
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "PARENT-MODEL"
    assert "error" not in result


def test_depth0_creds_model_counts_as_known():
    """creds['model'] (the resolved batch default) counts as known current."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "claude-sonnet-5"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
        creds_model="claude-sonnet-5",
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "claude-sonnet-5"
    assert "error" not in result


# ── normalization: case / provider-prefix ───────────────────────────────────


def test_depth0_case_and_prefix_variants_match():
    """Case-insensitive and provider-prefix-tolerant matching: a model string
    like 'Anthropic/claude-opus-5' matches roster entry 'claude-opus-5'."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "Anthropic/claude-opus-5"}, {"goal": _G1}],
        depth=0,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    assert captured[0]["model"] == "Anthropic/claude-opus-5"
    assert "error" not in result


# ── nested (depth >= 1): existing semantics preserved ───────────────────────


def test_nested_bare_stale_model_still_rejected():
    """A nested bare model= is rejected by role governance regardless of
    roster validity — the existing nested rule is unchanged."""
    result, captured = _dispatch_roster(
        [{"goal": _G0, "model": "claude-opus-5"}, {"goal": _G1}],
        depth=1,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert "error" in result, result
    assert "agent_type" in result["error"]
    assert captured == []


def test_nested_stale_model_with_agent_type_dropped_and_warned():
    """A nested STALE model= alongside agent_type= is dropped in favor of
    role resolution, and the drop is now surfaced as a warning in the result
    (previously only logged)."""
    result, captured = _dispatch_roster(
        [
            {"goal": _G0, "model": "claude-opus-4-7", "agent_type": "coder"},
            {"goal": _G1},
        ],
        depth=1,
        roster_cfg=ROSTER_CFG,
        entry_map=ROSTER_ENTRY_MAP,
    )
    assert len(captured) == 2, captured
    coder = next(c for c in captured if c["agent_type"] == "coder")
    assert coder["model"] != "claude-opus-4-7"
    warnings = result.get("model_roster_warnings")
    assert warnings, f"no roster warning surfaced in result: {result}"
    assert any("claude-opus-4-7" in w for w in warnings), warnings
