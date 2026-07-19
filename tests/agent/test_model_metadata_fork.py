"""Tests for fork-only additions to agent/model_metadata.py.

Verifies:
1. mDNS .local suffix detection (is_local_endpoint)
2. anthropic_content_blocks stashed ref fix for image token counting
"""


class TestModelMetadataFork:
    """Tests for fork additions in agent/model_metadata.py."""

    def test_mdns_local_suffix_detected(self):
        """Hostnames ending in .local are classified as local endpoints."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("http://mac-studio.local:52415") is True
        assert is_local_endpoint("http://my-machine.local:8080") is True
        assert is_local_endpoint("https://server.local") is True

    def test_non_mdns_not_local(self):
        """Regular hostnames without .local are not classified as mDNS local."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("https://api.anthropic.com") is False
        assert is_local_endpoint("https://www.google.com") is False
        assert is_local_endpoint("http://example.com:8080") is False

    def test_docker_internal_still_local(self):
        """Docker/internal hostnames remain local alongside .local."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("http://host.docker.internal:8080") is True
        assert is_local_endpoint("http://postgres.containers.internal:5432") is True

    def test_mdns_distinct_from_docker(self):
        """.local is a separate suffix list from Docker suffixes."""
        from agent.model_metadata import is_local_endpoint

        # .local should catch things Docker suffixes wouldn't
        assert is_local_endpoint("https://nas.local:5001") is True
        # But also catch IPs and unqualified names
        assert is_local_endpoint("http://my-server:8080") is True

    def test_image_token_count_uses_anthropic_content_blocks(self):
        """_count_image_tokens reads from anthropic_content_blocks on dict msgs."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [{"type": "text", "text": "hello"}],
            "anthropic_content_blocks": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
            ],
        }
        # Returns cost_per_image (100) for the one image in stashed blocks
        count = _count_image_tokens(msg, cost_per_image=100)
        assert count == 100

    def test_image_token_count_also_counts_content_images(self):
        """_count_image_tokens also counts image_url/image parts in content."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                {"type": "text", "text": "description"},
            ],
        }
        count = _count_image_tokens(msg, cost_per_image=100)
        # One image in content, no stashed blocks
        assert count == 100

    def test_anthropic_content_blocks_is_dict_key(self):
        """The code accesses anthropic_content_blocks via msg.get(), not attr."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [{"type": "text", "text": "no images here"}],
            "anthropic_content_blocks": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}},
                {"type": "text", "text": "some text"},
            ],
        }
        count = _count_image_tokens(msg, cost_per_image=85)
        assert count == 85  # one image in stashed blocks


class TestReasoningFieldDuplicationFix:
    """Regression tests for the preflight-estimate/status-bar mismatch.

    Root cause: when an Anthropic assistant message carries
    ``anthropic_content_blocks`` (the interleaved-thinking replay channel),
    the ``reasoning``/``reasoning_content``/``reasoning_details`` fields are
    pure duplicates of the thinking text already inside those blocks --
    ``_convert_assistant_message``'s interleaved-thinking fast path
    (agent/anthropic_adapter.py) replays ``anthropic_content_blocks``
    verbatim and never reads the three reasoning fields. Both rough-estimate
    char counters used to walk all four copies, quadruple-counting every
    thinking block on sessions with ``interleaved_thinking: true`` and
    driving the preflight compression estimate far above the real
    provider-reported ``prompt_tokens`` shown on the status bar (observed:
    ~600K rough vs ~284K real in one session, triggering compaction the
    status bar gave no indication was imminent).
    """

    def _make_msg_with_blocks(self, thinking_text: str) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "reasoning": thinking_text,
            "reasoning_content": thinking_text,
            "reasoning_details": [
                {"type": "thinking", "thinking": thinking_text, "signature": "sig"}
            ],
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": thinking_text, "signature": "sig"},
                {"type": "tool_use", "id": "x", "name": "terminal", "input": {"command": "ls"}},
            ],
            "tool_calls": [{"id": "x", "function": {"name": "terminal", "arguments": "{}"}}],
        }

    def test_estimate_message_chars_skips_reasoning_duplicates_when_blocks_present(self):
        from agent.model_metadata import _estimate_message_chars

        thinking_text = "chain of thought " * 200
        msg = self._make_msg_with_blocks(thinking_text)
        chars = _estimate_message_chars(msg)
        # Should be close to ONE copy of the thinking text (inside
        # anthropic_content_blocks) plus small JSON structural overhead --
        # not 4x that, which is what the bug produced.
        assert chars < len(thinking_text) * 1.5

    def test_count_message_chars_with_image_credit_skips_reasoning_duplicates(self):
        from agent.model_metadata import _count_message_chars_with_image_token_credit

        thinking_text = "chain of thought " * 200
        msg = self._make_msg_with_blocks(thinking_text)
        chars, credit = _count_message_chars_with_image_token_credit(msg)
        assert chars < len(thinking_text) * 1.5

    def test_reasoning_still_counted_when_no_anthropic_content_blocks(self):
        """Non-Anthropic providers (OpenRouter, DeepSeek, local) don't stash
        anthropic_content_blocks -- reasoning fields are the ONLY copy of
        the thinking text there and must still be counted in full."""
        from agent.model_metadata import (
            _estimate_message_chars,
            _count_message_chars_with_image_token_credit,
        )

        thinking_text = "reasoning text " * 100
        msg = {
            "role": "assistant",
            "content": "final answer",
            "reasoning": thinking_text,
            "reasoning_content": thinking_text,
        }
        assert _estimate_message_chars(msg) > len(thinking_text)
        chars, _credit = _count_message_chars_with_image_token_credit(msg)
        assert chars > len(thinking_text)

    def test_estimate_request_tokens_rough_matches_real_usage_more_closely(self):
        """End-to-end: a session with several interleaved-thinking turns
        should no longer inflate the rough estimate to ~4x the real size."""
        from agent.model_metadata import estimate_request_tokens_rough

        thinking_text = "step by step reasoning about the next action " * 50
        messages = []
        for i in range(10):
            messages.append(self._make_msg_with_blocks(thinking_text))
            messages.append({
                "role": "tool",
                "tool_call_id": "x",
                "content": "tool output",
            })

        rough_with_fix = estimate_request_tokens_rough(messages)

        # Manually compute what the OLD (buggy) behavior would have
        # estimated: len(str(msg)) with no reasoning-duplicate stripping,
        # only the content/image stripping that already existed.
        old_style_chars = 0
        for msg in messages:
            old_style_chars += len(str(msg))
        old_style_tokens = (old_style_chars + 3) // 4

        # The fix must materially reduce the estimate versus the old
        # unstripped behavior -- this is the whole point of the fix.
        assert rough_with_fix < old_style_tokens * 0.6