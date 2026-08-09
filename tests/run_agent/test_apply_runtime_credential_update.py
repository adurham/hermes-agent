"""AIAgent.apply_runtime_credential_update — in-place auth swap on a bare
(api_key, base_url) refresh, without a full agent rebuild.

Regression: HermesCLI._ensure_runtime_credentials() previously discarded and
rebuilt the whole AIAgent on ANY resolved api_key change, including a benign
same-provider OAuth token refresh (the common case with Claude Code's shared
~/.claude/.credentials.json — one concurrent `hermes` process refreshing
invalidates every other open session's cached token). That printed a
disruptive "Initializing agent..." and lost in-memory turn state on every
refresh. apply_runtime_credential_update() is the reusable primitive the
caller now calls instead, mirroring the existing pool-driven
_swap_credential() path but entered from a raw (api_key, base_url) pair
since this fires before a turn starts (no failing pool entry to hand in).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def test_chat_completions_swaps_client_kwargs_in_place():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom",
        model="shared-model",
        api_key="old-key",
        base_url="https://a.example/v1",
        _client_kwargs={
            "api_key": "old-key",
            "base_url": "https://a.example/v1",
        },
        _credential_pool=None,
        _apply_client_headers_for_base_url=MagicMock(),
        _replace_primary_openai_client=MagicMock(return_value=True),
    )

    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        result = AIAgent.apply_runtime_credential_update(
            agent, api_key="new-key", base_url="https://a.example/v1"
        )

    assert result is True
    assert agent.api_key == "new-key"
    assert agent._client_kwargs["api_key"] == "new-key"
    agent._replace_primary_openai_client.assert_called_once_with(
        reason="runtime_credential_update"
    )


def test_anthropic_messages_rebuilds_client_and_preserves_oauth_flag():
    fake_old_client = MagicMock()
    agent = SimpleNamespace(
        api_mode="anthropic_messages",
        provider="anthropic",
        model="claude-sonnet-5",
        api_key="old-token",
        base_url="https://api.anthropic.com",
        _anthropic_client=fake_old_client,
        _anthropic_api_key="old-token",
        _anthropic_base_url="https://api.anthropic.com",
        _credential_pool=None,
    )
    fake_new_client = MagicMock()

    with (
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=fake_new_client) as _build,
        patch("agent.anthropic_adapter._is_oauth_token", return_value=True),
        patch("run_agent.get_provider_request_timeout", return_value=600.0),
    ):
        result = AIAgent.apply_runtime_credential_update(
            agent, api_key="new-refreshed-token", base_url="https://api.anthropic.com"
        )

    assert result is True
    fake_old_client.close.assert_called_once()
    assert agent._anthropic_client is fake_new_client
    assert agent._anthropic_api_key == "new-refreshed-token"
    assert agent.api_key == "new-refreshed-token"
    assert agent._is_anthropic_oauth is True
    _build.assert_called_once()


def test_credential_pool_is_updated_when_supplied():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom",
        model="shared-model",
        api_key="old-key",
        base_url="https://a.example/v1",
        _client_kwargs={"api_key": "old-key", "base_url": "https://a.example/v1"},
        _credential_pool=None,
        _apply_client_headers_for_base_url=MagicMock(),
        _replace_primary_openai_client=MagicMock(return_value=True),
    )
    _new_pool = object()

    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        AIAgent.apply_runtime_credential_update(
            agent,
            api_key="new-key",
            base_url="https://a.example/v1",
            credential_pool=_new_pool,
        )

    assert agent._credential_pool is _new_pool


def test_returns_false_on_missing_or_invalid_base_url():
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom",
        api_key="old-key",
        base_url="https://a.example/v1",
        _credential_pool=None,
    )

    assert AIAgent.apply_runtime_credential_update(agent, api_key="new-key", base_url="") is False
    assert AIAgent.apply_runtime_credential_update(agent, api_key="new-key", base_url=None) is False


def test_returns_false_and_does_not_raise_when_replace_client_fails():
    """A client-rebuild failure inside the swap must surface as a clean
    False return (so the caller falls back to a full agent rebuild),
    never an uncaught exception that would crash the turn-start path.
    """
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="custom",
        model="shared-model",
        api_key="old-key",
        base_url="https://a.example/v1",
        _client_kwargs={"api_key": "old-key", "base_url": "https://a.example/v1"},
        _credential_pool=None,
        _apply_client_headers_for_base_url=MagicMock(),
        _replace_primary_openai_client=MagicMock(side_effect=RuntimeError("boom")),
    )

    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        result = AIAgent.apply_runtime_credential_update(
            agent, api_key="new-key", base_url="https://a.example/v1"
        )

    assert result is False
