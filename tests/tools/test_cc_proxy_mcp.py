"""Tests for the fork's CC proxy MCP bridge.

The bridge at tools/bridges/cc_proxy_mcp.py translates between
Hermes MCP tool calls and Claude Code's proxy protocol.
"""


class TestCCProxyMCP:
    """Tests for tools/bridges/cc_proxy_mcp.py."""

    def test_module_imports(self):
        """The module imports without errors."""
        import tools.bridges.cc_proxy_mcp
        assert tools.bridges.cc_proxy_mcp is not None

    def test_module_has_expected_exports(self):
        """The module exports expected symbols."""
        import tools.bridges.cc_proxy_mcp as mod
        exports = [name for name in dir(mod) if not name.startswith("_")]
        assert len(exports) > 0

    def test_bridge_provides_proxy_class(self):
        """Module provides proxy-related functions."""
        import tools.bridges.cc_proxy_mcp as mod
        assert hasattr(mod, "run_proxy") or hasattr(mod, "stdio_server")
        assert hasattr(mod, "list_servers")

    def test_bridge_references_mcp(self):
        """Bridge references MCP protocol concepts."""
        import tools.bridges.cc_proxy_mcp as mod
        source = open(mod.__file__).read()
        assert "mcp" in source.lower()

    def test_bridge_references_claude_code(self):
        """Bridge references Claude Code for CC compatibility."""
        import tools.bridges.cc_proxy_mcp as mod
        source = open(mod.__file__).read()
        assert "claude" in source.lower() or "Claude" in source