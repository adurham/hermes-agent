"""The terminal's PATH: the user's own tools first, Hermes's after them.

Hermes's process puts its store's tool dirs first (``pm.install.activate``), so Hermes's own
code resolves the pinned binaries. Terminal commands belong to the user and the agent, so they
run the tools the user installed, and Hermes's tools are the fallback for a machine without
them. Hermes's own operations must not depend on this PATH: they resolve pinned tools
through PM (``pm.env_for``, ``pm.installed_package``).
"""
from __future__ import annotations

import os
from pathlib import Path


def _python_stub_dir(directory: str) -> bool:
    """True for the Windows app-alias dir when its ``python.exe`` is only the Store stub.

    The stub opens the Microsoft Store instead of running Python. A real Store Python puts its
    aliases in the same dir, and it also ships a ``python3.N.exe``, which the stub does not.
    """
    parts = [os.path.normcase(part) for part in Path(directory).parts[-2:]]
    if parts != [os.path.normcase("Microsoft"), os.path.normcase("WindowsApps")]:
        return False
    try:
        return not any(Path(directory).glob("python3.*.exe"))
    except OSError:
        return True


def _python_dirs() -> set[str]:
    from pm.install import env_for

    path = env_for("python", base_env={"PATH": ""}).get("PATH", "")
    return {os.path.normcase(d) for d in path.split(os.pathsep) if d}


def user_facing_path(path: str) -> str:
    """*path* with Hermes's store tool dirs moved behind the user's own entries.

    Hermes's Python stops just before the app-alias dir that holds the Store stub, so a
    machine without Python still runs a real one.
    """
    from pm.install import _store_path_dirs

    ours = _store_path_dirs()
    if not ours:
        return path
    keys = {os.path.normcase(d) for d in ours}
    kept = [p for p in path.split(os.pathsep) if p and os.path.normcase(p) not in keys]
    python = _python_dirs()
    cut = next((i for i, p in enumerate(kept) if _python_stub_dir(p)), len(kept))
    early = [d for d in ours if os.path.normcase(d) in python]
    late = [d for d in ours if os.path.normcase(d) not in python]
    return os.pathsep.join(dict.fromkeys([*kept[:cut], *early, *kept[cut:], *late]))
