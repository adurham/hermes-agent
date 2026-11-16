"""The gateway's private listener answers an uncredentialed ``/api/status`` with liveness only.

``/api/status`` is public (``PUBLIC_API_PATHS``), so no auth gate looks at it. On the gateway's own
loopback listener for native clients every other route wants a native ticket (or the session
token), and any local process can reach the port, so a caller without one gets an allowlisted
liveness / auth-shape body: no version, host paths, gateway PID, install id or profile names.
Desktop sends a ticket on every request to this listener and keeps reading the full body. The
standalone dashboard's public body and the Webapp's signed-out body are pinned elsewhere
(``test_webapp_status_signed_out.py``) and stay as they are.
"""
from __future__ import annotations

import types

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import hermes_cli.web_server_gateway as _web_server_gateway
from hermes_cli import web_server
from hermes_cli.dashboard_auth import native_http

LOOPBACK = "http://127.0.0.1:9120"
ALLOWED = {"gateway_running", "gateway_state", "overall", "auth_required", "auth_providers", "auth_flows"}
NEVER_UNAUTHENTICATED = (
    "version", "release_date", "config_version", "latest_config_version", "can_update_hermes",
    "hermes_home", "config_path", "env_path", "gateway_pid", "gateway_health_url", "gateways",
    "install_id", "profiles", "parked_profiles", "gateway_shared_with", "gateway_mode",
    "active_sessions", "components", "memory", "disk", "nous_session_valid")
TICKET = "x-hermes-gateway-ticket"


@pytest.fixture
def gateway_listener(monkeypatch, tmp_path):
    """The dashboard app as ``GatewayDashboardMount`` binds it: loopback, gate off, a runner set
    (which makes the surface ``gateway``), two profiles installed."""
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    for key, value in (("bound_host", "127.0.0.1"), ("bound_port", 9120), ("auth_required", False),
                       ("trusted_public_hosts", frozenset()),
                       ("gateway_runner", types.SimpleNamespace(session_runtime_descriptor=None))):
        monkeypatch.setattr(web_server.app.state, key, value, raising=False)
    for key in ("ui_surface", "listener_public_url", "webapp_home"):
        if hasattr(web_server.app.state, key):
            monkeypatch.delattr(web_server.app.state, key)
    monkeypatch.setattr(_web_server_gateway, "_collect_profile_gateway_topology_cached", lambda: {
        "profiles": ["default", "coder"], "parked_profiles": [], "gateway_mode": "multiplex",
        "gateways": []})
    home = str(tmp_path)

    # The outermost seam redeems a native ticket on every request, public paths included; the real
    # redemption needs a live runner's ticket store, so stand in for it with one known ticket. Like
    # the real seam, an unredeemable ticket is a 401 and no ticket header is no verdict (None).
    async def authenticate(request):
        ticket = request.headers.get(TICKET)
        if ticket is None:
            return None
        if ticket != "valid-ticket":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        request.state.native_http_principal = {"profile_id": home, "purpose": "native-http"}
        return None
    monkeypatch.setattr(native_http, "authenticate_native_http", authenticate)
    return TestClient(web_server.app, base_url=LOOPBACK)


def test_uncredentialed_status_is_the_allowlisted_liveness_body(gateway_listener):
    response = gateway_listener.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ALLOWED
    assert body["auth_required"] is False and body["gateway_running"] in (True, False)
    assert body["overall"] in ("ok", "degraded")
    assert isinstance(body["gateway_state"], (str, type(None)))
    assert [key for key in NEVER_UNAUTHENTICATED if key in body] == []
    # Same gate as every other route on this listener.
    assert gateway_listener.get("/api/sessions").status_code == 401


def test_uncredentialed_status_ignores_profile_so_it_is_no_profile_oracle(gateway_listener):
    plain = gateway_listener.get("/api/status").json()
    for query in ("?profile=coder", "?profile=nope", "?profile=current"):
        response = gateway_listener.get("/api/status" + query)
        assert response.status_code == 200
        assert set(response.json()) == set(plain) == ALLOWED


@pytest.mark.parametrize("credential", ["native_ticket", "session_token"])
def test_credentialed_status_keeps_the_full_body(gateway_listener, credential):
    headers = ({TICKET: "valid-ticket"} if credential == "native_ticket"
               else {"X-Hermes-Session-Token": web_server._SESSION_TOKEN})
    body = gateway_listener.get("/api/status", headers=headers).json()
    for key in ("version", "release_date", "install_id", "profiles", "active_sessions",
                "hermes_home", "config_path", "env_path", "gateway_pid", "gateways"):
        assert key in body, key
    assert body["profiles"] == ["default", "coder"]


@pytest.mark.parametrize("headers", [
    {"X-Hermes-Session-Token": "forged"},
    {"Authorization": "Bearer forged"},
])
def test_a_credential_the_listener_refuses_reads_as_uncredentialed(gateway_listener, headers):
    assert set(gateway_listener.get("/api/status", headers=headers).json()) == ALLOWED


def test_a_forged_ticket_is_refused_before_the_route(gateway_listener):
    assert gateway_listener.get("/api/status", headers={TICKET: "forged"}).status_code == 401


def test_hosted_sign_in_on_the_listener_trims_a_signed_out_caller_too(gateway_listener, monkeypatch):
    """``dashboard.public_url`` engages sign-in on this listener: the decision still follows the
    caller's credential, not the gate flag, and the advertised auth shape stays truthful."""
    import plugins.dashboard_auth.basic as basic_plugin
    from hermes_cli.dashboard_auth import clear_providers, register_provider

    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    clear_providers()
    register_provider(basic_plugin.BasicAuthProvider(
        username="admin", password_hash=basic_plugin.hash_password("hunter2"), secret=b"s" * 32))
    try:
        body = gateway_listener.get("/api/status").json()
        assert set(body) == ALLOWED
        assert body["auth_required"] is True and body["auth_providers"] == ["basic"]
        assert "cookie" in body["auth_flows"]
        full = gateway_listener.get("/api/status", headers={TICKET: "valid-ticket"}).json()
        assert full["install_id"] and full["profiles"] == ["default", "coder"]
    finally:
        clear_providers()


def test_gated_listener_with_a_verified_session_gets_the_full_body(gateway_listener, monkeypatch):
    """Sign-in on this listener: a session the gate would accept is a credential (full body)."""
    from hermes_cli.dashboard_auth import middleware
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(middleware, "peek_session", lambda request: types.SimpleNamespace(user_id="u"))
    body = gateway_listener.get("/api/status").json()
    assert body["install_id"] and body["profiles"] == ["default", "coder"] and "version" in body


def _session_check_fault(monkeypatch):
    """Sign-in on this listener, and the session check raises something it does not expect."""
    from hermes_cli.dashboard_auth import middleware

    def fault(request):
        raise RuntimeError("session store unavailable")
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(middleware, "peek_session", fault)
    return TestClient(web_server.app, base_url=LOOPBACK, raise_server_exceptions=False)


def test_a_session_check_fault_reads_as_uncredentialed_not_a_500(gateway_listener, monkeypatch):
    client = _session_check_fault(monkeypatch)
    response = client.get("/api/status")
    assert response.status_code == 200
    assert set(response.json()) == ALLOWED
    assert "session store unavailable" not in response.text
    full = client.get("/api/status", headers={TICKET: "valid-ticket"})
    assert full.status_code == 200 and full.json()["install_id"]


def test_dashboard_surface_body_is_unchanged(gateway_listener, monkeypatch):
    """No runner: the standalone ``hermes dashboard`` keeps main's loopback body for every caller."""
    monkeypatch.setattr(web_server.app.state, "gateway_runner", None, raising=False)
    body = gateway_listener.get("/api/status").json()
    for key in ("version", "install_id", "profiles", "hermes_home", "config_path", "gateway_pid"):
        assert key in body, key
