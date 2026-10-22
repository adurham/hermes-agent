"""Remote Webapp access (``hermes_cli.webapp_access``, ``/api/webapp-access/*``).

Contracts: Web Access writes only the ``webapp:`` block (the own-URL mode's URL in
``webapp.public_url``), LAN access registers the LAN callback, and nothing is written or restarted
without a Nous account to sign in with.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from hermes_cli import web_server

    c = TestClient(web_server.app, raise_server_exceptions=False)
    c.headers["Authorization"] = f"Bearer {web_server._SESSION_TOKEN}"
    return c


@pytest.fixture()
def restarts(monkeypatch):
    """Capture the gateway restart that applies the config instead of spawning it."""
    import hermes_cli.web_server_gateway as gw

    calls: list[str] = []

    def _restart(profile, *, what, label):
        calls.append(what)
        return {"restart_started": True}

    monkeypatch.setattr(gw, "_restart_gateway_after", _restart)
    return calls


@pytest.fixture()
def nous(monkeypatch):
    """A signed-in Nous account; portal registration is captured (no network)."""
    from hermes_cli import webapp_access

    callbacks: list[str] = []
    monkeypatch.setattr(webapp_access, "nous_signed_in", lambda: True)
    monkeypatch.setattr(webapp_access, "_register_nous_sign_in", callbacks.append)
    monkeypatch.setattr(webapp_access, "lan_address", lambda: "192.168.1.50")
    return callbacks


def _config() -> dict:
    from hermes_cli.config import load_config

    return load_config()


def _dashboard_setup() -> tuple[object, object]:
    """The standalone dashboard's own public URL, in config.yaml and in .env, as Web Access found it."""
    from hermes_cli.config import load_env, read_raw_config

    return read_raw_config().get("dashboard", {}).get("public_url"), load_env().get("HERMES_DASHBOARD_PUBLIC_URL")


def test_web_access_keeps_its_url_in_the_webapp_block_and_leaves_the_dashboard_alone(client, restarts, nous):
    """Start and stop write only ``webapp.*``: the own-URL mode pins ``webapp.public_url``, LAN mode
    drops it (its callback follows each request), and the standalone dashboard's
    ``dashboard.public_url`` and ``.env`` pin, which a tunnelled dashboard needs after a restart,
    stay exactly as the user set them."""
    from hermes_cli.config import save_config, save_env_value

    save_config({**_config(), "dashboard": {"public_url": "https://dashboard.example.test"}})
    save_env_value("HERMES_DASHBOARD_PUBLIC_URL", "https://dashboard.example.test")
    dashboard = _dashboard_setup()

    response = client.post("/api/webapp-access/start", json={"mode": "public", "public_url": "https://hermes.example.test"})
    assert response.status_code == 200, response.text
    assert nous == ["https://hermes.example.test/auth/callback"]
    assert {key: _config()["webapp"].get(key) for key in ("enabled", "host", "public_url")} == {
        "enabled": True, "host": "127.0.0.1", "public_url": "https://hermes.example.test"}
    assert response.json()["public_url"] == "https://hermes.example.test"
    assert _dashboard_setup() == dashboard

    assert client.post("/api/webapp-access/stop").status_code == 200
    assert _config()["webapp"]["enabled"] is False
    assert _dashboard_setup() == dashboard

    assert client.post("/api/webapp-access/start", json={"mode": "lan"}).status_code == 200
    assert nous[-1] == "http://192.168.1.50:9120/auth/callback"
    assert _config()["webapp"]["host"] == "0.0.0.0"
    assert not _config()["webapp"].get("public_url")
    assert _dashboard_setup() == dashboard
    assert restarts == ["turning on Web Access", "turning off Web Access", "turning on Web Access"]


def test_start_without_a_nous_account_writes_and_restarts_nothing(client, restarts, nous, monkeypatch):
    from hermes_cli import webapp_access

    monkeypatch.setattr(webapp_access, "nous_signed_in", lambda: False)

    assert client.post("/api/webapp-access/start", json={"mode": "lan"}).status_code == 400
    assert nous == []
    assert restarts == []
    assert not _config().get("webapp", {}).get("enabled")


@pytest.mark.parametrize(("webapp", "mode"), [
    ({"enabled": True}, "local"),
    ({"enabled": True, "host": "::1"}, "local"),
    ({"enabled": True, "host": "0.0.0.0"}, "lan"),
    ({"enabled": True, "public_url": "https://hermes.example.test"}, "public"),
    ({"enabled": False, "host": "0.0.0.0", "public_url": "https://hermes.example.test"}, None),
])
def test_status_names_the_mode_the_config_serves(client, webapp, mode):
    """The pane's mode follows the config: the documented loopback setup (``webapp.enabled: true``)
    serves this computer only, a wildcard host serves the network, and ``webapp.public_url`` serves
    the user's own URL. A loopback Webapp is never shown as "My own URL" with no address."""
    from hermes_cli.config import save_config

    save_config({**_config(), "webapp": webapp})

    status = client.get("/api/webapp-access/status")
    assert status.status_code == 200, status.text
    assert status.json()["mode"] == mode


def test_start_keeps_a_custom_port_and_builds_the_addresses_on_it(client, restarts, nous):
    """A ``webapp.port`` the user chose (a second gateway on this machine needs its own) survives
    every start and stop, and the status and the LAN callback use it, even while Web Access is off."""
    from hermes_cli.config import save_config

    save_config({**_config(), "webapp": {"enabled": False, "port": 9333}})
    assert client.get("/api/webapp-access/status").json()["port"] == 9333

    assert client.post("/api/webapp-access/start", json={"mode": "lan"}).status_code == 200
    assert nous == ["http://192.168.1.50:9333/auth/callback"]
    assert _config()["webapp"]["port"] == 9333

    assert client.post("/api/webapp-access/stop").status_code == 200
    response = client.post("/api/webapp-access/start", json={"mode": "public", "public_url": "https://hermes.example.test"})
    assert response.status_code == 200, response.text
    assert _config()["webapp"]["port"] == 9333
    assert response.json()["port"] == 9333
