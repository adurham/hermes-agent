"""Fork-only Anthropic server-tool passes (native web search + tool_search).

FORK-ONLY. Does not exist upstream and must never be sent upstream
(see FORK.md "Why a fork").

Why this module exists
----------------------
Upstream's ``agent/anthropic_message_convert.py`` converged on the fork's
OpenAI->Anthropic converter (ordered-block replay, orphan stripping,
``_ensure_leading_user_turn``, the thinking-signature ladder, screenshot
eviction) and is now equivalent-or-better, so the fork's vendored copy
(``agent/fork/anthropic_messages.py``, deleted in the same commit that added
this file) was retired in favour of upstream's implementation.

What upstream does NOT have is the handful of Anthropic *server-tool* passes
that exist purely to serve the fork's native-web-search feature
(``agent/fork/anthropic_native_web_search.py``). That feature puts Anthropic's
native ``web_search_20250305`` server tool on the request wire; the model then
searches mid-generation and Anthropic streams back ``server_tool_use`` /
``web_search_tool_result`` blocks that have strict, undocumented-until-you-400
replay rules. Those rules live here, isolated in one fork-owned module, so
upstream can keep refactoring its converter with a near-zero conflict surface.

The passes, and the HTTP 400 each one prevents
----------------------------------------------
1. ``preserve_server_tool_blocks`` — recomposition path: re-emit the
   ``server_tool_blocks`` the transport stashed on the message, before
   text/tool_use blocks. Without it the server-side tool evidence is silently
   dropped from the replayed turn.
2. ``_normalize_tool_search_results`` — strip response-only fields from
   tool_search result blocks and collapse the variant-suffixed wire type to
   the bare canonical ``tool_search_tool_result`` the INPUT validator accepts
   ("Extra inputs are not permitted" / "Input tag ... does not match any of
   the expected tags").
3. ``_relocate_orphaned_tool_search_results`` — Anthropic delivers a
   tool_search result in a *later* response than the ``server_tool_use`` that
   issued it, but the input validator demands same-message pairing
   ("tool_search_tool_<variant> tool use with id ... was found without a
   corresponding ... _tool_result block").
4. ``_strip_web_search_orphans`` — native web_search requires the
   ``server_tool_use`` and its ``web_search_tool_result`` in the SAME assistant
   message. Compaction or message-merging can split the pair, so strip whichever
   half lost its partner ("web_search_tool_result must have a corresponding
   server_tool_use block before it").
5. ``_drop_unpaired_server_tool_use`` — drop a ``server_tool_use`` whose result
   never arrived at all (stream interruption, timeout, cancel mid-response);
   otherwise every subsequent API call replays the orphan and 400s forever.
6. ``_canonicalize_tool_search_result_types`` — defense-in-depth re-run of the
   type collapse for sessions persisted by an older Hermes that stored the
   variant-suffixed type. Idempotent; the bare canonical is its own fixed point.
7. ``_move_client_tool_use_blocks_to_end`` — a client ``tool_use`` must sit
   AFTER any server-side blocks in its own message, or the next message's
   ``tool_result`` is not "immediately after" it and the validator 400s.

Plus one deliberate divergence retained from upstream (see ``strip_replay_citations``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("run_agent")

_THINKING_TYPES = frozenset(("thinking", "redacted_thinking"))


def _is_tool_search_result_type(btype: Any) -> bool:
    """True for the bare canonical ``tool_search_tool_result`` and every
    variant-suffixed wire form (``tool_search_tool_regex_tool_result``, ...).

    NOTE: the bare canonical satisfies both halves of this predicate
    (``"tool_search_tool_result"`` does start with ``tool_search_tool_`` and
    does end with ``_tool_result``), which is exactly why normalizing to the
    canonical form BEFORE the relocation pass is safe — the relocator uses the
    same predicate and still matches an already-normalized block.
    """
    return (
        isinstance(btype, str)
        and btype.startswith("tool_search_tool_")
        and btype.endswith("_tool_result")
    )


def _assistant_block_lists(result: List[Dict[str, Any]]):
    """``(index, message)`` for assistant messages whose content is a block list."""
    for i, m in enumerate(result):
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            yield i, m


def strip_replay_citations(block: Dict[str, Any]) -> Dict[str, Any]:
    """Drop ``citations`` from a text block on the verbatim-replay path.

    DELIBERATE DIVERGENCE from upstream, retained from the fork's converter.
    Upstream preserves ``citations`` when replaying a stored text block; the
    fork strips them. This is kept because pass 4
    (``_strip_web_search_orphans``) can remove a ``web_search_tool_result``
    whose partner went missing, and a surviving text block citing it would then
    carry a dangling ``encrypted_index`` reference — the shape the original
    fork comment recorded a 400 for ("unexpected tool_use_id found in
    web_search_tool_result blocks").

    Honest caveat: that 400 was observed during orphaned web_search replay and
    its cause was never isolated to citations specifically, so this strip is
    conservative rather than proven-necessary. It is scoped to the
    verbatim-replay path only (the recomposition path keeps citations, matching
    both upstream and the retired fork converter). Revisiting it safely needs a
    live-API check plus per-message citation invalidation (keep web_search
    citations only when that message's use/result pair was undisturbed AND it
    still carries a ``web_search_tool_result``); until then, absence cannot
    400 and is pinned by a test.
    """
    if isinstance(block, dict):
        block.pop("citations", None)
    return block


def preserve_server_tool_blocks(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pass 1 — Anthropic server-side tool blocks for the recomposition path.

    ``server_tool_use`` / ``web_search_tool_result`` / tool_search result blocks
    are stashed on the OpenAI-format message by the transport
    (``agent/transports/anthropic.py`` -> ``chat_completion_helpers``) and must
    be re-emitted verbatim BEFORE the turn's text and tool_use blocks.

    Returns the blocks to prepend (empty list when there are none). Never
    mutates ``m``.
    """
    preserved = m.get("server_tool_blocks")
    if not isinstance(preserved, list):
        return []
    blocks: List[Dict[str, Any]] = []
    for sb in preserved:
        if not isinstance(sb, dict):
            continue
        sb_type = sb.get("type")
        if sb_type in ("server_tool_use", "web_search_tool_result"):
            # Request-shape compatible; pass through as-is.
            blocks.append(dict(sb))
        elif _is_tool_search_result_type(sb_type):
            from agent.anthropic_adapter import _normalize_tool_search_result_for_input
            blocks.append(_normalize_tool_search_result_for_input(sb))
    return blocks


def _normalize_tool_search_results(result: List[Dict[str, Any]]) -> None:
    """Pass 2 — allowlist tool_search result blocks to their INPUT shape.

    Anthropic's tool_search result blocks arrive carrying response-only fields
    (``text``, ``citations``, inner ``rank``/``description``/``input_schema``)
    that the input validator rejects with "Extra inputs are not permitted", and
    a variant-suffixed ``type`` it rejects with an input-tag mismatch.

    Runs over the assembled wire shape so BOTH producer paths are covered with
    one pass: upstream's verbatim replay (which routes unknown block types
    through the generic ``_sanitize_block_for_anthropic_input`` allowlist, and
    that fails open for tool_search types — leaving the response-only fields in
    place) and the recomposition path. Idempotent. Mutates ``result`` in place.
    """
    from agent.anthropic_adapter import _normalize_tool_search_result_for_input

    for _, m in _assistant_block_lists(result):
        m["content"] = [
            _normalize_tool_search_result_for_input(b)
            if isinstance(b, dict) and _is_tool_search_result_type(b.get("type"))
            else b
            for b in m["content"]
        ]


def _strip_web_search_orphans(result: List[Dict[str, Any]]) -> None:
    """Pass 4 — per message, drop a native web_search half that lost its partner.

    ``web_search_20250305`` requires the ``server_tool_use`` and its
    ``web_search_tool_result`` in the SAME assistant message, use immediately
    before result. Compaction or message-merging can split the pair; strip
    orphans in either direction so neither half survives alone.
    Mutates ``result`` in place.
    """
    for _, m in _assistant_block_lists(result):
        content = m["content"]
        use_ids: set = set()
        result_ids: set = set()
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "server_tool_use" and b.get("name") == "web_search":
                if isinstance(b.get("id"), str):
                    use_ids.add(b["id"])
            elif btype == "web_search_tool_result":
                if isinstance(b.get("tool_use_id"), str):
                    result_ids.add(b["tool_use_id"])
        orphans = use_ids.symmetric_difference(result_ids)
        if not orphans:
            continue
        kept = [
            b for b in content
            if not (
                isinstance(b, dict)
                and (
                    (b.get("type") == "server_tool_use"
                     and b.get("name") == "web_search"
                     and b.get("id") in orphans)
                    or (b.get("type") == "web_search_tool_result"
                        and b.get("tool_use_id") in orphans)
                )
            )
        ]
        m["content"] = kept or [{"type": "text", "text": "(empty)"}]


def _drop_unpaired_server_tool_use(result: List[Dict[str, Any]]) -> None:
    """Pass 5 — drop ``server_tool_use`` blocks whose result never arrived.

    Stream interruption / timeout / cancel mid-response leaves a use with no
    result anywhere, and every subsequent API call replays the orphan and 400s.

    Runs AFTER relocation (pass 3) and the web_search orphan pass (pass 4) so a
    split-but-repairable pair is fixed first and only a genuinely missing result
    triggers a drop. The result-id set spans BOTH tool_search result ids and
    ``web_search_tool_result`` ids — without the latter, a ``server_tool_use``
    legitimately paired with a web_search result would be misclassified as
    orphaned and dropped, stranding the result block and 400ing the next
    request. Mutates ``result`` in place.
    """
    result_ids: set = set()
    for _, m in _assistant_block_lists(result):
        for b in m["content"]:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if _is_tool_search_result_type(btype) or btype == "web_search_tool_result":
                if isinstance(b.get("tool_use_id"), str):
                    result_ids.add(b["tool_use_id"])
    for _, m in _assistant_block_lists(result):
        content = m["content"]
        kept = [
            b for b in content
            if not (
                isinstance(b, dict)
                and b.get("type") == "server_tool_use"
                and isinstance(b.get("id"), str)
                and b["id"] not in result_ids
            )
        ]
        if len(kept) != len(content):
            m["content"] = kept or [{"type": "text", "text": "(empty)"}]


def apply_server_tool_passes(result: List[Dict[str, Any]]) -> None:
    """Run every fork-only server-tool pass over the assembled message list.

    Called from ``agent/anthropic_message_convert.convert_messages_to_anthropic``
    after the thinking-signature ladder and before screenshot eviction — the
    same position the passes occupied in the fork's retired converter.
    Mutates ``result`` in place.
    """
    from agent.anthropic_adapter import (
        _canonicalize_tool_search_result_types,
        _move_client_tool_use_blocks_to_end,
        _relocate_orphaned_tool_search_results,
    )

    _normalize_tool_search_results(result)            # 2
    _relocate_orphaned_tool_search_results(result)    # 3
    _strip_web_search_orphans(result)                 # 4
    _drop_unpaired_server_tool_use(result)            # 5
    _canonicalize_tool_search_result_types(result)    # 6
    _move_client_tool_use_blocks_to_end(result)       # 7
