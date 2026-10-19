"""/browser connect|disconnect through the real WS legacy fallback (dokterdok N25, #134889 CLI-9).

An owner serving ONE profile runs them: every chat on it is that profile's, and Ink, Desktop and the
classic CLI all send /browser here. An owner serving SEVERAL profiles refuses them by name, so a
browser one profile connects never reaches another profile's sessions; status still answers."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env

_OTHER_CHAT = 'http://127.0.0.1:9333'


@pytest.fixture(scope='module')
def receipt(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    tmp = tmp_path_factory.mktemp('browser-scope')
    home, user = tmp / 'state', tmp / 'user'
    home.mkdir()
    user.mkdir()
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED='1')
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/browser_scope_peer.py')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((home / 'receipt.json').read_text())


@pytest.mark.parametrize('action', ['connect', 'disconnect', 'status', 'missing'])
def test_several_served_profiles_keep_the_process_wide_cdp(receipt, action):
    row = receipt['two:' + action]
    assert row['cdp'] == _OTHER_CHAT and row['reaps'] == 0, row  # nothing process-wide changed
    if action in ('connect', 'disconnect'):
        assert row['code'] == 4030 and row['result'] is None, row  # refused in words, not -32601
        assert 'several profiles' in row['message'], row
    else:
        assert row['code'] is None and row['result']['url'] == _OTHER_CHAT, row


def test_one_served_profile_connects_and_disconnects_the_gateways_browser(receipt):
    connect = receipt['one:connect']
    assert connect['code'] is None and connect['result']['connected'] is True, connect
    assert connect['cdp'] == f"http://127.0.0.1:{connect['cdp_port']}" and connect['reaps'] > 0, connect
    disconnect = receipt['one:disconnect']
    assert disconnect['code'] is None and disconnect['cdp'] is None, disconnect
    assert receipt['one:status']['result']['connected'] is False
