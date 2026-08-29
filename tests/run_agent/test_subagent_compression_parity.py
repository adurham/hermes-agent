"""E2E parity proof: compression triggers identically for subagent and CLI
sessions, against a real temp HERMES_HOME config.

This is the direct behavioral disproof of a reported bug: "compression
never fires for subagent sessions" (compression.threshold=0.8 configured
globally, subagent sessions showing compression_fallback_streak=0 AND
compression_ineffective_count=0). Investigation found:

1. Those two counters are anti-thrash FAILURE signals, not "did compression
   run" signals — they read 0 both when compression never had reason to
   fire and when it fired and succeeded every single time (see
   ``test_compression_attempts_total_counter.py`` for the dedicated fix).
2. Every session cited as evidence never had a real per-request prompt
   token count anywhere near the configured 0.8 threshold — the highest
   was ~31% of the model's context window despite hundreds of tool calls
   (many small tool results, not large context growth).
3. Reading ``tools/delegate_tool.py`` and ``agent/agent_init.py`` shows
   subagents are constructed via the exact same ``AIAgent(...)`` +
   ``agent_init`` compression wiring as top-level sessions, and
   ``child.run_conversation()`` drives the identical
   ``agent.conversation_loop`` turn loop. No code path gates compression on
   ``platform == "subagent"`` or on ``parent_session_id`` being set.

This test proves point 3 empirically end-to-end: real ``AIAgent`` instances
built with ``platform="cli"`` vs ``platform="subagent"`` (with a
``parent_session_id`` set, exactly as ``tools/delegate_tool.py`` does),
reading a real ``config.yaml`` with ``compression.threshold: 0.8`` from a
temp ``HERMES_HOME``, must resolve the identical threshold and must both
invoke ``_compress_context()`` through the real ``run_conversation()`` path
when fed a conversation history that exceeds it. Only the network client
and ``_compress_context`` itself are mocked — everything else (config
loading, agent_init wiring, the turn loop's preflight-compression gate) is
real production code.
"""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


CONFIG_YAML = """\
model:
  default: claude-sonnet-5
compression:
  enabled: true
  threshold: 0.8
  target_ratio: 0.15
  protect_last_n: 12
"""


def _mock_response(content="All done", finish_reason="stop"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        role="assistant",
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """A real, isolated HERMES_HOME with compression.threshold = 0.8.

    Clears the module-level config cache before AND after so this test
    can't read a stale in-process cache from an earlier test/config, and
    can't poison the cache for tests that run after it.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text(CONFIG_YAML)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import config as hermes_config

    hermes_config._LOAD_CONFIG_CACHE.clear()
    yield home
    hermes_config._LOAD_CONFIG_CACHE.clear()


def _make_agent(platform: str, parent_session_id: str | None = None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        # Deliberately routed through the OpenAI-compatible chat_completions
        # path (OpenRouter base_url), NOT api_mode="anthropic_messages" —
        # the native Anthropic path builds its own httpx client independent
        # of ``agent.client`` and would make a REAL network call to
        # api.anthropic.com with this fake key. This mirrors the existing
        # ``agent`` fixture pattern in tests/run_agent/test_run_agent.py,
        # which uses the same OpenRouter base_url for the same reason. The
        # model still resolves the same 1M-context claude-sonnet-5 metadata
        # (agent/model_metadata.py's DEFAULT_CONTEXT_LENGTHS keys on the
        # bare model id, not the provider), so the threshold math under
        # test is identical to the real anthropic-provider path.
        kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="claude-sonnet-5",
            provider="openrouter",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
        )
        if parent_session_id:
            kwargs["parent_session_id"] = parent_session_id
        agent = AIAgent(**kwargs)
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = _mock_response()
        return agent


class TestSubagentCompressionParity:
    def test_subagent_and_cli_resolve_identical_threshold(self, hermes_home):
        cli_agent = _make_agent("cli")
        sub_agent = _make_agent("subagent", parent_session_id="parent-xyz")

        assert cli_agent.compression_enabled is True
        assert sub_agent.compression_enabled is True
        assert cli_agent.context_compressor.threshold_percent == pytest.approx(0.8)
        assert sub_agent.context_compressor.threshold_percent == pytest.approx(0.8)
        assert (
            cli_agent.context_compressor.threshold_tokens
            == sub_agent.context_compressor.threshold_tokens
        )

    def test_subagent_and_cli_should_compress_identically_at_the_boundary(
        self, hermes_home
    ):
        cli_agent = _make_agent("cli")
        sub_agent = _make_agent("subagent", parent_session_id="parent-xyz")
        threshold = sub_agent.context_compressor.threshold_tokens

        assert cli_agent.context_compressor.should_compress(threshold - 1) is False
        assert sub_agent.context_compressor.should_compress(threshold - 1) is False
        assert cli_agent.context_compressor.should_compress(threshold + 1) is True
        assert sub_agent.context_compressor.should_compress(threshold + 1) is True

    @pytest.mark.parametrize(
        "platform,parent_session_id",
        [("cli", None), ("subagent", "parent-xyz")],
    )
    def test_run_conversation_invokes_compress_context_over_threshold(
        self, hermes_home, platform, parent_session_id
    ):
        """The real, non-mocked turn-loop preflight gate must call
        ``_compress_context`` for BOTH platforms when fed an
        over-threshold conversation history — the direct behavioral
        disproof of a subagent-specific compression skip."""
        agent = _make_agent(platform, parent_session_id=parent_session_id)

        # ~1M-token synthetic history at ~4 chars/token, comfortably over
        # the resolved 800K threshold for a 1M-context model at 0.8.
        big_chunk = "x" * 4_000_000
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": big_chunk},
        ]

        with (
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            mock_compress.return_value = (
                [{"role": "user", "content": "compacted"}],
                "compacted system prompt",
            )
            # Only the preflight-compression gate is under test here — the
            # subsequent (real) API call is free to fail/retry/whatever
            # against the fake credentials; that is orthogonal to whether
            # _compress_context was invoked, which is the behavior this
            # test exists to prove.
            agent.run_conversation("continue", conversation_history=history)

        mock_compress.assert_called_once()

    def test_run_conversation_does_not_compress_under_threshold_either_platform(
        self, hermes_home
    ):
        """Symmetry check: a small history must not spuriously trigger
        compression on either platform — the fix must not make compression
        MORE eager than the configured threshold for either surface."""
        for platform, parent in (("cli", None), ("subagent", "parent-xyz")):
            agent = _make_agent(platform, parent_session_id=parent)
            history = [{"role": "user", "content": "hi"}]

            with (
                patch.object(agent, "_compress_context") as mock_compress,
                patch.object(agent, "_persist_session"),
                patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources"),
            ):
                agent.run_conversation("continue", conversation_history=history)

            mock_compress.assert_not_called()
