"""Copilot's in-app device-code login hooks (``plugins/model-providers/copilot/setup.py``) against a
local fake of GitHub's device endpoints: the codes, the RFC 8628 poll answers and where the token lands."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

import providers


class _FakeGitHub(BaseHTTPRequestHandler):
    replies: list[dict] = []
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        type(self).seen.append({"path": self.path, **{k: v[0] for k, v in fields.items()}})
        body = json.dumps(type(self).replies.pop(0)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def github(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeGitHub.replies, _FakeGitHub.seen = [], []
    profile = providers.get_provider_profile("copilot")
    setup = sys.modules[f"{type(profile).__module__}.setup"]
    monkeypatch.setattr(setup, "_DEVICE_LOGIN_BASE", f"http://127.0.0.1:{server.server_port}")
    yield profile, _FakeGitHub
    server.shutdown()


def test_start_and_poll_follow_the_terminal_flow(github):
    profile, fake = github
    fake.replies = [
        {"device_code": "dev-1", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
         "expires_in": 900, "interval": 5},
        {"error": "authorization_pending"},
        {"error": "slow_down", "interval": 12},
        {"error": "slow_down"},
        {"access_token": "gho_fake"},
    ]
    login = profile.start_device_login()
    assert {k: login[k] for k in ("user_code", "verification_url", "expires_in", "interval")} == {
        "user_code": "ABCD-1234", "verification_url": "https://github.com/login/device",
        "expires_in": 900, "interval": 8}   # GitHub's interval + the terminal flow's 3 s safety margin
    state = login["state"]
    assert profile.poll_device_login(state) == {"status": "pending"}
    slowed = profile.poll_device_login(state)
    assert (slowed["status"], slowed["interval"]) == ("pending", 15)   # server-supplied interval honoured
    state = slowed["state"]
    assert profile.poll_device_login(state)["interval"] == 20          # +5 s without one (RFC 8628)
    assert profile.poll_device_login(state) == {"status": "approved", "credential": "gho_fake"}
    assert [s["path"] for s in fake.seen] == ["/login/device/code"] + ["/login/oauth/access_token"] * 4
    assert {s["device_code"] for s in fake.seen[1:]} == {"dev-1"}


@pytest.mark.parametrize(("error", "status"), [("expired_token", "expired"), ("access_denied", "denied"),
                                               ("unsupported_grant_type", "error")])
def test_terminal_poll_errors_end_the_login(github, error, status):
    profile, fake = github
    fake.replies = [{"error": error}]
    assert profile.poll_device_login({"device_code": "dev-1", "interval": 5})["status"] == status


def test_saved_token_is_what_the_card_reports(github, tmp_path):
    profile, _fake = github
    assert profile.account_status()["logged_in"] is False
    profile.save_device_login("gho_saved")
    assert "COPILOT_GITHUB_TOKEN=gho_saved" in (tmp_path / ".env").read_text()
    status = profile.account_status()
    assert (status["logged_in"], status["source"], status["source_label"]) == (True, "env_var", "COPILOT_GITHUB_TOKEN")
