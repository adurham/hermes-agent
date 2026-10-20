"""Failure-mode coverage for tools/bridges/cc_proxy_mcp.py.

Background (2026-10-20): every connector bridge shares ONE rotating Claude Code
refresh chain. A connector whose own claude.ai OAuth was revoked (mcp-proxy 401
``mcp_invalid_oauth_token``) made each bridge spawn force-refresh the shared
chain -- ~2,200 rotations/day -- until a refresh raced and the chain died with
``invalid_grant``. The bridge then exited before serving stdio, Hermes logged
an opaque "Connection closed", registered zero tools, and the agent told the
user Slack did not exist. These tests pin each piece of the fix.

All token values here are fake literals; nothing touches the real keychain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from tools.bridges import cc_proxy_mcp as ccp


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def file_store(tmp_path, monkeypatch):
    """A file-backend CredStore on a temp creds file (no keychain, no real home)."""
    monkeypatch.delenv("CC_PROXY_KEYCHAIN_SERVICE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    creds = tmp_path / ".credentials.json"

    def write(access="at-old", refresh="rt-old", expires_in_s=-60):
        creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh,
            "expiresAt": int((ccp.time.time() + expires_in_s) * 1000)}}))

    write()
    with patch("platform.system", return_value="Linux"):
        store = ccp.CredStore(creds)
    store.write = write  # type: ignore[attr-defined]
    store.read = lambda: json.loads(creds.read_text())["claudeAiOauth"]  # type: ignore[attr-defined]
    return store


def _token_endpoint(handler):
    """Patch the refresh POST to a MockTransport handler; records request bodies."""
    seen: list[dict] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request, len(seen))

    real_client = httpx.Client

    def _client(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(_wrapped))

    return patch.object(ccp.httpx, "Client", _client), seen


def _ok(access="at-new", refresh="rt-new"):
    return httpx.Response(200, json={"access_token": access, "refresh_token": refresh, "expires_in": 3600})


def _invalid_grant():
    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token not found or invalid"})


# --- refresh chain robustness --------------------------------------------------

class TestRefreshChain:
    def test_expired_token_refreshes_and_persists_rotated_refresh_token(self, file_store):
        p, seen = _token_endpoint(lambda req, n: _ok())
        with p:
            tok = file_store._get_locked(False)
        assert tok == "at-new"
        assert seen[0]["refresh_token"] == "rt-old"
        assert file_store.read()["refreshToken"] == "rt-new"  # saved BEFORE returning

    def test_invalid_grant_is_reauth_and_never_replayed(self, file_store):
        p, seen = _token_endpoint(lambda req, n: _invalid_grant())
        with p:
            with pytest.raises(ccp.ConnectorError) as ei:
                file_store._get_locked(False)
            assert ei.value.kind == ccp.KIND_REAUTH
            # A second process/attempt must not POST the known-dead token again.
            with pytest.raises(ccp.ConnectorError) as ei2:
                file_store._get_locked(False)
        assert ei2.value.kind == ccp.KIND_REAUTH
        assert "cached verdict" in ei2.value.detail
        assert len(seen) == 1
        # The shared state stores only a fingerprint, never the token.
        state = file_store._statefile.read_text()
        assert "rt-old" not in state

    def test_invalid_grant_race_retries_once_with_the_newly_stored_token(self, file_store):
        # Another writer rotated the chain between our read and our POST.
        def handler(req, n):
            if n == 1:
                file_store.write(access="at-sibling", refresh="rt-sibling", expires_in_s=-60)
                return _invalid_grant()
            return _ok(access="at-final", refresh="rt-final")

        p, seen = _token_endpoint(handler)
        with p:
            tok = file_store._get_locked(False)
        assert [s["refresh_token"] for s in seen] == ["rt-old", "rt-sibling"]
        assert tok == "at-final"
        assert file_store.read()["refreshToken"] == "rt-final"

    def test_invalid_grant_race_uses_siblings_fresh_access_token_without_posting(self, file_store):
        def handler(req, n):
            file_store.write(access="at-sibling", refresh="rt-sibling", expires_in_s=3600)
            return _invalid_grant()

        p, seen = _token_endpoint(handler)
        with p:
            tok = file_store._get_locked(False)
        assert tok == "at-sibling"
        assert len(seen) == 1

    def test_refresh_timeout_is_network_with_chain_warning(self, file_store):
        def handler(req, n):
            raise httpx.ReadTimeout("timed out", request=req)

        p, _ = _token_endpoint(handler)
        with p:
            with pytest.raises(ccp.ConnectorError) as ei:
                file_store._get_locked(False)
        assert ei.value.kind == ccp.KIND_NETWORK
        assert "invalid_grant" in ei.value.detail  # tells the user what may follow

    def test_refresh_timeout_is_long(self):
        assert ccp.REFRESH_TIMEOUT_SECS >= 90

    def test_save_failure_is_retried_then_logged_loudly(self, file_store, caplog):
        calls = {"n": 0}
        real_save = file_store._save_raw

        def flaky(data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("keychain locked")
            real_save(data)

        p, _ = _token_endpoint(lambda req, n: _ok())
        with p, patch.object(file_store, "_save_raw", side_effect=flaky), patch.object(ccp.time, "sleep"):
            assert file_store._get_locked(False) == "at-new"
        assert calls["n"] == 2
        assert file_store.read()["refreshToken"] == "rt-new"

    def test_save_never_succeeding_logs_error(self, file_store, caplog):
        p, _ = _token_endpoint(lambda req, n: _ok())
        with p, patch.object(file_store, "_save_raw", side_effect=RuntimeError("denied")), \
                patch.object(ccp.time, "sleep"), caplog.at_level("ERROR", logger="cc_proxy_mcp"):
            file_store._get_locked(False)
        assert "COULD NOT BE SAVED" in caplog.text
        assert "at-new" not in caplog.text and "rt-new" not in caplog.text


class TestForcedRefreshDiscipline:
    def test_rejected_token_differs_from_stored_uses_stored_without_refresh(self, file_store):
        file_store.write(access="at-sibling", refresh="rt-x", expires_in_s=3600)
        p, seen = _token_endpoint(lambda req, n: _ok())
        with p:
            tok = file_store._get_locked(True, rejected_access="at-mine")
        assert tok == "at-sibling" and seen == []

    def test_forced_refresh_cooldown_is_cross_process(self, file_store):
        file_store.write(access="at-live", refresh="rt-live", expires_in_s=3600)
        p, seen = _token_endpoint(lambda req, n: _ok(access=f"at-{n}", refresh=f"rt-{n}"))
        with p:
            assert file_store._get_locked(True, rejected_access="at-live") == "at-1"
            # A SECOND process (fresh CredStore on the same chain) inside the cooldown.
            with patch("platform.system", return_value="Linux"):
                sibling = ccp.CredStore(file_store.path)
            assert sibling._get_locked(True, rejected_access="at-1") == "at-1"
        assert len(seen) == 1


# --- upstream 401 classification ----------------------------------------------

class _FakeStore:
    service = "Claude Code-credentials-test"

    def __init__(self):
        self.calls = []

    async def get_access_token(self, force_refresh=False, rejected_access=None):
        self.calls.append((force_refresh, rejected_access))
        return "at-2" if force_refresh else "at-1"


def _drive_flow(store, verdict, responses):
    async def run():
        flow = ccp._auth_flow(store, verdict, httpx.Request("POST", "https://x"))
        sent = [await flow.asend(None)]
        for resp in responses:
            try:
                sent.append(await flow.asend(resp))
            except StopAsyncIteration:
                break
        return sent
    return asyncio.run(run())


class TestUpstream401:
    def test_connector_level_401_does_not_touch_the_shared_chain(self):
        store, verdict = _FakeStore(), ccp._AuthVerdict()
        body = {"type": "error", "error": {"type": "authentication_error",
                                           "message": "OAuth token has been invalidated. Re-authentication is required.",
                                           "details": {"error_code": "mcp_invalid_oauth_token"}}}
        sent = _drive_flow(store, verdict, [httpx.Response(401, json=body)])
        assert len(sent) == 1  # no retry
        assert store.calls == [(False, None)]  # never force-refreshed
        assert verdict.error.kind == ccp.KIND_CONNECTOR_UNAUTHORIZED

    def test_generic_401_refreshes_once_naming_the_rejected_token(self):
        store, verdict = _FakeStore(), ccp._AuthVerdict()
        body = {"type": "error", "error": {"type": "authentication_error", "message": "Invalid or missing authentication"}}
        sent = _drive_flow(store, verdict, [httpx.Response(401, json=body), httpx.Response(200, json={})])
        assert len(sent) == 2
        assert sent[1].headers["Authorization"] == "Bearer at-2"
        assert store.calls == [(False, None), (True, "at-1")]
        assert verdict.error is None

    def test_401_after_refresh_is_reauth(self):
        store, verdict = _FakeStore(), ccp._AuthVerdict()
        r401 = httpx.Response(401, json={"error": {"message": "nope"}})
        _drive_flow(store, verdict, [r401, httpx.Response(401, json={})])
        assert verdict.error.kind == ccp.KIND_REAUTH


# --- classification + messages ---------------------------------------------

class TestClassification:
    def test_taskgroup_wrapped_connect_error_is_network(self):
        exc = BaseExceptionGroup("unhandled errors in a TaskGroup",
                                 [ExceptionGroup("inner", [httpx.ConnectError("nodename nor servname provided")])])
        assert ccp.classify_exception(exc).kind == ccp.KIND_NETWORK

    def test_opaque_mcp_error_uses_auth_flow_verdict(self):
        hint = ccp.ConnectorError(ccp.KIND_CONNECTOR_UNAUTHORIZED, "mcp-proxy 401 mcp_invalid_oauth_token")
        exc = BaseExceptionGroup("tg", [RuntimeError("Server returned an error response")])
        assert ccp.classify_exception(exc, auth_hint=hint) is hint

    def test_connector_error_inside_group_wins(self):
        inner = ccp.ConnectorError(ccp.KIND_REAUTH, "refresh token rejected: invalid_grant")
        assert ccp.classify_exception(BaseExceptionGroup("tg", [inner])) is inner

    def test_unknown_is_upstream_and_redacted(self):
        err = ccp.classify_exception(RuntimeError("boom " + "A" * 60))
        assert err.kind == ccp.KIND_UPSTREAM and "A" * 40 not in err.detail

    def test_config_dir_from_keychain_suffix(self, tmp_path, monkeypatch):
        cfg = tmp_path / ".claude-personal"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        svc = "Claude Code-credentials-" + hashlib.sha256(str(cfg).encode()).hexdigest()[:8]
        assert ccp.config_dir_for_service(svc) == cfg
        assert ccp.relogin_command(svc) == f"CLAUDE_CONFIG_DIR={cfg} claude auth login"

    def test_reauth_message_contains_exact_relogin_command(self, tmp_path, monkeypatch):
        cfg = tmp_path / "proxy-login"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        svc = "Claude Code-credentials-" + hashlib.sha256(str(cfg).encode()).hexdigest()[:8]
        msg = ccp.human_message(ccp.ConnectorError(ccp.KIND_REAUTH, "refresh token rejected: invalid_grant",
                                                   service=svc), "slack")
        assert msg.startswith("Slack connector auth expired")
        assert f"`CLAUDE_CONFIG_DIR={cfg} claude auth login`" in msg
        assert "/reload-mcp" in msg

    @pytest.mark.parametrize("kind,needle", [
        (ccp.KIND_NETWORK, "VPN"),
        (ccp.KIND_CONNECTOR_UNAUTHORIZED, "claude.ai Settings > Connectors"),
        (ccp.KIND_UPSTREAM, "status.anthropic.com"),
    ])
    def test_each_kind_names_its_fix(self, kind, needle):
        assert needle in ccp.human_message(ccp.ConnectorError(kind, "x"), "slack")

    def test_missing_pinned_entry_is_reauth(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_PROXY_KEYCHAIN_SERVICE", "Claude Code-credentials-deadbeef")
        with patch("platform.system", return_value="Darwin"), \
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 44, "", "nf")):
            with pytest.raises(ccp.ConnectorError) as ei:
                ccp.CredStore(tmp_path / "x.json")
        assert ei.value.kind == ccp.KIND_REAUTH and ei.value.service.endswith("deadbeef")


# --- degraded mode -----------------------------------------------------------

class TestDegradedStub:
    def test_stub_tool_names_the_failure(self):
        err = ccp.ConnectorError(ccp.KIND_REAUTH, "refresh token rejected: invalid_grant",
                                 service="Claude Code-credentials-00000000")
        tool = ccp.stub_tool(err, "slack")
        assert tool.name == "auth_expired_relogin_required"
        assert tool.description.startswith("UNAVAILABLE -- Slack connector auth expired")
        assert tool.meta[ccp.DEGRADED_META_KEY]["kind"] == ccp.KIND_REAUTH

    def test_stub_passes_hermes_description_injection_scan(self):
        from tools.mcp_tool_schema import _scan_mcp_description
        for kind in ccp.STUB_TOOL_NAMES:
            tool = ccp.stub_tool(ccp.ConnectorError(kind, "detail", service="Claude Code-credentials"), "slack")
            assert _scan_mcp_description("slack", tool.name, tool.description) == []

    def test_main_serves_stub_instead_of_exiting(self, monkeypatch):
        monkeypatch.setenv("CC_PROXY_KEYCHAIN_SERVICE", "Claude Code-credentials-00000000")
        captured = {}

        async def fake_degraded(err, connector, recheck):
            captured.update(err=err, connector=connector)

        args = ccp.argparse.Namespace(health=False, connector="slack", server_id=None, list=False,
                                      no_degraded=False, json=False)
        with patch("platform.system", return_value="Darwin"), \
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 44, "", "")), \
                patch.object(ccp, "run_degraded", fake_degraded):
            assert asyncio.run(ccp.main_async(args)) == 0
        assert captured["err"].kind == ccp.KIND_REAUTH and captured["connector"] == "slack"

    @pytest.mark.platforms("macos")
    def test_real_stdio_subprocess_lists_and_calls_stub(self, tmp_path):
        """End to end, as Hermes spawns it: a bridge pinned to a keychain entry that
        does not exist must answer initialize/list_tools/call_tool over stdio."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = {**os.environ, "CC_PROXY_KEYCHAIN_SERVICE": "Claude Code-credentials-00000000",
               "HERMES_HOME": str(tmp_path)}
        params = StdioServerParameters(command=sys.executable, args=[ccp.__file__, "--connector", "slack"], env=env)

        async def run():
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    tools = (await s.list_tools()).tools
                    res = await s.call_tool(tools[0].name, {})
                    return init, tools, res

        init, tools, res = asyncio.run(asyncio.wait_for(run(), 60))
        assert [t.name for t in tools] == ["auth_expired_relogin_required"]
        assert "auth expired" in (init.instructions or "")
        assert res.is_error is True
        assert "STILL UNAVAILABLE" in res.content[0].text and "claude auth login" in res.content[0].text


# --- health check --------------------------------------------------------------

class TestHealth:
    def test_config_parsing_finds_bridge_entries_only(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("""
mcp_servers:
  slack:
    command: python
    args: [/x/cc_proxy_mcp.py, --connector, slack]
    env: {CC_PROXY_KEYCHAIN_SERVICE: Claude Code-credentials-abc}
  notion:
    command: python
    args: [/x/cc_proxy_mcp.py, --connector, notion]
    enabled: false
  other:
    command: node
    args: [server.js]
""")
        entries = ccp._bridge_entries_from_config(cfg)
        assert [(e["name"], e["connector"], e["service"], e["enabled"]) for e in entries] == [
            ("slack", "slack", "Claude Code-credentials-abc", True), ("notion", "notion", None, False)]

    def test_health_reports_each_connector_and_exit_code(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.yaml").write_text("""
mcp_servers:
  slack: {command: p, args: [cc_proxy_mcp.py, --connector, slack]}
  prov: {command: p, args: [cc_proxy_mcp.py, --connector, Tanium Provisioner]}
""")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("CC_PROXY_KEYCHAIN_SERVICE", raising=False)

        async def fake_check(store, connector, server_id=None):
            if connector == "slack":
                return None
            return ccp.ConnectorError(ccp.KIND_CONNECTOR_UNAUTHORIZED, "mcp-proxy 401 mcp_invalid_oauth_token")

        args = ccp.argparse.Namespace(health=True, connector=None, json=True)
        with patch.object(ccp, "CredStore", lambda p: object()), patch.object(ccp, "check_connector", fake_check):
            rc = asyncio.run(ccp.run_health(args))
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert {r["name"]: r["status"] for r in out} == {"slack": "ok", "prov": "connector_unauthorized"}

    def test_check_connector_classifies_401_bodies(self):
        class Store:
            service = "svc"

            async def get_access_token(self, **kw):
                return "at"

        def handler(req):
            sid = req.url.path.rsplit("/", 1)[-1]
            if sid == "conn":
                return httpx.Response(401, json={"error": {"message": "invalidated",
                                                           "details": {"error_code": "mcp_invalid_oauth_token"}}})
            if sid == "tok":
                return httpx.Response(401, json={"error": {"message": "Invalid or missing authentication"}})
            return httpx.Response(200, text="ok")

        real = httpx.AsyncClient

        def client(*a, **kw):
            kw.pop("timeout", None)
            return real(transport=httpx.MockTransport(handler))

        with patch.object(ccp.httpx, "AsyncClient", client):
            assert asyncio.run(ccp.check_connector(Store(), "x", "good")) is None
            assert asyncio.run(ccp.check_connector(Store(), "x", "conn")).kind == ccp.KIND_CONNECTOR_UNAUTHORIZED
            assert asyncio.run(ccp.check_connector(Store(), "x", "tok")).kind == ccp.KIND_REAUTH
