"""Anthropic Messages API transport: conversion via agent/anthropic_adapter.py, normalization here."""

from typing import Any, Dict, List, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall

_MCP_PREFIX = "mcp__"
_THINKING_TYPES = ("thinking", "redacted_thinking")

# FORK: the dynamically synthesized bridge tools (tools/tool_search.py) are dispatched by a name
# check in agent/tool_executor.py and are NEVER registered in tools/registry.py, so a registry
# lookup can't resolve them. Mirrored as a literal (rather than importing
# tools.tool_search.BRIDGE_TOOL_NAMES) because that module imports tools.connectors, which calls
# ``registry.register(...)`` at import time — under a test that patches ``tools.registry.registry``
# the registration raises and any broad except around the import would silently disable bridge-name
# resolution. Kept in sync with tools/tool_search.py::BRIDGE_TOOL_NAMES.
_BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})


def _unprefix_oauth_tool_name(name: str) -> str:
    """Reverse the OAuth-wire ``mcp__`` prefix back to the registered tool name.
    Two originals map onto one wire name (``read_file`` / ``mcp_linear_get_issue``), so
    resolve by registry lookup, never rewriting a name that already resolves natively.
    OAuth wire aliases are checked LAST so a real tool under the wire name still wins."""
    from agent.anthropic_adapter import _OAUTH_TOOL_NAME_REVERSE_ALIASES
    from tools.registry import registry as _tool_registry
    bare = name[len(_MCP_PREFIX):]
    # FORK: the tool_search/tool_describe/tool_call bridge tools (tools/tool_search.py) are
    # synthesized dynamically and dispatched by a name check in agent/tool_executor.py — they
    # are NEVER in tools/registry.py, so the lookups below always miss for them. Checked FIRST
    # and against a literal set, deliberately not via `from tools.tool_search import
    # BRIDGE_TOOL_NAMES`: that module pulls in tools.connectors, which calls
    # ``registry.register(...)`` at import time, and under a test that patches
    # ``tools.registry.registry`` with a stand-in the registration raises — silently turning
    # this lookup into a no-op and leaving the wire name stuck as ``mcp__tool_call``.
    if bare in _BRIDGE_TOOL_NAMES:
        return bare
    for candidate in (name, "mcp_" + bare, bare):
        if _tool_registry.get_entry(candidate):
            return candidate
    return _OAUTH_TOOL_NAME_REVERSE_ALIASES.get(bare, name)


# build_kwargs params forwarded to build_anthropic_kwargs, with the defaults applied when absent.
_BUILD_KWARG_DEFAULTS = {
    "max_tokens": 16384, "reasoning_config": None, "tool_choice": None, "is_oauth": False, "preserve_dots": False,
    "context_length": None, "base_url": None, "fast_mode": False, "drop_context_1m_beta": False,
    # FORK params (see agent/anthropic_adapter.py::build_anthropic_kwargs):
    # tool_search_config — see _apply_tool_search; None/disabled = no transformation.
    # session_id — folded into the metadata.user_id blob for per-session correlation.
    # cache_tools / cache_ttl — native tools[] cache-control layout.
    "tool_search_config": None, "session_id": None, "cache_tools": False, "cache_ttl": "5m",
}


class AnthropicTransport(ProviderTransport):
    """Transport for api_mode='anthropic_messages'."""

    _STOP_REASON_MAP = {
        "end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop",
        "refusal": "content_filter", "model_context_window_exceeded": "length",
    }

    @property
    def api_mode(self) -> str:
        return "anthropic_messages"

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """Convert OpenAI messages to an Anthropic (system, messages) tuple; ``base_url`` affects thinking-signature handling."""
        from agent.anthropic_message_convert import convert_messages_to_anthropic
        return convert_messages_to_anthropic(messages, base_url=kwargs.get("base_url"))

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """Convert OpenAI tool schemas to Anthropic input_schema format."""
        from agent.anthropic_message_convert import convert_tools_to_anthropic
        return convert_tools_to_anthropic(tools)

    def build_kwargs(
        self, model: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, **params,
    ) -> Dict[str, Any]:
        """Build kwargs for ``client.beta.messages.{create,stream}`` (converts messages and
        tools internally). FORK: shaped for the BETA namespace — typed kwargs for ``thinking``,
        ``output_config``, ``context_management``, ``betas``, ``speed`` and ``metadata`` pass
        through directly, without the ``extra_body``/``extra_headers`` workarounds the plain
        ``messages.*`` namespace needed. See _BUILD_KWARG_DEFAULTS for the forwarded params."""
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model=model, messages=messages, tools=tools,
            **{key: params.get(key, default) for key, default in _BUILD_KWARG_DEFAULTS.items()},
        )

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Parse content blocks (text/thinking/tool_use), map stop_reason, collect reasoning_details."""
        import json
        from agent.anthropic_message_convert import _sanitize_replay_block, _to_plain_data
        strip_tool_prefix = kwargs.get("strip_tool_prefix", False)
        text_parts, reasoning_parts, reasoning_details, tool_calls = [], [], [], []
        # FORK (native web search / server-side tools): server_tool_use, web_search_tool_result and
        # tool_search_tool_*_tool_result blocks are executed Anthropic-side, not locally. Keep them
        # so they survive into the next turn's history (Anthropic requires the tool_result blocks to
        # be present when re-submitting prior assistant turns referencing them) and so the UI can
        # render a search-citation panel.
        server_tool_blocks: list[dict] = []
        # Anthropic signs each thinking block against the blocks PRECEDING it; when thinking
        # interleaves with tool_use the parallel lists lose that order and replay -> HTTP 400.
        ordered_blocks = []
        for block in response.content:
            block_dict = _to_plain_data(block)
            # Sanitize at capture so output-only SDK fields never persist and replay (400).
            clean_block = _sanitize_replay_block(block_dict) if isinstance(block_dict, dict) else None
            if clean_block is not None:
                ordered_blocks.append(clean_block)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type in _THINKING_TYPES:
                if block.type == "thinking":
                    reasoning_parts.append(block.thinking)
                detail = clean_block if clean_block is not None else block_dict  # raw only if sanitize dropped it
                if isinstance(detail, dict):
                    reasoning_details.append(detail)
            elif block.type == "tool_use":
                name = block.name
                if strip_tool_prefix and name.startswith(_MCP_PREFIX):
                    name = _unprefix_oauth_tool_name(name)
                    # FORK: keep the verbatim replay copy (``ordered_blocks``, persisted as
                    # provider_data["anthropic_content_blocks"]) in sync with the resolved name.
                    # Otherwise the replay copy keeps the raw OAuth wire name while ``tool_calls``
                    # carries the reversed one; on the next turn _strip_unknown_tool_blocks() finds
                    # no match and rewrites the historical tool_use/tool_result into a truncated
                    # "tool no longer available" breadcrumb, corrupting the model's view of its
                    # own prior turn.
                    if isinstance(clean_block, dict) and clean_block.get("type") == "tool_use":
                        clean_block["name"] = name
                tool_calls.append(ToolCall(id=block.id, name=name, arguments=json.dumps(block.input)))
            elif block.type in ("server_tool_use", "web_search_tool_result") or block.type.startswith("tool_search_tool_"):
                # FORK: tool_search_tool_<variant>_tool_result carries the discovered tool_reference
                # array; Anthropic auto-expands those across history only while we round-trip the
                # block back in messages. The type is variant-specific, so match by prefix.
                if isinstance(block_dict, dict):
                    server_tool_blocks.append(block_dict)

        # FORK: canonicalize tool_search_tool_*_tool_result types to the bare
        # ``tool_search_tool_result`` form before persisting — Anthropic's INPUT validator only
        # accepts the canonical type, while the wire OUTPUT uses variant-suffixed ones.
        from agent.anthropic_adapter import _canonicalize_tool_search_result_types
        if server_tool_blocks:
            _canonicalize_tool_search_result_types(server_tool_blocks)
        provider_data = {"reasoning_details": reasoning_details} if reasoning_details else {}
        if server_tool_blocks:
            provider_data["server_tool_blocks"] = server_tool_blocks
        # Ordered channel only for the shape the parallel lists reconstruct wrongly.
        signed = any(b.get("type") in _THINKING_TYPES and (b.get("signature") or b.get("data")) for b in ordered_blocks)
        if signed and any(b.get("type") == "tool_use" for b in ordered_blocks):
            if ordered_blocks:
                _canonicalize_tool_search_result_types(ordered_blocks)
            provider_data["anthropic_content_blocks"] = ordered_blocks
        # Structured stop_details (Anthropic SDK 0.88+, propagated through
        # streaming in 0.98+).  Today only refusal stops carry detail
        # (category=cyber|bio + human-readable explanation); future stop
        # types may add more.  Surface as-is so callers/UI can present
        # the refusal explanation rather than a bare "refusal" string.
        stop_details = _to_plain_data(getattr(response, "stop_details", None))
        if stop_details:
            provider_data["stop_details"] = stop_details

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None, tool_calls=tool_calls or None,
            finish_reason=self.response_finish_reason(response),
            reasoning="\n\n".join(reasoning_parts) if reasoning_parts else None, usage=None,
            provider_data=provider_data or None,
        )

    def response_finish_reason(self, response: Any) -> str:
        """``stop_reason`` mapped to the OpenAI vocabulary. Bedrock InvokeModel guardrail blocks keep
        ``stop_reason=end_turn`` and hand back the guardrail's canned text as an ordinary reply; they
        must surface as ``content_filter`` so the loop treats them as a refusal, not model output."""
        from agent.bedrock_adapter import anthropic_response_guardrail_intervened
        if anthropic_response_guardrail_intervened(response):
            return "content_filter"
        return self.map_finish_reason(response.stop_reason)

    def validate_response(self, response: Any) -> bool:
        """Structural check; empty content is legitimate for ``end_turn``/``refusal`` (retrying
        either would loop forever). FORK: ``pause_turn`` is NOT accepted here — the caller's retry
        loop detects it via ``stop_reason`` and resumes separately with reduced effort."""
        content_blocks = getattr(response, "content", None)
        return isinstance(content_blocks, list) and (
            bool(content_blocks) or getattr(response, "stop_reason", None) in {"end_turn", "refusal"}
        )

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """Anthropic cache_read / cache_creation token counts."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return {"cached_tokens": cached, "creation_tokens": written} if cached or written else None


from agent.transports import register_transport  # noqa: E402

register_transport("anthropic_messages", AnthropicTransport)
