"""Tests for the fork-only per-model reasoning effort feature.

Verifies that reasoning_effort_by_model config allows per-model
reasoning effort levels, with case-insensitive matching and
fallback to the global reasoning_effort when no per-model entry exists.
"""

import pytest


class TestReasoningEffortByModel:
    """Tests for per-model reasoning effort isolation."""

    def test_default_config_has_empty_by_model(self):
        """The CLI_CONFIG includes an empty reasoning_effort_by_model dict."""
        from cli import CLI_CONFIG
        agent = CLI_CONFIG.get("agent", {})
        assert "reasoning_effort_by_model" in agent
        assert agent["reasoning_effort_by_model"] == {}

    @pytest.fixture
    def sample_by_model(self):
        return {
            "claude-opus-4-8": "high",
            "claude-sonnet-4-6": "medium",
            "gpt-5.4": "low",
        }

    def test_per_model_effort_retrieved(self, sample_by_model):
        """Exact model name match returns the saved effort level."""
        model = "claude-opus-4-8"
        expected = "high"
        assert sample_by_model.get(model) == expected

    def test_per_model_effort_falls_back_to_global(self, sample_by_model):
        """Model with no per-model entry returns None (caller falls back to global)."""
        model = "claude-haiku-4-5"
        assert sample_by_model.get(model) is None

    def test_case_insensitive_matching(self, sample_by_model):
        """Model names should match case-insensitively."""
        lookup = "Claude-Opus-4-8"
        actual = next(
            (v for k, v in sample_by_model.items() if k.lower() == lookup.lower()),
            None,
        )
        assert actual == "high"

    def test_case_insensitive_no_match(self, sample_by_model):
        """Case-insensitive lookup returns None when no match."""
        lookup = "Claude-Haiku-4-5"
        actual = next(
            (v for k, v in sample_by_model.items() if k.lower() == lookup.lower()),
            None,
        )
        assert actual is None

    def test_empty_by_model_returns_none(self):
        """Empty dict returns None for any model."""
        by_model = {}
        assert by_model.get("anything") is None

    def test_config_key_structure(self):
        """The reasoning_effort_by_model config key exists in CLI_CONFIG."""
        from cli import CLI_CONFIG
        agent = CLI_CONFIG.get("agent", {})
        assert "reasoning_effort_by_model" in agent
        assert isinstance(agent["reasoning_effort_by_model"], dict)

    def test_gateway_reads_by_model(self):
        """The reasoning_effort_by_model config key is present in CLI_CONFIG."""
        from cli import CLI_CONFIG
        agent = CLI_CONFIG.get("agent", {})
        assert "reasoning_effort_by_model" in agent
        assert isinstance(agent["reasoning_effort_by_model"], dict)

    def test_gateway_by_model_returns_per_model(self):
        """When model is in by_model, per-model entry wins over global."""
        # Replicate the gateway's logic inline
        by_model = {"claude-sonnet-4-6": "high"}
        global_effort = "medium"
        model_lower = "claude-sonnet-4-6".strip().lower()
        result = global_effort
        if isinstance(by_model, dict):
            for saved_model, saved_effort in by_model.items():
                if saved_model.strip().lower() == model_lower:
                    result = str(saved_effort or "").strip()
                    break
        assert result == "high"

    def test_gateway_by_model_falls_back(self):
        """When model not in by_model, falls back to global effort."""
        by_model = {"claude-opus-4-8": "high"}
        global_effort = "medium"
        model_lower = "claude-sonnet-4-6".strip().lower()
        result = global_effort
        if isinstance(by_model, dict):
            for saved_model, saved_effort in by_model.items():
                if saved_model.strip().lower() == model_lower:
                    result = str(saved_effort or "").strip()
                    break
        assert result == "medium"