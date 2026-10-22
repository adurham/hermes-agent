"""A uv re-pin carries the installers' bootstrap pins with it.

The curl|sh and irm|iex installers bootstrap uv (and Windows git) before any
checkout exists, so they carry those pins as fragments generated from
pm/lock.json. `pm update` and `pm lock --bump` rewrote only the lock: a uv bump
left the installers on the old uv until someone ran scripts/gen-bootstrap-pins.py
by hand, and the bootstrap-installer drift check failed.
"""

import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from pm import cli
from pm.lock import Lockfile
from pm.store import ALL_TARGETS
from pm.update import Resolved

REPO = Path(__file__).resolve().parents[2]
VERSION = "99.0.0"


def _update(lock):
    targets = [t for t in ALL_TARGETS if t != "linux-arm64-bionic"]
    decision = Resolved("uv", lock.version("uv"), "semver", version=VERSION,
                        per_target=dict.fromkeys(targets, VERSION))
    return cli._apply_pins([decision], lock)


def _bump(lock):
    return cli.cmd_lock(Namespace(name="uv", version=VERSION))


@pytest.mark.parametrize("repin", [_update, _bump], ids=["update", "bump"])
def test_uv_repin_regenerates_installer_pins(repin, tmp_path, monkeypatch):
    for rel in ("scripts/gen-bootstrap-pins.py", "scripts/install.sh", "scripts/install.ps1"):
        (tmp_path / rel).parent.mkdir(exist_ok=True)
        shutil.copy2(REPO / rel, tmp_path / rel)
    shutil.copytree(REPO / "pm", tmp_path / "pm", ignore=shutil.ignore_patterns("__pycache__"))
    lock = Lockfile(tmp_path / "pm" / "lock.json")
    monkeypatch.setattr(cli, "_lockfile", lambda: lock)
    monkeypatch.setattr(cli, "hash_url", lambda url: "0" * 64)
    monkeypatch.setattr(cli, "_install_names", lambda names: 0)
    monkeypatch.setattr(cli, "_sync_venv_step", lambda: True)

    assert repin(lock) == 0
    check = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "gen-bootstrap-pins.py"), "--check"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert check.returncode == 0, check.stderr
