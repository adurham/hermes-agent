"""Copilot endpoint identity, owned by the copilot plugin's profile hooks.

The premium-request ``x-initiator: user`` header (issue #3040) goes only to the Copilot API and
GitHub Models hosts (the copilot profile's ``request_headers``), and host-based routing must match
URL *hostnames*, never substrings (``providers.profile_for_host``): a proxy path or a lookalike
domain that merely contains a Copilot host is not Copilot.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def copilot_profile():
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None
    return profile


def _marks_user_turn(profile, url: str) -> bool:
    return "x-initiator" in profile.request_headers(base_url=url, user_initiated=True)


def _is_copilot_host(url: str) -> bool:
    from providers import profile_for_host

    profile = profile_for_host(url)
    return profile is not None and profile.name == "copilot"


class TestInitiatorHosts:
    """The x-initiator hook targets GitHub Copilot endpoints only."""

    def test_standard_copilot_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://api.githubcopilot.com") is True

    def test_github_models_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://models.github.ai/inference") is True

    def test_openrouter_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://openrouter.ai/api/v1") is False


def test_copilot_host_identity_is_host_anchored():
    assert _is_copilot_host("https://api.githubcopilot.com/v1") is True
    assert _is_copilot_host("https://proxy.test/api.githubcopilot.com/v1") is False
    assert _is_copilot_host("https://models.github.ai/inference") is True
    assert _is_copilot_host("https://models.github.ai.evil.com/v1") is False
