"""CLI-9 (#134889): a live ``/browser connect`` belongs to ONE profile on a multiplexed gateway.

The override map is keyed by ``hermes_home_key()``; browser sessions and CDP supervisors remember
the profile that created them, so reaping one profile's browsers never closes another's.
"""
import types

import pytest

from gateway.run import _profile_runtime_scope
from hermes_constants import hermes_home_key
from tools import browser_tool, browser_tool_cdp as bt_cdp, browser_tool_lifecycle as lifecycle
from tools import browser_tool_session as bt_session
from tools.browser_supervisor import SUPERVISOR_REGISTRY


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "config.yaml").write_text("browser:\n  cdp_url: ws://b.invalid/devtools/browser/PROFILE-B\n")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(bt_cdp, "_live_overrides", {})
    return a, b


def test_a_live_override_is_this_profiles_alone(profiles, monkeypatch):
    a, b = profiles
    with _profile_runtime_scope(a, hydrate_secrets=False):
        bt_cdp.set_live_cdp_override("http://127.0.0.1:9333")
        assert bt_cdp._get_cdp_override_raw() == "http://127.0.0.1:9333"
    with _profile_runtime_scope(b, hydrate_secrets=False):
        assert bt_cdp.live_cdp_override() == ""
        assert bt_cdp._get_cdp_override_raw().endswith("/PROFILE-B")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://operator:9222")  # an operator's env still applies
    with _profile_runtime_scope(b, hydrate_secrets=False):
        assert bt_cdp._get_cdp_override_raw() == "http://operator:9222"
    with _profile_runtime_scope(a, hydrate_secrets=False):
        assert bt_cdp._get_cdp_override_raw() == "http://127.0.0.1:9333"  # the live connect wins
        bt_cdp.set_live_cdp_override(None)
        assert bt_cdp._get_cdp_override_raw() == "http://operator:9222"


def test_a_new_session_records_its_profile(profiles, monkeypatch):
    a, _b = profiles
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(lifecycle, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bt_session, "_create_session_for_key", lambda key, local: {"session_name": "s"})
    monkeypatch.setattr(bt_cdp, "_ensure_cdp_supervisor", lambda key: None)
    with _profile_runtime_scope(a, hydrate_secrets=False):
        info = bt_session._get_session_info("task-a")
    assert info["home_key"] == hermes_home_key(a)


def test_reaping_one_profile_leaves_the_other_profiles_browser_and_supervisor(profiles, monkeypatch):
    a, b = profiles
    key_a, key_b = hermes_home_key(a), hermes_home_key(b)
    sessions = {"ta": {"home_key": key_a}, "ta::local": {"home_key": key_a}, "tb": {"home_key": key_b}}
    monkeypatch.setattr(browser_tool, "_active_sessions", sessions)
    closed = []

    def close(task_id):
        closed.append(task_id)
        sessions.pop(task_id, None)
    monkeypatch.setattr(lifecycle, "_cleanup_single_browser_session", close)
    stopped = []
    sup = lambda name: types.SimpleNamespace(stop=lambda: stopped.append(name))
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "_by_task", {"ta": sup("ta"), "vault-a": sup("vault-a"), "tb": sup("tb")})
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "_home_by_task", {"ta": key_a, "vault-a": key_a, "tb": key_b})
    lifecycle.cleanup_profile_browsers(key_a)
    assert sorted(closed) == ["ta", "ta::local"] and list(sessions) == ["tb"]
    assert sorted(stopped) == ["ta", "vault-a"] and list(SUPERVISOR_REGISTRY._by_task) == ["tb"]
