"""A fallback resolved during gateway credential resolution (before any AIAgent exists) must carry a
user-visible notice through the agent's one-shot fallback-notice mechanism (#74349)."""
from unittest.mock import patch

import gateway.run as gateway_run
from hermes_cli.auth import AuthError


def test_credential_resolution_fallback_carries_notice():
    fb = {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "k", "base_url": "u"}
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AuthError("expired")), \
         patch("hermes_cli.runtime_provider._get_model_config",
               return_value={"provider": "openai-codex", "default": "gpt-5.6-sol"}), \
         patch.object(gateway_run, "_try_resolve_fallback_provider", return_value=dict(fb)):
        kwargs = gateway_run._resolve_runtime_agent_kwargs()
    notice = kwargs["_fallback_notice"]
    assert "openai-codex/gpt-5.6-sol" in notice and "anthropic/claude-sonnet-5" in notice
