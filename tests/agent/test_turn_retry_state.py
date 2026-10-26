"""Provider detection used by the turn-retry credential-refresh gates."""

from __future__ import annotations

from types import SimpleNamespace


def test_copilot_provider_check_accepts_alias_spellings():
    """`/model` and profile configs can leave `github-copilot` / `github` as
    the provider spelling; the recovery gates must not silently skip them."""
    from agent.conversation_loop import _credential_refresh_profile

    def _owner(provider, base_url=""):
        profile = _credential_refresh_profile(SimpleNamespace(provider=provider, base_url=base_url))
        return profile.name if profile else None

    assert _owner("copilot") == "copilot"
    assert _owner("github-copilot") == "copilot"
    assert _owner("GitHub-Copilot") == "copilot"
    assert _owner("github") == "copilot"
    # URL fallback: unnormalized provider but a Copilot base URL.
    assert _owner("custom", "https://api.githubcopilot.com") == "copilot"
    assert _owner("openrouter", "https://openrouter.ai/api/v1") is None
