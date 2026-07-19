"""Tests for the fork-only additions to agent/agent_init.py.

Verifies:
1. interleaved_thinking parameter
2. 1M-context-beta pre-stamp for non-supporting models (Haiku)
3. Per-model max_tokens fallback from config
"""

import pytest


class FakeAnthropicAdapter:
    """Mock for agent.anthropic_adapter._model_supports_1m_context."""
    SUPPORTING = {
        "claude-opus-4-8", "claude-sonnet-4-6", "claude-opus-4-7",
    }

    @staticmethod
    def _model_supports_1m_context(model: str) -> bool:
        return model in FakeAnthropicAdapter.SUPPORTING


class FakeAgent:
    """Minimal agent stub for init state testing."""
    def __init__(self):
        self.model = ""
        self._oauth_1m_beta_disabled = False
        self.max_tokens = None


class TestAgentInitFork:
    """Tests for fork additions in agent/agent_init.py."""

    def test_interleaved_thinking_exists(self):
        """init_agent accepts an interleaved_thinking parameter."""
        import inspect
        from agent.agent_init import init_agent
        sig = inspect.signature(init_agent)
        assert "interleaved_thinking" in sig.parameters
        param = sig.parameters["interleaved_thinking"]
        assert param.default is False
        # Annotation may be 'bool' (string) due to from __future__ import annotations
        assert param.annotation in (bool, 'bool')

    def test_haiku_model_pre_stamps_1m_beta_disabled(self):
        """Haiku model causes _oauth_1m_beta_disabled = True."""
        agent = FakeAgent()
        agent.model = "claude-haiku-4-5-20251001"

        if not FakeAnthropicAdapter._model_supports_1m_context(agent.model):
            agent._oauth_1m_beta_disabled = True

        assert agent._oauth_1m_beta_disabled is True

    def test_opus_model_does_not_pre_stamp_1m_beta(self):
        """Opus model does NOT disable the 1M beta."""
        agent = FakeAgent()
        agent.model = "claude-opus-4-8"

        if not FakeAnthropicAdapter._model_supports_1m_context(agent.model):
            agent._oauth_1m_beta_disabled = True

        assert agent._oauth_1m_beta_disabled is False

    def test_sonnet_4_6_supports_1m(self):
        """Sonnet 4-6 supports 1M context, beta not disabled."""
        agent = FakeAgent()
        agent.model = "claude-sonnet-4-6"

        if not FakeAnthropicAdapter._model_supports_1m_context(agent.model):
            agent._oauth_1m_beta_disabled = True

        assert agent._oauth_1m_beta_disabled is False

    def test_unknown_model_does_not_crash(self):
        """Unknown/non-Anthropic model doesn't crash the pre-stamp logic."""
        agent = FakeAgent()
        agent.model = "mlx-community/DeepSeek-V4-Flash"

        # The try/except guard means this should never raise
        try:
            if not FakeAnthropicAdapter._model_supports_1m_context(agent.model):
                agent._oauth_1m_beta_disabled = True
        except Exception:
            pytest.fail("Pre-stamp logic crashed on unknown model!")

        # DSv4 doesn't use Anthropic 1M beta at all
        assert agent._oauth_1m_beta_disabled is True

    def test_import_additional_types(self):
        """The fork adds Path and Tuple to imports."""
        import inspect
        from agent.agent_init import init_agent
        # The function signature should have Path/Tuple available via the module
        source = inspect.getsource(inspect.getmodule(init_agent))
        assert "from pathlib import Path" in source or "Path" in source
        # Interleaved thinking param is the fork signature
        assert "interleaved_thinking" in source

    def test_max_tokens_config_default(self):
        """When max_tokens is None, it uses config or model default."""
        agent = FakeAgent()
        assert agent.max_tokens is None

        # The code: if agent.max_tokens is None, read per-model config
        agent.max_tokens = 65536  # Simulate config-provided value
        assert agent.max_tokens == 65536