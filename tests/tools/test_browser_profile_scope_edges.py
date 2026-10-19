"""GW-23 (#134889): per-profile browser scope edges left after CLI-9.

A multiplexed gateway serves every profile from one process; a live ``/browser connect`` endpoint,
a harness daemon's runtime and tmp dirs, and a browser session belong to ONE profile.
"""
import threading

import pytest

from agent import secret_scope
from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home, hermes_home_key
from tools import browser_tool_cdp as bt_cdp


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(bt_cdp, "_live_overrides", {})
    return a, b


def _in_bare_thread(fn):
    """Run ``fn`` on a stdlib thread started WITHOUT the caller's copied context."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()))
    t.start()
    t.join(5)
    return out["v"]


def test_a_bare_thread_on_a_multiplexed_gateway_never_resolves_the_launch_profiles_override(profiles, monkeypatch):
    """No profile scope bound on a multiplexed gateway: the launch profile's live endpoint is NOT the
    answer (``get_secret`` fails closed the same way). A bound scope still sees its own."""
    launch = get_hermes_home()
    with _profile_runtime_scope(launch, hydrate_secrets=False):
        bt_cdp.set_live_cdp_override("http://127.0.0.1:9401")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    assert _in_bare_thread(bt_cdp.live_cdp_override) == ""
    assert _in_bare_thread(bt_cdp._get_cdp_override_raw) == ""
    with _profile_runtime_scope(launch, hydrate_secrets=False):
        assert bt_cdp.live_cdp_override() == "http://127.0.0.1:9401"


def test_single_profile_bare_thread_keeps_the_process_override(profiles):
    bt_cdp.set_live_cdp_override("http://127.0.0.1:9402")
    assert _in_bare_thread(bt_cdp.live_cdp_override) == "http://127.0.0.1:9402"
    assert hermes_home_key() in bt_cdp._live_overrides
