"""Verify the compressor-facing usage dict isn't inflated by MoA reference
fan-out, and that Anthropic server-tool detection covers tool_search (not
just web_search/web_fetch).

Two related gaps found during adversarial review of the server-tool-folding
fix (see test_compression_trigger_excludes_server_tool_inflation.py):

1. MoA folds advisor fan-out usage into ``canonical_usage`` for cost
   tracking (correct — that's real spend), but the OLD code fed that same
   combined figure to the context compressor. An N-advisor MoA turn then
   looked exactly like an N-server-tool-pass turn: ~(N+1)x the real
   next-request context size, tripping compaction on a session that hadn't
   grown. The fix feeds the compressor ``aggregator_usage`` (pre-fold)
   instead, mirrored here.

2. ``response.usage.server_tool_use`` only exposes named counters for
   ``web_search_requests`` / ``web_fetch_requests`` — a tool_search-only
   turn (legacy ``server_side`` mode, same multi-pass folding per
   agent/anthropic_adapter.py's ``_apply_tool_search`` docstring) would
   report 0 there and slip through undetected. The fix additionally counts
   raw ``server_tool_use`` content blocks (a type shared by ALL Anthropic
   server tools), mirrored here.
"""

from types import SimpleNamespace

from agent.usage_pricing import CanonicalUsage


def _count_server_tool_blocks(response, api_mode):
    """Mirrors the block-counting logic added in conversation_loop.py."""
    count = 0
    if api_mode == "anthropic_messages":
        for blk in (getattr(response, "content", None) or []):
            blk_type = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
            if blk_type == "server_tool_use":
                count += 1
    return count


def _build_usage_dict(aggregator_usage, response, api_mode):
    """Mirrors the fixed usage_dict construction in conversation_loop.py."""
    block_count = _count_server_tool_blocks(response, api_mode)
    return {
        "prompt_tokens": aggregator_usage.prompt_tokens,
        "completion_tokens": aggregator_usage.output_tokens,
        "total_tokens": aggregator_usage.total_tokens,
        "input_tokens": aggregator_usage.input_tokens,
        "output_tokens": aggregator_usage.output_tokens,
        "cache_read_tokens": aggregator_usage.cache_read_tokens,
        "cache_write_tokens": aggregator_usage.cache_write_tokens,
        "reasoning_tokens": aggregator_usage.reasoning_tokens,
        "server_tool_requests": max(aggregator_usage.server_tool_requests, block_count),
    }


class TestUsageDictExcludesMoaFold:
    def test_moa_advisor_fanout_not_folded_into_compressor_usage(self):
        aggregator_usage = CanonicalUsage(input_tokens=5_000, output_tokens=300)
        advisor_usage = CanonicalUsage(input_tokens=400_000, output_tokens=50_000)  # 4 advisors
        combined = aggregator_usage + advisor_usage  # what canonical_usage becomes post-fold

        # Sanity: the combined figure IS the inflated one a naive feed would use.
        assert combined.prompt_tokens == 405_000

        usage_dict = _build_usage_dict(aggregator_usage, response=SimpleNamespace(content=[]), api_mode="anthropic_messages")
        assert usage_dict["prompt_tokens"] == 5_000

    def test_no_moa_fold_is_a_no_op(self):
        aggregator_usage = CanonicalUsage(input_tokens=5_000, output_tokens=300)
        usage_dict = _build_usage_dict(aggregator_usage, response=SimpleNamespace(content=[]), api_mode="anthropic_messages")
        assert usage_dict["prompt_tokens"] == 5_000


class TestServerToolBlockCounting:
    def test_tool_search_blocks_counted_even_without_named_usage_counter(self):
        # response.usage never names tool_search — server_tool_requests stays 0 —
        # but the response body carries two tool_search server_tool_use blocks.
        aggregator_usage = CanonicalUsage(input_tokens=900_000, output_tokens=300)
        response = SimpleNamespace(content=[
            {"type": "text", "text": "..."},
            {"type": "server_tool_use", "id": "t1", "name": "tool_search_tool_regex", "input": {}},
            {"type": "server_tool_use", "id": "t2", "name": "tool_search_tool_regex", "input": {}},
        ])
        usage_dict = _build_usage_dict(aggregator_usage, response, api_mode="anthropic_messages")
        assert usage_dict["server_tool_requests"] == 2

    def test_web_search_blocks_also_counted_generically(self):
        aggregator_usage = CanonicalUsage(input_tokens=900_000, output_tokens=300)
        response = SimpleNamespace(content=[
            {"type": "server_tool_use", "id": "t1", "name": "web_search", "input": {}},
        ])
        usage_dict = _build_usage_dict(aggregator_usage, response, api_mode="anthropic_messages")
        assert usage_dict["server_tool_requests"] == 1

    def test_non_anthropic_api_mode_never_scans_content(self):
        # A chat_completions response's .content (if present at all) isn't
        # block-shaped and must never be scanned.
        aggregator_usage = CanonicalUsage(input_tokens=5_000, output_tokens=300)
        response = SimpleNamespace(content="plain string, not blocks")
        usage_dict = _build_usage_dict(aggregator_usage, response, api_mode="chat_completions")
        assert usage_dict["server_tool_requests"] == 0

    def test_missing_content_attribute_does_not_crash(self):
        aggregator_usage = CanonicalUsage(input_tokens=5_000, output_tokens=300)
        response = SimpleNamespace()  # no .content at all
        usage_dict = _build_usage_dict(aggregator_usage, response, api_mode="anthropic_messages")
        assert usage_dict["server_tool_requests"] == 0

    def test_named_usage_counter_still_respected_when_higher(self):
        # If the usage object ever reports MORE requests than blocks visible
        # in this particular response shape, keep the max (belt-and-suspenders).
        aggregator_usage = CanonicalUsage(
            input_tokens=900_000,
            output_tokens=300,
            server_tool_web_search_requests=3,
        )
        response = SimpleNamespace(content=[
            {"type": "server_tool_use", "id": "t1", "name": "web_search", "input": {}},
        ])
        usage_dict = _build_usage_dict(aggregator_usage, response, api_mode="anthropic_messages")
        assert usage_dict["server_tool_requests"] == 3
