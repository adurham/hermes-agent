"""Regression tests for the anthropic_content_blocks double-count bug.

Symptom that triggered the fix: ``preflight compression`` would report
~753K tokens while the provider's actual prompt_tokens for the same
session sat at ~517K.  Root cause: assistant messages carry both a
text-extracted ``content`` field AND a stash of the full provider
response under ``anthropic_content_blocks``.  Older code looked for the
stash under the underscore-prefixed name ``_anthropic_content_blocks``
(which never matched the live key) and ended up counting the same
content twice -- once via ``content`` and again via the blocks JSON.
On a search-heavy session with many ``web_search_tool_result`` /
``thinking`` blocks the inflation hit 200-300K phantom tokens.

(The historical ``_anthropic_content_blocks`` underscore-prefixed stash was
removed once it was confirmed nothing wrote it anymore; the live key is the
unprefixed ``anthropic_content_blocks``.)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent.model_metadata import (
    _estimate_message_chars,
    estimate_request_tokens_rough,
)


def _big_search_result_block(payload_chars: int = 40_000) -> dict:
    """Anthropic-native web_search_tool_result block of approx N chars."""
    return {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_abc",
        "content": [{"type": "web_search_result", "title": "x", "url": "x", "page_age": "1d",
                     "snippet": "A" * payload_chars}],
    }


class TestNoDoubleCountWhenBlocksAndContentBothPresent:
    """The estimator must NOT count the same text twice."""

    def test_blocks_present_content_is_skipped(self):
        # Build a realistic assistant message: small visible text + a
        # huge anthropic_content_blocks array that contains the same
        # text plus a giant web_search_tool_result.
        big_block = _big_search_result_block(40_000)
        msg = {
            "role": "assistant",
            "content": "Here is a summary based on the search.",  # short extract
            "anthropic_content_blocks": [
                {"type": "text", "text": "Here is a summary based on the search."},
                big_block,
            ],
        }
        # With the bug: chars = len(content) + len(blocks json) -> ~40K
        # After fix: only blocks count, content is skipped.
        chars = _estimate_message_chars(msg)
        # Sanity floor: at least the blocks json
        blocks_only = len(str({"role": "assistant",
                               "anthropic_content_blocks": msg["anthropic_content_blocks"]}))
        # Allow a little slack for dict-repr formatting.
        assert chars <= blocks_only + 100, (
            f"chars={chars} should be near blocks_only={blocks_only}, "
            "but content appears to have been double-counted")
        # And it must NOT also include the duplicated content string twice.
        assert chars < blocks_only + len(msg["content"])

    def test_no_blocks_falls_back_to_content_unchanged(self):
        """A normal assistant message (no blocks) still uses content."""
        msg = {"role": "assistant", "content": "hello world"}
        chars = _estimate_message_chars(msg)
        assert chars >= len("hello world")
        # No regression on the historical path.
        expected = len(str({"role": "assistant", "content": "hello world"}))
        assert chars == expected


class TestPreflightRegressionScenario:
    """Reproduce the 753K-vs-517K gap with synthetic search-heavy history."""

    def test_search_heavy_session_does_not_inflate_2x(self):
        # 100 assistant turns, each with substantial extracted text +
        # a 40K web_search_tool_result block stashed.  Without the fix
        # the estimator counted *both* the extracted text on .content
        # AND the same text inside .anthropic_content_blocks, plus the
        # block JSON, so the session inflated by ~50%.  The extracted
        # text on .content mirrors a realistic assistant writeup that
        # paraphrases the search-result snippet -- about 4 KB.
        long_paraphrase = "Paraphrased summary of search results. " * 100  # ~4KB
        messages = []
        for i in range(100):
            messages.append({
                "role": "assistant",
                "content": long_paraphrase + f" [turn {i}]",
                "anthropic_content_blocks": [
                    {"type": "text", "text": long_paraphrase + f" [turn {i}]"},
                    _big_search_result_block(40_000),
                ],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"tc_{i}",
                "content": f"tool result {i}",
            })

        est = estimate_request_tokens_rough(messages)
        # Total blocks bytes alone: 100 * ~40K = ~4M chars -> ~1M tokens.
        # Without the fix the estimator would also count ~100 * len(content)
        # in addition; with the fix that duplication is gone.
        # We pin a generous ceiling derived from the no-dup size + 5%.
        # First compute what we'd expect with no double-counting:
        no_dup_chars = 0
        for m in messages:
            shadow = {k: v for k, v in m.items()}
            if "anthropic_content_blocks" in shadow:
                # blocks are the source of truth -> drop content
                shadow.pop("content", None)
            no_dup_chars += len(str(shadow))
        # Convert to tokens via the same /4 the estimator uses.
        no_dup_tokens = (no_dup_chars + 3) // 4
        # Estimator must be within 5% of the no-dup tokens (it adds
        # nothing else here since system_prompt/tools are empty).
        assert abs(est - no_dup_tokens) / max(no_dup_tokens, 1) < 0.05, (
            f"estimator={est:,} no_dup={no_dup_tokens:,} diff>5%"
        )
