"""Fork-owned Anthropic message converter.

``convert_messages_to_anthropic`` is the fork's heavily-diverged (~540-line vs
upstream's ~63) OpenAI→Anthropic message converter. It lived inline in
``anthropic_adapter.py`` where upstream's periodic extract-method refactors of
the per-message conversion tangled irreconcilably with the fork's inline form
(the worst conflict in both 2026-05 syncs).

Relocated here (a hard-fork boundary like ``agent/fork/*``). ``anthropic_adapter``
keeps a thin forwarder, so upstream can refactor its own converter freely — the
merge cost drops to take-ours on the forwarder. The low-level block/tool/content
helpers stay in ``anthropic_adapter`` (some are upstream-shared); this function
binds them locally via a lazy import to avoid the circular dependency.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("run_agent")


def convert_messages_to_anthropic(
    messages: List[Dict],
    base_url: str | None = None,
    model: str | None = None,
) -> Tuple[Optional[Any], List[Dict]]:
    """Convert OpenAI-format messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).
    System messages are extracted since Anthropic takes them as a separate param.
    system_prompt is a string or list of content blocks (when cache_control present).

    When *base_url* is provided and points to a third-party Anthropic-compatible
    endpoint, all thinking block signatures are stripped.  Signatures are
    Anthropic-proprietary — third-party endpoints cannot validate them and will
    reject them with HTTP 400 "Invalid signature in thinking block".

    When *model* matches the Kimi / Moonshot family (or *base_url* is a
    Kimi / Moonshot host), thinking blocks — signed or unsigned — are
    replayed as-is on assistant tool-call messages, unchanged. Kimi For
    Coding (K3+) issues AND validates its own thinking signatures; live
    probing (2026-07-18) confirmed both verbatim and content-mutated
    signed blocks round-trip with HTTP 200 on Kimi/Moonshot's Anthropic
    surface, so signature stripping there was silently discarding the
    model's prior chain-of-thought across turns. DeepSeek's /anthropic
    endpoint keeps the older contract (strip signed, preserve unsigned)
    since it cannot validate Anthropic signatures.
    """
    # Bind the adapter's helpers locally (lazy import avoids the circular
    # dependency: anthropic_adapter imports this module for its forwarder).
    from agent import anthropic_adapter as _aa
    _canonicalize_tool_search_result_types = _aa._canonicalize_tool_search_result_types
    _content_parts_to_anthropic_blocks = _aa._content_parts_to_anthropic_blocks
    _convert_content_to_anthropic = _aa._convert_content_to_anthropic
    _extract_preserved_thinking_blocks = _aa._extract_preserved_thinking_blocks
    _is_deepseek_anthropic_endpoint = _aa._is_deepseek_anthropic_endpoint
    _is_kimi_family_endpoint = _aa._is_kimi_family_endpoint
    _is_third_party_anthropic_endpoint = _aa._is_third_party_anthropic_endpoint
    _move_client_tool_use_blocks_to_end = _aa._move_client_tool_use_blocks_to_end
    _normalize_tool_search_result_for_input = _aa._normalize_tool_search_result_for_input
    _relocate_orphaned_tool_search_results = _aa._relocate_orphaned_tool_search_results
    _sanitize_block_for_anthropic_input = _aa._sanitize_block_for_anthropic_input
    _sanitize_tool_id = _aa._sanitize_tool_id
    system = None
    result = []

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        if role == "system":
            if isinstance(content, list):
                # Preserve cache_control markers on content blocks
                has_cache = any(
                    p.get("cache_control") for p in content if isinstance(p, dict)
                )
                if has_cache:
                    system = [p for p in content if isinstance(p, dict)]
                else:
                    system = "\n".join(
                        p["text"] for p in content if p.get("type") == "text"
                    )
            else:
                system = content
            continue

        if role == "assistant":
            # ── Verbatim replay (Anthropic-native) ──────────────────
            # When the assistant turn carries the original content array
            # captured by AnthropicTransport.normalize_response, replay
            # every block in its original position.  Anthropic signs
            # thinking blocks against their position in the response, and
            # context_management.clear_thinking_20251015 enforces that
            # each block stays in place across turns.  Recomposing from
            # reasoning_details + content + tool_calls reorders
            # interleaved thinking emitted under
            # interleaved-thinking-2025-05-14 and breaks signature
            # validation with HTTP 400 "thinking ... cannot be modified".
            #
            # Downstream (line ~2176+) still applies thinking-signature
            # management — strip-on-non-latest, downgrade-unsigned,
            # third-party-strip — over m["content"] regardless of which
            # branch produced it, so the verbatim blocks get the same
            # post-processing as recomposed ones.
            raw_blocks = m.get("anthropic_content_blocks")
            if isinstance(raw_blocks, list) and raw_blocks:
                # Security (upstream #19798 ported to the fork's verbatim-replay
                # path): the raw captured blocks carry the LIVE tool_use input
                # (un-redacted, as Anthropic returned it). The stored
                # ``tool_calls`` arguments, by contrast, were already run
                # through credential redaction before persistence. Re-source
                # each tool_use's ``input`` from that redacted map (keyed by id)
                # so a secret never leaks back onto the wire via this fast path.
                # Falls back to the raw input only when no redacted entry exists.
                _redacted_input_by_id: Dict[str, Any] = {}
                for _tc in m.get("tool_calls", []) or []:
                    if not isinstance(_tc, dict):
                        continue
                    _tc_id = _sanitize_tool_id(_tc.get("id", ""))
                    _fn = _tc.get("function", {}) or {}
                    _args = _fn.get("arguments", "{}")
                    try:
                        _redacted_input_by_id[_tc_id] = (
                            json.loads(_args) if isinstance(_args, str) else _args
                        )
                    except (json.JSONDecodeError, ValueError):
                        _redacted_input_by_id[_tc_id] = {}
                rebuilt: List[Dict[str, Any]] = []
                for b in copy.deepcopy(raw_blocks):
                    if not isinstance(b, dict):
                        rebuilt.append(b)
                        continue
                    btype = b.get("type", "")
                    # tool_search_tool_<variant>_tool_result blocks have
                    # their own input-shape normalizer (variant-specific
                    # inner structure that _sanitize_block_for_anthropic_input
                    # doesn't model).
                    if (
                        isinstance(btype, str)
                        and btype.startswith("tool_search_tool_")
                        and btype.endswith("_tool_result")
                    ):
                        rebuilt.append(_normalize_tool_search_result_for_input(b))
                    else:
                        # Strip response-only fields (e.g. text.parsed_output
                        # from structured output, or any future response-side
                        # field Anthropic adds).  Block position and the
                        # signed payload are preserved.
                        _clean = _sanitize_block_for_anthropic_input(b)
                        # Overlay the redacted input for tool_use blocks.
                        if (
                            isinstance(_clean, dict)
                            and _clean.get("type") == "tool_use"
                        ):
                            _bid = _sanitize_tool_id(_clean.get("id", ""))
                            if _bid in _redacted_input_by_id:
                                _clean["input"] = _redacted_input_by_id[_bid]
                        rebuilt.append(_clean)
                if not rebuilt:
                    rebuilt = [{"type": "text", "text": "(empty)"}]
                # Propagate top-level cache_control onto the last content
                # block — Anthropic's cache_control lives on content blocks,
                # not on the message dict.  The verbatim-replay path bypasses
                # the recomposition path that handles this below.
                _msg_cc = m.get("cache_control")
                if isinstance(_msg_cc, dict) and rebuilt:
                    _last = rebuilt[-1]
                    if isinstance(_last, dict) and "cache_control" not in _last:
                        _last["cache_control"] = dict(_msg_cc)
                result.append({"role": "assistant", "content": rebuilt})
                continue

            # ── Recomposition path (fallback / cross-provider) ──────
            blocks = _extract_preserved_thinking_blocks(m)
            # Anthropic server-side tool blocks (web_search etc.) — must be
            # re-emitted verbatim before text/tool_use blocks. Stored on the
            # message dict by run_agent._build_assistant_message after the
            # transport extracted them in normalize_response.
            preserved_server_blocks = m.get("server_tool_blocks")
            if isinstance(preserved_server_blocks, list):
                for sb in preserved_server_blocks:
                    if not isinstance(sb, dict):
                        continue
                    sb_type = sb.get("type", "")
                    if sb_type == "server_tool_use":
                        # server_tool_use is request-shape compatible; pass
                        # through as-is.
                        blocks.append(dict(sb))
                    elif sb_type == "web_search_tool_result":
                        blocks.append(dict(sb))
                    elif (
                        isinstance(sb_type, str)
                        and sb_type.startswith("tool_search_tool_")
                        and sb_type.endswith("_tool_result")
                    ):
                        blocks.append(_normalize_tool_search_result_for_input(sb))
            if content:
                if isinstance(content, list):
                    converted_content = _convert_content_to_anthropic(content)
                    if isinstance(converted_content, list):
                        blocks.extend(converted_content)
                else:
                    blocks.append({"type": "text", "text": str(content)})
            for tc in m.get("tool_calls", []):
                if not tc or not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(args) if isinstance(args, str) else args
                except (json.JSONDecodeError, ValueError):
                    parsed_args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": _sanitize_tool_id(tc.get("id", "")),
                    "name": fn.get("name", ""),
                    "input": parsed_args,
                })
            # Kimi's /coding endpoint (Anthropic protocol) requires assistant
            # tool-call messages to carry reasoning_content when thinking is
            # enabled server-side.  Preserve it as a thinking block so Kimi
            # can validate the message history.  See hermes-agent#13848.
            #
            # Accept empty string "" — _copy_reasoning_content_for_api()
            # injects "" as a tier-3 fallback for Kimi tool-call messages
            # that had no reasoning.  Kimi requires the field to exist, even
            # if empty.
            #
            # Prepend (not append): Anthropic protocol requires thinking
            # blocks before text and tool_use blocks.
            #
            # Guard: only add when reasoning_details didn't already contribute
            # thinking blocks.  On native Anthropic, reasoning_details produces
            # signed thinking blocks — adding another unsigned one from
            # reasoning_content would create a duplicate (same text) that gets
            # downgraded to a spurious text block on the last assistant message.
            reasoning_content = m.get("reasoning_content")
            _already_has_thinking = any(
                isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}
                for b in blocks
            )
            if isinstance(reasoning_content, str) and not _already_has_thinking:
                blocks.insert(0, {"type": "thinking", "thinking": reasoning_content})
            # Anthropic rejects empty assistant content
            effective = blocks or content
            if not effective or effective == "":
                effective = [{"type": "text", "text": "(empty)"}]
            # Propagate top-level cache_control (applied by
            # apply_anthropic_cache_control on the OpenAI-format message)
            # onto the last content block — Anthropic's cache_control
            # lives on content blocks, not on the message dict.
            _msg_cc = m.get("cache_control")
            if isinstance(_msg_cc, dict) and isinstance(effective, list) and effective:
                _last = effective[-1]
                if isinstance(_last, dict) and "cache_control" not in _last:
                    _last["cache_control"] = dict(_msg_cc)
            result.append({"role": "assistant", "content": effective})
            continue

        if role == "tool":
            # Sanitize tool_use_id and ensure non-empty content.
            # Computer-use (and other multimodal) tool results arrive as
            # either a list of OpenAI-style content parts, or a dict
            # marked `_multimodal` with an embedded `content` list. Convert
            # both into Anthropic `tool_result` inner blocks (text + image).
            multimodal_blocks: Optional[List[Dict[str, Any]]] = None
            if isinstance(content, dict) and content.get("_multimodal"):
                multimodal_blocks = _content_parts_to_anthropic_blocks(
                    content.get("content") or []
                )
                # Fallback text if the conversion produced nothing usable.
                if not multimodal_blocks and content.get("text_summary"):
                    multimodal_blocks = [
                        {"type": "text", "text": str(content["text_summary"])}
                    ]
            elif isinstance(content, list):
                converted = _content_parts_to_anthropic_blocks(content)
                if any(b.get("type") == "image" for b in converted):
                    multimodal_blocks = converted

            if multimodal_blocks:
                result_content: Any = multimodal_blocks
            elif isinstance(content, str):
                result_content = content
            else:
                result_content = json.dumps(content) if content else "(no output)"
            if not result_content:
                result_content = "(no output)"
            tool_result = {
                "type": "tool_result",
                "tool_use_id": _sanitize_tool_id(m.get("tool_call_id", "")),
                "content": result_content,
            }
            if isinstance(m.get("cache_control"), dict):
                tool_result["cache_control"] = dict(m["cache_control"])
            # Merge consecutive tool results into one user message
            if (
                result
                and result[-1]["role"] == "user"
                and isinstance(result[-1]["content"], list)
                and result[-1]["content"]
                and result[-1]["content"][0].get("type") == "tool_result"
            ):
                result[-1]["content"].append(tool_result)
            else:
                result.append({"role": "user", "content": [tool_result]})
            continue

        # Regular user message — validate non-empty content (Anthropic rejects empty)
        if isinstance(content, list):
            converted_blocks = _convert_content_to_anthropic(content)
            # Check if all text blocks are empty
            if not converted_blocks or all(
                b.get("text", "").strip() == ""
                for b in converted_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ):
                converted_blocks = [{"type": "text", "text": "(empty message)"}]
            result.append({"role": "user", "content": converted_blocks})
        else:
            # Validate string content is non-empty
            if not content or (isinstance(content, str) and not content.strip()):
                content = "(empty message)"
            result.append({"role": "user", "content": content})

    # Strip non-adjacent tool_use blocks — each tool_use must have a matching
    # tool_result in the IMMEDIATELY FOLLOWING user message.  A global ID match
    # is not enough: Anthropic rejects non-adjacent pairs with HTTP 400 even
    # when the IDs match somewhere later in the conversation (#52145).
    for i, m in enumerate(result):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        tool_use_ids_in_turn = {
            b.get("id")
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        }
        if not tool_use_ids_in_turn:
            continue

        # Collect result IDs from the immediately following user message only.
        adjacent_result_ids: set = set()
        if i + 1 < len(result):
            nxt = result[i + 1]
            if nxt.get("role") == "user" and isinstance(nxt.get("content"), list):
                for block in nxt["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        adjacent_result_ids.add(block.get("tool_use_id"))

        orphaned = tool_use_ids_in_turn - adjacent_result_ids
        if not orphaned:
            continue

        kept = [
            b
            for b in m["content"]
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in orphaned)
        ]
        # If stripping an orphaned tool_use mutated a turn that also carries a
        # signed thinking block, that block's Anthropic signature was computed
        # against the ORIGINAL (un-stripped) turn content and is now invalid.
        # Anthropic rejects the replayed turn with HTTP 400 "thinking blocks in
        # the latest assistant message cannot be modified". Flag the turn so the
        # thinking-signature pass below can demote the dead signature instead of
        # replaying it verbatim. See hermes-agent: extended-thinking + parallel
        # tool batch interrupted mid-flight → non-retryable 400 crash-loop.
        if len(kept) != len(m["content"]) and any(
            isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"}
            for b in m["content"]
        ):
            m["_thinking_signature_invalidated"] = True
        m["content"] = kept if kept else [{"type": "text", "text": "(tool call removed)"}]

    # Strip orphaned tool_result blocks (no matching tool_use precedes them).
    # This is the mirror of the above: context compression or session truncation
    # can remove an assistant message containing a tool_use while leaving the
    # subsequent tool_result intact.  Anthropic rejects these with a 400.
    tool_use_ids = set()
    for m in result:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block.get("id"))
    for m in result:
        if m["role"] == "user" and isinstance(m["content"], list):
            m["content"] = [
                b
                for b in m["content"]
                if b.get("type") != "tool_result" or b.get("tool_use_id") in tool_use_ids
            ]
            if not m["content"]:
                m["content"] = [{"type": "text", "text": "(tool result removed)"}]

    # Enforce strict role alternation (Anthropic rejects consecutive same-role messages)
    fixed = []
    for m in result:
        if fixed and fixed[-1]["role"] == m["role"]:
            if m["role"] == "user":
                # Merge consecutive user messages
                prev_content = fixed[-1]["content"]
                curr_content = m["content"]
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    fixed[-1]["content"] = prev_content + "\n" + curr_content
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    fixed[-1]["content"] = prev_content + curr_content
                else:
                    # Mixed types — wrap string in list
                    if isinstance(prev_content, str):
                        prev_content = [{"type": "text", "text": prev_content}]
                    if isinstance(curr_content, str):
                        curr_content = [{"type": "text", "text": curr_content}]
                    fixed[-1]["content"] = prev_content + curr_content
            else:
                # Consecutive assistant messages — merge text content.
                # Propagate the orphan-strip signature-invalidation flag onto the
                # surviving (prev) dict so the thinking-signature pass still sees it.
                if m.get("_thinking_signature_invalidated"):
                    fixed[-1]["_thinking_signature_invalidated"] = True
                # Drop thinking blocks from the *second* message: their
                # signature was computed against a different turn boundary
                # and becomes invalid once merged.
                if isinstance(m["content"], list):
                    m["content"] = [
                        b for b in m["content"]
                        if not (isinstance(b, dict) and b.get("type") in {"thinking", "redacted_thinking"})
                    ]
                prev_blocks = fixed[-1]["content"]
                curr_blocks = m["content"]
                if isinstance(prev_blocks, list) and isinstance(curr_blocks, list):
                    fixed[-1]["content"] = prev_blocks + curr_blocks
                elif isinstance(prev_blocks, str) and isinstance(curr_blocks, str):
                    fixed[-1]["content"] = prev_blocks + "\n" + curr_blocks
                else:
                    # Mixed types — normalize both to list and merge
                    if isinstance(prev_blocks, str):
                        prev_blocks = [{"type": "text", "text": prev_blocks}]
                    if isinstance(curr_blocks, str):
                        curr_blocks = [{"type": "text", "text": curr_blocks}]
                    fixed[-1]["content"] = prev_blocks + curr_blocks
        else:
            fixed.append(m)
    result = fixed

    # ── Thinking block signature management ──────────────────────────
    # Anthropic signs thinking blocks against the full turn content.
    # Any upstream mutation (context compression, session truncation,
    # orphan stripping, message merging) invalidates the signature,
    # causing HTTP 400 "Invalid signature in thinking block".
    #
    # Signatures are Anthropic-proprietary.  Third-party endpoints
    # (MiniMax, Microsoft Foundry, self-hosted proxies) cannot validate
    # them and will reject them outright.  When targeting a third-party
    # endpoint, strip ALL thinking/redacted_thinking blocks from every
    # assistant message — the third-party will generate its own
    # thinking blocks if it supports extended thinking.
    #
    # For direct Anthropic (strategy following clawdbot/OpenClaw):
    # 1. Strip thinking/redacted_thinking from all assistant messages
    #    EXCEPT the last one — preserves reasoning continuity on the
    #    current tool-use chain while avoiding stale signature errors.
    # 2. Downgrade unsigned thinking blocks (no signature) to text —
    #    Anthropic can't validate them and will reject them.
    # 3. Strip cache_control from thinking/redacted_thinking blocks —
    #    cache markers can interfere with signature validation.
    _THINKING_TYPES = frozenset(("thinking", "redacted_thinking"))
    _is_third_party = _is_third_party_anthropic_endpoint(base_url)

    last_assistant_idx = None
    for i in range(len(result) - 1, -1, -1):
        if result[i].get("role") == "assistant":
            last_assistant_idx = i
            break

    for idx, m in enumerate(result):
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue

        if _is_kimi_family_endpoint(base_url, model):
            # Kimi does not enforce thinking signatures — replay as-is.
            # Live probing (2026-07-18) showed Kimi For Coding (K3+) issues
            # AND validates its own thinking signatures, and Moonshot's
            # Anthropic surface accepts signed blocks the same way (both
            # verbatim and content-mutated signed blocks replay with HTTP
            # 200). The prior contract stripped signed blocks unconditionally
            # and silently discarded the model's prior chain-of-thought in
            # multi-turn conversations. One uniform rule for the whole Kimi
            # family now — no /coding-vs-Moonshot split, no signed/unsigned
            # split. Shared cleanup below still strips cache markers + the
            # internal invalidation flag.
            pass
        elif _is_deepseek_anthropic_endpoint(base_url):
            # DeepSeek's /anthropic endpoint enables thinking server-side and
            # requires unsigned thinking blocks on replayed assistant
            # tool-call messages, but — unlike Kimi — cannot validate
            # Anthropic-signed blocks. Strip signed, preserve unsigned.
            new_content = []
            for b in m["content"]:
                if not isinstance(b, dict) or b.get("type") not in _THINKING_TYPES:
                    new_content.append(b)
                    continue
                if b.get("signature") or b.get("data"):
                    # Anthropic-signed block — upstream can't validate, strip
                    continue
                # Unsigned thinking (synthesised from reasoning_content) —
                # keep it: the upstream needs it for message-history validation.
                new_content.append(b)
            m["content"] = new_content or [{"type": "text", "text": "(empty)"}]
        elif _is_third_party or idx != last_assistant_idx:
            # Third-party endpoint: strip ALL thinking blocks from every
            # assistant message — signatures are Anthropic-proprietary.
            # Direct Anthropic: strip from non-latest assistant messages only.
            stripped = [
                b for b in m["content"]
                if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
            ]
            m["content"] = stripped or [{"type": "text", "text": "(thinking elided)"}]
        else:
            # Latest assistant on direct Anthropic: keep signed thinking
            # blocks for reasoning continuity; downgrade unsigned ones to
            # plain text.
            #
            # Exception: if orphan-stripping (or another structural mutation)
            # removed a tool_use block from THIS turn, every thinking signature
            # on it was computed against the original content and is now dead.
            # Anthropic rejects the replayed turn with a non-retryable HTTP 400
            # ("thinking blocks in the latest assistant message cannot be
            # modified"). Demote ALL thinking blocks on this turn to text so the
            # turn replays cleanly. See the orphan-strip flag set above.
            signature_dead = bool(m.get("_thinking_signature_invalidated"))
            new_content = []
            for b in m["content"]:
                if not isinstance(b, dict) or b.get("type") not in _THINKING_TYPES:
                    new_content.append(b)
                    continue
                if signature_dead:
                    # Dead signature — preserve the reasoning text, drop the block.
                    thinking_text = b.get("thinking", "")
                    if thinking_text:
                        new_content.append({"type": "text", "text": thinking_text})
                elif b.get("type") == "redacted_thinking":
                    # Redacted blocks use 'data' for the signature payload
                    if b.get("data"):
                        new_content.append(b)
                    # else: drop — no data means it can't be validated
                elif b.get("signature"):
                    # Signed thinking block — keep it
                    new_content.append(b)
                else:
                    # Unsigned thinking — downgrade to text so it's not lost
                    thinking_text = b.get("thinking", "")
                    if thinking_text:
                        new_content.append({"type": "text", "text": thinking_text})
            m["content"] = new_content or [{"type": "text", "text": "(empty)"}]

        # Strip cache_control from any remaining thinking/redacted_thinking
        # blocks — cache markers interfere with signature validation.
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") in _THINKING_TYPES:
                b.pop("cache_control", None)

        # Internal bookkeeping flag must never leak into the API payload.
        m.pop("_thinking_signature_invalidated", None)

    # Anthropic's tool_search emits the result block in a *later* response
    # than the one that issued the server_tool_use, but its input validator
    # requires same-message pairing. Walk the assembled message list and
    # move any orphaned result blocks back to the assistant message that
    # owns the matching server_tool_use.
    _relocate_orphaned_tool_search_results(result)

    # Anthropic's native web_search (web_search_20250305) requires the
    # server_tool_use and its web_search_tool_result to live in the SAME
    # assistant message, with the use immediately before the result.
    # Compaction or message-merging can split the pair; strip orphans in
    # either direction per message so neither half survives without its
    # partner.
    for _m in result:
        if _m.get("role") != "assistant":
            continue
        _c = _m.get("content")
        if not isinstance(_c, list):
            continue
        _ws_use_ids: set = set()
        _ws_result_ids: set = set()
        for _b in _c:
            if not isinstance(_b, dict):
                continue
            _t = _b.get("type")
            if _t == "server_tool_use" and _b.get("name") == "web_search":
                _id = _b.get("id")
                if isinstance(_id, str):
                    _ws_use_ids.add(_id)
            elif _t == "web_search_tool_result":
                _ru = _b.get("tool_use_id")
                if isinstance(_ru, str):
                    _ws_result_ids.add(_ru)
        _ws_orphans = _ws_use_ids.symmetric_difference(_ws_result_ids)
        if _ws_orphans:
            _kept_ws = [
                _b for _b in _c
                if not (
                    isinstance(_b, dict)
                    and (
                        (_b.get("type") == "server_tool_use"
                         and _b.get("name") == "web_search"
                         and _b.get("id") in _ws_orphans)
                        or (_b.get("type") == "web_search_tool_result"
                            and _b.get("tool_use_id") in _ws_orphans)
                    )
                )
            ]
            _m["content"] = _kept_ws or [{"type": "text", "text": "(empty)"}]

    # Drop ``server_tool_use`` blocks whose paired result NEVER arrived
    # (stream interruption, timeout, cancel mid-response). Without this,
    # the assistant message has a use without a result, and every API
    # call replays the orphan and 400s. Runs after relocation so a
    # split-but-deliverable pair gets repaired first; only truly
    # missing results trigger a drop. Operates on the wire-shape
    # ``msg["content"]`` (lists of blocks).
    #
    # Includes both tool_search_tool_*_tool_result ids (cross-message
    # pairing already reconciled by the relocator above) and
    # web_search_tool_result ids (same-message pairing already
    # reconciled by the web_search orphan pass above) — without the
    # latter, a server_tool_use legitimately paired with a web_search
    # result would be misclassified as orphaned and dropped, stranding
    # the result block and triggering a 400 on the next request.
    _result_ids_wire: set = set()
    for _m in result:
        if _m.get("role") != "assistant":
            continue
        _c = _m.get("content")
        if not isinstance(_c, list):
            continue
        for _b in _c:
            if not isinstance(_b, dict):
                continue
            _t = _b.get("type")
            if not isinstance(_t, str):
                continue
            if (
                _t == "tool_search_tool_result"
                or (_t.startswith("tool_search_tool_") and _t.endswith("_tool_result"))
                or _t == "web_search_tool_result"
            ):
                _ru = _b.get("tool_use_id")
                if isinstance(_ru, str):
                    _result_ids_wire.add(_ru)
    for _m in result:
        if _m.get("role") != "assistant":
            continue
        _c = _m.get("content")
        if not isinstance(_c, list):
            continue
        _kept = [
            _b for _b in _c
            if not (
                isinstance(_b, dict)
                and _b.get("type") == "server_tool_use"
                and isinstance(_b.get("id"), str)
                and _b["id"] not in _result_ids_wire
            )
        ]
        if len(_kept) != len(_c):
            _m["content"] = _kept or [{"type": "text", "text": "(empty)"}]

    # Defense-in-depth: canonicalize tool_search_tool_*_tool_result block
    # types to the bare ``tool_search_tool_result`` form. The capture-time
    # fix in ``agent/transports/anthropic.py`` handles fresh responses,
    # but sessions persisted with a variant-suffixed type (e.g. from an
    # earlier broken Hermes version that stored the wire variant, or
    # any code path that bypasses the SDK Pydantic layer) need the same
    # rewrite at outbound request time so Anthropic's input validator
    # doesn't reject them with 400. Idempotent: bare canonical is the
    # fixed point.
    _canonicalize_tool_search_result_types(result)

    # Reorder client tool_use blocks to the end of each assistant message
    # when followed by server-side blocks. Anthropic's input validator
    # demands the next message's tool_result be "immediately after" the
    # client tool_use, with no intervening server-side blocks. The model
    # sometimes emits tool_search AFTER deciding to call a client tool;
    # the captured response carries that order verbatim and 400s on
    # replay until we move the client tool_use past the server-side
    # blocks. Idempotent: already-tail-positioned tool_use is skipped.
    _move_client_tool_use_blocks_to_end(result)

    # ── Image eviction: keep only the most recent N screenshots ─────
    # computer_use screenshots (base64 images) sit inside tool_result
    # blocks: they accumulate and are sent with every API call. Each
    # costs ~1,465 tokens; after 10+ the conversation becomes slow
    # even for simple text queries. Walk backward, keep the most recent
    # _MAX_KEEP_IMAGES, replace older ones with a text placeholder.
    _MAX_KEEP_IMAGES = 3
    _image_count = 0
    for msg in reversed(result):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            has_image = any(
                isinstance(b, dict) and b.get("type") == "image"
                for b in inner
            )
            if not has_image:
                continue
            _image_count += 1
            if _image_count > _MAX_KEEP_IMAGES:
                block["content"] = [
                    b if b.get("type") != "image"
                    else {"type": "text", "text": "[screenshot removed to save context]"}
                    for b in inner
                ]

    return system, result
