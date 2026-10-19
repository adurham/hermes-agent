"""``/reasoning show|hide`` and ``/footer`` on a local session take effect on every local surface (T7).

The gateway handler keeps writing the per-platform key (``display.platforms.cli.*``: LOCAL's config key,
``_platform_config_key``). Every local surface reads that key: Ink and Desktop read ``config.get full``'s
``display`` block, the classic CLI reads its display config at start, and the gateway's local turns
resolve the launch default. None of them may fall back to the global key the command never wrote.
"""
import asyncio
import json
import signal
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket


@pytest.mark.platforms("linux")  # the daemon is stopped with SIGINT, which raises on Windows
def test_reasoning_hide_and_footer_reach_the_local_clients_config_read(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user, work = tmp_path / 'state', tmp_path / 'user', tmp_path / 'work'
    home.mkdir(mode=0o700)
    user.mkdir()
    work.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'display-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        # An earlier per-platform footer setting: the one /footer must flip for this surface.
        'display': {'show_reasoning': True, 'platforms': {'cli': {'runtime_footer': {'enabled': True}}}},
    }))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')
    out = {}

    async def probe(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='display', source='tui', toolsets=[],
                                cwd=str(work))
            sid = created['result']['session_id']
            for command in ('reasoning hide', 'footer off'):
                out[command] = (await rpc(ws, 'slash.exec', session_id=sid, command=command))['result']
            full = await rpc(ws, 'config.get', session_id=sid, key='full')
            out['display'] = full['result']['config']['display']

    try:
        with daemon(root, home, env, barrier=False) as (proc, desc):
            asyncio.run(probe(desc))
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=20)
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)

    from hermes_cli.config import read_user_config_raw
    saved = read_user_config_raw(home / 'config.yaml')
    # The per-platform key is what the command wrote; no second, global key appears.
    assert saved['display']['platforms']['cli']['show_reasoning'] is False, saved
    assert saved['display']['show_reasoning'] is True, saved
    assert saved['display']['platforms']['cli']['runtime_footer']['enabled'] is False, saved
    assert 'runtime_footer' not in {k for k in saved['display'] if k != 'platforms'}, saved
    # The local clients' config read resolves the local key.
    assert out['display']['show_reasoning'] is False, out
    assert out['display']['runtime_footer']['enabled'] is False, out


def test_launch_default_reads_the_local_key():
    """An Ink/Desktop session with no session flag streams per ``display.platforms.cli.show_reasoning``."""
    from gateway.local_viewer_events import launch_shows_reasoning

    class _Policy:
        source = 'tui'

        def __init__(self, display):
            self._display = display

        def config(self, _authority, secrets=False):
            return {'display': self._display}

    assert launch_shows_reasoning(_Policy({'show_reasoning': True, 'platforms': {'cli': {'show_reasoning': False}}}),
                                  None) is False
    assert launch_shows_reasoning(_Policy({'show_reasoning': True}), None) is True
    assert launch_shows_reasoning(_Policy({'show_reasoning': False}), None) is False


def test_quoted_local_false_stays_off():
    """A quoted ``"false"`` under the local key is off on every surface, not a truthy string."""
    from gateway.display_config import local_display_config
    from gateway.local_viewer_events import launch_shows_reasoning

    display = {'show_reasoning': False, 'platforms': {'cli': {'show_reasoning': 'false'}}}

    class _Policy:
        source = 'tui'

        def config(self, _authority, secrets=False):
            return {'display': display}

    assert local_display_config(display)['show_reasoning'] is False
    assert launch_shows_reasoning(_Policy(), None) is False


def test_classic_cli_display_reads_the_local_key():
    from gateway.display_config import local_display_config

    display = local_display_config({'show_reasoning': True, 'runtime_footer': {'enabled': False},
                                    'platforms': {'cli': {'show_reasoning': False,
                                                          'runtime_footer': {'enabled': True}}}})
    assert display['show_reasoning'] is False
    assert display['runtime_footer']['enabled'] is True
    # No local key: the global value stands.
    assert local_display_config({'show_reasoning': True})['show_reasoning'] is True


def test_classic_cli_reasoning_hide_saves_the_local_key(monkeypatch):
    from hermes_cli import cli_commands_mixin as mixin

    saved = {}
    monkeypatch.setattr(mixin, '_save', lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(mixin, '_cp', lambda *a, **k: None)
    cli = object.__new__(mixin.CLICommandsMixin)
    cli.agent = None
    cli.show_reasoning = True
    cli._handle_reasoning_command('/reasoning hide')
    assert saved == {'display.platforms.cli.show_reasoning': False}
    assert cli.show_reasoning is False
