"""The self-activating shebang prologue: when does it re-activate?

``scripts/_hermes-python`` is the POSIX shebang target. Its one interesting
decision is staleness: ``__HERMES_ACTIVATED`` holds the installed-state file the
environment was built against, so any of ``uv.lock`` / ``pyproject.toml`` /
``pm/lock.json`` being newer than that file means the inherited environment
predates its inputs.

These drive the real prologue with controlled mtimes. A ``python3`` shim is put
on PATH because the prologue execs ``python3`` by name — on some hosts (stock
Windows) that name does not exist, and the subject here is the staleness
branch, not interpreter resolution.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROLOGUE = REPO_ROOT / "scripts" / "_hermes-python"
DEMO = REPO_ROOT / "scripts" / "_activation_demo.py"
GUARD = REPO_ROOT / "scripts" / "_activation.py"

LONG_AGO = "2019-01-01 00:00:00"
STAMP_TIME = "2020-06-01 00:00:00"
JUST_AFTER = "2021-01-01 00:00:00"


def _posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def _bash() -> str:
    found = shutil.which("bash")
    if found and "windowsapps" not in str(found).lower():
        return found
    if sys.platform == "win32":
        for rel in (("Git", "bin", "bash.exe"), ("Git", "usr", "bin", "bash.exe")):
            cand = Path(os.environ.get("ProgramFiles", r"C:\Program Files")).joinpath(*rel)
            if cand.exists():
                return str(cand)
    return found or "bash"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repo-shaped tree with a stub activate that announces each sourcing."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "pm").mkdir()
    (root / "shim").mkdir()
    for src in (PROLOGUE, DEMO, GUARD):
        shutil.copy2(src, root / "scripts" / src.name)
    for name in ("uv.lock", "pyproject.toml"):
        (root / name).touch()
    (root / "pm" / "lock.json").touch()
    (root / "stamp").touch()

    shim = root / "shim" / "python3"
    # Quote in posix form: the shim is a /bin/sh script, where backslashes in
    # an unquoted word are escape characters (repo pattern: _fake_store).
    shim.write_text("#!/bin/sh\nexec '%s' \"$@\"\n" % _posix(Path(sys.executable)), encoding="utf-8")
    shim.chmod(0o755)

    # Stands in for the real activate: announces itself and exports the stamp
    # path, exactly as activation_environment() now supplies it.
    (root / "activate").write_text(
        "echo 'ACTIVATED' >&2\n"
        "export __HERMES_ACTIVATED=\"%s/stamp\"\n"
        "export PATH=\"%s/shim:$PATH\"\n" % (_posix(root), _posix(root)),
        encoding="utf-8",
    )
    return root


def _touch_at(path: Path, stamp: str) -> None:
    """Set a file's mtime without shelling out (the test runner scrubs PATH,
    so `touch` is not reliably available)."""
    when = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    os.utime(path, (when, when))


def run_prologue(root: Path, sentinel: str | None) -> tuple[int, str]:
    env = {**os.environ, "PATH": f"{_posix(root / 'shim')}{os.pathsep}{os.environ.get('PATH', '')}"}
    env.pop("__HERMES_ACTIVATED", None)
    if sentinel is not None:
        env["__HERMES_ACTIVATED"] = sentinel.replace("{root}", _posix(root))
    result = subprocess.run(
        [_bash(), _posix(PROLOGUE), _posix(root / "scripts" / DEMO.name)],
        capture_output=True, text=True, cwd=_posix(root), env=env, timeout=60,
    )
    return result.returncode, result.stderr


def test_no_sentinel_activates(checkout: Path):
    code, err = run_prologue(checkout, None)
    assert code == 0, err
    assert "ACTIVATED" in err


def test_current_environment_is_left_alone(checkout: Path):
    """Inputs older than the stamp: the inherited env is current, so the
    prologue must not pay for a re-sync on every run."""
    _touch_at(checkout / "stamp", STAMP_TIME)
    for name in ("uv.lock", "pyproject.toml", "pm/lock.json"):
        _touch_at(checkout / name, LONG_AGO)
    code, err = run_prologue(checkout, "{root}/stamp")
    assert code == 0, err
    assert "ACTIVATED" not in err


@pytest.mark.parametrize("input_name", ["uv.lock", "pyproject.toml", "pm/lock.json"])
def test_input_newer_than_stamp_reactivates(checkout: Path, input_name: str):
    _touch_at(checkout / "stamp", STAMP_TIME)
    for name in ("uv.lock", "pyproject.toml", "pm/lock.json"):
        _touch_at(checkout / name, LONG_AGO)
    _touch_at(checkout / input_name, JUST_AFTER)
    code, err = run_prologue(checkout, "{root}/stamp")
    assert code == 0, err
    assert "ACTIVATED" in err, f"{input_name} newer than the stamp must re-activate"


@pytest.mark.parametrize("sentinel", ["{root}/gone", "1"])
def test_unusable_sentinel_activates(checkout: Path, sentinel: str):
    """A missing stamp path forces activation; the literal ``1`` written by an
    older activate must keep working rather than silently reading as current."""
    _touch_at(checkout / "stamp", STAMP_TIME)
    code, err = run_prologue(checkout, sentinel)
    assert code == 0, err
    assert "ACTIVATED" in err