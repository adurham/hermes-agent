"""Anthropic prompt caching — pure functions, no AIAgent dependency.

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Anthropic allows up to 4 cache_control
breakpoints. Strategy:
  1. System prompt (stable across all turns), split at the static system
     prefix (``build_system_prompt_parts()['stable']``) when available so
     only the stable prefix anchors the cache.
  2. Last entry of ``tools[]`` (anchors the system+tools prefix so a
     ToolSearch-driven tools[] mutation only forces ONE rebuild — without
     this, every following turn re-bills the entire message history at
     ``input_tokens`` rates instead of ``cache_read_input_tokens``)
  3-4. Last 2 non-system messages (rolling window)
"""

import copy
from dataclasses import dataclass
from typing import Any, Dict, List

from agent.prompt_cache_boundary import find_stable_prefix


# LEGACY back-compat only (FORK.md 2026-06-02, retired 2026-08-04): the fork
# used to inject this in-band boundary marker into the built system prompt so
# the cache layer could split at it. The mechanism is gone — the structured
# split (``build_system_prompt_parts()`` + ``static_system_prefix``) replaced
# it — but system prompts persisted by sentinel-era sessions still CONTAIN
# the marker text. ``strip_volatile_sentinel`` is the single surviving strip:
# it runs once, where a stored prompt is adopted from the session DB
# (``conversation_loop._restore_or_build_system_prompt``), so historical
# sessions never leak the marker to the model. Do not add new call sites and
# do not re-introduce insertion.
SYSTEM_VOLATILE_SENTINEL = "<<<HERMES_SYS_VOLATILE_BOUNDARY>>>"
_SENTINEL_FULL = "\n\n" + SYSTEM_VOLATILE_SENTINEL + "\n\n"


def strip_volatile_sentinel(text: str) -> str:
    """Remove the legacy volatile-boundary sentinel, restoring the plain
    ``\\n\\n`` separator (byte-identical to what sentinel-era code sent to
    the model). No-op when the sentinel is absent. Backward compatibility
    for stored sessions only — see the comment above."""
    if SYSTEM_VOLATILE_SENTINEL in text:
        return text.replace(_SENTINEL_FULL, "\n\n")
    return text


@dataclass(frozen=True)
class PromptCachePlan:
    """Request-local message and tool sections with their cache markers."""

    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]


def envelope_tool_part_cache_markers_supported(provider: str | None, base_url: str | None) -> bool:
    """Whether the envelope-layout route honors part-level markers on role:tool.

    OpenRouter/Nous Portal relocate a part-level ``cache_control`` onto the ``tool_result``
    block; LiteLLM-style proxies copy parts verbatim, so it lands at ``tool_result.content[0]``
    (non-retryable 400). There, role:tool carries no part markers and the budget reallocates.
    """
    from agent.agent_runtime_helpers import _is_litellm_route

    return not _is_litellm_route((provider or "").strip().lower(), base_url or "")


def _text_part(text: str, cache_marker: dict | None = None) -> dict:
    part: dict = {"type": "text", "text": text}
    if cache_marker is not None:
        part["cache_control"] = cache_marker
    return part


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False,
                        tool_part_markers: bool = True) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool" and not native_anthropic and not tool_part_markers:
        # LiteLLM-style envelope: a part marker → tool_result.content[0] → non-retryable 400.
        return
    if (role == "tool" and native_anthropic) or content is None or content == "":
        # Native role:tool: top-level marker, the adapter moves it inside tool_result. Empty
        # content: no part can carry it, and OpenRouter rejects a top-level marker on role:tool
        # (silent hang) and ignores it on empty assistant turns — skip those on the envelope.
        if not (role in ("tool", "assistant") and not native_anthropic):
            msg["cache_control"] = cache_marker
    elif isinstance(content, str):
        stable_prefix = find_stable_prefix(content) if role == "user" else None
        if stable_prefix is not None and content[len(stable_prefix):].strip():
            # Builder-declared boundary: the scaffold carries the breakpoint and the volatile
            # tail rides unmarked. Request-local only — the stored message stays a string.
            msg["content"] = [_text_part(stable_prefix, cache_marker), _text_part(content[len(stable_prefix):])]
        else:
            msg["content"] = [_text_part(content, cache_marker)]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = cache_marker


def _can_carry_marker(msg: dict, native_anthropic: bool, tool_part_markers: bool = True) -> bool:
    """True if a marker on this message is actually honored by the provider.

    Native Anthropic honors every message; the envelope layout only honors markers inside
    content parts (empty content wastes a breakpoint) and ``tool_part_markers=False`` excludes
    role:tool too (400). Must agree with :func:`_apply_cache_marker` (marks the LAST part).
    """
    if native_anthropic:
        return True
    if msg.get("role") == "tool" and not tool_part_markers:
        return False
    content = msg.get("content")
    return isinstance(content[-1], dict) if isinstance(content, list) and content else isinstance(content, str) and content != ""


def _build_marker(ttl: str) -> Dict[str, str]:
    """Build a cache_control marker dict for the given TTL ('5m' or '1h')."""
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


# Alibaba-family providers (Qwen routes): five-minute context cache, 1h tier rejected. Shared
# with agent_runtime_helpers.anthropic_prompt_cache_policy so the opt-in and the TTL clamp
# never desync. Do NOT narrow this set to extend a TTL — narrowing DISABLES caching.
ALIBABA_FAMILY_PROVIDERS = frozenset({"opencode", "opencode-go", "opencode-zen", "alibaba"})

# 1h-tier ALLOW-list: only routes wire-measured to retain a 1h marker. Other opencode routes
# are UNMEASURED, not known-bad (opencode-go's `ephemeral_5m_input_tokens` label is not
# evidence of the retention window).
MEASURED_1H_PROVIDERS = frozenset({"opencode-go"})

# Models measured to ignore the 1h tier on a MEASURED_1H_PROVIDERS route; consulted only
# there (the same model on its own endpoint is a separate route).
NO_1H_TIER_MODELS = frozenset({"minimax-m2.5"})


def _flat_model(model: str) -> str:
    """Bare model id, tolerating aggregator prefixes (``vendor/model``)."""
    return (model or "").strip().rsplit("/", 1)[-1].lower()


def is_qwen_model(model: str) -> bool:
    """True when ``model`` names a Qwen-family model (shared with anthropic_prompt_cache_policy)."""
    return "qwen" in (model or "").lower()


# ``prompt_caching.cache_ttl: auto`` picks the tier by who paces the session. The 1h tier
# writes at 2x base vs 1.25x for 5m and only pays off when turns are more than five minutes
# apart — a person stepping away and coming back. Machine-paced sessions call every few
# seconds until they finish and never collect the retention, so they stay on 5m.
# Measured on one install (2 days of per-call logs): 63% of interactive cache-write tokens
# were cold re-writes after a 5–60 min idle gap (1h saves ~42% of write cost there), while
# 1h on subagents/cron would have cost ~49% more.
AUTO_CACHE_TTL = "auto"
MACHINE_PACED_SOURCES = frozenset({"subagent", "cron", "oneshot", "webhook", "kanban", "api", "tool", "batch"})


def auto_cache_ttl_for_source(source: str | None) -> str:
    """The tier ``auto`` resolves to for a session source: ``5m`` for machine-paced sources,
    ``1h`` for everything a human types into (cli, tui, desktop, messaging platforms)."""
    return "5m" if (source or "").strip().lower() in MACHINE_PACED_SOURCES else "1h"


def effective_cache_ttl(ttl: str | None, *, model: str = "", provider: str = "") -> str:
    """Clamp a requested cache TTL to what the destination route supports (``None`` → ``5m``).

    Qwen/Alibaba routes drop ``1h`` (→ ``5m``) except on ``MEASURED_1H_PROVIDERS`` minus
    ``NO_1H_TIER_MODELS``; that check runs BEFORE the generic Qwen clamp, which would swallow it.
    """
    if ttl != "1h":
        return ttl or "5m"
    provider_lower = (provider or "").lower()
    if provider_lower in MEASURED_1H_PROVIDERS:
        return "5m" if _flat_model(model) in NO_1H_TIER_MODELS else "1h"
    return "5m" if is_qwen_model(model) or provider_lower in ALIBABA_FAMILY_PROVIDERS else "1h"


def _apply_system_cache_markers(
    message: dict, cache_marker: dict, static_system_prefix: str | None, *,
    native_anthropic: bool, mark_suffix: bool = True, fallback_to_whole: bool = True,
) -> int:
    """Mark the static system prefix (and optionally the full prompt); returns markers applied.

    The stored system prompt stays one string, split only in the request. ``mark_suffix=False``
    is the tool-cache-plan layout (suffix budget spent on the tools array); ``fallback_to_whole=
    False`` marks nothing when the split is impossible. When the prompt IS the prefix the whole
    message is one block — never an empty text block (400).
    """
    content = message.get("content")
    if isinstance(static_system_prefix, str) and static_system_prefix and isinstance(content, str) and content.startswith(static_system_prefix):
        suffix = content[len(static_system_prefix):]
        if suffix.strip():
            message["content"] = [_text_part(static_system_prefix, cache_marker),
                                  _text_part(suffix, cache_marker if mark_suffix else None)]
            return 2 if mark_suffix else 1
    elif not fallback_to_whole:
        return 0
    _apply_cache_marker(message, cache_marker, native_anthropic=native_anthropic)
    return 1


def _has_part_marker(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(part, dict) and "cache_control" in part for part in content)


def strip_anthropic_cache_control(api_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove ``cache_control`` markers and undo decoration-produced list shapes (in place).

    Used before re-decorating after a mid-turn failover. Flattening to a string is restricted
    to the exact shapes :func:`apply_anthropic_cache_control` produces from string content
    (single text part, two-part system split, two-part skill split) so the ``""``-join is
    byte-exact. Marker removal is copy-on-write on part dicts: parts can alias caller-held
    lists and stripping must never rewrite the stored transcript.
    """
    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        msg.pop("cache_control", None)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role")
        # The skill split is the only decoration marking the FIRST part of a user message,
        # so the shape alone identifies it even after the prefix registry evicted the entry.
        skill_split_shape = (role == "user" and len(content) == 2 and all(isinstance(p, dict) for p in content)
                             and "cache_control" in content[0] and "cache_control" not in content[1])
        if _has_part_marker(content):
            content = msg["content"] = [
                {k: v for k, v in part.items() if k != "cache_control"}
                if isinstance(part, dict) and "cache_control" in part else part for part in content
            ]
        plain_text_parts = content and all(
            isinstance(part, dict) and part.get("type", "text") == "text"
            and isinstance(part.get("text"), str) and set(part.keys()) <= {"type", "text"} for part in content
        )
        if plain_text_parts and (len(content) == 1 or (role == "system" and len(content) == 2) or skill_split_shape):
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
    parts = [p for m in messages if isinstance(m, dict) and isinstance(m.get("content"), list) for p in m["content"]]
    return sum(1 for item in [*messages, *parts, *tools] if isinstance(item, dict) and "cache_control" in item)


def _completed_transaction_endpoint_indexes(messages: List[Dict[str, Any]], *, native_anthropic: bool) -> List[int]:
    """Select legal ends of completed tool runs and ordinary turns."""

    def _tool_run_end(start: int) -> int:
        end = start
        while end < len(messages) and isinstance(messages[end], dict) and messages[end].get("role") == "tool":
            end += 1
        return end

    endpoints: List[int] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") == "system":
            index += 1
            continue
        role = message.get("role")

        if role == "assistant" and message.get("tool_calls"):
            result_end = _tool_run_end(index + 1)
            if result_end > index + 1 and _can_carry_marker(messages[result_end - 1], native_anthropic):
                endpoints.append(result_end - 1)
            index = result_end
            continue

        if role == "tool":
            index = _tool_run_end(index)
            continue

        open_turn = (role == "user" and index + 1 < len(messages)) or (
            role == "assistant" and message.get("content") in (None, ""))
        if not open_turn and _can_carry_marker(message, native_anthropic):
            endpoints.append(index)
        index += 1
    return endpoints


def build_prompt_cache_plan(
    api_messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] | None, *,
    cache_ttl: str = "5m", native_anthropic: bool = False, static_system_prefix: str | None = None,
    direct_native_tool_cache: bool = False, tool_part_markers: bool = True,
) -> PromptCachePlan:
    """Build copy-on-write cache sections for one resolved request destination
    (``tool_part_markers=False`` keeps markers off role:tool parts on LiteLLM-style routes)."""
    messages = list(api_messages or [])
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and (
            "cache_control" in msg or isinstance(msg.get("content"), list)
        ):
            messages[i] = strip_anthropic_cache_control([dict(msg)])[0]
    planned_tools = strip_anthropic_tool_cache_control(tools)

    if not direct_native_tool_cache or not planned_tools:
        planned_messages = apply_anthropic_cache_control(
            messages, cache_ttl=cache_ttl, native_anthropic=native_anthropic,
            static_system_prefix=static_system_prefix, tool_part_markers=tool_part_markers)
        return PromptCachePlan(messages=planned_messages, tools=planned_tools)

    marker = _build_marker(cache_ttl)
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        # Tool-cache layout: only the static prefix carries a system-side marker; the
        # volatile suffix's budget is spent on the tools array.
        messages[0] = copy.deepcopy(messages[0])
        _apply_system_cache_markers(messages[0], marker, static_system_prefix,
                                    native_anthropic=True, mark_suffix=False, fallback_to_whole=False)
    planned_tools[-1]["cache_control"] = dict(marker)
    for endpoint in _completed_transaction_endpoint_indexes(messages, native_anthropic=True)[-2:]:
        messages[endpoint] = copy.deepcopy(messages[endpoint])
        _apply_cache_marker(messages[endpoint], marker, native_anthropic=True)

    return PromptCachePlan(messages=messages, tools=planned_tools)


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    reserve_tools_breakpoint: bool = True,
    static_system_prefix: str | None = None,
    tool_part_markers: bool = True,
) -> List[Dict[str, Any]]:
    """Apply caching strategy to messages for Anthropic models.

    Places cache_control breakpoints on the system prompt + the last
    non-system messages. When ``reserve_tools_breakpoint`` is True, only
    2 message-side breakpoints are used so the caller can apply the 4th
    on the last entry of ``tools[]`` (see
    ``apply_anthropic_tools_cache_control``). Otherwise 3 message-side
    breakpoints are used (legacy behaviour).

    When ``static_system_prefix`` exactly matches the beginning of a string
    system prompt, it receives an early marker and the full system prompt gets
    a trailing marker, so a memory edit or date rollover in the volatile tail
    no longer cold-rewrites the stable identity + tool guidance. Without that
    prefix, the legacy single-system-block layout is retained.

    Idempotent: pre-existing ``cache_control`` markers are stripped from a per-message copy before new ones
    are placed, so calling this twice (or handing it messages a prior call already marked) can never
    accumulate past 4 markers. Only messages that already carry a marker pay the copy cost — a shallow
    top-level copy suffices because :func:`strip_anthropic_cache_control` is copy-on-write on content parts
    — and the rest of the copy-on-write contract is unchanged (#90971).
    """
    if not api_messages:
        return api_messages

    messages = list(api_messages)
    marker = _build_marker(cache_ttl)

    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and ("cache_control" in msg or _has_part_marker(msg.get("content"))):
            messages[i] = strip_anthropic_cache_control([dict(msg)])[0]

    breakpoints_used = 0
    if messages[0].get("role") == "system":
        messages[0] = copy.deepcopy(messages[0])
        breakpoints_used = _apply_system_cache_markers(messages[0], marker, static_system_prefix,
                                                       native_anthropic=native_anthropic)

    # Reserve one breakpoint for tools[] so a tools mutation only forces
    # one rebuild, not every subsequent message re-bill.
    budget = 4 - breakpoints_used - (1 if reserve_tools_breakpoint else 0)
    non_sys = [
        i
        for i in range(len(messages))
        if messages[i].get("role") != "system"
        and _can_carry_marker(
            messages[i],
            native_anthropic=native_anthropic,
            tool_part_markers=tool_part_markers,
        )
    ]
    for idx in non_sys[-budget:]:
        # Deep-copy only the messages that receive a marker — the caller's
        # history must never be mutated (the rest of the list stays shared).
        messages[idx] = copy.deepcopy(messages[idx])
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic, tool_part_markers=tool_part_markers)

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
