"""PM repair replays the selected dependency graph without broken app imports."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pm.lock import Facts
from pm.packages import uv_env
from tests.pm.test_workspace_build_inputs import _wheel


@pytest.mark.parametrize("failure", [None, "missing_lock", "corrupt_facts", "empty_environment", "missing_extras", "validation", "publication"])
def test_repair_restores_recorded_plugin_dependencies_without_config(tmp_path, monkeypatch, failure):
    import pm.paths as paths
    import pm.workspace as workspace
    from hermes_cli.runtime_paths import selected_venv, site_packages

    engine = importlib.import_module("pm.ensure")
    uv = shutil.which("uv")
    assert uv, "recovery integration requires real uv"
    core = tmp_path / "core"
    core.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "core_dep", "1.0")
    _wheel(wheels, "plugin_dep", "1.0")
    (core / "pyproject.toml").write_text(
        '[project]\nname="repair-core"\nversion="1"\nrequires-python=">=3.11"\n'
        'dependencies=["core-dep==1.0"]\n[project.optional-dependencies]\nall=[]\n'
        '[tool.uv]\npackage=false\nno-index=true\n'
        f'find-links=[{json.dumps(wheels.as_posix())}]\n', encoding="utf-8",
    )
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "pyproject.toml").write_text(
        '[project]\nname="repair-plugin"\nversion="1"\nrequires-python=">=3.11"\n'
        'dependencies=["plugin-dep==1.0"]\n[tool.uv]\npackage=false\n', encoding="utf-8",
    )
    monkeypatch.setattr(paths, "repo_root", lambda: core)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(engine, "uv", lambda **kwargs: (
        uv, {**uv_env(kwargs.get("base_env")), "UV_PYTHON": sys.executable, "UV_OFFLINE": "1"},
    ))
    monkeypatch.setattr(engine, "lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(workspace, "enabled_member_dirs", lambda: [plugin])
    # The committed core lock is independent of the plugin union.
    env = {**uv_env(), "UV_PYTHON": sys.executable, "UV_OFFLINE": "1"}
    env.pop("UV_NO_CONFIG")
    subprocess.run([uv, "lock"], cwd=core, env=env, capture_output=True, check=True, timeout=60)
    engine.sync_venv([], explicit=True)
    old = selected_venv(core)
    old_fact = Facts(paths.runtime_facts_path()).get("venv")
    old_lock = Path(old_fact["resolved_lock"]).read_bytes()
    config = tmp_path / "home" / "config.yaml"
    config.write_bytes(b"plugins:\n  enabled: [repair-plugin]\n")
    config_before = config.read_bytes()
    shutil.rmtree(site_packages(old) / "core_dep")
    shutil.rmtree(site_packages(old) / "plugin_dep")

    def broken_config(*args, **kwargs):
        raise AssertionError("repair must use the recorded graph, not parse config")
    monkeypatch.setattr(engine, "lazy_installs_allowed", broken_config)
    monkeypatch.setattr(workspace, "enabled_member_dirs", broken_config)
    if failure:
        from pm import recovery
        from pm.package import InstallError

        if failure == "corrupt_facts":
            paths.runtime_facts_path().write_text("invalid recorded state", encoding="utf-8")
        elif failure in {"empty_environment", "missing_extras"}:
            data = json.loads(paths.runtime_facts_path().read_text(encoding="utf-8"))
            recorded = data["packages"]["venv"]
            if failure == "empty_environment":
                recorded["environment"] = ""
            else:
                recorded.pop("extras")
            paths.runtime_facts_path().write_text(json.dumps(data), encoding="utf-8")
        old_facts = paths.runtime_facts_path().read_bytes()
        def fail(*args, **kwargs):
            raise InstallError("venv", "injected validation or publication failure")
        if failure == "missing_lock":
            Path(old_fact["resolved_lock"]).unlink()
        elif failure == "validation":
            monkeypatch.setattr(recovery, "validate_environment", fail)
        elif failure == "publication":
            monkeypatch.setattr(Facts, "record_state", fail)
        with pytest.raises((InstallError, ValueError)):
            engine.sync_venv(repair=True)
        assert paths.runtime_facts_path().read_bytes() == old_facts
        if failure not in {"corrupt_facts", "empty_environment"}:
            assert selected_venv(core) == old
        assert config.read_bytes() == config_before
        assert old.is_dir()
        return

    engine.sync_venv(repair=True)
    restored = selected_venv(core)
    assert restored != old
    python = restored / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [str(python), "-I", "-c", "import core_dep, plugin_dep; print(core_dep.__version__, plugin_dep.__version__)"],
        cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30,
    )
    assert result.stdout.strip() == "1.0 1.0"
    new_fact = Facts(paths.runtime_facts_path()).get("venv")
    assert new_fact["stamp"] == old_fact["stamp"]
    assert Path(new_fact["resolved_lock"]).read_bytes() == old_lock
    assert old.is_dir() and not (site_packages(old) / "plugin_dep").exists()
    assert config.read_bytes() == config_before
