"""``MANAGED_SCOPE_CAPABILITIES``: a literal tuple a launcher can read without importing hermes."""

from __future__ import annotations

import ast
from pathlib import Path

from hermes_cli import managed_scope

# Every managed behaviour a deployment launcher may depend on. Names are append-only.
EXPECTED = {
    "strict-mode",
    "layered-managed-dirs",
    "save-config-keeps-user-values",
    "raw-config-readers-honor-overlay",
    "blocked-features-bare-and-platform-names",
    "plugins-disabled-globs",
    "plugins-user-dir-allowlist",
    "plugins-toolsets-opt-in",
}


def _literal_from_source() -> object:
    """Read the declaration the way a launcher would: parse the source, never import it."""
    source = Path(managed_scope.__file__).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MANAGED_SCOPE_CAPABILITIES" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("MANAGED_SCOPE_CAPABILITIES is not a module-level assignment")


def test_declaration_is_a_literal_tuple_of_unique_nonempty_strings():
    value = _literal_from_source()
    assert isinstance(value, tuple)
    assert all(isinstance(name, str) and name for name in value)
    assert len(set(value)) == len(value)


def test_imported_value_matches_the_source_literal():
    assert managed_scope.MANAGED_SCOPE_CAPABILITIES == _literal_from_source()


def test_declares_every_behaviour_the_launcher_depends_on():
    assert EXPECTED <= set(managed_scope.MANAGED_SCOPE_CAPABILITIES)


def test_declared_entry_points_exist():
    # A declared name must not outlive the code behind it; spot-check the ones with a public seam here.
    assert callable(managed_scope.strict_mode)
    assert callable(managed_scope.get_managed_dirs)
    assert callable(managed_scope.apply_managed_overlay)
