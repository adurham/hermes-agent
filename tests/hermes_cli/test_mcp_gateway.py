"""Tests for hermes_cli/mcp_gateway.py -- the `hermes mcp-gateway` stdio
MCP server exposing the remote hermes gateway's /v1/runs API.

Regression coverage for the mcp 2.0 SDK migration (2026-08-19). The
migration (`11a9dcf5`, "feat(mcp): migrate to the mcp 2.x SDK") explicitly
ported FastMCP -> MCPServer in mcp_serve.py and
agent/transports/hermes_tools_mcp_server.py, but never touched this file --
a third FastMCP consumer using the exact same removed
`mcp.server.fastmcp.FastMCP` import. mcp 2.0 dropped that module entirely,
so `hermes mcp-gateway` raised `ModuleNotFoundError: No module named
'mcp.server.fastmcp'` at import time on any install with mcp==2.0.0 --
this file had zero test coverage before this fix, so nothing caught it.
"""

import asyncio


class TestMCPGatewayImports:
    def test_module_imports_under_installed_mcp_sdk(self):
        """Bare import must succeed against whatever mcp version is
        installed. This is the single assertion that would have caught the
        2.0 migration gap immediately (it failed with ModuleNotFoundError
        before this fix, on any environment with mcp==2.0.0 installed)."""
        import importlib
        import hermes_cli.mcp_gateway as mod
        importlib.reload(mod)

    def test_server_object_constructed(self):
        import hermes_cli.mcp_gateway as mod
        assert mod.mcp is not None
        assert mod.mcp.name == "hermes-gateway"

    def test_all_five_tools_registered(self):
        """Exercises the actual decorator registration path, not just
        import -- confirms @mcp.tool() bound all 5 tools under whichever
        SDK generation (FastMCP or MCPServer) resolved."""
        import hermes_cli.mcp_gateway as mod

        async def _list():
            return await mod.mcp.list_tools()

        tools = asyncio.run(_list())
        names = {t.name for t in tools}
        assert names == {
            "submit_task",
            "get_run_status",
            "tail_run_events",
            "stop_run",
            "list_recent_runs",
        }
