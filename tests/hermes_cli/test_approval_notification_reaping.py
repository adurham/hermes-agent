"""The approval-prompt desktop notification must not leak zombie processes.

``HermesCLI._fire_attention_signals`` spawns ``osascript`` for the macOS
banner.  It used to ``Popen`` it and drop the handle, so nothing ever called
``wait()``: each approval prompt left one ``<defunct>`` child behind for the
lifetime of the CLI.  These tests run the real spawn path against a fake
``osascript`` on ``PATH`` (no real AppleScript, no network) and check the
child's process state with ``ps``.
"""

import os
import shutil
import stat
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from tests.hermes_cli.test_approval_attention_signals import _make_cli

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("ps") is None,
    reason="needs POSIX zombies and ps",
)


def _install_fake_osascript(tmp_path, body: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "osascript"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _ps_state(pid: int) -> str:
    """'' when the pid is gone (reaped), else ps's STAT column (e.g. 'Z')."""
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return out.stdout.strip()


def _fire_with_fake(tmp_path, monkeypatch, body: str):
    """Fire the notification through the real Popen; return (procs, elapsed)."""
    bin_dir = _install_fake_osascript(tmp_path, body)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    cli = _make_cli({"bell_on_prompt": False, "notify_on_prompt": True})

    spawned = []  # strong refs: CPython's GC-time _cleanup must not mask a leak
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    with patch("sys.platform", "darwin"), patch("subprocess.Popen", recording_popen):
        start = time.monotonic()
        cli._fire_attention_signals("Approval needed: echo hi")
        elapsed = time.monotonic() - start
    assert len(spawned) == 1, "notification was not spawned"
    assert spawned[0].args[0] == "osascript"
    return spawned[0], elapsed


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_exited_notifier_is_reaped_not_left_defunct(tmp_path, monkeypatch):
    proc, _ = _fire_with_fake(tmp_path, monkeypatch, "exit 0")
    reaped = _wait_until(lambda: _ps_state(proc.pid) == "")
    assert reaped, (
        f"osascript child {proc.pid} never reaped; ps state={_ps_state(proc.pid)!r}"
    )
    assert proc.returncode == 0


def test_hanging_notifier_never_blocks_prompt_and_is_killed(tmp_path, monkeypatch):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_ATTENTION_NOTIFY_TIMEOUT_S", 0.3)
    proc, elapsed = _fire_with_fake(tmp_path, monkeypatch, "exec sleep 60")
    assert elapsed < 1.0, f"prompt path blocked {elapsed:.2f}s on the notifier"
    # Bounded wait expires -> killed and reaped, no lingering child or zombie.
    assert _wait_until(lambda: _ps_state(proc.pid) == ""), (
        f"hung notifier {proc.pid} not killed/reaped; ps state={_ps_state(proc.pid)!r}"
    )
    assert proc.returncode is not None and proc.returncode != 0
