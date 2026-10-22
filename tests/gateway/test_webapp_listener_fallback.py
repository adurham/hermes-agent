"""The gateway keeps a listener for native clients whatever happens to the Webapp's.

When the Webapp's listener cannot start, even after its dashboard mount is up (another process won the
race to listen on the port), the gateway comes up on main's private loopback listener instead: native
Desktop, CLI and TUI clients still connect, the mount carries none of the Webapp's state, and
``hermes webapp --status`` says why the Webapp is not served. Each case runs a real gateway listener
in a child process (``fixtures/webapp_fallback_peer.py``) with its own ``HERMES_HOME``.
"""
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run(tmp_path, spec):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / "state", tmp_path / "user"
    for path in (home, user):
        path.mkdir()
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED="1")
    result = subprocess.run(
        [sys.executable, str(root / "tests/gateway/fixtures/webapp_fallback_peer.py"), json.dumps(spec)],
        cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
    receipt = home / "receipt.json"
    assert result.returncode == 0, result.stdout + result.stderr + (receipt.read_text() if receipt.exists() else "")
    return json.loads(receipt.read_text())


@pytest.fixture(scope="module")
def lost_listen_race(tmp_path_factory):
    port = _free_port()
    spec = {"webapp": {"enabled": True, "port": port}, "fail_after_mount": True}
    return port, _run(tmp_path_factory.mktemp("lost-listen-race"), spec)


def test_a_webapp_listener_failing_after_its_mount_leaves_native_clients_a_listener(lost_listen_race):
    port, report = lost_listen_race
    assert report["start_error"] is None, report.get("start_traceback")
    assert report["api_origin"].startswith("http://127.0.0.1:")
    assert report["api_origin"] != f"http://127.0.0.1:{port}"
    assert report["native_session_list"] is True
    assert report["status"]["state"] == "unavailable"
    assert f"127.0.0.1:{port}" in report["status"]["reason"]


def test_the_private_listener_after_a_failed_webapp_mount_is_mains(lost_listen_race):
    # No Webapp surface, no Webapp bundle and not the browser's credential: what main's listener has.
    _, report = lost_listen_race
    assert report["state"]["mounted"] is True
    assert report["state"]["surface"] == "<absent>"
    assert report["state"]["web_dist_set"] is False
    assert report["state"]["session_token_is_browser_credential"] is False
    assert "WEBAPP_BUNDLE" not in report["index"]["body"]
