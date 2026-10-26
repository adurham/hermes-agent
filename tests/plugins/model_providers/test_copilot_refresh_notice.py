"""Copilot's credential-refresh status lines keep main's wording (``ProviderProfile.credential_refresh_notice``)."""

import providers


def test_copilot_refresh_notices_match_the_pre_seam_text():
    profile = providers.get_provider_profile("copilot")
    assert profile.credential_refresh_notice(reason="unauthorized") == (
        "Copilot credentials refreshed after 401. Retrying request...")
    assert profile.credential_refresh_notice(reason="stale") == (
        "Copilot credential re-exchanged after model_not_available 400. Retrying request...")


def test_profiles_without_a_notice_get_the_generic_wording():
    from providers.base import ProviderProfile

    assert ProviderProfile(name="plain").credential_refresh_notice(reason="unauthorized") == ""
