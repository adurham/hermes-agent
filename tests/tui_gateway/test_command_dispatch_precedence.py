"""The desktop's own slash handler must win over a plugin command of the same name.

``PluginContext.register_command`` lets a plugin own the name of a CLI-only built-in
(those never reach a gateway surface). ``command.dispatch`` had plugins ahead of its
own built-in map, which would have let such a plugin shadow this surface's handlers
(/focus, /snapshot, /queue, /undo, ...). Built-ins come first; a name the surface does
not handle still falls through to the plugin.

The dispatch function is registered from ``tui_gateway.methods_tools`` but runs with
``tui_gateway.server``'s globals (the server imports and re-homes it), so the stages are
patched through ``fn.__globals__`` rather than on the methods_tools module.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch


def _server():
    # Same import window as tests/tui_gateway/test_goal_command.py: keep process boot
    # out of the mocked import.
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        return importlib.import_module("tui_gateway.server")


def _stages(server, monkeypatch):
    globals_ = server._methods["command.dispatch"].__globals__
    builtins = globals_["_SLASH_BUILTINS"]
    return globals_, builtins


def test_a_plugin_command_does_not_shadow_a_tui_builtin(monkeypatch):
    server = _server()
    globals_, builtins = _stages(server, monkeypatch)
    order = []

    def builtin(rid, params, session, name, arg):
        order.append("builtin")
        return {"jsonrpc": "2.0", "id": rid, "result": {"type": "exec", "output": "builtin"}}

    def plugin(*args, **kwargs):
        order.append("plugin")
        return {"jsonrpc": "2.0", "id": 1, "result": {"type": "exec", "output": "plugin"}}

    monkeypatch.setitem(builtins, "focus", builtin)
    monkeypatch.setitem(globals_, "_dispatch_plugin", plugin)

    server._methods["command.dispatch"](1, {"name": "focus", "arg": "", "session_id": "missing"})

    assert order == ["builtin"]


def test_tui_builtin_map_matches_the_registration_constant():
    """``register_command`` refuses exactly the names this surface dispatches itself."""
    from hermes_cli.commands import TUI_DISPATCHED_BUILTINS

    server = _server()
    builtins = server._methods["command.dispatch"].__globals__["_SLASH_BUILTINS"]

    assert set(builtins) == TUI_DISPATCHED_BUILTINS


def test_a_plugin_command_still_runs_when_the_surface_has_no_builtin(monkeypatch):
    server = _server()
    globals_, builtins = _stages(server, monkeypatch)
    order = []

    def plugin(*args, **kwargs):
        order.append("plugin")
        return {"jsonrpc": "2.0", "id": 1, "result": {"type": "exec", "output": "plugin"}}

    assert "delegation" not in builtins or builtins["delegation"] is None
    monkeypatch.setitem(globals_, "_dispatch_plugin", plugin)

    result = server._methods["command.dispatch"](1, {"name": "delegation", "arg": "", "session_id": "missing"})

    assert order == ["plugin"]
    assert result["result"]["output"] == "plugin"
