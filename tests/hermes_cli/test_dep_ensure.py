"""ensure_dependency routes through pm: availability checks stay local,
installs go to pm.ensure, and pm's lazy-install policy owns refusal."""

from unittest.mock import patch

import pytest


def test_unknown_dep_refused():
    from hermes_cli.dep_ensure import ensure_dependency

    assert ensure_dependency("not-a-dep") is False

@pytest.mark.platforms("linux")
def test_find_install_script_from_checkout(tmp_path):
    """_find_install_script finds scripts/install.sh in a git checkout.

    ``platforms("linux")``: the POSIX arm picks ``install.sh`` + ``bash``, which is
    already what ``_IS_WINDOWS`` reports here — nothing needs faking.
    """
    from hermes_cli.dep_ensure import _find_install_script
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.sh").write_text("#!/bin/bash", encoding="utf-8")
    path, shell = _find_install_script(package_dir=tmp_path / "hermes_cli", repo_root=tmp_path)
    assert path is not None
    assert path.name == "install.sh"
    assert shell == "bash"


def test_available_dep_short_circuits(monkeypatch):
    from hermes_cli import dep_ensure

    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: True, ("node",))
    )
    called = []
    with patch("pm.ensure", side_effect=lambda *a, **k: called.append(a)):
        assert dep_ensure.ensure_dependency("node") is True
    assert called == []


def test_missing_dep_installs_through_pm(monkeypatch):
    from hermes_cli import dep_ensure

    state = {"installed": False}
    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: state["installed"], ("node",))
    )

    def fake_ensure(name, **kw):
        assert name == "node"
        state["installed"] = True

    with patch("pm.ensure", side_effect=fake_ensure):
        assert dep_ensure.ensure_dependency("node") is True


def test_pm_refusal_reports_and_returns_false(monkeypatch, capsys):
    from hermes_cli import dep_ensure

    monkeypatch.setitem(
        dep_ensure._DEPS, "node", (lambda: False, ("node",))
    )
    import pm as pm_mod

    def refuse(name, **kw):
        raise pm_mod.InstallError(name, "lazy installs are disabled", "run `hermes pm install`")

    with patch("pm.ensure", side_effect=refuse):
        assert dep_ensure.ensure_dependency("node", interactive=True) is False
    out = capsys.readouterr().out
    assert "hermes pm install" in out


def test_browser_check_consults_pm(monkeypatch):
    from hermes_cli import dep_ensure

    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    with patch("pm.is_installed", return_value=True):
        assert dep_ensure._browser_available() is True
    with patch("pm.is_installed", return_value=False):
        assert dep_ensure._browser_available() is False

@pytest.mark.platforms("windows")
def test_ensure_dependency_uses_powershell_on_windows(tmp_path):
    """``platforms("windows")``: the assertion is that we shell out to a real
    PowerShell. Faking ``_IS_WINDOWS`` on Linux also required faking
    ``shutil.which`` into inventing a powershell.exe that isn't there."""
    from hermes_cli.dep_ensure import ensure_dependency
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "install.ps1").write_text("# fake")
    with patch("hermes_cli.dep_ensure._DEP_CHECKS", {"node": lambda: False}), \
         patch("hermes_cli.dep_ensure._find_install_script", return_value=(scripts_dir / "install.ps1", "powershell")), \
         patch("hermes_cli.dep_ensure.shutil") as mock_shutil, \
         patch("hermes_constants.get_hermes_home", return_value=tmp_path / "fakehome"), \
         patch("subprocess.run") as mock_run, \
         patch("sys.stdin") as mock_stdin:
        mock_shutil.which.side_effect = lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" if name == "powershell" else None
        mock_stdin.isatty.return_value = False
        mock_run.return_value = type("R", (), {"returncode": 0})()
        ensure_dependency("node", interactive=False)
        cmd = mock_run.call_args[0][0]
        assert "powershell" in cmd[0].lower()
        assert "-Ensure" in cmd
        assert cmd[cmd.index("-Ensure") + 1] == "node"
        assert "-HermesHome" in cmd
        assert str(tmp_path / "fakehome") in cmd
