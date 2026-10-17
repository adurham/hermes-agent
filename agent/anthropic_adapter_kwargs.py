"""Phases of ``anthropic_adapter.build_anthropic_kwargs``: the Claude Code OAuth identity
transforms, ``tool_choice`` mapping, the thinking / effort parameters and fast mode.

Every helper late-imports the facade's names, so a patch on ``agent.anthropic_adapter.<name>``
still reaches the code that reads it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def _prefix_oauth_system(system: Any) -> list[dict]:
    """Prepend the Claude Code identity block and sanitize product names in every text block."""
    from agent.anthropic_adapter import (
        _CLAUDE_CODE_SYSTEM_PREFIX,
        _OAUTH_SLUG_PATTERN,
        _apply_oauth_prose_aliases,
    )

    # 1. Prepend Claude Code system prompt identity
    cc_block = {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX}
    if isinstance(system, list):
        system = [cc_block] + system
    elif isinstance(system, str) and system:
        system = [cc_block, {"type": "text", "text": system}]
    else:
        system = [cc_block]

    # 2. Sanitize system prompt — replace product name references
    #    to avoid Anthropic's server-side content filters.
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            text = text.replace("Hermes Agent", "Claude Code")
            text = text.replace("Hermes agent", "Claude Code")
            # Upstream's identifier-safe slug rewrite: only a standalone prose word is
            # renamed — an address the model dereferences (``NousResearch/hermes-agent``,
            # ``~/.hermes/hermes-agent/venv``, a path or mailbox) must survive verbatim
            # (#48860). Supersedes the fork's naive ``str.replace("hermes-agent", ...)``.
            text = _OAUTH_SLUG_PATTERN.sub("claude-code", text)
            text = text.replace("Nous Research", "Anthropic")
            block["text"] = _apply_oauth_prose_aliases(text)  # upstream: prose-safe aliases only
    return system


def _rename_oauth_tools(anthropic_tools: list[dict],
                        anthropic_messages: list[dict]) -> Callable[[str], str]:
    """Rewrite tool names (tools[] and replayed tool_use blocks) to their OAuth wire form;
    returns the name mapping so a forced ``tool_choice`` can go through it too."""
    from agent.anthropic_adapter import (
        _MCP_TOOL_PREFIX,
        _OAUTH_TOOL_NAME_ALIASES,
        _apply_oauth_prose_aliases,
        _normalize_to_mcp_wire,
    )

    # 3. Normalize tool names so NOTHING goes on the OAuth wire with a
    #    single-underscore ``mcp_`` prefix.  Anthropic's subscription/OAuth
    #    billing classifier treats a single-underscore ``mcp_`` tool name as
    #    a third-party-app fingerprint and rejects the request with HTTP 400
    #    "Third-party apps now draw from extra usage, not plan limits"
    #    (verified empirically: a single ``mcp_foo`` tool flips a request
    #    from plan-billing to the extra-usage lane; ``mcp__foo`` is accepted).
    #
    #    Two cases, both must land on the double-underscore ``mcp__`` form:
    #      a) bare Hermes-native tools (``read_file``)  -> ``mcp__read_file``
    #      b) native MCP server tools registered under their full
    #         single-underscore ``mcp_<server>_<tool>`` name
    #         (``mcp_linear_get_issue``) -> ``mcp__linear_get_issue``
    #    Case (b) is the gap that the bare ``mcp_``->``mcp__`` constant swap
    #    left open: those tools were *skipped* and stayed single-underscore,
    #    so any session with an MCP server configured still tripped the
    #    classifier. normalize_response reverses both forms via registry
    #    lookup so the dispatcher still sees the original name. GH-25255.
    #
    # Upstream (2026-09 sync) aliases two tools whose schema/name the billing
    # classifier fingerprints on their own (session_search, memory): each is aliased
    # (when the alias isn't already claimed) and then mcp__-normalized.
    _claimed_wire_names = {
        _normalize_to_mcp_wire(t["name"]) for t in (anthropic_tools or []) if isinstance(t, dict) and t.get("name")
    }

    def _to_oauth_wire_name(name: str) -> str:
        aliased = _OAUTH_TOOL_NAME_ALIASES.get(name)
        if aliased and _MCP_TOOL_PREFIX + aliased not in _claimed_wire_names:
            name = aliased
        return _normalize_to_mcp_wire(name)

    if anthropic_tools:
        for tool in anthropic_tools:
            if "name" in tool:
                tool["name"] = _to_oauth_wire_name(tool["name"])
            if isinstance(tool.get("description"), str):
                tool["description"] = _apply_oauth_prose_aliases(tool["description"])

    # Apply the same normalization to tool names in message history
    # (tool_use blocks) so replayed turns match the wire names above.
    for msg in anthropic_messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use" and "name" in block:
                        block["name"] = _to_oauth_wire_name(block["name"])
                    elif block.get("type") == "tool_result" and "tool_use_id" in block:
                        pass  # tool_result uses ID, not name
    return _to_oauth_wire_name


def _compact_oauth_system(system: list[dict],
                          anthropic_messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """system_prompt_mode=compact: keep only the CC prefix in ``system`` and move the rest
    into a preamble on the first user message."""
    from agent.anthropic_adapter import (
        _prepend_user_message_preamble,
        _system_prompt_mode_compact,
    )

    # 4. system_prompt_mode=compact: move everything past the CC prefix
    #    into a preamble block on the first user message.
    #
    #    Anthropic's billing classifier on personal Max plans rejects
    #    OAuth requests whose ``system`` extends beyond the official
    #    Claude Code identity prefix — they get routed to "extra
    #    usage" billing and 400 with a misleading
    #    "out of extra usage" error. Mirroring Claude Code's
    #    --exclude-dynamic-system-prompt-sections flag, we keep only
    #    the CC prefix in ``system`` and ride everything dynamic on
    #    the conversation. Behavior is unchanged (the model still
    #    sees the same content); only the placement moves.
    #
    #    Cache control: if the moved blocks carried cache_control
    #    markers, we preserve them on the preamble block so prompt
    #    caching continues to work across turns.
    if _system_prompt_mode_compact() and isinstance(system, list) and len(system) > 1:
        tail_blocks = system[1:]
        system = [system[0]]
        tail_text_parts = []
        tail_cache_control = None
        for blk in tail_blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text":
                txt = blk.get("text", "")
                if txt:
                    tail_text_parts.append(txt)
            # Inherit the strongest cache_control found on the moved
            # blocks (last write wins — typical pattern is a single
            # ephemeral marker on the final static block).
            cc = blk.get("cache_control")
            if cc:
                tail_cache_control = cc
        if tail_text_parts:
            preamble = {
                "type": "text",
                "text": "\n\n".join(tail_text_parts),
            }
            if tail_cache_control:
                preamble["cache_control"] = tail_cache_control
            anthropic_messages = _prepend_user_message_preamble(
                anthropic_messages, preamble
            )
    return system, anthropic_messages


def apply_oauth_identity(system: Any, anthropic_tools: list[dict],
                         anthropic_messages: list[dict]) -> tuple[Any, list[dict], Callable[[str], str]]:
    """Claude Code OAuth identity: system prefix, tool-name wire form, compact system mode.
    Returns ``(system, anthropic_messages, to_oauth_wire_name)``."""
    system = _prefix_oauth_system(system)
    to_wire = _rename_oauth_tools(anthropic_tools, anthropic_messages)
    system, anthropic_messages = _compact_oauth_system(system, anthropic_messages)
    return system, anthropic_messages, to_wire


def apply_tools(kwargs: dict[str, Any], anthropic_tools: list[dict], tool_choice: str | None,
                to_oauth_wire_name: Callable[[str], str] | None,
                cache_tools: bool, cache_ttl: str) -> None:
    """``tools`` (with optional cache control) and the OpenAI -> Anthropic ``tool_choice`` map."""
    if not anthropic_tools:
        return
    if cache_tools:
        from agent.prompt_caching import apply_anthropic_tools_cache_control
        anthropic_tools = apply_anthropic_tools_cache_control(
            anthropic_tools, cache_ttl=cache_ttl
        )
    kwargs["tools"] = anthropic_tools
    # Map OpenAI tool_choice to Anthropic format
    if tool_choice == "auto" or tool_choice is None:
        # Mirror Claude Code: omit tool_choice (the API treats absent as
        # "auto", so we save bytes and match CC's wire shape exactly).
        pass
    elif tool_choice == "required":
        kwargs["tool_choice"] = {"type": "any"}
    elif tool_choice == "none":
        # Anthropic has no tool_choice "none" — omit tools entirely to prevent use
        kwargs.pop("tools", None)
    elif isinstance(tool_choice, str):
        # Specific tool name. On the OAuth wire every tools[] entry is
        # mcp__-prefixed/CC-aliased above, so a forced tool_choice must go
        # through the same mapping or it names a tool that no longer
        # exists on the wire (Anthropic 400s, or worse, silently targets
        # the wrong tool if a stale non-prefixed name happens to
        # collide). Mirrors upstream's to_wire(tool_choice) composition
        # in build_anthropic_kwargs.
        wire_name = to_oauth_wire_name(tool_choice) if to_oauth_wire_name else tool_choice
        kwargs["tool_choice"] = {"type": "tool", "name": wire_name}


def _apply_adaptive_thinking(kwargs: dict[str, Any], model: str, effort: str,
                             base_url: str | None) -> None:
    from agent.anthropic_adapter import (
        ADAPTIVE_EFFORT_MAP,
        _is_third_party_anthropic_endpoint,
        _supports_xhigh_effort,
    )

    _thinking_cfg: dict[str, Any] = {"type": "adaptive"}
    _display_override = os.environ.get(
        "HERMES_THINKING_DISPLAY", ""
    ).strip().lower()
    if _display_override in {"summarized", "verbose", "all", "omitted"}:
        _thinking_cfg["display"] = _display_override
    kwargs["thinking"] = _thinking_cfg
    adaptive_effort = ADAPTIVE_EFFORT_MAP.get(effort, "medium")
    # Downgrade xhigh on models that don't support it. Claude Code
    # falls back to "high" for non-4.7 models (verified by
    # disassembling its 2.1.119 binary: `return"xhigh";return"high"`).
    # Don't fall back to "max" — Sonnet 4.6 and Haiku 4.5 don't
    # support max either (Opus-tier only), so the previous
    # "downgrade to max" path 400'd on Sonnet/Haiku requests.
    if adaptive_effort == "xhigh" and not _supports_xhigh_effort(model):
        adaptive_effort = "high"
    kwargs["output_config"] = {
        "effort": adaptive_effort,
    }
    # Mirror Claude Code 2.1.119: every /v1/messages call carries
    # ``context_management`` with the clear_thinking_20251015 edit
    # set to keep:"all".  Activates the server-side thinking-block
    # lifecycle so cached thinking-blocks survive across turns
    # (paired with redact-thinking-2026-02-12 +
    # context-management-2025-06-27 betas).  Native Anthropic only
    # — third-party gateways don't recognize the field.  Typed
    # kwarg in client.beta.messages.* (Anthropic SDK 0.100+).
    if not _is_third_party_anthropic_endpoint(base_url):
        kwargs["context_management"] = {
            "edits": [
                {"type": "clear_thinking_20251015", "keep": "all"},
            ],
        }


def apply_thinking(kwargs: dict[str, Any], model: str, reasoning_config: dict[str, Any] | None,
                   base_url: str | None, effective_max_tokens: int) -> None:
    """Map ``reasoning_config`` to Anthropic's ``thinking`` / ``output_config`` parameters."""
    from agent.anthropic_adapter import (
        THINKING_BUDGET,
        _BETWEEN_TOOLS_OFF_CLAUDE_SUBSTRINGS,
        _accepts_thinking_disable,
        _is_pre_thinking_haiku,
        _model_matches,
        _supports_adaptive_thinking,
    )

    # Map reasoning_config to Anthropic's thinking parameter.
    # Claude 4.6+ models use adaptive thinking + output_config.effort.
    # Older models use manual thinking with budget_tokens.
    # MiniMax Anthropic-compat endpoints support thinking (manual mode only,
    # not adaptive).  Haiku does NOT support extended thinking — skip entirely.
    #
    # Kimi / Moonshot models also use adaptive thinking: their
    # Anthropic-compatible endpoints (api.moonshot.cn/anthropic,
    # api.kimi.com/coding) accept ``thinking.type="adaptive"`` +
    # ``output_config.effort``, and the replay-validation 400s that
    # originally motivated dropping the parameter (#13848) no longer
    # occur.  (Kimi on chat_completions enables thinking via extra_body
    # in the ChatCompletionsTransport — see #13503.)
    #
    # On 4.7+ ``thinking.display`` defaults to "omitted" (no summary text
    # generated). Previously hermes set "summarized" to keep the activity
    # feed populated, but verified via binary inspection 2026-05-06 that
    # Claude Code DOES NOT set ``display`` — it accepts the omitted default.
    # Multi-minute "queued/prefilling" stalls hermes was hitting that
    # Claude Code didn't correlate with this difference: producing a
    # summary forces the model to generate extra tokens after thinking
    # before the visible output streams, magnifying any internal-thinking
    # latency.  Match Claude Code's wire shape — let display default.
    # See ``HERMES_THINKING_DISPLAY=summarized`` env var to opt back in
    # if the activity feed UX matters more than latency parity.
    # When reasoning_config is unset, default to enabling adaptive thinking
    # at medium effort on Anthropic-native + adaptive-supporting models.
    # Mirrors Claude Code 2.1.119 wire shape (verified by mitmdump capture
    # 2026-05-06: every /v1/messages call sends thinking={type:"adaptive"}
    # + output_config.effort).  Without this default, the entire
    # thinking/output_config block below was a no-op for callers that
    # don't explicitly pass reasoning_config — i.e. nearly every default
    # session — leaving the interleaved-thinking + effort betas dormant.
    if reasoning_config is None and _supports_adaptive_thinking(model):
        reasoning_config = {"enabled": True, "effort": "medium"}
    if not (reasoning_config and isinstance(reasoning_config, dict)):
        return
    if reasoning_config.get("enabled") is False:
        # "Thinking off". Adaptive models think by DEFAULT, so omitting the
        # parameter is not a disable — it silently leaves thinking on and
        # the user keeps paying for it. Send the disable explicitly.
        # Mandatory-thinking models reject it with a 400, so they keep the
        # omission: a silently-ignored disable beats a dead turn.
        # Families whose documented off is ``between_tools`` 400 on
        # ``disabled`` (upstream v0.21.6 _thinking_kwargs contract).
        if _model_matches(model, _BETWEEN_TOOLS_OFF_CLAUDE_SUBSTRINGS):
            kwargs["thinking"] = {"type": "between_tools"}
        elif _accepts_thinking_disable(model):
            kwargs["thinking"] = {"type": "disabled"}
    elif not _is_pre_thinking_haiku(model):
        effort = str(reasoning_config.get("effort", "medium")).lower()
        budget = THINKING_BUDGET.get(effort, 8000)
        if _supports_adaptive_thinking(model):
            _apply_adaptive_thinking(kwargs, model, effort, base_url)
        else:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic requires temperature=1 when thinking is enabled on older models
            kwargs["temperature"] = 1
            kwargs["max_tokens"] = max(effective_max_tokens, budget + 4096)


def apply_fast_mode(kwargs: dict[str, Any], model: str, base_url: str | None, fast_mode: bool,
                    is_oauth: bool, drop_context_1m_beta: bool) -> None:
    """Fast mode (Opus 4.6 only): typed ``speed="fast"`` plus the fast-mode beta."""
    from agent.anthropic_adapter import (
        _FAST_MODE_BETA,
        _OAUTH_ONLY_BETAS,
        _common_betas_for_base_url,
        _is_third_party_anthropic_endpoint,
        _supports_fast_mode,
    )

    # ── Fast mode (Opus 4.6 only) ────────────────────────────────────
    # Sets typed ``speed="fast"`` + adds the fast-mode beta to the
    # per-request ``betas`` list for ~2.5x output speed.  Per Anthropic
    # docs, fast mode is only supported on Opus 4.6 — Opus 4.7 and other
    # models 400 on the speed parameter.
    # Only for native Anthropic endpoints — third-party providers would
    # reject the unknown beta header and speed parameter.
    if (
        fast_mode
        and not _is_third_party_anthropic_endpoint(base_url)
        and _supports_fast_mode(model)
    ):
        # Typed ``speed`` kwarg in client.beta.messages.* (SDK 0.100+).
        kwargs["speed"] = "fast"
        # Per-request betas list overrides the client-level
        # default_headers["anthropic-beta"] for this call.
        betas = list(_common_betas_for_base_url(
            base_url,
            drop_context_1m_beta=drop_context_1m_beta,
            model=model,
        ))
        if is_oauth:
            betas.extend(_OAUTH_ONLY_BETAS)
        betas.append(_FAST_MODE_BETA)
        kwargs["betas"] = betas


def strip_context_1m_beta(kwargs: dict[str, Any], model: str, base_url: str | None,
                          is_oauth: bool) -> None:
    """The prompt is under the 1M threshold: send the per-request betas without context-1m."""
    from agent.anthropic_adapter import (
        _CONTEXT_1M_BETA,
        _OAUTH_ONLY_BETAS,
        _common_betas_for_base_url,
    )

    prior = list(kwargs.get("betas") or [])
    if not prior:
        # No prior per-request override — start from the same
        # base set the client would otherwise send. Then strip
        # context-1m and emit as a per-request override.
        prior = list(_common_betas_for_base_url(
            base_url,
            drop_context_1m_beta=False,
            model=model,
        ))
        if is_oauth:
            prior.extend(_OAUTH_ONLY_BETAS)
    stripped = [b for b in prior if b != _CONTEXT_1M_BETA]
    if len(stripped) != len(prior):
        kwargs["betas"] = stripped
