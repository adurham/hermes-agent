"""hermes.install.run: the installers' local receipt, counted by a later start only while collection is on."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.observability import relay_shared_metrics
from hermes_cli.observability import shared_metrics_contract as contract
from hermes_cli.observability import shared_metrics_install_run as install_run
from hermes_cli.observability import shared_metrics_process as process_metrics

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"
SCHEMA = ROOT / "hermes_cli" / "observability" / "schemas" / "hermes.shared_metrics.v4.schema.json"


@pytest.fixture
def marks(tmp_path, monkeypatch):
    captured: list[tuple[str, dict]] = []
    policy = {"on": True}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config_readonly",
        lambda: {"telemetry": {"shared_metrics": {"enabled": policy["on"]}}},
    )
    monkeypatch.setattr(relay_shared_metrics, "enabled", lambda: policy["on"])
    store = {"saves": True}

    def saved(rows):
        if store["saves"]:
            captured.extend(rows)
        return len(rows) if store["saves"] else 0

    monkeypatch.setattr(relay_shared_metrics, "record_process_marks_saved", saved, raising=False)
    yield SimpleNamespace(rows=captured, policy=policy, home=tmp_path / "home", store=store)


def _receipt(**overrides) -> dict:
    now = int(time.time())
    receipt = {"id": "ab" * 16, "installer": "install_sh", "outcome": "failed", "failed_stage": "repository",
               "failure_class": "git_clone_failed", "started_at": now - 200, "finished_at": now}
    return {**receipt, **overrides}


def _park(home: Path, receipt: dict, name: str | None = None) -> Path:
    directory = install_run.pending_installs_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name or receipt.get('id', 'x')}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_schema_and_installers_match_the_contract_exactly():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    by_name = {d["properties"]["name"]["const"]: d for d in schema["$defs"].values() if "properties" in d}
    dims = by_name[contract.INSTALL_RUN_METRIC]["properties"]["dimensions"]
    assert {field: set(spec["enum"]) for field, spec in dims["properties"].items()} == {
        field: set(values) for field, values in contract._COUNTER_DIMENSION_VALUES[contract.INSTALL_RUN_METRIC].items()}
    assert set(dims["required"]) == set(dims["properties"])
    assert {"$ref": "#/$defs/install_run_counter"} in schema["properties"]["metrics"]["items"]["oneOf"]

    sh, ps1 = INSTALL_SH.read_text(encoding="utf-8"), INSTALL_PS1.read_text(encoding="utf-8")
    # Every fail()/Fail call site passes a class from the contract, and every class is used somewhere.
    sh_sites = re.findall(r'(?<![-\w])fail "(?:[^"\\$]|\\.|\$\([^)]*\)|\$)*" ?([a-z_]*)', sh)
    ps1_sites = re.findall(r'(?<![-\w])Fail "(?:[^"`$]|`.|\$\([^)]*\)|\$)*" ?([a-z_$(]*)', ps1)
    assert sh_sites and ps1_sites
    assert "" not in sh_sites, "an install.sh fail() call site passes no failure class"
    assert "" not in ps1_sites, "an install.ps1 Fail call site passes no failure class"
    used = {c for c in sh_sites + ps1_sites if not c.startswith("$")} | {"setup_failed", "gateway_failed"}
    assert used | {"interrupted", "none", "other"} == contract.INSTALL_RUN_FAILURE_CLASSES
    stage_names = re.search(r"stage_names\(\) \{\n\s+printf '%s\\n' ([a-z -]+)\n", sh).group(1).split()
    assert {s.replace("-", "_") for s in stage_names} == contract.INSTALL_RUN_STAGES
    ps1_stages = re.findall(r'@\{ name = "([a-z-]+)"', ps1)
    assert {s.replace("-", "_") for s in ps1_stages} == contract.INSTALL_RUN_STAGES


def test_receipt_is_reported_once_out_of_contract_ones_dropped(marks):
    good = _park(marks.home, _receipt())
    success = _park(marks.home, _receipt(id="cd" * 16, installer="install_ps1", outcome="success",
                                         failed_stage="none", failure_class="none"))
    bad = [
        _park(marks.home, _receipt(id="01" * 16, failure_class="git clone of https://host/x failed")),
        _park(marks.home, _receipt(id="02" * 16, failed_stage="/home/someone")),
        _park(marks.home, {**_receipt(id="03" * 16), "reason": "free text"}),
        _park(marks.home, _receipt(id="04" * 16, outcome="success")),  # success naming a failed stage
    ]
    marks.store["saves"] = False  # a busy store keeps every in-contract receipt for the next start
    install_run.report_pending_installs(marks.home)
    assert good.exists() and success.exists() and not any(p.exists() for p in bad)
    assert marks.rows == []

    marks.store["saves"] = True
    install_run.report_pending_installs(marks.home)
    _park(marks.home, _receipt())  # the same receipt left behind by a failed delete
    install_run.report_pending_installs(marks.home)
    assert sorted(marks.rows, key=lambda r: r[1]["outcome"]) == [
        (contract.INSTALL_RUN_MARK, {"duration_bucket": "2m_to_5m", "failed_stage": "repository",
                                     "failure_class": "git_clone_failed", "installer": "install_sh", "outcome": "failed"}),
        (contract.INSTALL_RUN_MARK, {"duration_bucket": "2m_to_5m", "failed_stage": "none",
                                     "failure_class": "none", "installer": "install_ps1", "outcome": "success"}),
    ]
    assert list(install_run.pending_installs_dir(marks.home).iterdir()) == []
    for mark, data in marks.rows:
        assert contract.counter_dimensions_are_valid(contract._DECISION_MARK_METRICS[mark], data)


def test_start_with_collection_off_purges_receipts_unreported(marks, monkeypatch):
    receipt = _park(marks.home, _receipt())
    monkeypatch.setattr(process_metrics, "_STATE", {})
    marks.policy["on"] = False
    process_metrics.begin_process("cli")
    assert not receipt.parent.exists()
    marks.policy["on"] = True
    install_run.report_pending_installs(marks.home)
    assert marks.rows == []


@pytest.mark.skipif(sys.platform == "win32", reason="runs the real install.sh")
def test_real_install_sh_ladder_leaves_only_closed_tokens(tmp_path):
    """The real ladder with git missing from PATH fails in prerequisites and parks one closed receipt."""
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("bash", "uname", "date", "od", "tr", "mkdir", "mv", "curl", "id", "cat", "dirname"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    home = tmp_path / "hermes home"
    result = subprocess.run(
        [str(tools / "bash"), str(INSTALL_SH), "--non-interactive", "--hermes-home", str(home),
         "--dir", str(tmp_path / "checkout")],
        env={"HOME": str(tmp_path), "PATH": str(tools), "HERMES_REPO_URL": "https://example.invalid/x.git"},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "git is required" in result.stderr
    receipts = list(install_run.pending_installs_dir(home).glob("*.json"))
    assert len(receipts) == 1
    text = receipts[0].read_text(encoding="utf-8")
    assert "example.invalid" not in text and str(tmp_path) not in text and "required" not in text
    receipt = json.loads(text)
    assert install_run.install_run_fields(receipt) == {
        "duration_bucket": "lt_30s", "failed_stage": "prerequisites", "failure_class": "git_missing",
        "installer": "install_sh", "outcome": "failed",
    }
