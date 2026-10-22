"""Behind the sign-in gate, signing the Webapp out signs every browser out.

On a LAN or own-URL Webapp a browser signs in with the dashboard's sign-in (a password, or an OAuth
provider) and keeps that login in cookies. Each login on the Webapp is bound to the gateway's browser
credential, so ``hermes webapp --sign-out``, which replaces that credential, ends every one of them:
the login's requests get 401 and the sign-in view, the sockets and host terminals it opened close with
4401, tickets it minted open nothing, and its cookies replayed from anywhere get nothing. Signing in
again works. The standalone dashboard has no browser credential and keeps its logins as on main.
"""
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.hermes_cli.test_web_host_terminal_persistence import metadata
from tests.hermes_cli.test_webapp_sign_out_sockets import _close_code
from tests.hermes_cli.test_webapp_socket_tickets import _TOKEN, serve_webapp

WEBAPP = "http://hermes.lan:9120"
USER, PASSWORD = "owner", "throwaway-test-password"


@pytest.fixture
def gated(tmp_path, monkeypatch):
    """The Webapp behind the sign-in gate with a password and an OAuth provider registered."""
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit
    from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests
    from plugins.dashboard_auth.basic import BasicAuthProvider, hash_password
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    webapp = serve_webapp(tmp_path, monkeypatch)
    state = webapp.server.app.state
    monkeypatch.setattr(state, "bound_host", "hermes.lan")
    monkeypatch.setattr(state, "bound_port", 9120, raising=False)
    monkeypatch.setattr(state, "auth_required", True)
    _reset_for_tests()
    _reset_password_rate_limit()
    clear_providers()
    register_provider(BasicAuthProvider(username=USER, password_hash=hash_password(PASSWORD), secret=b"k" * 32))
    register_provider(StubAuthProvider())
    webapp.browser = lambda: TestClient(webapp.server.app, base_url=WEBAPP)
    try:
        yield webapp
    finally:
        clear_providers()
        _reset_for_tests()
        _reset_password_rate_limit()


def _sign_in_with_password(client):
    signed_in = client.post("/auth/password-login", json={"provider": "basic", "username": USER, "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text


def _sign_in_with_oauth(client):
    started = client.get("/auth/login?provider=stub", follow_redirects=False)
    state = started.headers["location"].split("state=")[1]
    assert client.get(f"/auth/callback?code=stub_code&state={state}", follow_redirects=False).status_code == 302


SIGN_INS = {"password": _sign_in_with_password, "oauth": _sign_in_with_oauth}


@pytest.fixture
def sign_out(gated, tmp_path):
    """Ask the gateway serving *gated* for ``webapp-sign-out`` over its control socket."""
    from gateway.control_socket import GatewayControlServer
    from gateway.webapp_listener import register_control_verbs

    home = tmp_path / ".hermes"
    runner = SimpleNamespace(
        session_control_server=GatewayControlServer(home),
        webapp_status={"state": "serving", "url": WEBAPP, "reason": None},
        session_api=SimpleNamespace(app=SimpleNamespace(web=gated.server)))
    register_control_verbs(runner, home)

    def ask():
        raw = json.dumps({"protocol": 1, "verb": "webapp-sign-out", "id": 1, "params": {}}).encode()
        answer = json.loads(runner.session_control_server.handle_request_line(raw, "same-user"))
        assert answer["ok"], answer
        assert gated.server._SESSION_TOKEN != _TOKEN
        return answer["result"]
    return ask


def _signed_out(response):
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"] in {"unauthenticated", "session_expired"} and body["login_url"].startswith("/login")


@pytest.mark.parametrize("sign_in", SIGN_INS)
def test_signing_out_ends_every_login_behind_the_gate(gated, sign_out, sign_in):
    browser = gated.browser()
    SIGN_INS[sign_in](browser)
    assert browser.get("/api/auth/me").status_code == 200
    sign_out()
    _signed_out(browser.get("/api/auth/me"))
    _signed_out(browser.post("/api/webapp/terminal-ticket"))
    _signed_out(browser.post("/api/auth/ws-ticket"))
    page = browser.get("/", follow_redirects=False)
    assert page.status_code == 302 and page.headers["location"].startswith("/login")


def test_old_login_cookies_replayed_after_sign_out_get_nothing(gated, sign_out):
    browser = gated.browser()
    _sign_in_with_password(browser)
    stolen = dict(browser.cookies.items())
    sign_out()
    replay = gated.browser()
    replay.cookies.update(stolen)
    _signed_out(replay.get("/api/auth/me"))


def test_signing_in_again_after_sign_out_works(gated, sign_out):
    browser = gated.browser()
    _sign_in_with_password(browser)
    sign_out()
    _signed_out(browser.get("/api/auth/me"))
    _sign_in_with_password(browser)
    assert browser.get("/api/auth/me").status_code == 200
    assert browser.post("/api/webapp/terminal-ticket").status_code == 200


def test_a_credential_replaced_while_the_gateway_was_down_signs_logins_out(gated, monkeypatch):
    """The CLI replaces the stored credential when no gateway is running; the next gateway serves
    the new one, and logins bound to the old one are signed out."""
    browser = gated.browser()
    _sign_in_with_password(browser)
    monkeypatch.setattr(gated.server, "_SESSION_TOKEN", "n" * 43)
    _signed_out(browser.get("/api/auth/me"))


def test_signing_out_closes_the_sockets_and_host_terminal_a_login_opened(gated, sign_out):
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket

    browser = gated.browser()
    _sign_in_with_password(browser)
    terminal = browser.post("/api/webapp/terminal-ticket").json()["ticket"]
    events = browser.post("/api/auth/ws-ticket").json()["ticket"]
    other = mint_ticket(user_id="someone-else", provider="stub")
    with TestClient(gated.host, base_url=WEBAPP) as shells, \
            shells.websocket_connect(f"ws://hermes.lan:9120/api/host-terminal?ticket={terminal}") as shell, \
            browser.websocket_connect(f"ws://hermes.lan:9120/api/events?channel=tab-1&ticket={events}") as stream, \
            browser.websocket_connect(f"ws://hermes.lan:9120/api/events?channel=kept&ticket={other}") as kept:
        metadata(shell)
        assert _close_code(shell, 0.2) is None and _close_code(stream, 0.2) is None
        result = sign_out()
        assert _close_code(shell) == 4401
        assert _close_code(stream) == 4401
        assert _close_code(kept, 0.5) is None
    assert result["connections_closed"] == 2
    assert result["sign_in_gate"] is True


def test_tickets_a_login_minted_before_sign_out_open_nothing_after(gated, sign_out):
    browser = gated.browser()
    _sign_in_with_password(browser)
    terminal = browser.post("/api/webapp/terminal-ticket").json()["ticket"]
    events = browser.post("/api/auth/ws-ticket").json()["ticket"]
    sign_out()
    with TestClient(gated.host, base_url=WEBAPP) as shells:
        for client, url in ((shells, f"ws://hermes.lan:9120/api/host-terminal?ticket={terminal}"),
                            (browser, f"ws://hermes.lan:9120/api/events?channel=tab-1&ticket={events}")):
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(url) as ws:
                    ws.receive_text()
            assert closed.value.code == 4401
    assert gated.shells == []


def test_the_standalone_dashboard_keeps_its_logins_as_on_main(gated, monkeypatch):
    monkeypatch.setattr(gated.server.app.state, "ui_surface", "dashboard")
    browser = gated.browser()
    _sign_in_with_password(browser)
    assert not any(name.endswith("hermes_webapp_signin") for name in browser.cookies.keys())
    monkeypatch.setattr(gated.server, "_SESSION_TOKEN", "n" * 43)
    assert browser.get("/api/auth/me").status_code == 200
