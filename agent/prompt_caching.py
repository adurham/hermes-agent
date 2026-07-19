"""Anthropic prompt caching.

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Anthropic allows up to 4 cache_control
breakpoints. Strategy:
  1. System prompt (stable across all turns)
  2. Last entry of ``tools[]`` (anchors the system+tools prefix so a
     ToolSearch-driven tools[] mutation only forces ONE rebuild — without
     this, every following turn re-bills the entire message history at
     ``input_tokens`` rates instead of ``cache_read_input_tokens``)
  3-4. Last 2 non-system messages (rolling window)

Pure functions -- no class state, no AIAgent dependency.
"""

import copy
from typing import Any, Dict, List, Optional, Tuple


# Boundary marker injected by ``agent.system_prompt.build_system_prompt``
# between the STABLE (+context) tiers and the VOLATILE tier (memory snapshot,
# user profile, daily timestamp). It lets the Anthropic cache layer place the
# system cache_control breakpoint at the END of the stable prefix instead of
# after the whole system block. Result: the stable identity + tool guidance
# (byte-stable across sessions/days) stays cache-warm even when memory or the
# date line changes — only the small volatile tail re-writes.
#
# The marker is internal-only: it is ALWAYS either stripped (non-split path)
# or consumed by the split (native Anthropic path) before the system prompt
# is sent, so the model never sees it. ``_SENTINEL_FULL`` includes the
# surrounding blank-line spacing so that stripping it reproduces the exact
# ``"\n\n"`` separator the old flat join produced — keeping sent bytes
# identical to pre-change behaviour for every non-split transport.
SYSTEM_VOLATILE_SENTINEL = "<<<HERMES_SYS_VOLATILE_BOUNDARY>>>"
_SENTINEL_FULL = "\n\n" + SYSTEM_VOLATILE_SENTINEL + "\n\n"


def strip_volatile_sentinel(text: str) -> str:
    """Remove the volatile-boundary sentinel, restoring the plain ``\\n\\n``
    separator. No-op when the sentinel is absent. Used on every non-split
    transport so the marker never reaches the model."""
    if SYSTEM_VOLATILE_SENTINEL in text:
        return text.replace(_SENTINEL_FULL, "\n\n")
    return text


def split_system_for_cache(text: str) -> Optional[Tuple[str, str]]:
    """Split the system prompt at the volatile boundary.

    Returns ``(stable_head, volatile_tail)`` where concatenating
    ``stable_head + volatile_tail`` reproduces the exact bytes of the
    stripped (model-visible) prompt — i.e. ``stable_head`` carries the
    trailing ``"\\n\\n"`` separator. Returns ``None`` when no sentinel is
    present (volatile tier empty, or an older stored prompt from before
    this change), in which case callers fall back to a single block.
    """
    idx = text.find(_SENTINEL_FULL)
    if idx < 0:
        return None
    head = text[:idx] + "\n\n"
    tail = text[idx + len(_SENTINEL_FULL):]
    return head, tail


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool" and native_anthropic:
        # Native Anthropic layout: top-level marker; the adapter moves it
        # inside the tool_result block.
        msg["cache_control"] = cache_marker
        return

    if content is None or content == "":
        if role == "tool" and not native_anthropic:
            # OpenRouter rejects top-level cache_control on role:tool (silent
            # hang) and an empty message has no content part to carry the
            # marker — skip. Non-empty tool content falls through below and
            # gets the marker on a content part, which OpenRouter honors.
            return
        if role == "assistant" and not native_anthropic:
            # Empty assistant turns are pure tool_calls. A top-level marker
            # here is ignored on the envelope layout, so skip.
            return
        msg["cache_control"] = cache_marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cache_marker


def _system_text(msg: dict) -> Optional[str]:
    """Return the flat text of a system message whether its content is a
    plain string or a single text block. Returns None for shapes we won't
    touch (multi-block already, non-text), leaving them to the legacy path."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and len(content) == 1:
        blk = content[0]
        if isinstance(blk, dict) and blk.get("type") == "text" and "cache_control" not in blk:
            return blk.get("text", "")
    return None


def _strip_system_sentinel(msg: dict) -> None:
    """Strip the volatile sentinel from a system message in place, so the
    marker never reaches the model on the single-block (non-split) path."""
    text = _system_text(msg)
    if text is None or SYSTEM_VOLATILE_SENTINEL not in text:
        return
    msg["content"] = strip_volatile_sentinel(text)


def _apply_split_system_marker(msg: dict, cache_marker: dict) -> bool:
    """Split a system message into ``[{stable+context, cache_control}, {volatile}]``.

    Returns True when the split was applied (sentinel found and content was a
    splittable string/single-text-block), False otherwise so the caller can
    fall back to the legacy single-block marking.
    """
    text = _system_text(msg)
    if text is None:
        return False
    parts = split_system_for_cache(text)
    if parts is None:
        return False
    stable_head, volatile_tail = parts
    blocks: List[Dict[str, Any]] = [
        {"type": "text", "text": stable_head, "cache_control": cache_marker},
    ]
    # Only emit the volatile block when it carries content; an empty tail
    # would just add a useless block.
    if volatile_tail:
        blocks.append({"type": "text", "text": volatile_tail})
    msg["content"] = blocks
    return True


def _can_carry_marker(msg: dict, native_anthropic: bool) -> bool:
    """True if a marker on this message is actually honored by the provider.

    On the native Anthropic layout every message works (top-level markers are
    relocated by the adapter). On the envelope layout (OpenRouter et al.) only
    markers inside content parts are honored: empty-content messages (e.g.
    assistant turns that are pure tool_calls) and empty tool messages would
    receive a top-level marker the provider ignores — wasting one of the four
    breakpoints. Skip those so the breakpoints land on messages that count.
    """
    if native_anthropic:
        return True
    content = msg.get("content")
    if content is None or content == "":
        return False
    if isinstance(content, list):
        # _apply_cache_marker only marks the LAST content part, so the carrier
        # predicate must agree: a list whose last element isn't a dict cannot
        # actually receive a marker and would waste a breakpoint. Mirror the
        # `content` truthiness + last-element-dict check in _apply_cache_marker.
        return bool(content) and isinstance(content[-1], dict)
    return isinstance(content, str)


def _build_cache_marker(cache_ttl: str = "5m") -> Dict[str, str]:
    """Build a cache_control marker dict for the given TTL ('5m' or '1h')."""
    marker: Dict[str, str] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    reserve_tools_breakpoint: bool = True,
) -> List[Dict[str, Any]]:
    """Apply caching strategy to messages for Anthropic models.

    Places cache_control breakpoints on the system prompt + the last
    non-system messages. When ``reserve_tools_breakpoint`` is True, only
    2 message-side breakpoints are used so the caller can apply the 4th
    on the last entry of ``tools[]`` (see
    ``apply_anthropic_tools_cache_control``). Otherwise 3 message-side
    breakpoints are used (legacy behaviour).

    Returns:
        Deep copy of messages with cache_control breakpoints injected.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_cache_marker(cache_ttl)

    breakpoints_used = 0
    if messages[0].get("role") == "system":
        # Stable|volatile split: on the native Anthropic layout, emit the
        # system prompt as a two-block content array
        # ``[{stable+context, cache_control}, {volatile}]`` so the cache
        # breakpoint sits at the END of the stable prefix rather than after
        # the whole block. The volatile tail stays cached cumulatively by the
        # first message breakpoint, so multi-turn within a session is
        # unchanged — but a memory edit or date rollover no longer cold-
        # rewrites the stable identity + tool guidance. Breakpoint COUNT is
        # unchanged (still one on the system param). Falls back to a single
        # marked block when no sentinel is present (volatile empty, or an
        # older stored prompt). Non-native transports strip the sentinel and
        # take the legacy single-block path.
        split_done = False
        if native_anthropic:
            split_done = _apply_split_system_marker(messages[0], marker)
        if not split_done:
            _strip_system_sentinel(messages[0])
            _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    # Reserve one breakpoint for tools[] so a tools mutation only forces
    # one rebuild, not every subsequent message re-bill.
    budget = 4 - breakpoints_used - (1 if reserve_tools_breakpoint else 0)
    non_sys = [
        i
        for i in range(len(messages))
        if messages[i].get("role") != "system"
        and _can_carry_marker(messages[i], native_anthropic=native_anthropic)
    ]
    for idx in non_sys[-budget:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)

    return messages


def apply_anthropic_tools_cache_control(
    anthropic_tools: List[Dict[str, Any]],
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    """Mark the last entry in ``tools[]`` with ``cache_control`` so the
    ``system + tools`` prefix is cached as a unit.

    Why this matters: Anthropic caches by request prefix in the order
    ``system → tools → messages``. Without a breakpoint AT or AFTER
    ``tools[]``, any change to ``tools[]`` (a ToolSearch ``select:`` load,
    an MCP reconnect, a subagent toolset switch) invalidates the cache for
    every subsequent turn — the message history is forced through
    ``input_tokens`` instead of ``cache_read_input_tokens`` until the
    session ends. With this breakpoint, a tools mutation costs ONE
    rebuild and the cache re-establishes on the next turn.

    Mutates a copy; safe to call on the same list passed to the API.
    """
    if not anthropic_tools:
        return anthropic_tools
    out = copy.deepcopy(anthropic_tools)
    marker = _build_cache_marker(cache_ttl)
    last = out[-1]
    if isinstance(last, dict):
        last["cache_control"] = marker
    return out
