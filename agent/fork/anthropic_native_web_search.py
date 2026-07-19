"""Provider-aware web search: native Anthropic server tool vs client tool.

FORK-ONLY. Does not exist upstream and must never be sent upstream
(see FORK.md "Why a fork").

Problem this solves
-------------------
Hermes registers a client-side ``web_search`` tool (``tools/web_tools.py``)
that the agent calls and Hermes dispatches to a configured backend
(firecrawl / exa / parallel / tavily / searxng / brave-free / ddgs / xai).
On a first-party Anthropic endpoint ``check_web_api_key()`` reports the
tool as *available* purely because Anthropic credentials are present
(``ANTHROPIC_API_KEY`` / Claude Code OAuth) — but at dispatch time
``_get_search_backend()`` falls back to the ``firecrawl`` default, which
has no key, and the model gets::

    No web search provider configured. Run `hermes tools` to set one up.

Meanwhile Anthropic exposes a *native server-side* web search tool
(``web_search_20250305``): the model decides to search mid-generation,
Anthropic runs the search on its own infrastructure, and the results
stream back as ``server_tool_use`` / ``web_search_tool_result`` blocks.
The adapter already knows how to STORE and reconcile those result blocks
(``anthropic_adapter`` lines ~2230-2540 + ``agent/fork/anthropic_messages.py``)
— but nothing ever put the native tool *definition* on the request wire,
so the capability was half-built and never reachable.

What this module does
---------------------
When the inference endpoint is first-party Anthropic (Claude), swap the
client ``web_search`` tool entry for Anthropic's native server tool so the
model uses Claude's built-in search inline. On any other endpoint
(MiniMax / Kimi / DeepSeek / Bedrock / Vertex / custom Anthropic-compatible
gateways, or a non-Anthropic provider entirely) leave the client tool in
place — those either don't support the native tool or aren't Claude.

Priority, restated to match the user's intent:
  * Claude  → native server tool (``web_search_20250305``)
  * non-Claude → client ``web_search`` tool (existing backend dispatch)

The model calls a tool literally named ``web_search`` either way; only the
*execution side* changes. ``web_extract`` is intentionally left on the
client path this round (its native analog is a separate ``web_fetch``
server tool with different availability).

Config (``~/.hermes/config.yaml``)::

    web:
      anthropic_native_search: true   # default true; set false to force the
                                       # client tool even on Claude
      anthropic_native_search_max_uses: 5   # cap native searches per turn

Wire shape produced (Anthropic SDK ``BetaWebSearchTool20250305Param``)::

    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

``web_search_20250305`` is GA on the Messages API and needs no extra
``anthropic-beta`` header.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The client tool name Hermes registers (tools/web_tools.py).
_CLIENT_WEB_SEARCH_NAME = "web_search"

# Anthropic native server tool identifiers (SDK BetaWebSearchTool20250305Param).
_NATIVE_TOOL_TYPE = "web_search_20250305"
_NATIVE_TOOL_NAME = "web_search"

_DEFAULT_MAX_USES = 5


def _load_web_config() -> Dict[str, Any]:
    """Load the ``web:`` section from config.yaml. Never raises."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("web", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _native_search_enabled() -> bool:
    """Return True unless the user explicitly disabled native search.

    Default ON: the whole point is that web search "just works" on Claude
    without a third-party key. ``web.anthropic_native_search: false`` opts
    back into the client tool on Claude (e.g. to force a specific backend).
    """
    val = _load_web_config().get("anthropic_native_search", True)
    if isinstance(val, str):
        return val.strip().lower() not in {"false", "0", "no", "off"}
    return bool(val)


def _native_search_max_uses() -> Optional[int]:
    """Resolve the per-turn ``max_uses`` cap for the native tool.

    Returns a positive int, or None to omit the field (Anthropic then
    applies its own default). Garbage config falls back to the default.
    """
    raw = _load_web_config().get("anthropic_native_search_max_uses", _DEFAULT_MAX_USES)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_USES
    return n if n > 0 else None


def is_first_party_anthropic(base_url: Optional[str]) -> bool:
    """Return True for the first-party Anthropic API (incl. OAuth / Claude Code).

    Mirrors ``anthropic_adapter._is_third_party_anthropic_endpoint`` (the
    canonical predicate: any host that is not ``*anthropic.com*`` is a
    third-party proxy). We deliberately scope the native-tool swap to
    first-party Anthropic only — Bedrock/Vertex Claude *can* support the
    native tool but require extra setup and classify as third-party here,
    so they keep the client tool until explicitly opted in.
    """
    try:
        from agent.anthropic_adapter import _is_third_party_anthropic_endpoint

        return not _is_third_party_anthropic_endpoint(base_url)
    except Exception:
        # Conservative: if we can't tell, don't swap — leave the client tool.
        logger.debug(
            "native-web-search: endpoint classification failed; "
            "leaving client web_search tool in place",
            exc_info=True,
        )
        return False


def _build_native_tool(template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Construct the native server-tool param dict.

    Preserves ``cache_control`` from the replaced client tool entry so the
    ``system + tools`` prompt-cache breakpoint placement is unchanged.
    """
    native: Dict[str, Any] = {
        "type": _NATIVE_TOOL_TYPE,
        "name": _NATIVE_TOOL_NAME,
    }
    max_uses = _native_search_max_uses()
    if max_uses is not None:
        native["max_uses"] = max_uses
    if isinstance(template, dict) and isinstance(template.get("cache_control"), dict):
        native["cache_control"] = dict(template["cache_control"])
    return native


def apply_native_web_search(
    anthropic_tools: List[Dict[str, Any]],
    base_url: Optional[str],
) -> List[Dict[str, Any]]:
    """Swap the client ``web_search`` tool for Anthropic's native server tool.

    Called from ``anthropic_adapter.build_anthropic_kwargs`` after the
    OpenAI→Anthropic tool conversion (and OAuth cc-aliasing), before
    ``_apply_tool_search`` / cache-control marking.

    Returns the input list UNCHANGED when:
      * native search is disabled in config, or
      * the endpoint is not first-party Anthropic (non-Claude), or
      * there is no client ``web_search`` entry to swap, or
      * a native web search tool is already present (idempotent).

    Otherwise returns a new list with the ``web_search`` entry replaced
    in place (order preserved). Never raises — on any unexpected error the
    original list is returned so web search degrades to the client path
    rather than breaking the request.
    """
    try:
        if not anthropic_tools:
            return anthropic_tools
        if not _native_search_enabled():
            return anthropic_tools
        if not is_first_party_anthropic(base_url):
            return anthropic_tools

        # Idempotent: if a native web-search server tool is already in the
        # array (e.g. re-entrant build), don't double-inject.
        if any(
            isinstance(t, dict) and t.get("type") == _NATIVE_TOOL_TYPE
            for t in anthropic_tools
        ):
            return anthropic_tools

        # Find the client web_search entry. A plain converted client tool
        # has a "name" and an "input_schema" but no server-tool "type".
        idx = next(
            (
                i
                for i, t in enumerate(anthropic_tools)
                if isinstance(t, dict)
                and t.get("name") == _CLIENT_WEB_SEARCH_NAME
                and "type" not in t
            ),
            None,
        )
        if idx is None:
            return anthropic_tools

        out = list(anthropic_tools)
        out[idx] = _build_native_tool(anthropic_tools[idx])
        logger.info(
            "native-web-search: swapped client web_search tool for Anthropic "
            "native %s (first-party endpoint)",
            _NATIVE_TOOL_TYPE,
        )
        return out
    except Exception:
        logger.debug(
            "native-web-search: swap failed; leaving client web_search tool",
            exc_info=True,
        )
        return anthropic_tools
