"""Every provider-declared ``post_setup`` key must resolve to a registered hook.

A provider row (bundled or plugin) advertises ``post_setup: <key>`` so that picking it in ``hermes tools``
installs its optional dependency. ``valid_post_setup_keys()`` derives its allowlist from exactly those
declarations, so a declared key with no entry in ``_POST_SETUP_HOOKS`` passes validation and then silently
no-ops: ``_run_post_setup`` resolves through ``.get(key, lambda: None)``.

A key that survives a refactor of the hook table without its installation entry leaves the user with a
"Saved" provider whose package was never installed.

These are contracts between pieces of data, not snapshots: they hold for any provider set, so new no-key
providers get the same protection.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from hermes_cli.tools_config_post_setup import (
    _POST_SETUP_HOOKS, _PYTHON_POST_SETUP_HOOKS, valid_post_setup_keys,
)

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_every_declared_post_setup_key_has_a_hook():
    missing = sorted(valid_post_setup_keys() - set(_POST_SETUP_HOOKS))
    assert not missing, (
        f"provider(s) declare post_setup key(s) with no registered hook: {missing} - "
        "picking that provider would silently skip its dependency install. "
        "Add a _PYTHON_POST_SETUP_HOOKS entry (or a bespoke _POST_SETUP_HOOKS hook) for each key."
    )


def test_every_python_hook_installs_a_declared_pyproject_extra():
    """A python hook installs through ``pm.sync_venv([extra])``; an extra pyproject does not declare would
    make the install fail (or no-op) at the moment a user picks the provider."""
    declared = set(tomllib.loads(PYPROJECT.read_text(encoding="utf-8-sig"))["project"]["optional-dependencies"])
    undeclared = sorted({spec["extra"] for spec in _PYTHON_POST_SETUP_HOOKS.values()} - declared)
    assert not undeclared, f"python post_setup hooks name extras pyproject.toml does not declare: {undeclared}"

