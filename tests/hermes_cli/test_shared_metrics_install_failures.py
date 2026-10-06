"""Why installs and updates fail: failure_class on hermes.extension.install.count and
hermes.update.run, registry on hub skill installs (closed sets, never error text)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.observability as observability
from hermes_cli.observability import shared_metrics_contract as contract
from hermes_cli.observability import shared_metrics_fields as fields
from hermes_cli.observability import shared_metrics_update as update_metrics

_SCHEMA = Path(observability.__file__).parent / "schemas/hermes.shared_metrics.v4.schema.json"


def _schema_dimensions(metric: str) -> list[dict]:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    (definition,) = (d for d in schema["$defs"].values() if d.get("properties", {}).get("name", {}).get("const") == metric)
    dims = definition["properties"]["dimensions"]
    return dims["oneOf"] if "oneOf" in dims else [dims]


@pytest.mark.parametrize("metric", [contract.EXTENSION_INSTALL_METRIC, contract.UPDATE_RUN_METRIC])
def test_schema_current_shape_is_exactly_the_contract_and_the_pre_split_shape_still_drains(metric):
    current, legacy = _schema_dimensions(metric)
    values = contract._COUNTER_DIMENSION_VALUES[metric]
    assert set(current["required"]) == set(values) == contract._METRIC_FIELDS[metric] - set(
        contract._IDENTIFIER_FIELDS.get(metric, ()))
    for field, spec in current["properties"].items():
        if "enum" in spec:
            assert set(spec["enum"]) == set(values[field]), field
    assert set(legacy["required"]) in {frozenset(s) for s in contract._LEGACY_METRIC_FIELDS[metric]}


def test_registry_ids_are_exactly_the_hub_router_adapters():
    from tools.skills_hub_search import create_source_router

    assert {src.source_id() for src in create_source_router()} == contract.EXTENSION_REGISTRY_IDS
    assert contract.EXTENSION_REGISTRIES == contract.EXTENSION_REGISTRY_IDS | {"none", "other", "unresolved"}
    assert contract.EXTENSION_FAILURE_CLASSES == frozenset().union(*contract.EXTENSION_KIND_FAILURE_CLASSES.values())


def test_a_raised_install_error_is_classified_by_type_and_never_carries_its_text():
    """Invariant: no free-form string reaches failure_class; an untagged exception keeps only its type's
    class, a user path in its message never leaves."""
    from hermes_cli.plugins_cmd import PluginOperationError

    leaky = "/home/alice/secret-repo: Permission denied"
    cases = [
        ("plugin", PluginOperationError(leaky, failure_class="clone_failed"), "clone_failed"),
        ("plugin", PluginOperationError(leaky), "other"),
        ("mcp_server", PermissionError(13, leaky), "permission"),
        ("mcp_server", ConnectionRefusedError(leaky), "network"),
        ("skill", FileNotFoundError(leaky), "filesystem_error"),
        ("skill", type("AcmeInternalError", (Exception,), {})(leaky), "exception"),
        ("skill", type("Tagged", (Exception,), {"failure_class": leaky})(), "other"),
    ]
    for kind, error, expected in cases:
        dims = fields.extension_install_fields(kind=kind, source="url", name=leaky, outcome="failed", error=error)
        assert dims["failure_class"] == expected, (kind, error)
        assert leaky not in json.dumps(dims)
        assert contract.counter_dimensions_are_valid(contract.EXTENSION_INSTALL_METRIC, dims)
    ok = fields.extension_install_fields(kind="skill", source="hub", name="x", outcome="success", registry="skills.sh")
    assert (ok["failure_class"], ok["registry"]) == ("none", "skills-sh")
    assert fields.extension_install_fields(kind="plugin", source="url", name=None, outcome="ok")["registry"] == "none"


def _receipt(outcome: str, stages: list[tuple[str, str]], **extra) -> dict:
    marks = [{"name": name, "outcome": result, "at": "2026-10-06T10:00:01+00:00",
              **({"mode": "git"} if name == "apply" else {})} for name, result in stages]
    return {"schema": 1, "update_id": "a" * 32, "started_at": "2026-10-06T10:00:00+00:00",
            "finished_at": "2026-10-06T10:00:02+00:00", "outcome": outcome, "pre_update": {}, "stages": marks,
            "steps": [], "fleet": [], **extra}


_ALL_PASSED = [("plan", "success"), ("snapshot", "success"), ("apply", "success"), ("deps", "success"),
               ("build", "success"), ("restart", "success")]


def test_a_failed_update_whose_every_stage_passed_names_where_and_why_it_failed():
    """Invariant: a failed git run never reads failed_stage=other. Both receipt shapes that did on main:
    the post-restart verification's ``partial`` with a clean fleet, and a run ending on a skipped
    restart that left the fleet owing one (update_completion._complete_selected exit 1)."""
    partial = _receipt("partial", _ALL_PASSED, fleet=[{"state": "current"}],
                       runtime_outcomes=[{"outcome": "unaccounted"}])
    skipped = _receipt("failed", [*_ALL_PASSED[:5], ("restart", "skipped")], exit_code=1,
                       stop_reason="completion exited 1")
    windows = _receipt("partial", _ALL_PASSED, fleet=[{"state": "current"}],
                       gateway_restart={"incomplete": True, "phase_error": "x"})
    stale = _receipt("partial", _ALL_PASSED, fleet=[{"state": "stale"}])
    got = {}
    for label, receipt in {"partial": partial, "skipped": skipped, "windows": windows, "stale": stale}.items():
        run, _ = update_metrics.update_receipt_fields(receipt)
        assert run["apply_mode"] == "git" and run["outcome"] == "failed"
        assert run["failed_stage"] != "other", label
        assert contract.counter_dimensions_are_valid(contract.UPDATE_RUN_METRIC, run)
        got[label] = (run["failed_stage"], run.get("failure_class"))
    assert got == {
        "partial": ("verify", "fleet_unverified"), "skipped": ("restart", "restart_failed"),
        "windows": ("verify", "restart_failed"), "stale": ("verify", "fleet_stale"),
    }


@pytest.mark.parametrize(("receipt", "expected"), [
    (_receipt("success", _ALL_PASSED), "none"),
    (_receipt("refused", [], steps=[{"name": "admission", "ok": False}], stop_reason="docker"), "managed_install"),
    (_receipt("refused", [("plan", "success"), ("snapshot", "success")], exit_code=2,
              stop_reason="historical takeover completion"), "lock_held"),
    (_receipt("failed", [("plan", "success"), ("snapshot", "success")], exit_code=1, stop_reason="sys.exit(1)"),
     "aborted_before_apply"),
    (_receipt("failed", [("plan", "success"), ("snapshot", "success")]), "git_failed"),
    (_receipt("failed", [("plan", "success")], exit_code=1,
              stop_reason="KeyboardInterrupt: /home/alice/x"), "interrupted"),
    (_receipt("failed", [("plan", "success")], exit_code=1, stop_reason="PermissionError: [Errno 13] /x"),
     "os_error"),
    (_receipt("failed", [("plan", "success")], exit_code=1, stop_reason="InstallError: venv: uv"), "deps_failed"),
    (_receipt("failed", [("plan", "success"), ("snapshot", "success"), ("apply", "success")], exit_code=1,
              stop_reason="completion exited 1"), "deps_failed"),
    (_receipt("failed", _ALL_PASSED[:4] + [("build", "failed"), ("restart", "success")], exit_code=1), "build_failed"),
    (_receipt("failed", [("plan", "success")], exit_code=1, stop_reason="AcmeError: secret"), "exception"),
])
def test_update_failure_class_reads_only_receipt_fields(receipt, expected):
    run, _ = update_metrics.update_receipt_fields(receipt)
    assert run["failure_class"] == expected
    assert contract.counter_dimensions_are_valid(contract.UPDATE_RUN_METRIC, run)
