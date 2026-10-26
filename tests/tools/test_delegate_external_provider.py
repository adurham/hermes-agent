"""A selected external-process provider must survive child command resolution."""
import os
import sys
from types import SimpleNamespace

import pytest

from providers.base import ProviderProfile
from tools.delegate_tool_config import _resolve_child_runtime


@pytest.mark.parametrize("external", [True, False])
def test_pinned_command_retains_selected_external_provider(external, process_profiles):
    profile = ProviderProfile(name="test-process-provider", display_name="Test", auth_type="external_process" if external else "api_key")
    fallback = ProviderProfile(name="fallback-acp", auth_type="external_process", process_command="agent-cli",
                               process_command_fallback=True)
    process_profiles(profile, fallback)
    parent = SimpleNamespace(model="test-model", provider=profile.name, base_url="process://test", api_mode="chat_completions", acp_args=[])
    result = _resolve_child_runtime(
        parent, {}, "external-process", model=None, override_provider=profile.name,
        override_base_url=None, override_api_key=None, override_api_mode=None,
        override_acp_command=sys.executable, override_acp_args=[],
    )
    assert result["provider"] == (profile.name if external else fallback.name)
    assert result["requested_provider"] == result["provider"]
    assert result["acp_command"] == sys.executable


def _resolve_pinned(command, provider="api-provider"):
    parent = SimpleNamespace(model="m", provider=provider, base_url="https://api.example/v1",
                             api_mode="chat_completions", acp_args=[])
    return _resolve_child_runtime(
        parent, {}, "key", model=None, override_provider=provider, override_base_url=None, override_api_key=None,
        override_api_mode=None, override_acp_command=command, override_acp_args=[],
    )


@pytest.fixture
def process_profiles(monkeypatch):
    """Swap the provider registry the resolver reads for a fixed set of profiles."""
    import providers

    def install(*profiles):
        by_name = {p.name: p for p in profiles}
        monkeypatch.setattr(providers, "get_provider_profile", by_name.get)
        monkeypatch.setattr(providers, "list_providers", lambda: list(profiles))
    return install


def test_pinned_command_resolves_the_profile_that_launches_that_binary(process_profiles):
    binary = os.path.basename(sys.executable)
    process_profiles(
        ProviderProfile(name="api-provider", auth_type="api_key"),
        ProviderProfile(name="generic-acp", auth_type="external_process", process_command="agent-cli",
                        process_command_fallback=True),
        ProviderProfile(name="harness-acp", auth_type="external_process", process_command=binary,
                        api_mode="chat_completions"),
    )
    result = _resolve_pinned(sys.executable)
    assert (result["provider"], result["api_mode"], result["acp_command"]) == (
        "harness-acp", "chat_completions", sys.executable)


def test_pinned_command_without_a_matching_or_fallback_profile_fails_loudly(process_profiles):
    process_profiles(
        ProviderProfile(name="api-provider", auth_type="api_key"),
        ProviderProfile(name="harness-acp", auth_type="external_process", process_command="some-other-cli"),
    )
    with pytest.raises(ValueError, match="does not belong to any installed external-process provider"):
        _resolve_pinned(sys.executable)
