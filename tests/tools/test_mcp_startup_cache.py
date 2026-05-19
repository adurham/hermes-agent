"""Tests for the MCP startup cache (tools/mcp_tool.py).

The startup cache persists each MCP server's discovered tool list under
``$HERMES_HOME/cache/mcp_tools/<server>.json`` so subsequent sessions can
register from disk instantly and lazy-spawn the real connection only when
the model first calls one of those tools. These tests cover the cache
helpers, the round-trip through ``_make_cached_server_shell``, hash-based
invalidation, and the lazy-spawn handler integration.

All tests are pure in-process — no real MCP servers, no subprocess spawns,
no network calls.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_tool(name="search", description=None, input_schema=None):
    tool = SimpleNamespace()
    tool.name = name
    tool.description = description or f"Run {name}"
    tool.inputSchema = input_schema or {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    return tool


def _fake_initialize_result(resources=False, prompts=False):
    """Build a fake InitializeResult.capabilities matching the MCP SDK shape."""
    caps = SimpleNamespace(
        resources=SimpleNamespace() if resources else None,
        prompts=SimpleNamespace() if prompts else None,
    )
    return SimpleNamespace(capabilities=caps)


@pytest.fixture
def tmp_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME into a temp dir so cache writes don't leak."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # _cache_root() defers import of hermes_constants — patch it to point
    # at our tmp dir so the test works even when hermes_constants caches
    # its own state.
    import tools.mcp_tool as mt

    def _root():
        path = os.path.join(str(tmp_path), "cache", "mcp_tools")
        os.makedirs(path, exist_ok=True)
        return path

    monkeypatch.setattr(mt, "_cache_root", _root)
    yield tmp_path


# ---------------------------------------------------------------------------
# Hash + serialization
# ---------------------------------------------------------------------------

class TestHashServerConfig:
    def test_only_hashes_relevant_keys(self):
        """timeout/connect_timeout/enabled/sampling/tools shouldn't affect hash."""
        from tools.mcp_tool import _hash_server_config

        base = {"command": "npx", "args": ["-y", "x"], "env": {"K": "v"}}
        # These extras must not change the hash:
        with_extras = dict(
            base,
            timeout=180,
            connect_timeout=60,
            enabled=True,
            supports_parallel_tool_calls=True,
            sampling={"enabled": False},
            tools={"include": ["search"]},
        )
        assert _hash_server_config(base) == _hash_server_config(with_extras)

    def test_command_change_changes_hash(self):
        from tools.mcp_tool import _hash_server_config

        a = {"command": "npx", "args": ["x"]}
        b = {"command": "python", "args": ["x"]}
        assert _hash_server_config(a) != _hash_server_config(b)

    def test_args_change_changes_hash(self):
        from tools.mcp_tool import _hash_server_config

        a = {"command": "npx", "args": ["a"]}
        b = {"command": "npx", "args": ["b"]}
        assert _hash_server_config(a) != _hash_server_config(b)

    def test_url_change_changes_hash(self):
        from tools.mcp_tool import _hash_server_config

        a = {"url": "https://a.example.com"}
        b = {"url": "https://b.example.com"}
        assert _hash_server_config(a) != _hash_server_config(b)

    def test_empty_config_hashes_consistently(self):
        from tools.mcp_tool import _hash_server_config
        h1 = _hash_server_config({})
        h2 = _hash_server_config({})
        assert h1 == h2 and h1 != ""


# ---------------------------------------------------------------------------
# Save / load round-trip
# ---------------------------------------------------------------------------

class TestCacheRoundTrip:
    def test_save_then_load_returns_same_tools(self, tmp_hermes_home):
        from tools.mcp_tool import _save_cached_spec, _load_cached_spec

        config = {"command": "npx", "args": ["x"]}
        server = SimpleNamespace(
            _tools=[
                _fake_tool("alpha"),
                _fake_tool("beta", description="Do beta"),
            ],
            initialize_result=_fake_initialize_result(resources=True),
        )
        _save_cached_spec("srv", config, server)
        spec = _load_cached_spec("srv", config)
        assert spec is not None
        names = sorted(t.name for t in spec["tools"])
        assert names == ["alpha", "beta"]
        # Capability flag round-trips
        assert spec["capabilities"].resources is True
        assert spec["capabilities"].prompts is None

    def test_load_miss_when_file_absent(self, tmp_hermes_home):
        from tools.mcp_tool import _load_cached_spec

        assert _load_cached_spec("never_existed", {"command": "x"}) is None

    def test_load_miss_on_hash_mismatch(self, tmp_hermes_home):
        from tools.mcp_tool import _save_cached_spec, _load_cached_spec

        original = {"command": "npx", "args": ["v1"]}
        server = SimpleNamespace(
            _tools=[_fake_tool("alpha")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("srv", original, server)
        edited = {"command": "npx", "args": ["v2"]}  # different args → different hash
        assert _load_cached_spec("srv", edited) is None

    def test_load_miss_on_corrupted_json(self, tmp_hermes_home):
        from tools.mcp_tool import _cache_path_for, _load_cached_spec

        path = _cache_path_for("srv")
        assert path is not None
        with open(path, "w") as f:
            f.write("{not valid json")
        assert _load_cached_spec("srv", {"command": "x"}) is None

    def test_load_miss_when_tools_list_empty(self, tmp_hermes_home):
        """An empty cached tool list is treated as a miss so live discovery runs."""
        from tools.mcp_tool import _save_cached_spec, _load_cached_spec

        config = {"command": "x"}
        server = SimpleNamespace(_tools=[], initialize_result=None)
        _save_cached_spec("srv", config, server)
        assert _load_cached_spec("srv", config) is None

    def test_load_miss_on_schema_version_bump(self, tmp_hermes_home):
        from tools.mcp_tool import _save_cached_spec, _load_cached_spec, _cache_path_for

        config = {"command": "x"}
        server = SimpleNamespace(
            _tools=[_fake_tool("alpha")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("srv", config, server)
        # Bump the version on disk to simulate a forward-incompatible upgrade
        path = _cache_path_for("srv")
        data = json.loads(open(path).read())
        data["version"] = 999
        with open(path, "w") as f:
            json.dump(data, f)
        assert _load_cached_spec("srv", config) is None


# ---------------------------------------------------------------------------
# Cache shell behaviour
# ---------------------------------------------------------------------------

class TestCachedServerShell:
    def test_shell_has_session_none(self, tmp_hermes_home):
        """A shell built from cache exposes the cached tools but no live session."""
        from tools.mcp_tool import (
            _save_cached_spec,
            _load_cached_spec,
            _make_cached_server_shell,
        )

        config = {"command": "npx", "args": ["x"], "timeout": 90}
        server = SimpleNamespace(
            _tools=[_fake_tool("alpha"), _fake_tool("beta")],
            initialize_result=_fake_initialize_result(prompts=True),
        )
        _save_cached_spec("srv", config, server)
        spec = _load_cached_spec("srv", config)
        shell = _make_cached_server_shell("srv", config, spec)
        assert shell.session is None
        assert shell.name == "srv"
        assert shell.tool_timeout == 90.0
        assert [t.name for t in shell._tools] == ["alpha", "beta"]
        assert shell.initialize_result is not None
        assert shell.initialize_result.capabilities.prompts is True
        assert shell.initialize_result.capabilities.resources is None


# ---------------------------------------------------------------------------
# register_mcp_servers cache fast path
# ---------------------------------------------------------------------------

class TestRegisterMcpServersCachePath:
    def test_cache_hit_skips_live_discovery(self, tmp_hermes_home, monkeypatch):
        """A pre-warmed cache means _discover_and_register_server is never called."""
        from tools.mcp_tool import (
            register_mcp_servers,
            _save_cached_spec,
            _servers,
        )

        # Pre-warm the cache for "myserv"
        config = {"command": "npx", "args": ["x"]}
        fake_server = SimpleNamespace(
            _tools=[_fake_tool("alpha"), _fake_tool("beta")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("myserv", config, fake_server)

        async def fail(*a, **k):  # noqa: ARG001 — should never be invoked
            raise AssertionError(
                "Live discovery must not run when the cache is valid"
            )

        # Prevent the background refresh from actually firing — we don't want
        # the daemon thread to try connecting during the test.
        monkeypatch.setattr(
            "tools.mcp_tool._spawn_background_refresh", lambda *a, **k: None
        )
        try:
            with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
                 patch("tools.mcp_tool._discover_and_register_server", side_effect=fail):
                names = register_mcp_servers({"myserv": config})
            # Both tools should be visible to the registry
            assert "myserv_alpha" in names
            assert "myserv_beta" in names
            # And a cache shell with session=None should be in _servers
            shell = _servers.get("myserv")
            assert shell is not None
            assert shell.session is None
        finally:
            _servers.pop("myserv", None)
            from tools.registry import registry
            registry.deregister("myserv_alpha")
            registry.deregister("myserv_beta")

    def test_cache_miss_falls_through_to_live_discovery(self, tmp_hermes_home, monkeypatch):
        """No cache entry → behaviour matches pre-cache (live discovery)."""
        from tools.mcp_tool import register_mcp_servers, _servers, MCPServerTask, _ensure_mcp_loop

        live_calls = []

        async def fake_register(name, cfg):
            live_calls.append(name)
            server = MCPServerTask(name)
            server.session = MagicMock()
            server._tools = [_fake_tool("alpha")]
            server._registered_tool_names = [f"{name}_alpha"]
            _servers[name] = server
            return [f"{name}_alpha"]

        try:
            with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
                 patch("tools.mcp_tool._discover_and_register_server", side_effect=fake_register):
                _ensure_mcp_loop()
                register_mcp_servers({"freshsrv": {"command": "npx", "args": ["x"]}})
            assert live_calls == ["freshsrv"]
            assert "freshsrv" in _servers
            assert _servers["freshsrv"].session is not None
        finally:
            _servers.pop("freshsrv", None)

    def test_no_cache_env_var_forces_live_discovery(self, tmp_hermes_home, monkeypatch):
        """HERMES_MCP_NO_CACHE=1 must bypass the cache fast path."""
        from tools.mcp_tool import (
            register_mcp_servers,
            _save_cached_spec,
            _servers,
            MCPServerTask,
            _ensure_mcp_loop,
        )

        # Pre-warm a valid cache
        config = {"command": "npx", "args": ["x"]}
        fake_server = SimpleNamespace(
            _tools=[_fake_tool("alpha")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("ns", config, fake_server)
        monkeypatch.setenv("HERMES_MCP_NO_CACHE", "1")

        async def fake_register(name, cfg):
            server = MCPServerTask(name)
            server.session = MagicMock()
            server._tools = [_fake_tool("live")]
            server._registered_tool_names = [f"{name}_live"]
            _servers[name] = server
            return [f"{name}_live"]

        try:
            with patch("tools.mcp_tool._MCP_AVAILABLE", True), \
                 patch("tools.mcp_tool._discover_and_register_server", side_effect=fake_register):
                _ensure_mcp_loop()
                register_mcp_servers({"ns": config})
            # Live discovery ran (got "live"), not the cached "alpha"
            assert _servers["ns"].session is not None
            assert any(
                getattr(t, "name", "") == "live"
                for t in _servers["ns"]._tools
            )
        finally:
            _servers.pop("ns", None)


# ---------------------------------------------------------------------------
# Lazy-spawn through the tool handler
# ---------------------------------------------------------------------------

class TestLazySpawn:
    def test_tool_handler_triggers_spawn_on_cache_shell(self, tmp_hermes_home, monkeypatch):
        """Calling a cache-registered tool spawns the real session once."""
        from tools.mcp_tool import (
            _save_cached_spec,
            _load_cached_spec,
            _make_cached_server_shell,
            _make_tool_handler,
            _servers,
            MCPServerTask,
            _ensure_mcp_loop,
        )

        # Build a shell from a fresh cache entry
        config = {"command": "npx", "args": ["x"]}
        fake_disc = SimpleNamespace(
            _tools=[_fake_tool("alpha")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("svc", config, fake_disc)
        spec = _load_cached_spec("svc", config)
        shell = _make_cached_server_shell("svc", config, spec)
        _servers["svc"] = shell
        assert _servers["svc"].session is None

        spawn_count = [0]

        async def fake_connect(name, cfg):
            spawn_count[0] += 1
            real = MCPServerTask(name)
            real.session = MagicMock()
            real.session.call_tool = MagicMock()

            async def _ct(tool_name, arguments=None):
                # MCP SDK shape
                return SimpleNamespace(
                    content=[SimpleNamespace(text='{"ok": true}')],
                    isError=False,
                    structuredContent=None,
                )

            real.session.call_tool = _ct
            real._tools = []
            real._rpc_lock = asyncio.Lock()
            return real

        try:
            with patch("tools.mcp_tool._connect_server", side_effect=fake_connect), \
                 patch("tools.mcp_tool._load_mcp_config",
                       return_value={"svc": config}):
                _ensure_mcp_loop()
                handler = _make_tool_handler("svc", "alpha", tool_timeout=10)
                # First call: should lazy-spawn
                result = handler({"q": "hi"})
                assert spawn_count[0] == 1
                # Real session is now in _servers; second call doesn't re-spawn
                result2 = handler({"q": "again"})
                assert spawn_count[0] == 1
                # Both calls succeeded
                payload = json.loads(result)
                assert payload.get("result") == '{"ok": true}'
                payload2 = json.loads(result2)
                assert payload2.get("result") == '{"ok": true}'
                assert _servers["svc"].session is not None
        finally:
            _servers.pop("svc", None)

    def test_tool_handler_returns_error_when_server_removed(self, tmp_hermes_home):
        """Calling a tool whose server was removed from config fails cleanly."""
        from tools.mcp_tool import _make_tool_handler, _servers, _make_cached_server_shell, _load_cached_spec, _save_cached_spec, _ensure_mcp_loop

        config = {"command": "npx", "args": ["x"]}
        fake_disc = SimpleNamespace(
            _tools=[_fake_tool("alpha")],
            initialize_result=_fake_initialize_result(),
        )
        _save_cached_spec("ghost", config, fake_disc)
        spec = _load_cached_spec("ghost", config)
        _servers["ghost"] = _make_cached_server_shell("ghost", config, spec)

        try:
            # Simulate config having dropped this server
            with patch("tools.mcp_tool._load_mcp_config", return_value={}):
                _ensure_mcp_loop()
                handler = _make_tool_handler("ghost", "alpha", tool_timeout=5)
                result = handler({})
                parsed = json.loads(result)
                assert "error" in parsed
                assert "no longer in config" in parsed["error"]
        finally:
            _servers.pop("ghost", None)
