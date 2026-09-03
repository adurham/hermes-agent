"""Golden byte contract for the exo canonical serializer.

The exo inference server's prefix cache is keyed by a near-exact match on
the serialized request body (~2 trailing tokens of tolerance). A round-4
audit against the live cluster proved that 6 of 9 tested serialization
variants zeroed ``cached_tokens`` for the entire prompt — any byte delta
(reordered JSON field, injected whitespace, message reordering) silently
forfeits the whole cached prefix.

These tests FREEZE that contract as exact byte strings:

* (a) idempotency/stability — same logical prompt serialized twice yields
  byte-identical output, INCLUDING when the two inputs are semantically
  identical but built with different dict insertion orders (the case that
  actually proves canonicalization rather than mere determinism).
* (b) frozen golden fixtures — 6 representative logical conversations
  pinned to exact expected byte strings (json.dumps(...).encode()).
  Any future drift in field order, nesting order, or whitespace turns CI
  red. These fixtures are the contract; regenerate them ONLY with a
  corresponding FORK.md entry explaining why the cache-warming cost of the
  change is acceptable.
* (c) drift detector — mutated payloads (reordered fields, injected
  whitespace) MUST differ from the golden bytes, proving the fixtures have
  teeth against the round-4 failure class.
* (d) non-exo fail-safe — non-exo providers keep current behavior
  byte-for-byte (including the single-space ``reasoning_content`` pad a
  require-side provider expects), and the exo provider match is EXACT
  (``exonerate`` / ``exo-foo`` are NOT treated as exo).

A mutation check was run against this suite during development: with the
serializer's ordering guarantee neutered (keys emitted in reversed order),
the fixture tests FAIL — see FORK.md for the verbatim failure output.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.exo_canonical_serializer import (
    _CANONICAL_MESSAGE_KEY_ORDER,
    canonicalize_exo_messages,
)
from agent.message_sanitization import omits_reasoning_pad_for_provider
from agent.transports import get_transport

# ---------------------------------------------------------------------------
# Shared logical conversations — each defined ONCE as content, then built
# twice with DIFFERENT dict insertion orders in the stability tests.
# ---------------------------------------------------------------------------


def _wire_bytes(messages: list[Any], provider: str = "exo") -> bytes:
    """Serialize the canonical form the way the OpenAI SDK would."""
    return json.dumps(
        canonicalize_exo_messages(messages, provider), separators=(",", ":")
    ).encode()


def _reordered(msg: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a message dict with REVERSED key insertion order."""
    return {k: msg[k] for k in reversed(list(msg.keys()))}


# 1. No-reasoning turn: assistant turn that carried no reasoning at all
#    (the exact shape that used to get the cache-killing single-space pad).
NO_REASONING_TURN = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is 2+2?"},
    {"role": "assistant", "content": "4."},
]

# 2. Reasoning turn: genuine (non-pad) reasoning echoed verbatim.
REASONING_TURN = [
    {"role": "user", "content": "Think step by step: what is 12*11?"},
    {
        "role": "assistant",
        "content": "132.",
        "reasoning_content": "12*11 = 12*10 + 12 = 120 + 12 = 132.",
    },
]

# 3. Tool-call turn with nested tool_calls structure.
TOOL_CALL_TURN = [
    {"role": "user", "content": "List the files in /tmp."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_001",
                "type": "function",
                "function": {
                    "name": "list_files",
                    "arguments": '{"path": "/tmp"}',
                },
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_001", "content": "a.txt\nb.txt"},
]

# 4. Multi-turn conversation with reasoning + tool calls interleaved.
MULTI_TURN = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "q1"},
    {
        "role": "assistant",
        "content": "",
        "reasoning_content": "real thought",
        "tool_calls": [
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_a", "content": "r1"},
    {"role": "user", "content": "q2"},
    {"role": "assistant", "content": "final answer"},
]

# 5. Message-ordering variant: tool result arrives before a second user
#    turn — same content as MULTI_TURN but a different logical shape, and
#    with an extra unknown key that must land AFTER the known keys sorted.
ORDERING_VARIANT = [
    {"role": "user", "content": "hello"},
    {
        "role": "assistant",
        "tool_calls": [
            {
                "function": {"arguments": "{\"x\": 1}", "name": "g"},
                "type": "function",
                "id": "call_b",
            }
        ],
        "content": "",
    },
    {"role": "tool", "content": "ok", "tool_call_id": "call_b"},
    {
        "role": "assistant",
        "content": "done",
        "custom_provider_field": "z",
        "api_content": "sidecar",
    },
]

ALL_FIXTURES = {
    "no_reasoning_turn": NO_REASONING_TURN,
    "reasoning_turn": REASONING_TURN,
    "tool_call_turn": TOOL_CALL_TURN,
    "multi_turn": MULTI_TURN,
    "ordering_variant": ORDERING_VARIANT,
}


# ---------------------------------------------------------------------------
# (a) Idempotency / stability — including the different-insertion-order case
# ---------------------------------------------------------------------------


class TestStability:
    def test_same_input_serializes_identically_twice(self) -> None:
        msgs = [dict(m) for m in MULTI_TURN]
        first = _wire_bytes(msgs)
        second = _wire_bytes([dict(m) for m in MULTI_TURN])
        assert first == second

    @pytest.mark.parametrize(
        "name", sorted(ALL_FIXTURES.keys()), ids=sorted(ALL_FIXTURES.keys())
    )
    def test_different_insertion_order_same_bytes(self, name: str) -> None:
        """THE canonicalization test: semantically identical inputs built
        with different dict insertion orders must produce byte-identical
        output. Dict insertion order varies by construction path, so this
        is the property the wire actually needs."""
        canonical_form = ALL_FIXTURES[name]
        variant_form = [_reordered(m) for m in canonical_form]
        # Sanity: the variants really are insertion-order-different.
        assert list(variant_form[0].keys()) != list(canonical_form[0].keys())
        assert _wire_bytes(canonical_form) == _wire_bytes(variant_form)

    def test_nested_tool_call_insertion_order_same_bytes(self) -> None:
        """A reordered nested tool_calls dict (function keys reversed)
        breaks the bytes just as surely as a reordered top-level one."""
        straight = _wire_bytes(TOOL_CALL_TURN)
        nested_reordered = json.loads(json.dumps(TOOL_CALL_TURN))
        nested_reordered[1]["tool_calls"][0] = {
            "function": {
                "arguments": '{"path": "/tmp"}',
                "name": "list_files",
            },
            "type": "function",
            "id": "call_001",
        }
        assert _wire_bytes(nested_reordered) == straight


# ---------------------------------------------------------------------------
# (b) Frozen golden fixtures — exact expected byte strings
# ---------------------------------------------------------------------------

# These were generated from the serializer AFTER it was implemented and
# verified; they freeze the contract. Comparisons are exact `==` on bytes.
GOLDEN: dict[str, bytes] = {
    "no_reasoning_turn": json.dumps(
        [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4."},
        ],
        separators=(",", ":"),
    ).encode(),
    "reasoning_turn": json.dumps(
        [
            {"role": "user", "content": "Think step by step: what is 12*11?"},
            {
                "role": "assistant",
                "content": "132.",
                "reasoning_content": "12*11 = 12*10 + 12 = 120 + 12 = 132.",
            },
        ],
        separators=(",", ":"),
    ).encode(),
    "tool_call_turn": json.dumps(
        [
            {"role": "user", "content": "List the files in /tmp."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": '{"path": "/tmp"}',
                        },
                    }
                ],
            },
            {"role": "tool", "content": "a.txt\nb.txt", "tool_call_id": "call_001"},
        ],
        separators=(",", ":"),
    ).encode(),
    "multi_turn": json.dumps(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "real thought",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "content": "r1", "tool_call_id": "call_a"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "final answer"},
        ],
        separators=(",", ":"),
    ).encode(),
    "ordering_variant": json.dumps(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "g", "arguments": "{\"x\": 1}"},
                    }
                ],
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_b"},
            {
                "role": "assistant",
                "content": "done",
                "api_content": "sidecar",
                "custom_provider_field": "z",
            },
        ],
        separators=(",", ":"),
    ).encode(),
    "pad_stripped_turn": json.dumps(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ],
        separators=(",", ":"),
    ).encode(),
}


class TestGoldenFixtures:
    @pytest.mark.parametrize("name", sorted(GOLDEN.keys()), ids=sorted(GOLDEN.keys()))
    def test_exact_bytes(self, name: str) -> None:
        if name == "pad_stripped_turn":
            # A single-space reasoning_content pad (the round-3 pad) must be
            # OMITTED from the canonical exo bytes entirely.
            msgs = [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "hey",
                    "reasoning_content": " ",
                },
            ]
        else:
            msgs = ALL_FIXTURES[name]
        assert _wire_bytes(msgs) == GOLDEN[name]

    def test_key_order_constant_is_frozen(self) -> None:
        assert _CANONICAL_MESSAGE_KEY_ORDER == (
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
            "tool_call_id",
            "name",
        )

    def test_wire_transport_output_matches_serializer_bytes(self) -> None:
        """The wired-in chokepoint (convert_messages via build_kwargs) must
        produce byte-identical output to the serializer for representative
        inputs — proves the wiring, not just the function."""
        transport = get_transport("chat_completions")
        assert transport is not None
        kwargs = transport.build_kwargs(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[dict(m) for m in MULTI_TURN],
            provider="exo",
        )
        wire = json.dumps(kwargs["messages"], separators=(",", ":")).encode()
        assert wire == GOLDEN["multi_turn"]

    def test_wire_transport_output_matches_serializer_reordered(self) -> None:
        transport = get_transport("chat_completions")
        assert transport is not None
        kwargs = transport.build_kwargs(
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            messages=[_reordered(m) for m in TOOL_CALL_TURN],
            provider="exo",
        )
        wire = json.dumps(kwargs["messages"], separators=(",", ":")).encode()
        assert wire == GOLDEN["tool_call_turn"]


# ---------------------------------------------------------------------------
# (c) Drift detector — mutated payloads MUST differ from the golden bytes
# ---------------------------------------------------------------------------


class TestDriftDetector:
    """Proves the fixtures have teeth: the round-4 failure class (a field
    reorder or a whitespace injection reaching the wire) would have been
    caught by exactly this comparison."""

    def test_reordered_fields_produce_different_bytes(self) -> None:
        golden = GOLDEN["reasoning_turn"]
        mutated = json.dumps(
            [
                {"role": "user", "content": "Think step by step: what is 12*11?"},
                {
                    "reasoning_content": "12*11 = 12*10 + 12 = 120 + 12 = 132.",
                    "content": "132.",
                    "role": "assistant",
                },
            ],
            separators=(",", ":"),
        ).encode()
        assert mutated != golden

    def test_injected_whitespace_produces_different_bytes(self) -> None:
        golden = GOLDEN["no_reasoning_turn"]
        mutated = json.dumps(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is 2+2?"},
                # a single-space pad injected into a no-reasoning turn
                {"role": "assistant", "content": "4.", "reasoning_content": " "},
            ],
            separators=(",", ":"),
        ).encode()
        assert mutated != golden

    def test_injected_whitespace_in_content_produces_different_bytes(self) -> None:
        golden = GOLDEN["tool_call_turn"]
        mutated = json.dumps(
            [
                {"role": "user", "content": "List the files in /tmp."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_001",
                            "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path": "/tmp"}',
                            },
                        }
                    ],
                },
                # trailing space injected into the tool result
                {"role": "tool", "tool_call_id": "call_001", "content": "a.txt\nb.txt "},
            ],
            separators=(",", ":"),
        ).encode()
        assert mutated != golden

    def test_reordered_nested_tool_call_produces_different_bytes(self) -> None:
        golden = GOLDEN["tool_call_turn"]
        mutated = json.dumps(
            [
                {"role": "user", "content": "List the files in /tmp."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            # function keys swapped relative to canonical
                            "function": {
                                "arguments": '{"path": "/tmp"}',
                                "name": "list_files",
                            },
                            "type": "function",
                            "id": "call_001",
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_001", "content": "a.txt\nb.txt"},
            ],
            separators=(",", ":"),
        ).encode()
        assert mutated != golden


# ---------------------------------------------------------------------------
# (d) Non-exo fail-safe + exact-match predicate
# ---------------------------------------------------------------------------


class TestNonExoFailSafe:
    def test_non_exo_provider_output_untouched(self) -> None:
        msgs = [dict(m) for m in MULTI_TURN]
        out = canonicalize_exo_messages(msgs, "anthropic")
        # Identity: the SAME list object, not a rebuilt copy.
        assert out is msgs
        # And the single-space pad survives for a require-side non-exo
        # provider (current shipped behavior).
        padded = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey", "reasoning_content": " "},
        ]
        out_padded = canonicalize_exo_messages(padded, "ollama-cloud")
        assert out_padded[1]["reasoning_content"] == " "
        assert json.dumps(out_padded, separators=(",", ":")).encode() == json.dumps(
            padded, separators=(",", ":")
        ).encode()

    def test_none_provider_output_untouched(self) -> None:
        msgs = [dict(m) for m in MULTI_TURN]
        assert canonicalize_exo_messages(msgs, None) is msgs

    def test_unknown_provider_output_untouched(self) -> None:
        msgs = [dict(m) for m in MULTI_TURN]
        assert canonicalize_exo_messages(msgs, "custom:myrelay") is msgs

    @pytest.mark.parametrize("lookalike", ["exonerate", "exo-foo", "my-exo", "EXO "])
    def test_exact_match_only(self, lookalike: str) -> None:
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey", "reasoning_content": " "},
        ]
        if lookalike == "EXO ":
            # Case/whitespace-normalized EXACT match: "EXO " IS exo.
            assert omits_reasoning_pad_for_provider(lookalike)
            out = canonicalize_exo_messages(msgs, lookalike)
            assert "reasoning_content" not in out[1]
            return
        # Everything else must NOT be treated as exo — bytes untouched.
        assert not omits_reasoning_pad_for_provider(lookalike)
        out = canonicalize_exo_messages(msgs, lookalike)
        assert out is msgs, f"{lookalike!r} must NOT be treated as exo"

    def test_custom_exo_prefix_is_exo(self) -> None:
        assert omits_reasoning_pad_for_provider("custom:exo")
        assert omits_reasoning_pad_for_provider("exo")
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey", "reasoning_content": " "},
        ]
        out = canonicalize_exo_messages(msgs, "custom:exo")
        assert "reasoning_content" not in out[1]

    def test_transport_non_exo_unchanged(self) -> None:
        """Through the wired chokepoint: a non-exo provider's messages come
        out byte-identical to what went in (pad still present)."""
        transport = get_transport("chat_completions")
        assert transport is not None
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey", "reasoning_content": " "},
        ]
        kwargs = transport.build_kwargs(
            model="deepseek-chat",
            messages=msgs,
            provider="deepseek",
        )
        assert kwargs["messages"][1]["reasoning_content"] == " "
        assert json.dumps(kwargs["messages"], separators=(",", ":")).encode() == (
            json.dumps(msgs, separators=(",", ":")).encode()
        )