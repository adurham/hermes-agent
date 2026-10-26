"""The Windows hand-off relays the stages `hermes update` publishes, instead of one static line.

The real ``windows.ps1`` runs a fixture ``hermes`` whose update publishes a stage through the real
``hermes_cli.update_stage.publish_stage`` and then waits to be released. On main the hand-off never
told the child where to publish, so the window (and the hand-off log) read "Updating code and
dependencies" for the whole 20-60 minute run (#139952, #135363).
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

from tests.installation_launcher_fixture import publish_fixture_launcher
from tests.scripts.desktop_update.windows_handoff_support import _log

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = ROOT / "scripts/desktop-update/windows.ps1"
STAGE_CLI = """
import os, sys, time
from pathlib import Path
def main():
    if '--version' in sys.argv:
        print('Install directory: ' + str(Path(__file__).resolve().parents[1])); return 0
    if '--help' in sys.argv:
        print('--keep-stash'); return 0
    if sys.argv[1:2] == ['update']:
        sys.path.insert(0, os.environ['HANDOFF_REPO'])
        # The fixture install ships its own hermes_cli package: load the tree's.
        for name in [m for m in sys.modules if m == 'hermes_cli' or m.startswith('hermes_cli.')]:
            del sys.modules[name]
        from hermes_cli.update_stage import publish_stage
        publish_stage('Packaging the desktop app')
        hold = Path(os.environ['HANDOFF_HOLD'])
        deadline = time.monotonic() + 60
        while not hold.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    return 0
if __name__ == '__main__':
    sys.exit(main())
"""


@pytest.mark.platforms("windows")
def test_handoff_shows_the_stage_the_update_publishes(tmp_path: Path) -> None:
    install = tmp_path / "checkout"
    publish_fixture_launcher(install, STAGE_CLI)
    home = tmp_path / "home"
    home.mkdir()
    hold = tmp_path / "release-update"
    script = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-InstallRoot", str(install), "-NoUi"],
        cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(tmp_path / "empty-store"),
             "HANDOFF_HOLD": str(hold), "HANDOFF_REPO": str(ROOT)},
    )
    try:
        deadline = time.monotonic() + 90
        while "stage: Packaging the desktop app" not in _log(home):
            assert time.monotonic() < deadline and script.poll() is None, (
                "the hand-off never relayed the update's own stage:\n" + _log(home)[-3000:])
            time.sleep(0.2)
    finally:
        hold.write_text("", encoding="utf-8")
        try:
            script.wait(timeout=120)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(script.pid)], capture_output=True, check=False)
            script.wait(timeout=30)
