"""Round-trip tests for Anthropic server-side tool_search blocks.

The tool_search server-side tool produces blocks whose response shape
diverges from the input shape. The API will 400 on resubmit if any
response-only field (``text``, ``citations``, etc.) leaks back, or if
the type discriminator carries a variant suffix (e.g.
``tool_search_tool_regex_tool_result``).

This module validates every code path that touches these blocks against
the Anthropic SDK's documented input TypedDicts so we don't have to
discover the schema one rejected field at a time.
"""

from __future__ import annotations

from typing import Any, Dict, get_type_hints

import pytest

from agent.anthropic_adapter import (
    _normalize_tool_reference_for_input,
    _normalize_tool_search_result_for_input,
    _normalize_tool_search_result_inner,
    convert_messages_to_anthropic,
)


def _typed_dict_keys(td_cls) -> set[str]:
    """Return the set of field names declared on a TypedDict class."""
    return set(get_type_hints(td_cls).keys())


# ---------------------------------------------------------------------------
# Schema-derived expected key sets (from the Anthropic SDK TypedDicts)
# ---------------------------------------------------------------------------
try:
    from anthropic.types.beta import (
        beta_tool_reference_block_param,
        beta_tool_search_tool_result_block_param,
        beta_tool_search_tool_result_error_param,
        beta_tool_search_tool_search_result_block_param,
    )

    OUTER_KEYS = _typed_dict_keys(
        beta_tool_search_tool_result_block_param.BetaToolSearchToolResultBlockParam
    )
    INNER_RESULT_KEYS = _typed_dict_keys(
        beta_tool_search_tool_search_result_block_param.BetaToolSearchToolSearchResultBlockParam
    )
    INNER_ERROR_KEYS = _typed_dict_keys(
        beta_tool_search_tool_result_error_param.BetaToolSearchToolResultErrorParam
    )
    REF_KEYS = _typed_dict_keys(
        beta_tool_reference_block_param.BetaToolReferenceBlockParam
    )
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — skip if SDK not installed in env
    SDK_AVAILABLE = False
    OUTER_KEYS = INNER_RESULT_KEYS = INNER_ERROR_KEYS = REF_KEYS = set()


pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE, reason="anthropic SDK not installed"
)


# ---------------------------------------------------------------------------
# Sample response payloads — what Anthropic actually returns
# ---------------------------------------------------------------------------
def _sample_outer_response(
    *, variant: str = "regex", with_text: bool = True, with_citations: bool = True
) -> Dict[str, Any]:
    """Build a synthetic outer block as it appears in a streamed response."""
    block: Dict[str, Any] = {
        # The first error from the live API referenced
        # ``tool_search_tool_regex_tool_result`` — exercise the
        # variant-suffixed form even though the SDK 0.86.0 declares the
        # canonical type. This is what our normalizer must rewrite.
        "type": f"tool_search_tool_{variant}_tool_result",
        "tool_use_id": f"srvtoolu_{variant}_abc123",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [
                {
                    "type": "tool_reference",
                    "tool_name": "mcp__example__do_thing",
                    # Response carries arbitrary extras; allowlist must drop them.
                    "description": "RESPONSE-ONLY",
                    "input_schema": {"type": "object"},
                },
                {
                    "type": "tool_reference",
                    "tool_name": "mcp__example__other",
                    "extra_meta": "RESPONSE-ONLY",
                },
            ],
        },
    }
    if with_text:
        block["text"] = "Found 2 tools matching the query"
    if with_citations:
        block["citations"] = [{"type": "char_location", "start_char": 0, "end_char": 5}]
    return block


def _sample_outer_response_error() -> Dict[str, Any]:
    return {
        "type": "tool_search_tool_regex_tool_result",
        "tool_use_id": "srvtoolu_err",
        "content": {
            "type": "tool_search_tool_result_error",
            "error_code": "execution_time_exceeded",
            "message": "RESPONSE-ONLY",
        },
        "text": "RESPONSE-ONLY",
    }


# ---------------------------------------------------------------------------
# tool_reference normalization
# ---------------------------------------------------------------------------
class TestNormalizeToolReference:
    def test_strips_response_only_fields(self):
        ref = {
            "type": "tool_reference",
            "tool_name": "mcp__x__y",
            "description": "BAD",
            "input_schema": {"x": 1},
            "rank": 0.95,
        }
        out = _normalize_tool_reference_for_input(ref)
        assert set(out.keys()).issubset(REF_KEYS)
        assert out == {"type": "tool_reference", "tool_name": "mcp__x__y"}

    def test_preserves_cache_control(self):
        ref = {
            "type": "tool_reference",
            "tool_name": "x",
            "cache_control": {"type": "ephemeral"},
        }
        out = _normalize_tool_reference_for_input(ref)
        assert out["cache_control"] == {"type": "ephemeral"}
        assert set(out.keys()).issubset(REF_KEYS)

    def test_handles_string_input_defensively(self):
        out = _normalize_tool_reference_for_input("foo")
        assert out == {"type": "tool_reference", "tool_name": "foo"}

    def test_drops_non_dict_cache_control(self):
        ref = {"type": "tool_reference", "tool_name": "x", "cache_control": "bad"}
        out = _normalize_tool_reference_for_input(ref)
        assert "cache_control" not in out


# ---------------------------------------------------------------------------
# Inner content normalization
# ---------------------------------------------------------------------------
class TestNormalizeInnerSearchResult:
    def test_search_result_strips_extras_and_normalizes_refs(self):
        inner = {
            "type": "tool_search_tool_search_result",
            "text": "BAD",
            "citations": ["BAD"],
            "tool_references": [
                {"type": "tool_reference", "tool_name": "a", "description": "BAD"},
            ],
        }
        out = _normalize_tool_search_result_inner(inner)
        assert set(out.keys()).issubset(INNER_RESULT_KEYS)
        assert out["type"] == "tool_search_tool_search_result"
        assert out["tool_references"] == [{"type": "tool_reference", "tool_name": "a"}]

    def test_error_variant_strips_extras(self):
        inner = {
            "type": "tool_search_tool_result_error",
            "error_code": "unavailable",
            "message": "BAD",
            "details": {"x": 1},
        }
        out = _normalize_tool_search_result_inner(inner)
        assert set(out.keys()).issubset(INNER_ERROR_KEYS)
        assert out == {
            "type": "tool_search_tool_result_error",
            "error_code": "unavailable",
        }

    def test_unknown_inner_type_passes_through(self):
        inner = {"type": "future_unknown_type", "data": 1}
        assert _normalize_tool_search_result_inner(inner) == inner

    def test_non_dict_passes_through(self):
        assert _normalize_tool_search_result_inner("foo") == "foo"


# ---------------------------------------------------------------------------
# Outer block normalization
# ---------------------------------------------------------------------------
class TestNormalizeOuterToolSearchResult:
    @pytest.mark.parametrize("variant", ["regex", "bm25"])
    def test_renames_variant_to_canonical_type(self, variant):
        sb = _sample_outer_response(variant=variant)
        out = _normalize_tool_search_result_for_input(sb)
        assert out["type"] == "tool_search_tool_result"

    def test_strips_response_only_fields_at_outer_level(self):
        sb = _sample_outer_response(with_text=True, with_citations=True)
        out = _normalize_tool_search_result_for_input(sb)
        assert "text" not in out
        assert "citations" not in out
        # Output keys must be a subset of the SDK's declared TypedDict keys.
        assert set(out.keys()).issubset(OUTER_KEYS)

    def test_preserves_required_fields(self):
        sb = _sample_outer_response()
        out = _normalize_tool_search_result_for_input(sb)
        assert out["tool_use_id"] == sb["tool_use_id"]
        assert "content" in out

    def test_inner_content_is_recursively_normalized(self):
        sb = _sample_outer_response()
        out = _normalize_tool_search_result_for_input(sb)
        inner = out["content"]
        assert set(inner.keys()).issubset(INNER_RESULT_KEYS)
        for ref in inner["tool_references"]:
            assert set(ref.keys()).issubset(REF_KEYS)
            assert "description" not in ref
            assert "input_schema" not in ref

    def test_handles_error_variant_inner_content(self):
        sb = _sample_outer_response_error()
        out = _normalize_tool_search_result_for_input(sb)
        assert set(out.keys()).issubset(OUTER_KEYS)
        assert "text" not in out
        inner = out["content"]
        assert inner == {
            "type": "tool_search_tool_result_error",
            "error_code": "execution_time_exceeded",
        }

    def test_handles_list_wrapped_content(self):
        """Some response shapes wrap inner content in a list — handle either form."""
        sb = {
            "type": "tool_search_tool_regex_tool_result",
            "tool_use_id": "srvtoolu_list",
            "content": [
                {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [
                        {"type": "tool_reference", "tool_name": "a", "description": "BAD"},
                    ],
                }
            ],
        }
        out = _normalize_tool_search_result_for_input(sb)
        assert isinstance(out["content"], list)
        assert out["content"][0]["tool_references"] == [
            {"type": "tool_reference", "tool_name": "a"}
        ]

    def test_preserves_outer_cache_control(self):
        sb = _sample_outer_response()
        sb["cache_control"] = {"type": "ephemeral"}
        out = _normalize_tool_search_result_for_input(sb)
        assert out["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Full message round-trip — convert_messages_to_anthropic
# ---------------------------------------------------------------------------
class TestConvertMessagesRoundTrip:
    def _build_assistant_msg(self, server_tool_blocks):
        return {
            "role": "assistant",
            "content": "Looking that up for you.",
            "server_tool_blocks": server_tool_blocks,
            "tool_calls": [],
        }

    def _walk(self, obj):
        """Yield every dict found anywhere in obj (recursive)."""
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self._walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._walk(v)

    def test_full_message_emits_canonical_outer_type(self):
        sb = _sample_outer_response()
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        # Find the tool_search_tool_result block in the output.
        ts_blocks = [
            d for d in self._walk(out_msgs)
            if isinstance(d, dict) and d.get("type") == "tool_search_tool_result"
        ]
        assert len(ts_blocks) == 1
        assert ts_blocks[0]["type"] == "tool_search_tool_result"

    def test_full_message_strips_all_response_only_fields(self):
        sb = _sample_outer_response(with_text=True, with_citations=True)
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        # No dict in the output should have a response-only field.
        forbidden = {"citations", "input_schema", "rank", "description"}
        for d in self._walk(out_msgs):
            for fld in forbidden:
                assert fld not in d, f"forbidden field {fld!r} in {d}"

    def test_text_field_does_not_leak_onto_tool_search_result(self):
        sb = _sample_outer_response(with_text=True, with_citations=True)
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        for d in self._walk(out_msgs):
            if d.get("type") == "tool_search_tool_result":
                assert "text" not in d
                assert "citations" not in d
            if d.get("type") == "tool_search_tool_search_result":
                assert "text" not in d
                assert "citations" not in d

    @pytest.mark.parametrize("variant", ["regex", "bm25"])
    def test_variant_suffixed_response_normalizes_to_canonical(self, variant):
        sb = _sample_outer_response(variant=variant)
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        # Should not have any variant-suffixed types in output.
        for d in self._walk(out_msgs):
            t = d.get("type")
            if isinstance(t, str) and t.startswith("tool_search_tool_") and t.endswith("_tool_result"):
                assert t == "tool_search_tool_result"

    def test_full_message_outputs_only_sdk_declared_keys(self):
        """Strict allowlist: every block type emitted should only contain
        keys declared by the corresponding SDK TypedDict."""
        sb = _sample_outer_response()
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        for d in self._walk(out_msgs):
            t = d.get("type")
            if t == "tool_search_tool_result":
                assert set(d.keys()).issubset(OUTER_KEYS), (
                    f"tool_search_tool_result has extra keys: "
                    f"{set(d.keys()) - OUTER_KEYS}"
                )
            elif t == "tool_search_tool_search_result":
                assert set(d.keys()).issubset(INNER_RESULT_KEYS)
            elif t == "tool_search_tool_result_error":
                assert set(d.keys()).issubset(INNER_ERROR_KEYS)
            elif t == "tool_reference":
                assert set(d.keys()).issubset(REF_KEYS)

    def test_error_variant_round_trip_is_clean(self):
        sb = _sample_outer_response_error()
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        for d in self._walk(out_msgs):
            t = d.get("type")
            if t == "tool_search_tool_result":
                assert set(d.keys()).issubset(OUTER_KEYS)
            if t == "tool_search_tool_result_error":
                assert set(d.keys()).issubset(INNER_ERROR_KEYS)
                assert "message" not in d

    def test_assistant_content_remains_text_block(self):
        """When the assistant message has plain text content, the conversion
        should still emit a text block alongside the rebuilt tool_search
        block — not lose the user-facing reply."""
        sb = _sample_outer_response()
        msg = self._build_assistant_msg([sb])
        _, out_msgs = convert_messages_to_anthropic(
            [{"role": "user", "content": "hi"}, msg]
        )
        assistant_msg = out_msgs[-1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        text_blocks = [b for b in assistant_msg["content"] if b.get("type") == "text"]
        assert any("Looking that up" in b.get("text", "") for b in text_blocks)
