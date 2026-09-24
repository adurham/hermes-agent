"""Select prepared test dependencies for subprocesses with isolated E2E homes."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from pm.build_operations import verified_tools
from pm.environments import install_key, site_packages
from pm.lock import Facts, Lockfile
from pm.paths import lockfile_path
from pm.store import current_target


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
    (state / "facts.json").write_text(json.dumps({"schema": 1, "packages": {"venv": {
        "environment": str(environment),
    }}}), encoding="utf-8")

    # The test venv is built from PM's pinned Python. Reuse ONLY its verified
    # tool artifacts; a venv selection alone leaves plugin activation trying
    # to download Python from the network inside this isolated home.
    source = Path(sys.base_prefix).resolve().parent
    if not (source / "facts.json").is_file():
        return  # A system/Nix test interpreter has no selected PM tool store.
    facts = Facts(source / "facts.json", strict=True)
    records = {name: facts.get(name) for name in ("uv", "python")}
    python = records["python"]
    if not python or (source / python["entry"]).resolve() != Path(sys.base_prefix).resolve():
        raise AssertionError(f"test interpreter is not from the selected PM store: {source}")
    verified_tools(("uv", "python"), source_store=source, target=current_target(),
                   lock=Lockfile(lockfile_path()))
    tools = hermes_home / "tools"
    tools.mkdir()
    for record in records.values():
        assert record is not None
        entry = record["entry"]
        (tools / entry).symlink_to(source / entry, target_is_directory=True)
    (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": records}), encoding="utf-8")
