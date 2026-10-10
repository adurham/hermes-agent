"""Auto-route failures must be VISIBLE, with a cause.

The router is deliberately fail-open, but a router that never ran used to be
indistinguishable from one that had nothing to do: everything landed on
"inherited the PARENT's own model/provider" (or the blanket config default)
with no indication of WHY. This covers:

  * ``route_task_models(..., diagnostics=)`` naming each skip reason;
  * the delegation result's warning carrying that cause to the caller;
  * a classifier FAILURE logging at WARNING once per process per cause
    (it used to be a DEBUG line, i.e. invisible in a normal run).

One test per reason, asserted through the real dispatch path where the
warning is what the model actually sees.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import hermes_cli.personas as ruflo
import pytest
import tools.async_delegation as ad
import tools.delegate_tool as dt
import tools.delegation_router as dr


ROLE_MAP = {"coder": "m-coder", "researcher": "m-researcher"}


def _classify_stub(error: str):
    """A _classify double that records a failure cause and returns {}.

    Mutates the CALLER's failure dict (never a copy): `kw.get("failure") or {}`
    would replace an empty-but-real dict with a throwaway, silently dropping
    the cause the code under test is supposed to report.
    """
    def _stub(pending, **kw):
        failure = kw.get("failure")
        if isinstance(failure, dict):
            failure["error"] = error
        return {}
    return _stub


def _route(tasks, *, cfg=None, provider="anthropic", role_map=None, diagnostics=None):
    return dr.route_task_models(
        tasks, ROLE_MAP if role_map is None else role_map,
        {} if cfg is None else cfg, provider, diagnostics,
    )


# ── One test per skip reason ──────────────────────────────────────────────


def test_reason_auto_route_disabled():
    diag = {}
    assert _route([{"goal": "x"}], cfg={"auto_route": {"enabled": False}}, diagnostics=diag) == {}
    assert diag["skipped"] == "auto_route disabled"


def test_reason_provider_gate_closed_names_provider_and_allowlist():
    diag = {}
    cfg = {"auto_route": {"enabled": True, "providers": ["anthropic", "ollama-cloud"]}}
    assert _route([{"goal": "x"}], cfg=cfg, provider="exo", diagnostics=diag) == {}
    assert diag["skipped"] == "provider gate closed"
    assert diag["provider"] == "exo"
    assert diag["allowed"] == ["anthropic", "ollama-cloud"]


def test_reason_no_tier_role_has_a_model():
    diag = {}
    cfg = {"auto_route": {"enabled": True, "providers": ["anthropic"]}}
    assert _route([{"goal": "x"}], cfg=cfg, role_map={}, diagnostics=diag) == {}
    assert diag["skipped"] == "no tier role has a model"


def test_reason_classifier_call_raised():
    diag = {}
    with patch.object(dr, "_classify", side_effect=RuntimeError("boom")):
        assert _route([{"goal": "x"}], diagnostics=diag) == {}
    assert diag["skipped"] == "auto-route failed"
    assert "RuntimeError" in diag["classifier_error"]


def test_reason_classifier_returned_nothing_after_an_exception():
    """The classifier swallowing its own call failure (timeout, client down)
    is the common real case: it returns {} rather than raising."""
    diag = {}
    with patch.object(dr, "_classify") as m:
        m.side_effect = _classify_stub("classifier call failed: TimeoutError: timed out")
        assert _route([{"goal": "x"}], diagnostics=diag) == {}
    assert diag["skipped"] == "classifier returned nothing"
    assert "TimeoutError" in diag["classifier_error"]


def test_reason_classifier_returned_unparsable_output():
    diag = {}
    with patch.object(dr, "_classify") as m:
        m.side_effect = _classify_stub("unparsable classifier reply: 'not json'")
        assert _route([{"goal": "x"}], diagnostics=diag) == {}
    assert diag["skipped"] == "classifier returned nothing"
    assert "unparsable" in diag["classifier_error"]


def test_reason_no_routable_task():
    diag = {}
    assert _route([{"goal": "x", "model": "pinned"}], diagnostics=diag) == {}
    assert diag["skipped"] == "no routable task"


def test_diagnostics_untouched_when_routing_succeeds():
    """A routed batch must not claim a skip reason."""
    diag = {}
    with patch.object(dr, "_classify", return_value={0: ("standard", "why", "")}):
        out = _route([{"goal": "x"}], cfg={"auto_route": {"enabled": True, "providers": ["anthropic"]}}, diagnostics=diag)
    assert out and out[0]["model"] == "m-coder"
    assert diag == {}


def test_diagnostics_optional():
    """Existing callers pass no diagnostics at all."""
    with patch.object(dr, "_classify", return_value={0: ("standard", "why", "")}):
        out = _route([{"goal": "x"}], cfg={"auto_route": {"enabled": True, "providers": ["anthropic"]}})
    assert out[0]["model"] == "m-coder"


# ── Classifier failure logging ────────────────────────────────────────────


def test_classifier_failure_warns_once_per_process_per_cause(caplog, monkeypatch):
    monkeypatch.setattr(dr, "_CLASSIFIER_FAILURE_WARNED", set())
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        side_effect=RuntimeError("no aux key"),
    ):
        with caplog.at_level(logging.WARNING, logger="tools.delegation_router"):
            dr._classify([(0, "goal")], timeout=1.0)
            dr._classify([(0, "goal")], timeout=1.0)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "classifier unavailable" in warnings[0].getMessage()


def test_distinct_causes_each_get_one_warning(caplog, monkeypatch):
    monkeypatch.setattr(dr, "_CLASSIFIER_FAILURE_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="tools.delegation_router"):
        dr._warn_classifier_failure("no auxiliary client available")
        dr._warn_classifier_failure("unparsable classifier reply: 'x'")
        dr._warn_classifier_failure("no auxiliary client available")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]


# ── The cause reaches the delegation result's warning ─────────────────────


def _dispatch(tasks, *, cfg, parent_provider="anthropic", diagnostics_setup=None):
    """Dispatch through the REAL delegate_task; the router's own behaviour is
    the thing under test (only the child build/spawn is stubbed)."""
    captured = []

    def _fake_build(**kw):
        captured.append({"task_index": kw.get("task_index"), "model": kw.get("model")})
        child = MagicMock()
        child.model = kw.get("model")
        return child

    parent = MagicMock()
    parent.model = "PARENT-MODEL"
    parent.provider = parent_provider
    parent.base_url = None
    parent.api_key = "sk-test"
    parent._delegate_depth = 0

    patches = [
        patch.object(dt, "_load_config", return_value=dict(cfg)),
        patch.object(ruflo, "get_role_entry_map", return_value=dict(ROLE_MAP_ENTRIES)),
        patch.object(ruflo, "get_role_model_map", return_value=dict(ROLE_MAP)),
        patch.object(dt, "_build_child_preserving_parent_tools", side_effect=_fake_build),
        patch.object(dt, "_run_single_child", return_value={
            "task_index": 0, "status": "completed", "summary": "ok", "api_calls": 1, "duration_seconds": 0.1,
        }),
        patch.object(dt, "_resolve_delegation_credentials", return_value=dict(CREDS)),
        patch.object(
            ad, "dispatch_async_delegation_batch",
            return_value={"status": "dispatched", "delegation_id": "d"},
        ),
    ]
    if diagnostics_setup is not None:
        diagnostics_setup(patches)

    for p in patches:
        p.start()
    try:
        result = json.loads(dt.delegate_task(
            tasks=tasks, parent_agent=parent, background=True,
        ))
    finally:
        for p in reversed(patches):
            p.stop()
    return result, captured


ROLE_MAP_ENTRIES = {"coder": {"model": "m-coder"}, "researcher": {"model": "m-researcher"}}
CREDS = {"model": "m-config-default", "provider": "anthropic", "base_url": None, "api_key": "sk-test", "api_mode": None,
         "command": None, "args": None}
_G0 = "first real task with enough length"


def _omission_warnings(result):
    return [w for w in (result.get("model_roster_warnings") or []) if "no agent_type= and no model=" in w]


def test_gate_closed_cause_reaches_the_warning():
    """The reported failure mode: a session whose provider is not in
    delegation.auto_route.providers gets a warning naming THAT, not a generic
    'a model was picked for you'."""
    cfg = {
        "model": "m-config-default", "provider": "anthropic",
        "auto_route": {"enabled": True, "providers": ["ollama-cloud"]},
    }
    result, _captured = _dispatch([{"goal": _G0}], cfg=cfg, parent_provider="claude-subscription-directsdk-experimental")

    warns = _omission_warnings(result)
    assert len(warns) == 1, warns
    assert "auto-route did not run" in warns[0], warns
    assert "provider gate closed" in warns[0], warns
    assert "claude-subscription-directsdk-experimental" in warns[0], warns
    assert "ollama-cloud" in warns[0], warns


def test_disabled_cause_reaches_the_warning():
    cfg = {"model": "m-config-default", "provider": "anthropic", "auto_route": {"enabled": False}}
    result, _captured = _dispatch([{"goal": _G0}], cfg=cfg)

    warns = _omission_warnings(result)
    assert len(warns) == 1, warns
    assert "delegation.auto_route.enabled is false" in warns[0], warns


def test_classifier_failure_cause_reaches_the_warning():
    cfg = {"model": "m-config-default", "provider": "anthropic", "auto_route": {"enabled": True, "providers": ["anthropic"]}}

    def _patch_classifier(patches):
        patches.append(patch.object(
            dr, "_classify",
            side_effect=_classify_stub("classifier call failed: TimeoutError: timed out"),
        ))

    result, _captured = _dispatch([{"goal": _G0}], cfg=cfg, diagnostics_setup=_patch_classifier)

    warns = _omission_warnings(result)
    assert len(warns) == 1, warns
    assert "auto-route did not run" in warns[0], warns
    assert "no usable verdicts" in warns[0], warns
    assert "TimeoutError" in warns[0], warns


def test_routed_batch_warning_does_not_claim_a_skip():
    """Positive control: when routing DID happen, the warning names the
    decision and carries no 'did not run' clause."""
    cfg = {"model": "m-config-default", "provider": "anthropic", "auto_route": {"enabled": True, "providers": ["anthropic"]}}

    def _patch_classifier(patches):
        patches.append(patch.object(dr, "_classify", return_value={0: ("standard", "bounded work", "")}))

    result, captured = _dispatch([{"goal": _G0}], cfg=cfg, diagnostics_setup=_patch_classifier)

    assert captured[0]["model"] == "m-coder"
    warns = _omission_warnings(result)
    assert len(warns) == 1, warns
    assert "auto-route classifier" in warns[0], warns
    assert "did not run" not in warns[0], warns
