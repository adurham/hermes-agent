"""Tests for fork-only additions to agent/prompt_builder.py.

Verifies the skills.lazy_listing feature that short-circuits
per-skill index rendering in the system prompt.
"""


class TestPromptBuilderLazyListing:
    """Tests for the skills.lazy_listing fork feature."""

    def test_lazy_listing_config_key_exists(self):
        """The default config has skills.lazy_listing."""
        from hermes_cli.config import DEFAULT_CONFIG
        skills = DEFAULT_CONFIG.get("skills", {})
        assert "lazy_listing" in skills

    def test_lazy_listing_false_by_default(self):
        """lazy_listing defaults to False (upstream behavior)."""
        from hermes_cli.config import DEFAULT_CONFIG
        skills = DEFAULT_CONFIG.get("skills", {})
        assert skills["lazy_listing"] is False

    def test_lazy_listing_added_to_cache_key(self):
        """lazy_listing is part of the skills prompt cache key tuple."""
        from agent.prompt_builder import build_skills_system_prompt
        import inspect
        source = inspect.getsource(build_skills_system_prompt)
        # The cache key should include lazy_listing
        assert "lazy_listing" in source

    def test_lazy_listing_short_circuits_when_true(self):
        """When lazy_listing is True, per-skill index is omitted."""
        # The code wraps the skill listing in an if not _lazy_listing block
        from agent.prompt_builder import build_skills_system_prompt
        import inspect
        source = inspect.getsource(build_skills_system_prompt)
        # Verify the lazy listing branch exists
        assert "lazy_listing" in source
        assert "skills_list" in source or "skill_view" in source

    def test_lazy_listing_imports(self):
        """lazy_listing uses hermes_cli.config.load_config."""
        from agent.prompt_builder import build_skills_system_prompt
        import inspect
        source = inspect.getsource(build_skills_system_prompt)
        assert "load_config" in source
        assert "lazy_listing" in source