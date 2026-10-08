"""A native ticket's implicit selectors retain its secrets through nested config scopes."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_concurrent_ticket_profiles_bind_nested_config_secrets(tmp_path, monkeypatch):
    from agent.secret_scope import get_secret
    from hermes_cli.dashboard_auth.native_http import native_profile_scope
    from hermes_cli.web_server_profiles import _config_profile_scope
    from hermes_constants import get_hermes_home
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('SCOPE_SENTINEL', 'launch')
    monkeypatch.setenv('SCOPE_MISSING', 'launch-only')
    beta, gamma = home / 'profiles' / 'beta', home / 'profiles' / 'gamma'
    for profile in (beta, gamma):
        profile.mkdir(parents=True)
        (profile / '.env').write_text('SCOPE_SENTINEL=' + profile.name + '\n', encoding='utf-8')
    async def read(profile, selector):
        request = SimpleNamespace(state=SimpleNamespace(native_http_principal={'profile_id': str(profile)}))
        with native_profile_scope(request), _config_profile_scope(selector):
            await asyncio.sleep(0)
            assert get_hermes_home() == profile
            assert get_secret('SCOPE_SENTINEL') == profile.name
            assert get_secret('SCOPE_MISSING') is None
    await asyncio.gather(*(read(profile, selector) for profile in (beta, gamma) for selector in (None, '', 'current')))
