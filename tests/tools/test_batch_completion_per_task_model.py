"""Regression tests: batch completion reporting must show PER-TASK models.

Bug (2026-08-25): the async batch completion header printed the batch-level
default model (``creds["model"]``, resolved from
``delegation.by_provider.<p>.model`` / ``delegation.model``) for the whole
fan-out. Per-task models are resolved later, per child — an explicit per-task
``model``, an ``agent_type`` role-map hit, or an auto-route decision all
produced children running on a DIFFERENT model than the header claimed.

Observed impact: a batch dispatched with ``model="claude-opus-5"`` on every
task reported ``Model: claude-sonnet-5``, so the operator believed the work
had silently run on the cheaper model. Routing was correct the whole time;
only the reporting lied — which is arguably worse, because it is unfalsifiable
from the output alone.

These assert the invariant (reported model reflects what children actually
ran on), not the exact header wording.
"""

from tools.process_registry import _format_async_delegation as _fmt


def _evt(results, model="claude-sonnet-5"):
    return {
        "delegation_id": "deleg_test",
        "is_batch": True,
        "role": "leaf",
        "model": model,
        "goals": [r.get("goal", f"g{i}") for i, r in enumerate(results)],
        "results": results,
        "total_duration_seconds": 10,
        "status": "completed",
    }


def _r(idx, model, status="completed"):
    return {
        "task_index": idx,
        "status": status,
        "summary": f"summary {idx}",
        "model": model,
        "goal": f"g{idx}",
    }


def test_uniform_per_task_model_overrides_batch_default():
    """All children on opus-5 => header says opus-5, NOT the sonnet default.

    This is the exact reported bug.
    """
    evt = _evt([_r(0, "claude-opus-5"), _r(1, "claude-opus-5")])
    out = _fmt(evt)
    assert "claude-opus-5" in out
    assert "Model: claude-sonnet-5" not in out


def test_mixed_models_are_disclosed_and_named_per_task():
    """A heterogeneous fan-out must not be collapsed to one misleading label."""
    evt = _evt([_r(0, "claude-opus-5"), _r(1, "claude-haiku-4-5")])
    out = _fmt(evt)
    # The batch default is retained as context, but flagged as varying.
    assert "varies" in out
    # And each task names its own model so the mix is auditable.
    assert "model=claude-opus-5" in out
    assert "model=claude-haiku-4-5" in out


def test_matching_model_reports_cleanly():
    """No per-task divergence => plain label, no noisy 'varies' annotation."""
    evt = _evt([_r(0, "claude-sonnet-5"), _r(1, "claude-sonnet-5")])
    out = _fmt(evt)
    assert "Model: claude-sonnet-5" in out
    assert "varies" not in out
    # Per-task model annotation is reserved for heterogeneous batches.
    assert "model=claude-sonnet-5" not in out


def test_missing_per_task_model_falls_back_to_batch_default():
    """Older/partial results without a model must not blank the header."""
    evt = _evt([{"task_index": 0, "status": "completed", "summary": "s"}])
    out = _fmt(evt)
    assert "Model: claude-sonnet-5" in out


def test_failed_child_model_still_reported():
    """A child that errored still ran on a real model; keep it visible."""
    evt = _evt([_r(0, "claude-opus-5", status="error")])
    out = _fmt(evt)
    assert "claude-opus-5" in out
