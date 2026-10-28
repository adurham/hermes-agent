"""End-to-end gate tests for the session-token auth provider.

The bellona-shaped scenario: a non-loopback dashboard whose ONLY credential is the
provisioned ``HERMES_DASHBOARD_SESSION_TOKEN`` (the nix/agenix deployment story).
Before the session-token provider, that deployment hit the fail-closed gate, got
talked into basic auth by the interactive prompt, and its own token was rejected
on every gated leg (REST 401, ``/api/ws`` 403) — the desktop's "HTTP passes but the
WebSocket fails" incident. These tests pin the fixed behaviour:

  * the gate is satisfied with the token provider as the only registered provider,
    and still fails closed with none,
  * the REST bearer leg verifies the provisioned token,
  * the ``/api/ws`` upgrade accepts ``?token=<provisioned>`` and rejects anything else.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
import plugins.dashboard_auth.session_token as session_token_plugin


@pytest.fixture
def token():
    return secrets.token_urlsafe(32)


@pytest.fixture
def gated_token_app(token):
    """web_server.app in gated mode with ONLY the session-token provider registered."""
    _reset_for_tests()
    clear_providers()
    register_provider(session_token_plugin.SessionTokenProvider(token=token))
    prev = {k: getattr(web_server.app.state, k, None)
            for k in ("bound_host", "bound_port", "auth_required")}
    web_server.app.state.bound_host = "bellona.ts.net"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://bellona.ts.net")
    yield client
    clear_providers()
    _reset_for_tests()
    for k, v in prev.items():
        setattr(web_server.app.state, k, v)


def _fake_ws(*, query: dict, client_host: str = "203.0.113.7", path: str = "/api/ws"):
    class _QP:
        def __init__(self, q):
            self._q = q

        def get(self, k, default=""):
            return self._q.get(k, default)

    return SimpleNamespace(
        query_params=_QP(query),
        headers={},
        client=SimpleNamespace(host=client_host),
        url=SimpleNamespace(path=path),
    )


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


class TestGateEngagement:
    def test_token_provider_satisfies_the_fail_closed_gate(self, token):
        """A non-loopback bind with only the session-token provider starts."""
        clear_providers()
        register_provider(session_token_plugin.SessionTokenProvider(token=token))
        web_server._configure_auth_gate(
            "0.0.0.0", allow_public=False, ssh_session_token=None, ssh_owner_nonce=None)
        assert web_server.app.state.auth_required is True

    def test_no_providers_still_fails_closed(self, token):
        clear_providers()
        with pytest.raises(SystemExit):
            web_server._configure_auth_gate(
                "0.0.0.0", allow_public=False, ssh_session_token=None, ssh_owner_nonce=None)


# ---------------------------------------------------------------------------
# The two legs the desktop exercises
# ---------------------------------------------------------------------------


class TestRestBearerLeg:
    def test_provisioned_token_passes_the_gate(self, gated_token_app, token):
        r = gated_token_app.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code not in (401, 403, 503), r.status_code

    def test_foreign_token_is_401(self, gated_token_app, token):
        r = gated_token_app.get(
            "/api/session", headers={"Authorization": f"Bearer {secrets.token_urlsafe(32)}"})
        assert r.status_code == 401

    def test_the_desktops_stale_copy_is_401(self, gated_token_app, token):
        """The incident's trap: a token from BEFORE a secret rotation must not verify."""
        stale = secrets.token_urlsafe(32)
        assert stale != token
        r = gated_token_app.get("/api/session", headers={"Authorization": f"Bearer {stale}"})
        assert r.status_code == 401


class TestWsUpgradeLeg:
    def test_provisioned_token_is_accepted(self, gated_token_app, token):
        reason, credential = _web_server_chat._ws_auth_reason(
            _fake_ws(query={"token": token}))
        assert reason is None
        assert credential == "token"

    def test_foreign_token_is_rejected(self, gated_token_app, token):
        reason, _ = _web_server_chat._ws_auth_reason(
            _fake_ws(query={"token": secrets.token_urlsafe(32)}))
        assert reason == "token_invalid"

    def test_no_credential_is_rejected(self, gated_token_app):
        reason, credential = _web_server_chat._ws_auth_reason(_fake_ws(query={}))
        assert reason == "no_credential"
        assert credential == "none"
