"""Canonical byte-stable serializer for the exo provider wire path.

WHY THIS EXISTS (read before "tidying up" the key order — the order is the
contract, not an implementation detail):

The exo inference server's prompt prefix cache is keyed by a NEAR-EXACT
match on the serialized request body, with only ~2 trailing tokens of
tolerance. A round-4 audit against the live cluster proved the sensitivity
empirically: 6 of 9 tested serialization variants zeroed ``cached_tokens``
for the ENTIRE prompt. Any delta at all — a reordered JSON field, an
injected whitespace byte, a message reordering, a placeholder value —
silently forfeits the whole cached prefix. There is no error, no warning;
just a massive cost/latency regression on every subsequent turn.

The client therefore MUST serialize byte-identically for the same logical
prompt, every single time, regardless of which construction path built the
message dicts. Python dicts preserve insertion order and different build
sites (the assistant-message builder, the replay-copy path, the reapply
path, session resume) insert the same keys in different orders — so dict
insertion order is NOT a stable serialization key. This module rebuilds
every message dict in one explicit, frozen field order before the bytes
are handed to the OpenAI SDK (which serializes dicts in insertion order;
nothing else on the wire path sorts keys).

The exo pad-omission (commit bdc9b6f1fc) is integrated here rather than
left as an upstream-only concern: ``omits_reasoning_pad_for_provider`` —
the SAME predicate that owns the pad decision at every build site — gates
this serializer, and when it matches, a whitespace-only ``reasoning_content``
value (the synthetic single-space pad, or an empty string) is omitted
entirely. That is the identical invariant the shipped strip logic enforces
( ``apply_reasoning_content_policy``: ``omit_pad and not existing.strip()``
→ pop the key); this chokepoint is last-touch defense-in-depth so no build
site that missed the upstream strip can re-defeat the prefix cache at the
wire. Genuine (non-whitespace) reasoning is echoed verbatim, never trimmed.

Scope is fail-safe: ONLY the exact ``exo`` / ``custom:exo`` provider keys
(via the shared predicate — there is deliberately no second predicate here)
get canonicalized. Every other provider's bytes are returned untouched.

The golden byte contract is frozen by
``tests/agent/test_exo_canonical_serializer.py`` — any change to field
order, nesting order, or the pad invariant turns CI red by design.
"""

from __future__ import annotations

from typing import Any

from agent.message_sanitization import omits_reasoning_pad_for_provider

# Frozen top-level message field order for the exo wire payload.
# DO NOT REORDER. DO NOT "SORT ALPHABETICALLY FOR CLEANLINESS". The exo
# server's near-exact-match prefix cache (~2 trailing tokens of tolerance)
# turns any byte delta into a full cache miss (round-4 audit: 6 of 9
# serialization variants zeroed cached_tokens for the whole prompt).
_CANONICAL_MESSAGE_KEY_ORDER: tuple[str, ...] = (
    "role",
    "content",
    "reasoning_content",
    "tool_calls",
    "tool_call_id",
    "name",
)

# Frozen nested order for OpenAI tool_call entries and their function body.
# A reordered nested dict breaks the wire bytes just as surely as a
# reordered top-level dict.
_CANONICAL_TOOL_CALL_KEY_ORDER: tuple[str, ...] = ("id", "type", "function")
_CANONICAL_FUNCTION_KEY_ORDER: tuple[str, ...] = ("name", "arguments")


def _canonical_tool_call(tool_call: Any) -> Any:
    """Rebuild one tool_call entry in the frozen nested key order.

    Unknown/extra keys inside the entry (and inside ``function``) are
    emitted after the known ones, sorted by key name — deterministic
    regardless of the construction path's insertion order.
    """
    if not isinstance(tool_call, dict):
        return tool_call
    rebuilt: dict[str, Any] = {}
    for key in _CANONICAL_TOOL_CALL_KEY_ORDER:
        if key not in tool_call:
            continue
        if key == "function":
            function = tool_call["function"]
            if isinstance(function, dict):
                rebuilt_function: dict[str, Any] = {}
                for fn_key in _CANONICAL_FUNCTION_KEY_ORDER:
                    if fn_key in function:
                        rebuilt_function[fn_key] = function[fn_key]
                for fn_key in sorted(function):
                    if fn_key not in rebuilt_function:
                        rebuilt_function[fn_key] = function[fn_key]
                rebuilt["function"] = rebuilt_function
            else:
                rebuilt["function"] = function
        else:
            rebuilt[key] = tool_call[key]
    for key in sorted(tool_call):
        if key not in rebuilt:
            rebuilt[key] = tool_call[key]
    return rebuilt


def _canonical_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one message dict in the frozen key order.

    Unknown/extra keys are emitted after the known ones, sorted by key
    name — NOT by insertion order, which varies by construction path.
    """
    rebuilt: dict[str, Any] = {}
    for key in _CANONICAL_MESSAGE_KEY_ORDER:
        if key not in msg:
            continue
        if key == "tool_calls":
            tool_calls = msg["tool_calls"]
            if isinstance(tool_calls, list):
                rebuilt["tool_calls"] = [_canonical_tool_call(tc) for tc in tool_calls]
            else:
                rebuilt["tool_calls"] = tool_calls
        elif key == "reasoning_content":
            value = msg["reasoning_content"]
            # Pad-strip, last-touch form. The pad QUESTION is decided by the
            # same single predicate every build site uses
            # (omits_reasoning_pad_for_provider — exactly one source of
            # truth, no second exo predicate lives in this module). When it
            # matches, a whitespace-only reasoning_content (the synthetic
            # single-space pad, or "") must not reach the wire: it lands at
            # the first byte of the re-fed region and forfeits the server's
            # entire cached prefix (LCP defeat; cached_tokens 351 -> 0 live
            # evidence in commit bdc9b6f1fc). Genuine reasoning is echoed
            # verbatim. This duplicates the strip's INVARIANT, not its
            # decision logic — the shipped strip sites keep their behavior
            # untouched, and on the well-formed path this is a no-op.
            if isinstance(value, str) and not value.strip():
                # Deliberately OMITTED. The tail loop below must not
                # re-add it: known keys are excluded there by name, not
                # by rebuilt-membership.
                continue
            rebuilt[key] = value
        else:
            rebuilt[key] = msg[key]
    # Unknown/extra keys, after the known ones, sorted by name. Known keys
    # are excluded BY NAME (not by "not in rebuilt") so a key the ordered
    # pass deliberately omitted — the whitespace-only reasoning_content
    # pad — stays omitted.
    for key in sorted(msg):
        if key in _CANONICAL_MESSAGE_KEY_ORDER:
            continue
        if key in rebuilt:
            continue
        rebuilt[key] = msg[key]
    return rebuilt


def canonicalize_exo_messages(
    messages: list[Any], provider: Any = None
) -> list[Any]:
    """Return the canonical, byte-stable form of ``messages`` for a provider.

    Pure and deterministic: same input dict CONTENT => same output bytes,
    always, regardless of input key insertion order. Non-dict entries pass
    through untouched.

    Fail-safe scope: when ``provider`` is not exactly ``exo`` /
    ``custom:exo`` (per the shared ``omits_reasoning_pad_for_provider``
    predicate), ``messages`` is returned unmodified — every non-exo
    provider is byte-for-byte unchanged, including any
    ``reasoning_content`` pad a require-side provider expects.
    """
    if not omits_reasoning_pad_for_provider(provider):
        return messages
    return [
        _canonical_message(msg) if isinstance(msg, dict) else msg
        for msg in messages
    ]