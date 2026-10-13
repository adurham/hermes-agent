"""The TUI's profile preferences (/statusbar /mouse /density /details /focus /battery /theme
/indicator) are written by the shared owner into the OWNING profile's config.yaml, like main's
per-TUI sidecar ``config.set`` — never refused, never inference."""
import asyncio

import hermes_yaml as yaml
import pytest

from tests.gateway.test_session_busy_controls import owner, admissions
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket


def _display(home):
    return yaml.safe_load((home / 'config.yaml').read_text()).get('display', {})


@pytest.mark.platforms("linux")
def test_display_preferences_theme_and_indicator_persist_to_the_profile(tmp_path):
    with owner(tmp_path) as (home, peer, desc):
        async def probe():
            async with websocket(home, desc) as ws:
                sid = (await rpc(ws, 'session.create', request_id='prefs', source='tui',
                                 toolsets=[]))['result']['session_id']
                # Main's TUI sends display preferences without a session (profile-wide).
                for key, value, reported in [
                    ('statusbar', 'off', 'off'), ('mouse', 'wheel', 'wheel'), ('density', 'on', 'on'),
                    ('battery', 'on', 'on'), ('details_mode', 'expanded', 'expanded'),
                    ('details_mode.tools', 'hidden', 'hidden'), ('theme', 'dark', 'dark'),
                    ('indicator', 'ascii', 'ascii'),
                ]:
                    result = await rpc(ws, 'config.set', key=key, value=value)
                    assert result.get('result') == {'key': key, 'value': reported}, (key, result)
                focus = await rpc(ws, 'config.set', key='focus', value='on', session_id=sid)
                assert focus.get('result') == {'key': 'focus', 'value': 'on', 'tool_progress': 'off'}, focus
                display = _display(home)
                assert display['tui_statusbar'] == 'off' and display['mouse_tracking'] == 'wheel'
                assert display['tui_compact'] is True and display['battery'] is True
                assert display['details_mode'] == 'expanded'
                assert display['sections'] == {'thinking': 'expanded', 'tools': 'hidden',
                                               'subagents': 'expanded', 'activity': 'expanded'}
                assert display['tui_theme'] == 'dark' and display['tui_status_indicator'] == 'ascii'
                assert display['focus_view'] is True and display['tool_progress'] == 'off'
                # /focus also pins this session's tool-progress mode, as main's session did.
                assert (await rpc(ws, 'config.get', key='verbose', session_id=sid))['result']['value'] == 'off'
                assert (await rpc(ws, 'config.get', key='theme'))['result']['value'] == 'dark'
                assert (await rpc(ws, 'config.get', key='indicator'))['result']['value'] == 'ascii'
                for params, reason in [
                    ({'key': 'theme', 'value': 'neon'}, 'invalid_params'),
                    ({'key': 'indicator', 'value': 'sparkles'}, 'invalid_params'),
                    ({'key': 'details_mode.nope', 'value': 'hidden'}, 'invalid_params'),
                    ({'key': 'approvals.mode', 'value': 'off'}, 'invalid_params'),
                    ({'key': 'theme', 'value': 'dark', 'profile': 'foreign'}, 'profile_mismatch'),
                    ({'key': 'model', 'value': 'x', 'session_id': sid}, 'use_session_mutation_model'),
                ]:
                    assert (await rpc(ws, 'config.set', **params)).get('error', {}).get('message') == reason, params
                assert _display(home)['tui_theme'] == 'dark'
                assert not admissions(home) and not peer.requests
        asyncio.run(probe())
