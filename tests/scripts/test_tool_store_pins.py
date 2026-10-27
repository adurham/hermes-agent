"""run_tests.sh shares the account's tool store only when it can run from it as is.

A store whose facts match the checkout's pins but whose uv binary is missing or
not executable must not be shared: setup-hermes.sh would then re-stage uv into
the shared store with an unlocked ``rm -rf`` + publish that races other runs.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pm.defaults import default_packages
from pm.lock import Lockfile
from pm.package import StatePackage
from pm.registry import tool_roots, walk
from pm.store import current_target

ROOT = Path(__file__).resolve().parents[2]


def _matching_store(store: Path) -> Path:
    """A store whose facts record every walked tool at the lock's pins, each
    entry holding an executable binary. Returns setup-hermes.sh's uv path."""
    lock = Lockfile(ROOT / "pm" / "lock.json")
    names, target = lock.names(), current_target()
    setup_target = "-".join(target.split("-")[:2])  # setup-hermes.sh: "$os-$arch"
    packages = {}
    for package in walk(tool_roots(names) + default_packages(names) + ["venv"]):
        if isinstance(package, StatePackage):
            continue
        version = lock.version(package.name)
        entry_name = f"{package.name}-{version}-{setup_target if package.name == 'uv' else target}"
        entry = store / entry_name
        entry.mkdir(parents=True)
        binary = package.binary(entry, target)
        if binary is not None:
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
        packages[package.name] = {
            "entry": entry_name, "version": version, "target": target, "env": {},
            "artifacts": [a["sha256"] for a in lock.artifacts(package.name, target)],
        }
    (store / "facts.json").write_text(json.dumps({"schema": 1, "packages": packages}))
    return store / packages["uv"]["entry"] / "uv"


def _shares(store: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / "scripts/tool_store_pins.py"), str(ROOT), str(store)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result.returncode == 0


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("breakage", ["missing", "not_executable"])
def test_store_without_a_runnable_uv_is_not_shared(tmp_path, breakage):
    store = tmp_path / "tools"
    uv = _matching_store(store)
    assert _shares(store), "control: a complete store at this checkout's pins must be shared"
    if breakage == "missing":
        uv.unlink()
    else:
        uv.chmod(0o644)
    facts_before = (store / "facts.json").read_bytes()
    assert not _shares(store)
    assert uv.exists() == (breakage == "not_executable")
    assert breakage == "missing" or not uv.stat().st_mode & 0o111, "the check must stay read-only"
    assert (store / "facts.json").read_bytes() == facts_before
