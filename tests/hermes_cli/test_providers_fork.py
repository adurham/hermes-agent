"""Tests for hermes_cli/providers.py — fork-local provider-resolution behavior.

The fork's `google-gemini-cli` / `google-antigravity` alias + label coverage that
used to live here was removed with the Google Code Assist OAuth providers
themselves (see upstream `7130d60861`). What remains is the generic
unknown-provider passthrough contract, which is covered nowhere else.
"""


class TestProvidersFork:
    """Tests for fork additions in hermes_cli/providers.py."""

    def test_unknown_provider_passes_through(self):
        """normalize_provider returns the input unchanged for unknown providers."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("nonexistent-provider") == "nonexistent-provider"
