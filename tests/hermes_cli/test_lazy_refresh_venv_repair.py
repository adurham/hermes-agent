"""Tests for lazy-backend refresh venv repair (#57828 / #58004)."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_cli.main as m
import hermes_cli.main_install_repair as hermes_cli_main_install_repair
from hermes_cli import main_install_repair
from hermes_cli import update_cmd
import pytest






def test_detect_returns_none_when_probe_subprocess_fails(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        m, "_resolve_install_target_python", lambda *a, **k: python
    )
    monkeypatch.setattr(
        hermes_cli_main_install_repair, "_resolve_install_target_python", lambda *a, **k: python
    )
    monkeypatch.setattr(
        m.subprocess,
        "run",
        MagicMock(side_effect=OSError("exec failed")),
    )
    assert main_install_repair._detect_broken_lazy_refresh_imports(["uv", "pip"]) is None




def test_repair_runs_force_reinstall_with_pyproject_pins(
    tmp_path, monkeypatch
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent(
            """\
            [project]
            name = "fake"
            version = "0.0.0"
            dependencies = [
              "pyyaml==6.0.3",
              "click==8.2.1",
            ]
        """
        )
    )
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)

    calls: list[list[str]] = []

    def fake_install(cmd, **kwargs):
        calls.append(cmd)

    detect_calls = {"count": 0}

    def fake_detect(prefix, *, env=None):
        detect_calls["count"] += 1
        return []

    monkeypatch.setattr(main_install_repair, "_run_package_only_install", fake_install)
    monkeypatch.setattr(main_install_repair, "_detect_broken_lazy_refresh_imports", fake_detect)

    ok = main_install_repair._repair_broken_lazy_refresh_imports(
        ["uv", "pip"],
        ["PyYAML", "click"],
        env={"VIRTUAL_ENV": str(tmp_path)},
    )
    assert ok is True
    assert calls == [
        [
            "uv",
            "pip",
            "install",
            "--force-reinstall",
            "pyyaml==6.0.3",
            "click==8.2.1",
        ]
    ]
    assert detect_calls["count"] == 1


def test_refresh_resyncs_enabled_extras_via_pm(monkeypatch, capsys):
    """The post-rebuild refresh is one explicit pm sync of the enabled extras."""
    import importlib

    pm_ensure = importlib.import_module("pm.ensure")

    sync_calls: list[tuple[list[str] | None, bool]] = []
    monkeypatch.setattr(
        pm_ensure, "sync_venv", lambda extras=None, *, explicit=False: sync_calls.append((extras, explicit)))

    ok = m._refresh_active_lazy_features(["platform.telegram"])

    assert ok is True
    assert sync_calls == [(["platform.telegram"], True)]
    assert "Extra re-sync failed" not in capsys.readouterr().out


def test_refresh_reports_failure_and_stays_unsafe(monkeypatch, capsys):
    """A failed re-sync must not read as a healthy venv."""
    import importlib

    pm_ensure = importlib.import_module("pm.ensure")

    def broken_sync(extras=None, *, explicit=False):
        raise RuntimeError("uv sync failed")

    monkeypatch.setattr(pm_ensure, "sync_venv", broken_sync)

    assert m._refresh_active_lazy_features(["platform.telegram"]) is False
    assert "Extra re-sync failed" in capsys.readouterr().out


def test_refresh_uses_pre_rebuild_snapshot_when_provided(monkeypatch):
    """Replacement runtimes must not re-detect enabled extras after packages vanish."""
    import importlib

    pm_ensure = importlib.import_module("pm.ensure")

    monkeypatch.setattr(
        pm_ensure, "enabled_extras",
        lambda: pytest.fail("post-rebuild detection must not run"),
    )
    synced = []
    monkeypatch.setattr(
        pm_ensure, "sync_venv",
        lambda extras=None, *, explicit=False: synced.append((extras, explicit)),
    )

    assert m._refresh_active_lazy_features(["platform.telegram"]) is True
    assert synced == [(["platform.telegram"], True)]


def test_capture_active_tool_dependencies_uses_tools_status_probes(monkeypatch):
    from hermes_cli import tools_config_post_setup

    monkeypatch.setattr(
        tools_config_post_setup,
        "_module_installed",
        lambda module: module in {"langfuse", "ddgs"},
    )

    assert m._capture_active_tool_dependencies() == ["ddgs", "langfuse"]


def test_restore_active_tool_dependencies_uses_static_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        m,
        "_run_install_with_heartbeat",
        lambda cmd, *, env=None: calls.append((cmd, env)),
    )
    monkeypatch.setattr(
        hermes_cli_main_install_repair,
        "_run_package_only_install",
        lambda cmd, *, env=None: calls.append((cmd, env)),
    )

    env = {"VIRTUAL_ENV": "/tmp/venv"}
    m._restore_active_tool_dependencies(
        ["langfuse", "not-allowlisted"],
        ["uv", "pip"],
        env=env,
    )

    assert calls == [(["uv", "pip", "install", "langfuse", "--quiet"], env)]


def test_cmd_update_captures_and_propagates_pre_rebuild_snapshot(
    tmp_path, monkeypatch
):
    """The updater must carry pre-rebuild state into its repair refresh."""
    from hermes_cli import update_cmd

    (tmp_path / ".git").mkdir()
    snapshot = ["platform.telegram"]
    tool_snapshot = ["langfuse"]
    refresh_calls = []
    restore_calls = []

    class RestoreReached(Exception):
        pass

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_sync(extras=None, *, explicit=False):
        refresh_calls.append((sorted(extras or []), explicit))

    def fake_restore(dependencies, prefix, *, env=None):
        restore_calls.append((dependencies, prefix, env))
        raise RestoreReached

    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(m, "_capture_active_lazy_features", lambda: snapshot.copy())
    monkeypatch.setattr(
        m, "_capture_active_tool_dependencies", lambda: tool_snapshot.copy()
    )
    monkeypatch.setattr(m, "_is_windows", lambda: False)
    monkeypatch.setattr(hermes_cli_main_install_repair, "_is_windows", lambda: False)
    monkeypatch.setattr(m, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(m, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(m, "_resume_windows_gateways_after_update", lambda state: None)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *args: None)
    monkeypatch.setattr(m, "_get_origin_url", lambda *args: "https://github.com/NousResearch/hermes-agent.git")
    monkeypatch.setattr(m, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(m, "_stash_local_changes_if_needed", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (False, "broken")
    )
    monkeypatch.setattr(update_cmd, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        m, "_install_python_dependencies_with_optional_fallback", lambda *a, **k: None
    )
    monkeypatch.setattr(m, "_restore_active_tool_dependencies", fake_restore)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    import pm
    from pm.packages import uv_env as _uv_env

    def fake_uv(**kw):
        env = _uv_env()
        if kw.get("venv"):
            env["VIRTUAL_ENV"] = str(kw["venv"])
        return "uv", env

    monkeypatch.setattr(pm, "uv", fake_uv)
    monkeypatch.setattr(pm, "sync_venv", fake_sync)

    args = SimpleNamespace(
        yes=True,
        force=False,
        force_venv=False,
        no_backup=True,
        backup=False,
        branch=None,
    )
    with pytest.raises(RestoreReached):
        update_cmd._cmd_update_impl(args, gateway_mode=False)

    # The repair phase is one explicit pm sync carrying the pre-rebuild
    # extras snapshot; tool-dep restore still runs against the managed env
    # (#83914: UV vars stripped, VIRTUAL_ENV pointed at the install's venv).
    from pm.packages import uv_env as managed_python_env

    expected_env = managed_python_env()
    expected_env["VIRTUAL_ENV"] = str(tmp_path / "venv")
    assert refresh_calls == [(sorted(["all", *snapshot]), True)]
    assert restore_calls == [
        (
            tool_snapshot,
            ["uv", "pip"],
            expected_env,
        )
    ]










