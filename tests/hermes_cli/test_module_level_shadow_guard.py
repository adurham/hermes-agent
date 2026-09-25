"""Guard: a merge must not re-add a pre-refactor definition ON TOP of a re-export import.

Upstream keeps splitting the monoliths (``cli.py``, ``hermes_cli/gateway.py``) into
topical siblings behind facade re-export imports, e.g.::

    from hermes_cli.gateway_launchd import (  # noqa: E402,F401 — facade re-exports
        launchd_install,
        ...
    )

When a merge re-introduces the OLD in-file body BELOW that import, the ``def``
rebinds the same name and silently wins at import time: upstream's newer code
never runs, and the failure surfaces somewhere unrelated (a plist that lost its
``osascript`` wrapper, a missing ``start_now=`` kwarg, a stale ``_show_usage``
shadowing a fix). No test that merely imports the module can see it.

This guard is pure AST (no app imports) and asserts that the set of
module-level "shadow" names — names imported at module level AND defined in the
same file — never GROWS. Existing shadows are recorded in ``_KNOWN_SHADOWS``
and the test fails if a name is added, while allowing the known list to shrink
as the remaining copies are de-forked (delete entries from the set when you
fix them; never add one).

The 2026-09-24 sweep deleted 49 such shadows from ``hermes_cli/gateway.py``
(commit ``5d6dde7704``); the ``cli.py`` module-level clusters are still open
scope, hence the allowlist rather than a zero-tolerance assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules whose module-level names must never be shadowed again.
_SCANNED = (
    "cli.py",
    "hermes_cli/gateway.py",
)

# Known, still-open shadows: file -> names. This list may only SHRINK.
_KNOWN_SHADOWS: dict[str, frozenset[str]] = {
    "cli.py": frozenset(),
    "hermes_cli/gateway.py": frozenset(),
}


def _module_level_shadows(path: Path) -> set[str]:
    """Names imported at module level that the same module also re-defines."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()

    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])

    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

    return imported & defined


def test_no_new_module_level_shadows() -> None:
    """A shadow that is not on the known list means a merge re-added a def."""
    offenders: list[str] = []
    for rel in _SCANNED:
        found = _module_level_shadows(_REPO_ROOT / rel)
        known = _KNOWN_SHADOWS.get(rel, frozenset())
        new = found - set(known)
        if new:
            offenders.append(f"{rel}: {sorted(new)}")

    assert not offenders, (
        "Module-level shadow(s) appeared — a definition now rebinds a name that "
        "the same module imports from a topical sibling, so the sibling's code "
        "never runs (see this module's docstring). Delete the in-file def and "
        "port any fork-only lines into the sibling, or fix the merge. "
        + "; ".join(offenders)
    )


def test_known_shadow_list_has_no_stale_entries() -> None:
    """Keep the allowlist honest: drop entries once the copy is deleted."""
    stale: list[str] = []
    for rel, known in _KNOWN_SHADOWS.items():
        found = _module_level_shadows(_REPO_ROOT / rel)
        gone = set(known) - found
        if gone:
            stale.append(f"{rel}: {sorted(gone)}")

    assert not stale, (
        "These names are on the known-shadow allowlist but no longer shadow — "
        "delete them from _KNOWN_SHADOWS so the list keeps shrinking: "
        + "; ".join(stale)
    )
