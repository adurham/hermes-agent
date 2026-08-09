"""Regression tests for #78050 — A2A outbound client tools must register in
CLI/TUI sessions without eagerly importing the inbound platform adapter.

The a2a plugin is a bundled ``kind: platform`` plugin, which by design has its
module import deferred until something calls
``gateway.platform_registry.all_entries()`` / ``plugin_entries()``. CLI/TUI
processes never trigger that resolution, so before this fix the client tools
(a2a_call, a2a_discover, a2a_list, a2a_history, a2a_orchestrate) never
registered and ``resolve_toolset('a2a')`` silently returned ``[]``.

The fix teaches the deferred-platform registration path to honor an optional
``client_tools_module`` field in ``plugin.yaml`` so plugins that want their
lightweight client tools available everywhere can opt in, without giving up
the deferred-import benefit for the heavy adapter.
"""

from __future__ import annotations

import sys


# Module name the plugin loader assigns to the a2a plugin package. Derived from
# the registry key ``a2a-platform`` (see PluginManager slug rules) — NOT the
# on-disk ``plugins.platforms.a2a`` path.
A2A_SLUG_MOD = "hermes_plugins.a2a_platform"


def _reload_plugin_state():
    """Fresh discovery from a clean slate so each test sees a deterministic view.

    Any lingering a2a modules from a prior test would short-circuit the eager
    submodule import; the platform_registry singleton likewise remembers
    deferred loaders across tests.
    """
    for mod in list(sys.modules):
        if mod == A2A_SLUG_MOD or mod.startswith(A2A_SLUG_MOD + "."):
            del sys.modules[mod]
        elif mod.startswith("plugins.platforms.a2a"):
            del sys.modules[mod]

    from gateway import platform_registry as pr_mod
    pr_mod.platform_registry._entries.clear()
    pr_mod.platform_registry._deferred.clear()

    from tools.registry import registry
    with registry._lock:
        for name in [
            "a2a_call", "a2a_discover", "a2a_list",
            "a2a_history", "a2a_orchestrate",
        ]:
            registry._tools.pop(name, None)

    from hermes_cli import plugins as plugins_mod
    plugins_mod._plugin_manager = None
    plugins_mod.discover_plugins(force=True)
    return plugins_mod.get_plugin_manager()


class TestA2AClientToolsInCliTui:
    """Simulate the CLI/TUI path: discovery runs, but nothing forces
    ``platform_registry`` to resolve deferred entries."""

    def test_client_tools_registered_after_discovery(self):
        _reload_plugin_state()
        from tools.registry import registry
        for name in [
            "a2a_call", "a2a_discover", "a2a_list",
            "a2a_history", "a2a_orchestrate",
        ]:
            assert registry.get_entry(name) is not None, (
                f"{name} missing after plain plugin discovery (CLI/TUI path)"
            )
            assert registry.get_entry(name).toolset == "a2a"

    def test_resolve_toolset_a2a_non_empty_in_cli_tui(self):
        _reload_plugin_state()
        from toolsets import resolve_toolset
        tools = resolve_toolset("a2a")
        assert set(tools) >= {
            "a2a_call", "a2a_discover", "a2a_list",
            "a2a_history", "a2a_orchestrate",
        }

    def test_platform_toolset_shows_in_effective_configurable(self):
        _reload_plugin_state()
        from hermes_cli.tools_config import _get_effective_configurable_toolsets
        keys = {k for k, _, _ in _get_effective_configurable_toolsets()}
        assert "a2a" in keys

    def test_adapter_module_not_imported_by_discovery(self):
        _reload_plugin_state()
        # The heavy inbound adapter must stay deferred — importing it here
        # would defeat the whole point of the deferred-platform mechanism
        # (~20 platform SDK imports on every ``hermes chat`` startup).
        # NB: ``A2A_SLUG_MOD`` itself IS expected in sys.modules — the loader
        # deliberately creates a bare namespace shell for it so the client-tools
        # submodule's relative imports resolve. What must NOT happen is the
        # shell getting replaced by an executed __init__.py (asserted in
        # test_only_the_declared_client_tools_submodule_is_imported).
        assert f"{A2A_SLUG_MOD}.adapter" not in sys.modules
        assert not any(
            m.endswith(".adapter") and "a2a" in m
            for m in sys.modules
        )

    def test_only_the_declared_client_tools_submodule_is_imported(self):
        """Eager import must be surgical: the declared submodule (plus whatever
        IT imports) and nothing else — in particular NOT the plugin's own
        ``__init__.py``, which is what carries the heavy adapter import."""
        _reload_plugin_state()
        assert f"{A2A_SLUG_MOD}.tools" in sys.modules
        # The package entry itself must be a bare namespace shell we created,
        # never the executed __init__.py (which defines ``register``).
        shell = sys.modules[A2A_SLUG_MOD]
        assert not hasattr(shell, "register"), (
            "plugin __init__.py was executed — deferral defeated"
        )

    def test_other_deferred_platforms_not_eagerly_loaded(self):
        """Sanity: fixing a2a must not accidentally eager-load other bundled
        platform plugins that DON'T declare ``client_tools_module``.

        Derives the expected module names from the live manifests rather than
        hardcoding a prefix, so the assertion can't silently go vacuous if the
        slug scheme changes (an earlier version of this test matched
        ``hermes_plugins.platforms__*``, which matches nothing — the real slug
        is ``<name>_platform``).
        """
        mgr = _reload_plugin_state()
        others = {
            k: f"hermes_plugins.{k.replace('/', '__').replace('-', '_')}"
            for k, l in mgr._plugins.items()
            if l.manifest.kind == "platform"
            and not l.manifest.client_tools_module
        }
        assert len(others) >= 5, (
            f"expected several deferred platform plugins, got {others!r}"
        )
        offenders = {k: m for k, m in others.items() if m in sys.modules}
        assert offenders == {}, f"unexpected eager platform loads: {offenders}"

    def test_a2a_is_the_only_platform_plugin_declaring_client_tools(self):
        """Guard the blast radius: if another platform plugin opts in later,
        this test should fail so its lightweight-submodule contract gets a
        deliberate review (see #78050)."""
        mgr = _reload_plugin_state()
        optin = sorted(
            k for k, l in mgr._plugins.items()
            if l.manifest.kind == "platform" and l.manifest.client_tools_module
        )
        assert optin == ["a2a-platform"], (
            f"new client_tools_module opt-in(s) need review: {optin}"
        )


class TestNoDoubleRegistrationOnFullResolve:
    """If the platform_registry later resolves the a2a deferred loader (e.g. in
    a gateway process), the plugin's ``register()`` runs and would normally
    re-register the same client tools. Verify the sentinel prevents the double
    registration and its cross-toolset shadow warning."""

    def test_full_resolve_after_eager_client_tools_is_idempotent(self, caplog):
        _reload_plugin_state()
        from gateway.platform_registry import platform_registry
        from tools.registry import registry

        # Snapshot tool count for the a2a toolset before full resolve
        before = sum(
            1 for e in registry._tools.values() if e.toolset == "a2a"
        )
        # Force the deferred adapter loader to fire (gateway/setup path)
        platform_registry._resolve_all()
        after = sum(
            1 for e in registry._tools.values() if e.toolset == "a2a"
        )
        assert before == after == 5

        # And no "REJECTED" registration errors from the shadow guard.
        assert not any(
            "REJECTED" in rec.getMessage() and "a2a_" in rec.getMessage()
            for rec in caplog.records
        )
