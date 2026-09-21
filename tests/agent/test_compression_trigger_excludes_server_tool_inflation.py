"""Verify the compression trigger ignores server-tool-inflated prompt_tokens.

Anthropic server-tool calls (web_search / web_fetch) each run a separate
internal inference pass, and Anthropic folds every pass's usage into ONE
cumulative prompt_tokens figure with no other marker. A turn with N passes
can report ~(N+1)x the real next-request context size, which — before this
fix — could falsely trip compaction on a session that hadn't meaningfully
grown (root-caused 2026-07-24, session 20260723_211736_99ee22).

Mirrors the fixed gate in agent/conversation_loop.py (the tail-check right
after tool execution, ~line 5757) the same way
test_compression_trigger_excludes_reasoning.py mirrors the reasoning-token
fix a few lines above it.
"""

import types

from agent.model_metadata import estimate_request_tokens_rough


def _make_agent_stub(prompt_tokens, server_tool_requests, messages, tools=None):
    """Replicate the fixed gate logic from conversation_loop.py ~line 5757."""
    compressor = types.SimpleNamespace(
        last_prompt_tokens=prompt_tokens,
        last_server_tool_requests=server_tool_requests,
    )
    if compressor.last_prompt_tokens > 0 and not compressor.last_server_tool_requests:
        real_tokens = compressor.last_prompt_tokens
    elif compressor.last_prompt_tokens > 0:
        real_tokens = estimate_request_tokens_rough(messages, tools=tools)
    elif compressor.last_prompt_tokens == -1:
        real_tokens = 0
    else:
        real_tokens = estimate_request_tokens_rough(messages, tools=tools)
    return real_tokens, compressor


class TestCompressionTriggerExcludesServerToolInflation:
    def test_server_tool_inflated_reading_falls_back_to_rough_estimate(self):
        """A turn with several server-tool passes reports a huge
        prompt_tokens that does not reflect real context growth — the gate
        must not trust it directly.
        """
        small_messages = [{"role": "user", "content": "hi"}]
        real_tokens, _ = _make_agent_stub(
            prompt_tokens=975_000,  # inflated by folded server-tool passes
            server_tool_requests=4,
            messages=small_messages,
        )
        rough = estimate_request_tokens_rough(small_messages, tools=None)
        assert real_tokens == rough
        assert real_tokens < 10_000, (
            "Inflated server-tool prompt_tokens must not leak through as the "
            "compaction-trigger signal for a small conversation"
        )

    def test_no_server_tool_requests_uses_real_prompt_tokens(self):
        """Without server-tool passes, the real provider count is still used
        directly — this fix must not degrade the normal (accurate) path.
        """
        real_tokens, _ = _make_agent_stub(
            prompt_tokens=110_000,
            server_tool_requests=0,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert real_tokens == 110_000

    def test_zero_prompt_tokens_falls_back_to_rough_estimate(self):
        messages = [{"role": "user", "content": "hi"}]
        real_tokens, _ = _make_agent_stub(
            prompt_tokens=0,
            server_tool_requests=0,
            messages=messages,
        )
        assert real_tokens == estimate_request_tokens_rough(messages, tools=None)

    def test_sentinel_negative_one_yields_zero_regardless_of_server_tool_flag(self):
        real_tokens, _ = _make_agent_stub(
            prompt_tokens=-1,
            server_tool_requests=0,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert real_tokens == 0
