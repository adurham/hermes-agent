"""Stamp a build tree with a release version. The checkout is never the target.

The regex shapes are the canonical ones ``scripts/release.py`` already uses to
keep every mirror in lockstep. This writer differs in one respect: it takes the
tree as an argument, so a build stamps a copy and the source tree stays at the
placeholder version.
"""
from __future__ import annotations

import re
from pathlib import Path


def _rewrite(path: Path, pattern: str, replacement: str, *, count: int = 0, flags: int = 0) -> None:
    if not path.exists():
        return
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    path.write_bytes(re.sub(pattern, replacement, text, count=count, flags=flags).replace("\n", newline).encode("utf-8"))


def stamp(tree: Path, version: str, release_date: str) -> list[Path]:
    """Rewrite every version mirror under ``tree``. Returns the paths written."""
    written: list[Path] = []

    def touch(path: Path) -> None:
        if path.exists():
            written.append(path)

    init = tree / "hermes_cli" / "__init__.py"
    _rewrite(init, r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"')
    _rewrite(init, r'__release_date__\s*=\s*"[^"]+"', f'__release_date__ = "{release_date}"')
    touch(init)

    pyproject = tree / "pyproject.toml"
    _rewrite(pyproject, r'^version\s*=\s*"[^"]+"', f'version = "{version}"', count=1, flags=re.MULTILINE)
    touch(pyproject)

    desktop = tree / "apps" / "desktop" / "package.json"
    _rewrite(desktop, r'("version"\s*:\s*)"[^"]+"', rf'\g<1>"{version}"', count=1)
    touch(desktop)

    # The lockfile root version is the repo's own and stays put; only the
    # desktop workspace entry mirrors the release.
    lock = tree / "package-lock.json"
    _rewrite(lock, r'("apps/desktop"\s*:\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"version"\s*:\s*)"[^"]+"',
             rf'\g<1>"{version}"', count=1)
    touch(lock)

    uv_lock = tree / "uv.lock"
    _rewrite(uv_lock, r'(name = "hermes-agent"\nversion = )"[^"]+"', rf'\g<1>"{version}"', count=1)
    touch(uv_lock)

    installer = tree / "apps" / "bootstrap-installer"
    json_version = rf'\g<1>"{version}"'
    toml_version = f'version = "{version}"'
    for name, pattern, replacement, flags in (
        ("package.json", r'("version"\s*:\s*)"[^"]+"', json_version, 0),
        ("src-tauri/tauri.conf.json", r'("version"\s*:\s*)"[^"]+"', json_version, 0),
        ("src-tauri/Cargo.toml", r'^version\s*=\s*"[^"]+"', toml_version, re.MULTILINE),
        ("src-tauri/Cargo.lock", r'(name = "bootstrap-installer"\nversion = )"[^"]+"', json_version, 0),
    ):
        path = installer / name
        _rewrite(path, pattern, replacement, count=1, flags=flags)
        touch(path)

    return written
