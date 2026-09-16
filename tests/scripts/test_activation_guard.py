"""The guard repo scripts call to require an activated shell.

``scripts/_activation.py`` is stdlib-only and importable before the environment
it checks for exists; these tests pin its contract: it passes under activation,
and it exits naming the exact command for the caller's shell otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts._activation import ACTIVATION_ENV_VAR, activation_command, require_activation

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_require_activation_passes_when_the_sentinel_is_set(monkeypatch):
    monkeypatch.setenv(ACTIVATION_ENV_VAR, "1")
    require_activation()  # must return, not raise


def test_require_activation_exits_naming_the_activation_command(monkeypatch, capsys):
    monkeypatch.delenv(ACTIVATION_ENV_VAR, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        require_activation()
    assert excinfo.value.code not in (0, None)
    assert activation_command() in capsys.readouterr().err


@pytest.mark.platforms("posix")
def test_activation_command_is_the_posix_source_line():
    assert activation_command() == "source ./activate"


@pytest.mark.platforms("windows")
def test_activation_command_is_powershell_on_a_native_windows_shell(monkeypatch):
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.delenv("SHELL", raising=False)
    assert activation_command() == ". .\\activate.ps1"


def test_a_script_refuses_to_run_without_activation(tmp_path):
    """End-to-end: a real script importing the guard stops before its body."""
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\n"
        "from _activation import require_activation\n"
        "require_activation()\n"
        "print('ran')\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key != ACTIVATION_ENV_VAR}
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "ran" not in result.stdout
    assert activation_command() in result.stderr