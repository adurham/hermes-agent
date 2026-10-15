"""Unified-runtime cron fires share one Codex prompt-cache scope per job (#139434)."""

import pytest

from agent.transports import get_transport


@pytest.fixture
def transport():
    import agent.transports.codex
    return get_transport("codex_responses")


def test_cache_key_stable_for_unified_cron_session_ids(transport):
    """A scheduler admission, not the opaque local physical id, scopes cron cache reuse."""
    from gateway.session_cron import _execution

    messages = [{"role": "user", "content": "Hi"}]

    def build(sid):
        return transport.build_kwargs(model="gpt-5.4", messages=messages, tools=[], session_id=sid)

    token = _execution.set((object(), "local-first-fire", "job42", "fire-1"))
    try:
        first = build("local-first-fire")
        _execution.set((object(), "local-second-fire", "job42", "fire-2"))
        second = build("local-second-fire")
        # A session that is not the bound admission (e.g. a delegated child) keeps its own scope.
        child = build("local-child-of-fire")
        _execution.set((object(), "local-other-job", "job99", "fire-3"))
        other = build("local-other-job")
    finally:
        _execution.reset(token)

    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["prompt_cache_key"] != other["prompt_cache_key"]
    assert child["prompt_cache_key"] != first["prompt_cache_key"]
