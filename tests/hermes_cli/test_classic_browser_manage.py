"""CLI-9: the classic CLI's /browser goes to the gateway (``browser.manage``), like Ink and Desktop.

Browser tools run in the gateway, so a ``/browser connect`` that only rewrites the CLI process's
``BROWSER_CDP_URL`` changes nothing the agent uses. ``connect``/``disconnect`` follow Desktop's rule
(local gateway only); on a gateway serving several profiles they are refused by name, because the
live CDP endpoint is process-wide and must never reach another profile's sessions.
"""
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from cli import HermesCLI


class _Conn:
    closed = False
    final = False

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        reply = self.replies.get(method, {})
        return reply(params) if callable(reply) else reply


@pytest.fixture
def gateway_cli(monkeypatch):
    def make(**replies):
        conn = _Conn(replies)
        monkeypatch.setattr("hermes_cli.gateway_side.connection_for", lambda cli: conn)
        cli = HermesCLI.__new__(HermesCLI)
        cli.session_id = "local-s"
        cli.agent = SimpleNamespace(_checkpoint_mgr=None)
        return cli, conn
    return make


def _browser_calls(conn):
    return [params for method, params in conn.calls if method == "browser.manage"]


def test_classic_browser_connect_is_the_gateways_browser_manage(gateway_cli, monkeypatch, capsys):
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    cli, conn = gateway_cli(**{"browser.manage": lambda p: (
        {"connected": True, "url": p.get("url"), "messages": ["Chrome is already listening"]}
        if p["action"] == "connect" else {"connected": True, "url": "http://127.0.0.1:9333", "browser_use": False})})
    assert cli.process_command("/browser connect http://127.0.0.1:9333") is True
    assert _browser_calls(conn) == [{"action": "connect", "session_id": "local-s", "url": "http://127.0.0.1:9333"}]
    out = capsys.readouterr().out
    assert "Chrome is already listening" in out and "http://127.0.0.1:9333" in out
    import os
    assert "BROWSER_CDP_URL" not in os.environ  # the CLI process is not where the tools run
    cli.process_command("/browser status")
    assert _browser_calls(conn)[-1] == {"action": "status", "session_id": "local-s"}
    assert "http://127.0.0.1:9333" in capsys.readouterr().out
    cli.process_command("/browser disconnect")
    assert _browser_calls(conn)[-1] == {"action": "disconnect", "session_id": "local-s"}


def test_classic_browser_use_goes_to_the_gateway(gateway_cli, monkeypatch):
    cli, conn = gateway_cli(**{"browser.manage": {"connected": False, "browser_use": True}})
    monkeypatch.setattr(HermesCLI, "new_session", lambda self, *a, **k: None, raising=False)
    cli.process_command("/browser use off")
    assert _browser_calls(conn) == [{"action": "use", "session_id": "local-s", "enabled": False}]


def test_classic_browser_connect_against_a_remote_gateway_is_refused(gateway_cli, monkeypatch, capsys):
    """Desktop's rule: connect/disconnect manage a browser on the gateway HOST."""
    monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", "wss://gateway.example.com/api/ws")
    cli, conn = gateway_cli(**{"browser.manage": {"connected": False}})
    for line in ("/browser connect", "/browser disconnect"):
        cli.process_command(line)
    assert _browser_calls(conn) == []
    assert "local gateway" in capsys.readouterr().out
    cli.process_command("/browser status")  # status is right on any backend
    assert _browser_calls(conn) == [{"action": "status", "session_id": "local-s"}]


# --- the gateway side: browser.manage on a multiplexed owner -------------------------------------

class _Cdp(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from tui_gateway import server
    launch = tmp_path / "root"
    bee = launch / "profiles" / "bee"
    bee.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    import tools.browser_tool_lifecycle as lifecycle
    reaps = []
    monkeypatch.setattr(lifecycle, "cleanup_all_browsers", lambda: reaps.append(1))
    cdp = ThreadingHTTPServer(("127.0.0.1", 0), _Cdp)
    threading.Thread(target=cdp.serve_forever, daemon=True).start()
    yield server, launch, bee, f"http://127.0.0.1:{cdp.server_port}", reaps
    cdp.shutdown()


def _runner_serving(*names):
    authorities = [types.SimpleNamespace(profile_id=name, retiring=False) for name in names]
    return types.SimpleNamespace(session_authorities=types.SimpleNamespace(serving=lambda: authorities))


def _call(server, profile_id, params):
    from gateway.runtime_bootstrap import _PURPOSE_CAPABILITIES
    from tui_gateway.ws_legacy_fallback import dispatch_legacy
    actor = types.SimpleNamespace(profile_id=str(profile_id),
                                  capabilities=frozenset(_PURPOSE_CAPABILITIES["interactive"]))
    written, done = [], threading.Event()
    transport = types.SimpleNamespace(write=lambda frame: written.append(frame) or done.set() or True)
    reply = dispatch_legacy(server, {"jsonrpc": "2.0", "id": 1, "method": "browser.manage", "params": params},
                            transport, actor)
    return reply if reply is not None else (done.wait(10) and written[0])


def test_a_browser_one_profile_connects_never_reaches_another_profiles_session(homes, monkeypatch):
    """Multiplexed owner: connect/disconnect from ANY profile are refused by name, so the
    process-wide CDP endpoint never changes under another profile's sessions; status still answers."""
    import os
    from gateway import run_api
    server, launch, bee, url, reaps = homes
    monkeypatch.setattr(run_api, "_ACTIVE_RUNNER", _runner_serving("default", "bee"))
    for home in (launch, bee):
        for action in ("connect", "disconnect"):
            reply = _call(server, home, {"action": action, "url": url})
            assert reply["error"]["code"] == 4030, reply
            assert "profile" in reply["error"]["message"], reply
    assert "BROWSER_CDP_URL" not in os.environ and reaps == []
    status = _call(server, bee, {"action": "status"})
    assert status["result"]["connected"] is False, status


def test_single_profile_local_owner_connects_the_gateways_browser(homes, monkeypatch):
    """One profile: the gateway's browser tools are that profile's alone, so connect/disconnect run
    (what Ink, Desktop and now the classic CLI send)."""
    import os
    from gateway import run_api
    server, launch, _bee, url, reaps = homes
    # Multiplex mode is the default even with one profile: what counts is how many are SERVED.
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    monkeypatch.setattr(run_api, "_ACTIVE_RUNNER", _runner_serving("default"))
    reply = _call(server, launch, {"action": "connect", "url": url})
    assert reply["result"]["connected"] is True, reply
    assert os.environ["BROWSER_CDP_URL"] == url and reaps
    assert _call(server, launch, {"action": "status"})["result"]["url"] == url
    assert _call(server, launch, {"action": "disconnect"})["result"]["connected"] is False
    assert "BROWSER_CDP_URL" not in os.environ
