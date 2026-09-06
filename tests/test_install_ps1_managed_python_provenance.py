"""Installer delegates dependency selection to the pinned PM bootstrap."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.platforms("windows")
INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


@pytest.mark.parametrize("exit_code", [0, 17])
def test_python_stage_delegates_to_pin_without_touching_existing_env(tmp_path, exit_code):
    powershell = shutil.which("powershell")
    assert powershell
    install = tmp_path / "install"
    (install / "pm").mkdir(parents=True)
    (install / "pm" / "lock.json").write_text(json.dumps({"packages": {"python": {"version": "3.12.8+fixture"}}}))
    existing = install / "venv" / "sentinel"
    existing.parent.mkdir()
    existing.write_text("previous generation")
    log = tmp_path / "uv-call.json"
    wrapper = tmp_path / "boundary.ps1"
    wrapper.write_text(r'''
param([string]$Installer, [string]$InstallDir, [string]$HomeDir, [string]$Log)
$ErrorActionPreference = 'Stop'
function uv {
    ConvertTo-Json -Compress -InputObject @($args) | Set-Content -Encoding UTF8 $Log
    $global:LASTEXITCODE = [int]$env:PROBE_EXIT
}
function Get-Command {
    param([string]$Name)
    if ($Name -eq 'uv') { return [pscustomobject]@{ Source = 'uv' } }
    Microsoft.PowerShell.Core\Get-Command $Name
}
function Invoke-WebRequest { throw 'network access outside test boundary' }
. $Installer -Stage python-deps -Json -InstallDir $InstallDir -HermesHome $HomeDir
exit $LASTEXITCODE
''', encoding="utf-8-sig")
    run = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
                          "-Installer", str(INSTALLER), "-InstallDir", str(install), "-HomeDir", str(tmp_path / "home"), "-Log", str(log)],
                         cwd=tmp_path, env=dict(os.environ, PROBE_EXIT=str(exit_code)), stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=120)
    assert (run.returncode == 0) == (exit_code == 0), run.stdout + run.stderr
    assert json.loads(log.read_text(encoding="utf-8-sig")) == ["run", "--no-project", "--python", "3.12", "python", "-m", "pm.cli", "install"]
    assert existing.read_text() == "previous generation"
