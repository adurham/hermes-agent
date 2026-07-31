"""Tests for the fork's CC proxy MCP bridge.

The bridge at tools/bridges/cc_proxy_mcp.py translates between
Hermes MCP tool calls and Claude Code's proxy protocol.
"""

import subprocess
from unittest.mock import patch

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