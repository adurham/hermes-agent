"""Tests for the fork's CC proxy MCP bridge.

The bridge at tools/bridges/cc_proxy_mcp.py translates between
Hermes MCP tool calls and Claude Code's proxy protocol.
"""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import mcp.client.streamable_http
import mcp_types
import tools.bridges.cc_proxy_mcp as cc_proxy_mcp


def _fake_run_result(returncode=0, stdout="", stderr=""):
    result = subprocess.CompletedProcess(
        args=["security"], returncode=returncode, stdout=stdout, stderr=stderr,
    )
    return result


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


class TestResolveKeychainService:
    """Regression coverage for the account-suffixed Keychain service name bug.

    Claude Code >=2.1.114 can register its Keychain entry as
    "Claude Code-credentials-<hash>" rather than the bare
    "Claude Code-credentials". An exact-match ``security find-generic-password
    -s`` lookup against the bare name silently finds nothing on those
    installs, which previously made CredStore fall back to the (often
    stale/missing) file backend instead of the real, working credential.
    """

    def test_finds_suffixed_service_name(self):
        """dump-keychain output containing a suffixed entry is matched."""
        dump_output = (
            '    "svce"<blob>="Some Other App"\n'
            '    "svce"<blob>="Claude Code-credentials-3775e6c9"\n'
            '    "svce"<blob>="Unrelated Service"\n'
        )
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", return_value=_fake_run_result(0, dump_output)):
            service = cc_proxy_mcp._resolve_keychain_service()
        assert service == "Claude Code-credentials-3775e6c9"

    def test_prefers_exact_bare_name_when_present(self):
        """The un-suffixed legacy name wins if it's also present."""
        dump_output = (
            '    "svce"<blob>="Claude Code-credentials-3775e6c9"\n'
            '    "svce"<blob>="Claude Code-credentials"\n'
        )
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", return_value=_fake_run_result(0, dump_output)):
            service = cc_proxy_mcp._resolve_keychain_service()
        assert service == "Claude Code-credentials"

    def test_returns_none_when_no_match(self):
        dump_output = '    "svce"<blob>="Totally Unrelated"\n'
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", return_value=_fake_run_result(0, dump_output)):
            service = cc_proxy_mcp._resolve_keychain_service()
        assert service is None

    def test_returns_none_on_non_darwin(self):
        with patch("platform.system", return_value="Linux"):
            service = cc_proxy_mcp._resolve_keychain_service()
        assert service is None

    def test_returns_none_on_dump_failure(self):
        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", return_value=_fake_run_result(1, "", "denied")):
            service = cc_proxy_mcp._resolve_keychain_service()
        assert service is None


class TestCredStoreKeychainBackend:
    """CredStore must use the resolved (possibly suffixed) service name for
    every keychain operation, not the bare KEYCHAIN_SERVICE constant."""

    def test_uses_resolved_suffixed_service_for_read(self, tmp_path):
        missing_file = tmp_path / "does-not-exist" / ".credentials.json"
        find_password_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["security", "dump-keychain"]:
                return _fake_run_result(
                    0, '    "svce"<blob>="Claude Code-credentials-3775e6c9"\n'
                )
            if cmd[:2] == ["security", "find-generic-password"]:
                find_password_calls.append(cmd)
                if "-w" in cmd:
                    return _fake_run_result(0, '{"claudeAiOauth": {"accessToken": "tok"}}')
                # account-lookup call (no -w): return acct line
                return _fake_run_result(
                    0, '    "acct"<blob>="adam.durham"\n'
                )
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", side_effect=fake_run):
            store = cc_proxy_mcp.CredStore(missing_file)
            assert store._backend == "keychain"
            assert store._service == "Claude Code-credentials-3775e6c9"
            data = store._load_raw()

        assert data == {"claudeAiOauth": {"accessToken": "tok"}}
        # Every find-generic-password call must use the resolved suffixed
        # service name, never the bare constant.
        for cmd in find_password_calls:
            assert "-s" in cmd
            service_arg = cmd[cmd.index("-s") + 1]
            assert service_arg == "Claude Code-credentials-3775e6c9"

    def test_falls_back_to_file_backend_when_no_keychain_match(self, tmp_path):
        missing_file = tmp_path / "does-not-exist" / ".credentials.json"

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["security", "dump-keychain"]:
                return _fake_run_result(0, "")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("platform.system", return_value="Darwin"), \
             patch("subprocess.run", side_effect=fake_run):
            store = cc_proxy_mcp.CredStore(missing_file)

        assert store._backend == "file"


class TestSDKCompat:
    """Regression coverage for the mcp 2.0 SDK migration (2026-08-19).

    ``tools/mcp_tool.py`` and friends were ported to mcp 2.0 in commit
    11a9dcf5 ("feat(mcp): migrate to the mcp 2.x SDK"), but this bridge
    module was not in that diff -- it lives under tools/bridges/, one
    directory the migration's grep/review pass missed. mcp 2.0 dropped the
    deprecated `streamablehttp_client` alias (module-import-time
    ImportError -- broke every session using this bridge, e.g. the
    Claude-Code-connector proxy for Slack/Notion/PagerDuty/Microsoft365/
    StackOverflow) and removed `Server.list_tools()` / `Server.call_tool()`
    decorators in favor of `on_list_tools=`/`on_call_tool=` constructor
    kwargs. Both breakages were silent at the API-surface level (no
    deprecation warning survived to 2.0) and only surfaced as an
    ImportError or AttributeError at actual connection time -- exactly the
    kind of thing `test_module_imports` should catch, and would have,
    had it existed before this fix.
    """

    def test_module_imports_under_installed_mcp_sdk(self):
        """Bare import must succeed against whatever mcp version is
        installed. This is the single assertion that would have caught the
        2.0 migration gap immediately (it failed with ImportError before
        this fix, on any environment with mcp==2.0.0 installed)."""
        import importlib
        import tools.bridges.cc_proxy_mcp as mod
        importlib.reload(mod)

    def test_legacy_http_flag_matches_installed_sdk_generation(self):
        """`_MCP_LEGACY_HTTP` must reflect which entry point the installed
        SDK actually exposes, not a hardcoded assumption."""
        has_legacy = hasattr(
            mcp.client.streamable_http, "streamablehttp_client"
        )
        assert cc_proxy_mcp._MCP_LEGACY_HTTP == has_legacy

    def test_server_construction_matches_sdk_generation(self):
        """On mcp >= 1.24.0 (no decorator API), Server must be constructed
        via on_list_tools=/on_call_tool= kwargs, not the removed
        .list_tools()/.call_tool() decorators -- asserts the code path this
        bridge would actually take at runtime, not just that import works."""
        from mcp.server import Server

        if cc_proxy_mcp._MCP_LEGACY_HTTP:
            local = Server("cc-proxy-test")
            assert hasattr(local, "list_tools")
            assert hasattr(local, "call_tool")
        else:
            assert not hasattr(Server, "list_tools")
            assert not hasattr(Server, "call_tool")

            async def _on_list_tools(_ctx, _params):
                raise NotImplementedError

            async def _on_call_tool(_ctx, _params):
                raise NotImplementedError

            # Must not raise -- exercises the exact constructor call the
            # non-legacy branch of run_proxy() makes.
            Server(
                "cc-proxy-test",
                on_list_tools=_on_list_tools,
                on_call_tool=_on_call_tool,
            )


class TestCallToolErrorPropagation:
    """Regression coverage for a pre-existing (not introduced by the mcp
    2.0 port) bug found during a follow-up audit: run_proxy()'s call_tool
    relay used to rebuild `CallToolResult(content=resp.content)` from the
    upstream response instead of returning it directly, silently dropping
    `isError`/`structuredContent` (`is_error`/`structured_content` on
    mcp>=2.0 -- see `mcp_field()` in tools/mcp_tool.py for the same field
    rename). That turned every failed upstream tool call -- a Slack/Notion/
    PagerDuty/Microsoft365/StackOverflow tool erroring -- into an
    apparently-successful empty-content result for anything consuming this
    bridge. Confirmed via `git show <pre-migration-commit>:tools/bridges/
    cc_proxy_mcp.py` that the bug predates the SDK migration; it was never
    about the rename, just that the field was never forwarded either way.

    These tests exercise the actual `run_proxy()` coroutine end-to-end
    (mocking only the transport/stdio boundary) to assert the *registered
    handler* returns the upstream error status unchanged, rather than just
    checking source text for the fix.
    """

    async def _drive_run_proxy_and_capture_call_tool_handler(self, upstream_call_tool_result):
        """Run `run_proxy()` far enough to register its call_tool handler,
        capture that handler, invoke it with a mocked upstream session
        returning `upstream_call_tool_result`, and return what the handler
        returns to the local MCP Server. Mocks the transport (streamablehttp_
        client / httpx2 client) and stdio boundary; exercises everything
        from `ClientSession.__aenter__` onward for real.
        """
        captured = {}

        mock_upstream = MagicMock()
        mock_upstream.initialize = AsyncMock(return_value=None)
        mock_upstream.list_tools = AsyncMock(
            return_value=mcp_types.ListToolsResult(tools=[])
        )
        mock_upstream.call_tool = AsyncMock(return_value=upstream_call_tool_result)

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_upstream)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)

        mock_read_write_cm = MagicMock()
        mock_read_write_cm.__aenter__ = AsyncMock(
            return_value=("fake-read-stream", "fake-write-stream", None)
        )
        mock_read_write_cm.__aexit__ = AsyncMock(return_value=False)

        mock_stdio_cm = MagicMock()
        mock_stdio_cm.__aenter__ = AsyncMock(
            return_value=("fake-in-stream", "fake-out-stream")
        )
        mock_stdio_cm.__aexit__ = AsyncMock(return_value=False)

        class _FakeLocalServer:
            """Stand-in for mcp.server.Server capturing whichever
            registration path run_proxy() takes for the installed SDK
            generation (decorator vs. constructor kwargs)."""

            def __init__(self, name, on_list_tools=None, on_call_tool=None):
                if on_call_tool is not None:
                    captured["call_tool_handler"] = on_call_tool
                    captured["style"] = "constructor"

            def list_tools(self):
                def _decorator(fn):
                    return fn
                return _decorator

            def call_tool(self):
                def _decorator(fn):
                    captured["call_tool_handler"] = fn
                    captured["style"] = "decorator"
                    return fn
                return _decorator

            def create_initialization_options(self):
                return object()

            async def run(self, *args, **kwargs):
                return None

        with patch.object(cc_proxy_mcp, "streamablehttp_client", return_value=mock_read_write_cm), \
             patch.object(cc_proxy_mcp, "ClientSession", return_value=mock_session_cm), \
             patch.object(cc_proxy_mcp, "Server", _FakeLocalServer), \
             patch.object(cc_proxy_mcp, "stdio_server", return_value=mock_stdio_cm):
            if not cc_proxy_mcp._MCP_LEGACY_HTTP:
                mock_http_client_cm = MagicMock()
                mock_http_client_cm.__aenter__ = AsyncMock(return_value=MagicMock())
                mock_http_client_cm.__aexit__ = AsyncMock(return_value=False)
                with patch.object(cc_proxy_mcp.httpx2, "AsyncClient", return_value=mock_http_client_cm):
                    await cc_proxy_mcp.run_proxy("test-server-id", MagicMock())
            else:
                await cc_proxy_mcp.run_proxy("test-server-id", MagicMock())

        assert "call_tool_handler" in captured, "run_proxy() never registered a call_tool handler"
        handler = captured["call_tool_handler"]

        if captured["style"] == "decorator":
            return await handler("some_tool", {})
        else:
            fake_params = MagicMock()
            fake_params.name = "some_tool"
            fake_params.arguments = {}
            return await handler(MagicMock(), fake_params)

    def test_error_result_propagates_isError(self):
        """An upstream tool-call error must surface as an error downstream,
        not silently look like success."""
        import asyncio

        error_result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="boom")],
            isError=True,
        )
        result = asyncio.run(
            self._drive_run_proxy_and_capture_call_tool_handler(error_result)
        )
        # Handler must return the CallToolResult itself (or something that
        # preserves is_error=True), not a bare content list that discards
        # error status.
        assert isinstance(result, mcp_types.CallToolResult)
        assert result.is_error is True

    def test_structured_content_propagates(self):
        """structuredContent (mcp 2.0: structured_content) from the
        upstream tool call must not be dropped on the way through."""
        import asyncio

        result_with_structured = mcp_types.CallToolResult(
            content=[],
            structuredContent={"key": "value"},
        )
        result = asyncio.run(
            self._drive_run_proxy_and_capture_call_tool_handler(result_with_structured)
        )
        assert isinstance(result, mcp_types.CallToolResult)
        assert result.structured_content == {"key": "value"}

    def test_success_result_still_has_content(self):
        """Sanity check: the success path (content, no error) still works
        after the fix -- this isn't a one-sided fix that broke the common
        case while fixing the error case."""
        import asyncio

        ok_result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="fine")],
        )
        result = asyncio.run(
            self._drive_run_proxy_and_capture_call_tool_handler(ok_result)
        )
        assert isinstance(result, mcp_types.CallToolResult)
        assert result.is_error is False
        assert len(result.content) == 1