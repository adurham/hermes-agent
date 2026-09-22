"""Version stamping writes a build tree, never the checkout it was invoked from.

A release used to commit the bumped version back onto the branch. The version
now lives in the ref, so stamping is a build step: it rewrites a copy of the
tree and returns the paths it touched, and the caller's tree is unchanged.
"""
import json
from pathlib import Path

import pytest


def _tree(root: Path) -> None:
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "__init__.py").write_text(
        '__version__ = "0.0.0"\n__release_date__ = "2026.1.1"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text('version = "0.0.0"\n', encoding="utf-8")
    desktop = root / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text('{"version": "0.0.0"}\n', encoding="utf-8")
    (root / "package-lock.json").write_text(
        '{"version": "0.0.0", "packages": {"apps/desktop": {"name": "hermes", "version": "0.0.0"}}}\n',
        encoding="utf-8")
    (root / "uv.lock").write_text(
        '[[package]]\nname = "hermes-agent"\nversion = "0.0.0"\n'
        '[[package]]\nname = "other"\nversion = "0.0.0"\n', encoding="utf-8")
    installer = root / "apps" / "bootstrap-installer" / "src-tauri"
    installer.mkdir(parents=True)
    (root / "apps" / "bootstrap-installer" / "package.json").write_text(
        '{"name": "x", "version": "0.0.0"}\n', encoding="utf-8")
    (installer / "tauri.conf.json").write_text(
        '{"productName": "Hermes", "version": "0.0.0"}\n', encoding="utf-8")
    (installer / "Cargo.toml").write_text('[package]\nversion = "0.0.0"\n', encoding="utf-8")
    (installer / "Cargo.lock").write_text(
        '[[package]]\nname = "bootstrap-installer"\nversion = "0.21.1"\n', encoding="utf-8")


def test_stamping_writes_the_build_tree_and_leaves_the_source_tree(tmp_path):
    from scripts.releases.stamping import stamp

    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    _tree(source)
    _tree(build)
    before = (source / "pyproject.toml").read_text(encoding="utf-8")

    written = stamp(build, "0.21.5", "2026.9.22")

    assert (source / "pyproject.toml").read_text(encoding="utf-8") == before
    assert 'version = "0.21.5"' in (build / "pyproject.toml").read_text(encoding="utf-8")
    init = (build / "hermes_cli" / "__init__.py").read_text(encoding="utf-8")
    assert '__version__ = "0.21.5"' in init
    assert '__release_date__ = "2026.9.22"' in init
    assert json.loads((build / "apps" / "desktop" / "package.json").read_text())["version"] == "0.21.5"
    # The lockfile root stays; only the desktop workspace entry moves.
    lock = json.loads((build / "package-lock.json").read_text())
    assert lock["version"] == "0.0.0"
    assert lock["packages"]["apps/desktop"]["version"] == "0.21.5"
    # uv.lock records the root package once, and no other package moves with it.
    uv = (build / "uv.lock").read_text(encoding="utf-8")
    assert uv.count('version = "0.21.5"') == 1
    assert 'name = "hermes-agent"\nversion = "0.21.5"' in uv
    cargo_lock = (build / "apps" / "bootstrap-installer" / "src-tauri" / "Cargo.lock").read_text()
    assert 'version = "0.21.5"' in cargo_lock
    assert all(path.is_relative_to(build) for path in written)
