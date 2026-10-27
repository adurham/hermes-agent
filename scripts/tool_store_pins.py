"""Exit 0 only when a tool store already holds this checkout's pins.

Usage: python -I -B scripts/tool_store_pins.py REPO STORE

scripts/run_tests.sh asks this before sharing the account's tool store with a
faked HOME. facts.json selects ONE entry per package, not one per version:
provisioning a checkout with other pins re-points the account's own runtime at
them and pulls the selection from under a concurrent run on the store's pins.
So share only when every tool `pm install` would provision here (cmd_install's
roots, defaults and the venv's deps) is already recorded at this checkout's
pins AND runnable as is: a recorded entry whose binary is missing or not
executable sends setup-hermes.sh (uv) or `pm install` back into a re-stage
whose unlocked remove + publish races other runs on the shared store. Unlike
PM's own lookup this never heals an exec bit. Read-only: no lock, no install,
no bytecode; any unreadable record or error means "do not share".
"""
import os
import sys
from pathlib import Path


def _runnable(binary: Path) -> bool:
    return binary.is_file() and (os.name == "nt" or os.access(binary, os.X_OK))


def _setup_uv(store: Path, target: str, version: str) -> Path:
    """setup-hermes.sh's exact uv path: $store/uv-$uv_version-$os-$arch/uv[.exe]."""
    os_arch = "-".join(target.split("-")[:2])
    return store / f"uv-{version}-{os_arch}" / ("uv.exe" if os_arch.startswith("win32") else "uv")


def main(repo: str, store: Path) -> int:
    sys.path.insert(0, repo)
    from pm.defaults import default_packages
    from pm.lock import Facts, Lockfile
    from pm.package import StatePackage
    from pm.registry import tool_roots, walk
    from pm.store import current_target

    lock = Lockfile(Path(repo, "pm", "lock.json"))
    facts = Facts(store / "facts.json", strict=True)
    names, target = lock.names(), current_target()
    for package in walk(tool_roots(names) + default_packages(names) + ["venv"]):
        if isinstance(package, StatePackage):
            continue
        shas = tuple(a["sha256"] for a in lock.artifacts(package.name, target))
        if not facts.installed(package.name, lock.version(package.name), store, (target, shas) if shas else None):
            return 1
        binary = package.binary(store / facts.get(package.name)["entry"], target)
        if binary is not None and not _runnable(binary):
            return 1
    return 0 if _runnable(_setup_uv(store, target, lock.version("uv"))) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2])))
