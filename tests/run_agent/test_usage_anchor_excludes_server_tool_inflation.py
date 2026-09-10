"""Verify the usage anchor (agent._usage_anchor) skips server-tool-inflated
prompt_tokens.

Anthropic server-tool calls (web_search / web_fetch) each run a separate
internal inference pass, and Anthropic folds every pass's usage into ONE
cumulative prompt_tokens figure with no other marker. A turn with N passes
can report ~(N+1)x the real next-request context size.

test_compression_trigger_excludes_server_tool_inflation.py already covers
this for the post-tool-call tail-check gate (fixed 2026-07-24). The usage
ANCHOR capture site (agent/conversation_loop.py, ~line 4859) — which feeds
_midturn_request_pressure_tokens() -> anchored_context_tokens(), the figure
the mid-turn pre-API compression guard compares against threshold_tokens —
had no equivalent guard until this fix. An inflated anchor poisons every
subsequent anchored_context_tokens() call (inflated_anchor + small_delta)
until the next clean reading overwrites it, spuriously tripping compaction
on a session that hasn't meaningfully grown.

Live evidence (2026-09-10):
  session 20260910_004217_246abf: call #116 in=395153 (clean) -> call #117
  in=800044 server_tool_passes=1 -> "Pre-API compression: ~801,731 request
  tokens >= 800,000 threshold" fired immediately after.
  session 20260910_114733_8fc610: call #392 in=403827 (clean) -> call #393
  in=1231826 server_tool_passes=2 -> "Pre-API compression: ~1,232,996
  request tokens >= 800,000 threshold" fired; call #394 immediately after
  reported in=417807 (clean), proving the real context was nowhere near
  800K-1.2M.

Mirrors test_compression_trigger_excludes_server_tool_inflation.py's replica
pattern: reproduce the fixed gate logic standalone rather than importing the
full conversation loop.
"""

from agent.model_metadata import capture_usage_anchor


def _capture_anchor_if_not_inflated(usage_dict, prompt_tokens, completion_tokens, messages):
    """Replicate the fixed gate in conversation_loop.py ~line 4859."""
    if not usage_dict.get("server_tool_requests"):
        return capture_usage_anchor(prompt_tokens, completion_tokens, messages)
    return None


class TestUsageAnchorExcludesServerToolInflation:
    def test_inflated_reading_does_not_overwrite_anchor(self):
        """A turn with folded server-tool passes must not update the anchor."""
        messages = [{"role": "user", "content": "hi"}] * 5
        usage_dict = {
            "prompt_tokens": 1_231_826,
            "server_tool_requests": 2,
        }
        new_anchor = _capture_anchor_if_not_inflated(
            usage_dict, 1_231_826, 914, messages
        )
        assert new_anchor is None, (
            "An inflated (server-tool-folded) prompt_tokens reading must "
            "never be captured as the usage anchor — it would poison every "
            "subsequent anchored_context_tokens() call until the next "
            "clean reading arrives."
        )

    def test_clean_reading_still_updates_anchor(self):
        """Without server-tool passes, the anchor still updates normally —
        this fix must not degrade the accurate path."""
        messages = [{"role": "user", "content": "hi"}] * 5
        usage_dict = {
            "prompt_tokens": 403_827,
            "server_tool_requests": 0,
        }
        new_anchor = _capture_anchor_if_not_inflated(
            usage_dict, 403_827, 297, messages
        )
        assert new_anchor is not None
        assert new_anchor["prompt_tokens"] == 403_827

    def test_stale_anchor_survives_an_inflated_turn(self):
        """A previously-captured clean anchor is left in place (not cleared)
        when the current turn is inflated — the caller only skips the
        overwrite, it never actively invalidates the existing anchor."""
        messages = [{"role": "user", "content": "hi"}] * 5
        clean_usage = {"prompt_tokens": 403_827, "server_tool_requests": 0}
        stale_anchor = _capture_anchor_if_not_inflated(
            clean_usage, 403_827, 297, messages
        )
        assert stale_anchor is not None

        inflated_usage = {"prompt_tokens": 1_231_826, "server_tool_requests": 2}
        result = _capture_anchor_if_not_inflated(
            inflated_usage, 1_231_826, 914, messages + [{"role": "assistant", "content": "ok"}]
        )
        assert result is None
        # Simulating the real call site: `if new_anchor is not None:
        # agent._usage_anchor = new_anchor` — the caller's own stale_anchor
        # variable (standing in for agent._usage_anchor) is untouched here.
        assert stale_anchor["prompt_tokens"] == 403_827
