"""Router efforts disk mirror: hydration on a cold process and the stale-copy refresh trigger.

A mirror past ``_DISK_TTL_SECONDS`` (or with an unparseable ``ts``) is still served — a stale
vocabulary beats none — but queues one background refresh; a fresh one queues nothing.
"""

import json
import sys
import time

import pytest

EFFORTS = {"grok-4.6": ["low", "high"], "gpt-4.1-mini": []}


@pytest.fixture
def router(monkeypatch, tmp_path):
    from providers import get_provider_profile

    profile = get_provider_profile("router")
    mod = sys.modules[type(profile).__module__]
    path = tmp_path / "router_catalog.json"
    warms = []
    monkeypatch.setattr(mod, "_disk_path", lambda: path)
    monkeypatch.setattr(mod, "_efforts_cache", None)
    monkeypatch.setattr(mod, "_disk_checked", False)
    monkeypatch.setattr(mod, "_warm_efforts_async", lambda: warms.append(1))

    def write(ts):
        path.write_text(json.dumps({"ts": ts, "efforts": EFFORTS}), encoding="utf-8")

    return profile, mod, write, warms


def test_unparseable_timestamp_ages_out_to_the_ttl(router):
    _, mod, write, _ = router
    write("not-a-timestamp")
    parsed, age = mod._load_disk()
    assert parsed == EFFORTS
    assert age == mod._DISK_TTL_SECONDS


def test_unparseable_timestamp_serves_the_mirror_and_queues_a_refresh(router):
    profile, _, write, warms = router
    write("not-a-timestamp")
    assert profile.supported_reasoning_efforts("grok-4.6") == ("low", "high")
    assert warms == [1]


def test_stale_mirror_is_served_and_queues_a_refresh(router):
    profile, mod, write, warms = router
    write(time.time() - mod._DISK_TTL_SECONDS - 60)
    assert profile.supported_reasoning_efforts("gpt-4.1-mini") == ()
    assert warms == [1]


def test_fresh_mirror_is_served_without_a_refresh(router):
    profile, _, write, warms = router
    write(time.time() - 60)
    assert profile.supported_reasoning_efforts("grok-4.6") == ("low", "high")
    assert warms == []
