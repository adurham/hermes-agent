"""plugins.toolsets_opt_in: a plugin toolset is enabled for a platform only when its list names it.

By default every plugin toolset is OR-ed into a platform's selection unless it is default-off or the
user saved a list without it — so a plugin toolset an upstream release adds reaches sessions whatever
``platform_toolsets.<platform>`` says. The opt-in key makes the saved list authoritative for plugin
toolsets; MCP server toolsets are unaffected.
"""
import pytest


@pytest.fixture
def plugin_toolsets(monkeypatch):
    import hermes_cli.tools_config as tc
    import toolsets

    keys = {"probe_listed", "probe_unlisted"}
    monkeypatch.setattr(tc, "_get_plugin_toolset_keys", lambda: set(keys))
    original = toolsets.resolve_toolset

    def resolve(name, visited=None, *, include_registry=True):  # mirrors toolsets.resolve_toolset
        if name in keys:
            return [f"{name}_call"]
        return original(name, visited, include_registry=include_registry)

    monkeypatch.setattr(toolsets, "resolve_toolset", resolve)
    monkeypatch.setattr(tc, "resolve_toolset", resolve, raising=False)
    return tc


def test_default_keeps_unlisted_plugin_toolsets_on(plugin_toolsets):
    enabled = plugin_toolsets._get_platform_tools({"platform_toolsets": {"cli": ["terminal", "probe_listed"]}}, "cli")
    assert {"probe_listed", "probe_unlisted"} <= enabled


def test_opt_in_admits_only_the_listed_plugin_toolsets(plugin_toolsets):
    config = {"platform_toolsets": {"cli": ["terminal", "probe_listed"]}, "plugins": {"toolsets_opt_in": True}}
    enabled = plugin_toolsets._get_platform_tools(config, "cli")
    assert "probe_listed" in enabled and "terminal" in enabled
    assert "probe_unlisted" not in enabled


def test_opt_in_without_a_saved_list_admits_no_plugin_toolset(plugin_toolsets):
    enabled = plugin_toolsets._get_platform_tools({"plugins": {"toolsets_opt_in": "true"}}, "cli")
    assert not {"probe_listed", "probe_unlisted"} & enabled


def test_opt_in_leaves_mcp_server_toolsets_alone(plugin_toolsets):
    base = {"platform_toolsets": {"cli": ["terminal"]}, "mcp_servers": {"corp": {"command": "corp-mcp"}}}
    plain = plugin_toolsets._get_platform_tools(base, "cli")
    opted = plugin_toolsets._get_platform_tools({**base, "plugins": {"toolsets_opt_in": True}}, "cli")
    assert ("corp" in plain) == ("corp" in opted)
    assert plain - {"probe_listed", "probe_unlisted"} == opted
