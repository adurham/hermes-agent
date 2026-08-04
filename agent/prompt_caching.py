"""Anthropic prompt caching.

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Anthropic allows up to 4 cache_control
breakpoints. Strategy:
  1. System prompt (stable across all turns), split at the volatile boundary
     sentinel when present so only the stable prefix anchors the cache.
  2. Last entry of ``tools[]`` (anchors the system+tools prefix so a
     ToolSearch-driven tools[] mutation only forces ONE rebuild — without
     this, every following turn re-bills the entire message history at
     ``input_tokens`` rates instead of ``cache_read_input_tokens``)
  3-4. Last 2 non-system messages (rolling window)

Pure functions -- no class state, no AIAgent dependency.
"""

import copy
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PromptCachePlan:
    """Request-local message and tool sections with their cache markers."""

    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]

    @property
    def marker_count(self) -> int:
        """Wire-visible cache markers in this plan (computed on demand).

        Only tests consume this; keeping it lazy avoids walking every
        message part and tool schema on the per-request hot path.
        """
        return _count_cache_markers(self.messages, self.tools)


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


def _build_marker(ttl: str) -> Dict[str, str]:
    """Build a cache_control marker dict for the given TTL ('5m' or '1h')."""
    marker: Dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_system_cache_markers(
    message: dict,
    cache_marker: dict,
    static_system_prefix: str | None,
    *,
    native_anthropic: bool,
    mark_suffix: bool = True,
    fallback_to_whole: bool = True,
) -> int:
    """Mark the static system prefix (and optionally the full prompt).

    The system prompt remains one stored string. Splitting it only in the
    outgoing request keeps session persistence and non-Anthropic transports
    unchanged while making the stable prefix independently cacheable.

    ``mark_suffix=False`` is the tool-cache-plan layout: only the static
    prefix carries a marker, the volatile suffix rides unmarked (its
    breakpoint budget is spent on the tools array instead).

    ``fallback_to_whole=False`` skips marking entirely when the prefix
    split is not possible (no prefix, mismatched prefix, non-string
    content) instead of marking the whole message.

    When the prompt IS exactly the static prefix (empty suffix), the whole
    message is marked as a single block — never a two-part split with an
    empty text block, which Anthropic rejects.

    Returns the number of markers applied (0, 1, or 2).
    """
    content = message.get("content")
    if (
        isinstance(static_system_prefix, str)
        and static_system_prefix
        and isinstance(content, str)
        and content.startswith(static_system_prefix)
    ):
        suffix = content[len(static_system_prefix):]
        if suffix:
            suffix_part: dict = {"type": "text", "text": suffix}
            if mark_suffix:
                suffix_part["cache_control"] = cache_marker
            message["content"] = [
                {
                    "type": "text",
                    "text": static_system_prefix,
                    "cache_control": cache_marker,
                },
                suffix_part,
            ]
            return 2 if mark_suffix else 1
        # Empty suffix: the stored prompt IS the static prefix. Mark it as
        # one whole block — a [marked-prefix, ""] split would put an empty
        # text block on the wire (HTTP 400 on native Anthropic).
        _apply_cache_marker(message, cache_marker, native_anthropic=native_anthropic)
        return 1

    if not fallback_to_whole:
        return 0
    _apply_cache_marker(message, cache_marker, native_anthropic=native_anthropic)
    return 1


def strip_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove ``cache_control`` markers and undo decoration-produced list shapes.

    Used before re-applying decoration after a mid-turn provider failover so
    the mutated, undecorated shape (image shrink / ASCII cleanup / etc.) is
    preserved while markers match the *new* provider's cache policy (#72626).

    Flattening back to a plain string is restricted to the exact shapes
    :func:`apply_anthropic_cache_control` produces from string content —
    a single ``{"type": "text"}`` part, or the two-part ``[static, volatile]``
    system split — so the ``""``-join is provably byte-exact. Organic
    multi-part text (merged user turns, imported transcripts) and parts
    carrying extra keys (``citations`` etc.) keep their structure; only
    per-part markers are removed. Marker removal is copy-on-write on the
    part dicts: content parts may alias the persistent conversation history
    (the per-call copy is shallow), and stripping must never rewrite the
    stored transcript.

    Mutates the top-level message dicts of ``api_messages`` in place and
    returns the same list.
    """
    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        msg.pop("cache_control", None)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(part, dict) and "cache_control" in part for part in content):
            content = [
                {k: v for k, v in part.items() if k != "cache_control"}
                if isinstance(part, dict) and "cache_control" in part
                else part
                for part in content
            ]
            msg["content"] = content
        decoration_shape = content and all(
            isinstance(part, dict)
            and part.get("type", "text") == "text"
            and isinstance(part.get("text"), str)
            and set(part.keys()) <= {"type", "text"}
            for part in content
        ) and (
            len(content) == 1
            or (msg.get("role") == "system" and len(content) == 2)
        )
        if decoration_shape:
            msg["content"] = "".join(part["text"] for part in content)
    return api_messages


def strip_anthropic_tool_cache_control(tools: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    """Return copied tools without request-local Anthropic cache markers."""
    cleaned = copy.deepcopy(tools or [])
    for tool in cleaned:
        if isinstance(tool, dict):
            tool.pop("cache_control", None)
    return cleaned


def _count_cache_markers(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> int:
    """Count the wire-visible cache markers in a request-local plan."""
    count = sum(
        1
        for message in messages
        if isinstance(message, dict) and "cache_control" in message
    )
    count += sum(
        1
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict) and "cache_control" in part
    )
    return count + sum(
        1 for tool in tools if isinstance(tool, dict) and "cache_control" in tool
    )


def _completed_transaction_endpoint_indexes(
    messages: List[Dict[str, Any]], *, native_anthropic: bool,
) -> List[int]:
    """Select legal ends of completed tool runs and ordinary turns."""
    endpoints: List[int] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") == "system":
            index += 1
            continue

        if message.get("role") == "assistant" and message.get("tool_calls"):
            result_start = index + 1
            result_end = result_start
            while result_end < len(messages):
                result = messages[result_end]
                if not isinstance(result, dict) or result.get("role") != "tool":
                    break
                result_end += 1
            if result_end > result_start:
                endpoint = result_end - 1
                if _can_carry_marker(messages[endpoint], native_anthropic):
                    endpoints.append(endpoint)
            index = result_end
            continue

        if message.get("role") == "tool":
            while index < len(messages):
                result = messages[index]
                if not isinstance(result, dict) or result.get("role") != "tool":
                    break
                index += 1
            continue

        if message.get("role") == "user" and index + 1 < len(messages):
            index += 1
            continue

        if (
            message.get("role") == "assistant"
            and message.get("content") in (None, "")
        ):
            index += 1
            continue

        if _can_carry_marker(message, native_anthropic):
            endpoints.append(index)
        index += 1
    return endpoints


def build_prompt_cache_plan(
    api_messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]] | None,
    *,
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    static_system_prefix: str | None = None,
    direct_native_tool_cache: bool = False,
) -> PromptCachePlan:
    """Build isolated cache sections for one resolved request destination."""
    messages = copy.deepcopy(api_messages or [])
    strip_anthropic_cache_control(messages)
    planned_tools = strip_anthropic_tool_cache_control(tools)

    if not direct_native_tool_cache or not planned_tools:
        planned_messages = apply_anthropic_cache_control(
            messages,
            cache_ttl=cache_ttl,
            native_anthropic=native_anthropic,
            static_system_prefix=static_system_prefix,
        )
        return PromptCachePlan(messages=planned_messages, tools=planned_tools)

    marker = _build_marker(cache_ttl)
    if (
        messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
    ):
        # Tool-cache layout: only the static prefix carries a system-side
        # marker; the volatile suffix's budget is spent on the tools array.
        _apply_system_cache_markers(
            messages[0],
            marker,
            static_system_prefix,
            native_anthropic=True,
            mark_suffix=False,
            fallback_to_whole=False,
        )
    planned_tools[-1]["cache_control"] = dict(marker)
    for endpoint in _completed_transaction_endpoint_indexes(
        messages,
        native_anthropic=True,
    )[-2:]:
        _apply_cache_marker(messages[endpoint], marker, native_anthropic=True)

    return PromptCachePlan(messages=messages, tools=planned_tools)


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    reserve_tools_breakpoint: bool = True,
    static_system_prefix: str | None = None,
) -> List[Dict[str, Any]]:
    """Apply caching strategy to messages for Anthropic models.

    Places cache_control breakpoints on the system prompt + the last
    non-system messages. When ``reserve_tools_breakpoint`` is True, only
    2 message-side breakpoints are used so the caller can apply the 4th
    on the last entry of ``tools[]`` (see
    ``apply_anthropic_tools_cache_control``). Otherwise 3 message-side
    breakpoints are used (legacy behaviour).

    On the native Anthropic layout, the system prompt is split at the
    volatile-boundary sentinel (if present) so only the stable prefix
    anchors the cache. ``static_system_prefix`` is accepted for callers
    that use the prefix-match split path (e.g. ``build_prompt_cache_plan``)
    but the sentinel path takes precedence when the sentinel is present.

    Returns:
        Shallow copy of message list with selective deep copies of modified messages.
    """
    if not api_messages:
        return api_messages

    messages = list(api_messages)
    marker = _build_marker(cache_ttl)

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
        messages[0] = copy.deepcopy(messages[0])
        split_done = False
        if native_anthropic:
            split_done = _apply_split_system_marker(messages[0], marker)
        if not split_done:
            if static_system_prefix:
                breakpoints_used = _apply_system_cache_markers(
                    messages[0],
                    marker,
                    static_system_prefix,
                    native_anthropic=native_anthropic,
                )
            else:
                _strip_system_sentinel(messages[0])
                _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
                breakpoints_used = 1
        else:
            breakpoints_used = 1

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
        # Deep-copy only the messages that receive a marker — the caller's
        # history must never be mutated (the rest of the list stays shared).
        messages[idx] = copy.deepcopy(messages[idx])
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
    marker = _build_marker(cache_ttl)
    last = out[-1]
    if isinstance(last, dict):
        last["cache_control"] = marker
    return out
