"""Dispatch-level tests for the 2026-09-04 auto-routing visibility fixes.

Three interdependent changes, all observed through the SAME
``model_roster_warnings`` channel the roster guardrail already uses (so they
land in both the immediate tool-call response and the async completion
event):

1. **Silent omission is now visible.** A task stating neither ``model=`` nor
   ``agent_type=`` gets its model chosen for it — by the auto-route
   classifier, the blanket delegation default, or bare parent inheritance.
   All three used to be silent. Now whichever one fired is named in a
   warning. ``agent_type='auto'`` is an EXPLICIT opt-in to auto-routing and
   is exempt (routes identically, no warning).
2. **Explicit agent_type is BINDING (2026-10-13).** A task that DID state an
   ``agent_type`` may still be classified (same batch call) purely to compare
   tiers, but a deeper recommendation is now surfaced as an ADVISORY only —
   the stated agent_type, and therefore its role-map model, is NEVER replaced.
   (Before this change the classifier could silently escalate the role, which
   contradicted the documented precedence "explicit agent_type is intent".)
3. **Explicit model= is untouched.** It bypasses auto-route AND the
   escalate-check entirely — regression-guarded here.

Harness mirrors ``test_delegate_orchestrator_agent_type_gap.py`` /
``test_delegate_model_roster_guardrail.py``.
"""

import json
from unittest.mock import MagicMock, patch

import hermes_cli.personas as ruflo
import tools.async_delegation as ad
import tools.delegate_tool as dt


ROSTER_CFG = {
    "model": "claude-sonnet-5",
    "by_provider": {"anthropic": {"model": "claude-opus-5"}},
}
ROSTER_ENTRY_MAP = {
    # No provider pins: a pinned entry would trigger real credential
    # resolution, which this test deliberately doesn't mock (same choice as
    # the sibling guardrail tests).
    "researcher": {"model": "claude-haiku-4-5"},
    "coder": {"model": "claude-sonnet-4-6"},
    "system-architect": {"model": "claude-opus-4-7"},
    "pm": {"model": "claude-opus-5"},
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


def _dispatch(tasks, *, routes=None, depth=0, entry_map=None, **kwargs):
    """Dispatch a batch with the auto-route classifier's OUTPUT stubbed.

    ``routes`` is patched straight onto ``route_task_models`` (the router
    itself has its own unit tests); this exercises how delegate_task CONSUMES
    a routing decision. ``routes=None`` means the router returned nothing.
    """
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
    parent.model = "PARENT-MODEL"
    parent.provider = "anthropic"
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = depth

    import tools.delegation_router as dr

    _entries = dict(ROSTER_ENTRY_MAP if entry_map is None else entry_map)
    # delegate_tool loads BOTH maps: get_role_entry_map (raw entries, for the
    # per-role provider pin) and get_role_model_map (flattened role->model,
    # which is what role_map_model resolution actually reads). Patching only
    # the entry map leaves _role_model_map resolving from the sandboxed
    # (empty) config, so every role would silently fall through to the batch
    # default. Derive the flattened map the same way personas.py does.
    _models = {role: str(e["model"]) for role, e in _entries.items() if e.get("model")}

    with patch.object(
        dt, "_load_config", return_value=dict(ROSTER_CFG)
    ), patch.object(
        ruflo, "get_role_entry_map", return_value=_entries
    ), patch.object(
        ruflo, "get_role_model_map", return_value=_models
    ), patch.object(
        dt, "_resolve_delegation_credentials", return_value=dict(ROSTER_CREDS)
    ), patch.object(
        dt, "_build_child_preserving_parent_tools", side_effect=_fake_build
    ), patch.object(
        dr, "route_task_models", return_value=dict(routes or {})
    ) as route_mock, patch.object(
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

    return json.loads(result), captured, route_mock


def _warnings(result):
    return result.get("model_roster_warnings") or []


def _omission_warnings(result):
    return [w for w in _warnings(result) if "no agent_type= and no model=" in w]


def _escalation_warnings(result):
    return [w for w in _warnings(result) if "is BINDING" in w]


# ── Fix #1: silent omission is surfaced ───────────────────────────────────


def test_bare_task_surfaces_routing_decision_warning():
    """No model=, no agent_type= → a visible warning naming the decision."""
    result, captured, _ = _dispatch([{"goal": _G0}, {"goal": _G1}])
    assert len(captured) == 2, captured
    warns = _omission_warnings(result)
    assert len(warns) == 2, _warnings(result)
    assert all("agent_type='auto'" in w for w in warns), warns
    # Nothing routed this batch, so the blanket delegation default fired and
    # the warning must say so (not silently claim a classifier decision).
    assert any("delegation config default" in w for w in warns), warns
    assert any("claude-sonnet-5" in w for w in warns), warns


def test_bare_task_warning_names_the_auto_route_decision():
    """When the classifier DID route the task, the warning names its tier,
    role, model and reason — not a generic 'a model was picked'."""
    routes = {
        0: {
            "model": "claude-opus-4-7",
            "tier": "deep",
            "role": "system-architect",
            "reason": "touches auth",
        }
    }
    result, captured, _ = _dispatch([{"goal": _G0}, {"goal": _G1}], routes=routes)
    assert captured[0]["model"] == "claude-opus-4-7"
    warns = _omission_warnings(result)
    task0 = [w for w in warns if w.startswith("Task 0:")]
    assert len(task0) == 1, warns
    assert "auto-route classifier" in task0[0]
    assert "'deep'" in task0[0]
    assert "'system-architect'" in task0[0]
    assert "claude-opus-4-7" in task0[0]
    assert "touches auth" in task0[0]


def test_explicit_agent_type_does_not_trigger_omission_warning():
    """A stated agent_type is an explicit choice — no omission warning."""
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "coder"}, {"goal": _G1, "agent_type": "pm"}]
    )
    assert [c["agent_type"] for c in captured] == ["coder", "pm"]
    assert _omission_warnings(result) == [], _warnings(result)


def test_explicit_model_does_not_trigger_omission_warning():
    """A stated model is an explicit choice — no omission warning."""
    result, captured, _ = _dispatch(
        [
            {"goal": _G0, "model": "claude-opus-5"},
            {"goal": _G1, "model": "claude-opus-5"},
        ]
    )
    assert [c["model"] for c in captured] == ["claude-opus-5"] * 2
    assert _omission_warnings(result) == [], _warnings(result)


# ── Fix #1: agent_type="auto" is accepted and exempt ──────────────────────


def test_auto_agent_type_accepted_and_no_omission_warning():
    """agent_type='auto' is a valid explicit opt-in: it routes exactly like
    omission (eligible for auto-route) but emits NO omission warning."""
    routes = {
        0: {
            "model": "claude-opus-4-7",
            "tier": "deep",
            "role": "system-architect",
            "reason": "design work",
        }
    }
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "auto"}, {"goal": _G1, "agent_type": "auto"}],
        routes=routes,
    )
    assert len(captured) == 2, captured
    # Routed exactly like an omitted agent_type would be.
    assert captured[0]["model"] == "claude-opus-4-7"
    # ...and never treated as a persona named "auto".
    assert captured[0]["agent_type"] is None
    assert captured[1]["agent_type"] is None
    assert _omission_warnings(result) == [], _warnings(result)


def test_auto_agent_type_is_case_and_whitespace_tolerant():
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": " AUTO "}, {"goal": _G1, "agent_type": "Auto"}]
    )
    assert [c["agent_type"] for c in captured] == [None, None]
    assert _omission_warnings(result) == [], _warnings(result)


def test_auto_agent_type_reaches_the_router_unfiltered():
    """The tool must hand agent_type='auto' to the router verbatim so the
    router can put it in the FULL-route population (rather than the tool
    silently rewriting it to '' and losing the opt-in distinction)."""
    _result, _captured, route_mock = _dispatch(
        [{"goal": _G0, "agent_type": "auto"}, {"goal": _G1}]
    )
    task_list = route_mock.call_args.args[0]
    assert task_list[0].get("agent_type") == "auto"


def test_auto_agent_type_does_not_hit_the_role_model_map():
    """'auto' must never be looked up as a role: with a model_by_role entry
    literally named 'auto' present, an 'auto' task must NOT pick it up."""
    entry_map = {**ROSTER_ENTRY_MAP, "auto": {"model": "WRONG-MODEL-FROM-AUTO-ROLE"}}
    _result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "auto"}, {"goal": _G1}], entry_map=entry_map
    )
    assert captured[0]["model"] != "WRONG-MODEL-FROM-AUTO-ROLE"
    assert captured[0]["model"] == "claude-sonnet-5"  # batch default


# ── Fix #2: explicit agent_type is BINDING (advisory never overrides) ──────


def test_advisory_never_replaces_stated_agent_type_but_warns():
    """The router now returns an ADVISORY entry (no model/agent_type) for a
    deeper recommendation. The dispatched agent_type and its role-map model
    MUST survive verbatim, and the mismatch is surfaced as a warning."""
    routes = {
        0: {
            "advisory": True,
            "recommended_agent_type": "system-architect",
            "recommended_tier": "deep",
            "stated_agent_type": "researcher",
            "stated_rank": 0,
            "recommended_rank": 2,
            "reason": "auth surface",
        }
    }
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "researcher"}, {"goal": _G1, "agent_type": "coder"}],
        routes=routes,
    )
    kept = next(c for c in captured if c["task_index"] == 0)
    # BINDING: the stated role — and therefore ITS role-map model — is what runs.
    assert kept["agent_type"] == "researcher"
    assert kept["model"] == "claude-haiku-4-5"
    # The untouched sibling keeps its stated choice.
    other = next(c for c in captured if c["task_index"] == 1)
    assert other["agent_type"] == "coder"
    assert other["model"] == "claude-sonnet-4-6"

    warns = _escalation_warnings(result)
    assert len(warns) == 1, _warnings(result)
    assert "'researcher'" in warns[0]
    assert "'system-architect'" in warns[0]
    assert "0→2" in warns[0]
    assert "auth surface" in warns[0]
    assert "BINDING" in warns[0]
    # Must NOT claim a change happened.
    assert "Nothing was changed" in warns[0]


def test_no_advisory_entry_leaves_stated_choice_untouched():
    """No router entry (equal/lighter recommendation) → stated agent_type
    survives verbatim with no advisory warning."""
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "system-architect"}, {"goal": _G1}], routes={}
    )
    arch = next(c for c in captured if c["task_index"] == 0)
    assert arch["agent_type"] == "system-architect"
    assert arch["model"] == "claude-opus-4-7"
    assert _escalation_warnings(result) == []


def test_plain_route_entry_cannot_rewrite_stated_agent_type():
    """Even a full route entry (model + agent_type) must never rewrite a
    stated agent_type — only the explicit ``advisory`` marker is honoured, and
    even then only to warn."""
    routes = {
        0: {
            "model": "claude-opus-4-7",
            "tier": "deep",
            "role": "system-architect",
            "agent_type": "system-architect",
            "reason": "x",
        }
    }
    result, captured, _ = _dispatch(
        [{"goal": _G0, "agent_type": "researcher"}, {"goal": _G1}], routes=routes
    )
    arch = next(c for c in captured if c["task_index"] == 0)
    assert arch["agent_type"] == "researcher"
    assert arch["model"] == "claude-haiku-4-5"
    assert _escalation_warnings(result) == []


def test_every_role_explicit_agent_type_resolves_to_its_own_model(monkeypatch):
    """ACCEPTANCE REGRESSION: for EVERY configured role, passing that role as
    an explicit agent_type must resolve to that role's own model, with NO
    classifier side effect — even when the router (impossibly) returns a bogus
    advisory recommending a deeper role."""
    tasks = [{"goal": _G0, "agent_type": role} for role in ROSTER_ENTRY_MAP]
    routes = {
        i: {
            "advisory": True,
            "recommended_agent_type": "system-architect",
            "recommended_tier": "deep",
            "stated_agent_type": role,
            "stated_rank": 0,
            "recommended_rank": 2,
            "reason": "always deeper",
        }
        for i, role in enumerate(ROSTER_ENTRY_MAP)
    }
    _result, captured, _ = _dispatch(tasks, routes=routes)
    by_index = {c["task_index"]: c for c in captured}
    for i, role in enumerate(ROSTER_ENTRY_MAP):
        assert by_index[i]["agent_type"] == role, by_index[i]
        assert by_index[i]["model"] == ROSTER_ENTRY_MAP[role]["model"], by_index[i]


def test_pinned_role_never_inherits_the_parent_fallback_chain():
    """A ``pinned: true`` role is a HARD INVARIANT: it must not inherit the
    ambient parent fallback chain (which could silently reroute it). It gets
    an explicitly empty chain instead."""
    entry_map = {
        "exo-dsv41": {"model": "dsv41", "provider": "exo", "pinned": True},
    }
    from tools.delegate_tool import _resolve_task_routes

    with patch.object(dt, "_load_config", return_value=dict(ROSTER_CFG)), patch.object(
        ruflo, "get_role_entry_map", return_value=dict(entry_map)
    ), patch.object(
        ruflo, "get_role_model_map", return_value={"exo-dsv41": "dsv41"}
    ), patch.object(
        dt, "_resolve_delegation_credentials", return_value=dict(ROSTER_CREDS)
    ):
        routes, err = _resolve_task_routes(
            [{"goal": _G0, "agent_type": "exo-dsv41"}],
            dict(ROSTER_CREDS),
            cfg=dict(ROSTER_CFG),
            parent_agent=_pinned_parent(),
            top_role="leaf",
            roster_warnings=[],
        )
    assert err is None, err
    assert routes[0]["agent_type"] == "exo-dsv41"
    assert routes[0]["fallback_chain"] == [], routes[0]["fallback_chain"]


def _pinned_parent():
    parent = MagicMock()
    parent.model = "PARENT-MODEL"
    parent.provider = "anthropic"
    parent.api_key = "sk-test"
    parent._delegate_depth = 0
    return parent


# ── Fix #3: explicit model= bypasses everything (regression) ──────────────


def test_explicit_model_bypasses_autoroute_and_advisory():
    """Even if the router (impossibly) returned an advisory for a task
    carrying an explicit model=, the stated model must still win and nothing
    may be applied — 'a stated choice is intent'."""
    routes = {
        0: {
            "model": "claude-opus-4-7",
            "tier": "deep",
            "role": "system-architect",
            "agent_type": "system-architect",
            "reason": "x",
            "advisory": True,
            "recommended_agent_type": "system-architect",
            "recommended_tier": "deep",
            "stated_rank": 0,
            "recommended_rank": 2,
        }
    }
    result, captured, _ = _dispatch(
        [
            {"goal": _G0, "model": "claude-opus-5", "agent_type": "researcher"},
            {"goal": _G1, "model": "claude-opus-5"},
        ],
        routes=routes,
    )
    pinned = next(c for c in captured if c["task_index"] == 0)
    assert pinned["model"] == "claude-opus-5", (
        "explicit model= must win over any classifier-derived model"
    )
    bare_pinned = next(c for c in captured if c["task_index"] == 1)
    assert bare_pinned["model"] == "claude-opus-5"
    # No omission warning either — the caller stated a model.
    assert _omission_warnings(result) == [], _warnings(result)


def test_explicit_model_tasks_are_never_sent_to_the_classifier():
    """End-to-end with the REAL router: a task with an explicit model= must
    not reach _classify at all (proves the bypass is upstream of the LLM
    call, not just of the result application)."""
    import tools.delegation_router as dr

    tasks = [
        {"goal": _G0, "model": "claude-opus-5"},
        {"goal": _G1, "agent_type": "researcher"},
    ]
    with patch.object(dr, "_classify", return_value={}) as m:
        dr.route_task_models(
            tasks,
            {"researcher": "claude-haiku-4-5", "coder": "claude-sonnet-4-6",
             "system-architect": "claude-opus-4-7"},
            {},
            "anthropic",
        )
    (pending_arg,) = m.call_args.args
    assert [idx for idx, _ in pending_arg] == [1], pending_arg
