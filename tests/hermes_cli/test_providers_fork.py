"""Tests for hermes_cli/providers.py — fork additions to provider aliases and labels.

Verifies the fork-added provider aliases (google-gemini-cli, google-antigravity)
and label overrides.
"""


class TestProvidersFork:
    """Tests for fork additions in hermes_cli/providers.py."""

    def test_gemini_cli_alias_resolves(self):
        """'gemini-cli' alias resolves to 'google-gemini-cli'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("gemini-cli") == "google-gemini-cli"

    def test_gemini_oauth_alias_resolves(self):
        """'gemini-oauth' alias resolves to 'google-gemini-cli'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("gemini-oauth") == "google-gemini-cli"

    def test_antigravity_alias_resolves(self):
        """'antigravity' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("antigravity") == "google-antigravity"

    def test_antigravity_oauth_alias_resolves(self):
        """'antigravity-oauth' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("antigravity-oauth") == "google-antigravity"

    def test_antigravity_cli_alias_resolves(self):
        """'antigravity-cli' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("antigravity-cli") == "google-antigravity"

    def test_agy_alias_resolves(self):
        """'agy' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("agy") == "google-antigravity"

    def test_agy_cli_alias_resolves(self):
        """'agy-cli' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("agy-cli") == "google-antigravity"

    def test_google_antigravity_alias_resolves(self):
        """'google-antigravity-oauth' alias resolves to 'google-antigravity'."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("google-antigravity-oauth") == "google-antigravity"

    def test_google_gemini_cli_label(self):
        """google-gemini-cli has a custom label override."""
        from hermes_cli.providers import get_label
        label = get_label("google-gemini-cli")
        assert label is not None
        assert "Google" in label

    def test_unknown_provider_passes_through(self):
        """normalize_provider returns the input unchanged for unknown providers."""
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("nonexistent-provider") == "nonexistent-provider"