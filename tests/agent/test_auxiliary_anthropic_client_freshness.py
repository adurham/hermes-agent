"""Regression coverage for the Anthropic auxiliary-client cache staleness fix.

Root cause: agent.auxiliary_client._client_cache_key() hashes an ``api_key``
component into the cache key, but auxiliary Anthropic calls almost always
pass explicit_api_key=None (the common OAuth-via-Claude-Code path), so that
component is a constant "" and never varies when a *sibling* process
refreshes the shared ~/.claude/.credentials.json / macOS Keychain entry.
The cached AnthropicAuxiliaryClient then keeps using its stale baked-in
token until it gets a live 401 from Anthropic, wasting a guaranteed-fail
round-trip on every auxiliary call after every refresh.

The fix (_anthropic_cached_client_is_stale + its call site in
_get_cached_client) does a cheap, network-free pre-flight comparison of the
cached client's baked-in token against the currently-resolvable token, and
evicts+rebuilds BEFORE the request is issued rather than after a 401.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as aux


@pytest.fixture(autouse=True)
def _clean_aux_state():
    aux.shutdown_cached_clients()
    aux._anthropic_freshness_checked_at.clear()
    yield
    aux.shutdown_cached_clients()
    aux._anthropic_freshness_checked_at.clear()


def _fake_cached_client(token: str):
    return SimpleNamespace(api_key=token, close=MagicMock())


def test_stale_token_detected_and_evicted_before_request():
    """A cached client whose token no longer matches the resolvable token
    must be evicted so the NEXT call rebuilds with a fresh token, instead of
    reusing the stale one and eating a guaranteed 401."""
    cache_key = ("anthropic", False, "", "", "", (), False, "", None, "")
    stale_client = _fake_cached_client("old-token")
    aux._client_cache[cache_key] = (stale_client, "claude-sonnet-5", None)

    with patch.object(aux, "_select_pool_entry", return_value=(False, None)), \
         patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="new-token"):
        is_stale = aux._anthropic_cached_client_is_stale(cache_key, stale_client)

    assert is_stale is True


def test_matching_token_is_not_flagged_stale():
    """No refresh happened — the cached client's token still matches the
    resolvable token, so it must NOT be evicted (that would defeat caching
    entirely and rebuild a client on every single auxiliary call)."""
    cache_key = ("anthropic", False, "", "", "", (), False, "", None, "")
    fresh_client = _fake_cached_client("same-token")

    with patch.object(aux, "_select_pool_entry", return_value=(False, None)), \
         patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="same-token"):
        is_stale = aux._anthropic_cached_client_is_stale(cache_key, fresh_client)

    assert is_stale is False


def test_freshness_check_is_throttled_per_cache_key():
    """The check must not re-resolve the token (JSON file read / Keychain
    subprocess) on every single call for a hot auxiliary-call path
    (compression, approval checks) — only after the throttle interval."""
    cache_key = ("anthropic", False, "", "", "", (), False, "", None, "")
    client = _fake_cached_client("token-a")

    with patch.object(aux, "_select_pool_entry", return_value=(False, None)), \
         patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="token-b") as mock_resolve:
        first = aux._anthropic_cached_client_is_stale(cache_key, client)
        second = aux._anthropic_cached_client_is_stale(cache_key, client)

    assert first is True
    # Second call within the throttle window must short-circuit without
    # re-resolving the token.
    assert second is False
    assert mock_resolve.call_count == 1


def test_pool_backed_credentials_are_never_flagged_stale():
    """Pool-backed Anthropic credentials rotate through their own recovery
    path (mark_exhausted_and_rotate) — this check must defer to it entirely,
    never second-guessing a pool-selected entry."""
    cache_key = ("anthropic", False, "", "", "", (), False, "", "pool-hint", "")
    client = _fake_cached_client("pool-token")
    fake_entry = MagicMock()

    with patch.object(aux, "_select_pool_entry", return_value=(True, fake_entry)) as mock_select, \
         patch("agent.anthropic_adapter.resolve_anthropic_token") as mock_resolve:
        is_stale = aux._anthropic_cached_client_is_stale(cache_key, client)

    assert is_stale is False
    mock_select.assert_called_once()
    mock_resolve.assert_not_called()


def test_resolution_failure_never_blocks_cache_hit():
    """Any exception while resolving the current token must return False
    (not stale) — the reactive 401 path remains the correctness backstop,
    and a broken freshness check must never itself break normal caching."""
    cache_key = ("anthropic", False, "", "", "", (), False, "", None, "")
    client = _fake_cached_client("token")

    with patch.object(aux, "_select_pool_entry", side_effect=RuntimeError("boom")):
        is_stale = aux._anthropic_cached_client_is_stale(cache_key, client)

    assert is_stale is False


def test_get_cached_client_evicts_and_rebuilds_stale_anthropic_client():
    """End-to-end: _get_cached_client must not return a stale-token cached
    client — it should detect staleness, close+evict it, and build a fresh
    one instead of returning the guaranteed-to-401 cached entry."""
    stale_client = _fake_cached_client("old-token")
    fresh_client = _fake_cached_client("new-token")

    cache_key = aux._client_cache_key("anthropic", async_mode=False, model=None)
    aux._client_cache[cache_key] = (stale_client, "claude-sonnet-5", None)

    with patch.object(aux, "_select_pool_entry", return_value=(False, None)), \
         patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="new-token"), \
         patch("agent.auxiliary_client.resolve_provider_client", return_value=(fresh_client, "claude-sonnet-5")):
        client, model = aux._get_cached_client("anthropic")

    assert client is fresh_client
    stale_client.close.assert_called_once()
    assert aux._client_cache[cache_key][0] is fresh_client
