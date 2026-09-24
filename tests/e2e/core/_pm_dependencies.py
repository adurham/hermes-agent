"""Select prepared test dependencies for subprocesses with isolated E2E homes."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from pm.environments import install_key, site_packages


def select_test_dependencies(hermes_home: Path, checkout: Path) -> None:
    """Point sandbox PM facts at the real test venv instead of bootstrapping a second one."""
    test_venv = Path(sys.prefix)
    if not (test_venv / "pyvenv.cfg").is_file():
        return  # Nix/system Python has no prepared venv to select.
    selected = site_packages(test_venv)
    assert selected.is_dir(), f"test interpreter lacks site-packages: {test_venv}"
    state = hermes_home / "installs" / install_key(checkout)
    environment = state / "environments" / "e2e-test" / "venv"
    environment.mkdir(parents=True)
    shutil.copyfile(test_venv / "pyvenv.cfg", environment / "pyvenv.cfg")
    target = environment / selected.relative_to(test_venv)
    target.parent.mkdir(parents=True)
    target.symlink_to(selected, target_is_directory=True)
    (state / "facts.json").write_text(json.dumps({"packages": {"venv": {
        "environment": str(environment),
    }}}), encoding="utf-8")
