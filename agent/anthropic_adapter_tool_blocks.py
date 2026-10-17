"""Phases of ``anthropic_adapter._strip_unknown_tool_blocks``: the text breadcrumbs that
replace a stale ``tool_use`` / ``tool_result`` pair, and the per-message block reordering
Anthropic's validator needs after that rewrite.

Every helper late-imports the facade's names, so a patch on ``agent.anthropic_adapter.<name>``
still reaches the code that reads it.
"""

from __future__ import annotations

from typing import Any


def _unknown_tool_use_meta(name: Any, inp_str: str) -> dict:
    """Breadcrumb metadata for an unknown ``tool_use``: its name and a capped input echo."""
    if len(inp_str) > 200:
        inp_str = inp_str[:200] + "...(truncated)"
    return {
        "name": name or "(unnamed)",
        "input_summary": inp_str,
    }


def _tool_use_breadcrumb(meta: dict) -> dict:
    """Text block standing in for a ``tool_use`` whose tool is no longer available."""
    return {
        "type": "text",
        "text": (
            f"[Previous tool call: {meta['name']}("
            f"{meta['input_summary']}) — tool no longer available "
            f"in this turn.]"
        ),
    }


def _tool_result_breadcrumb(meta: dict, result_summary: str) -> dict:
    """Text block standing in for the ``tool_result`` of an unknown ``tool_use``."""
    if len(result_summary) > 400:
        result_summary = result_summary[:400] + "...(truncated)"
    return {
        "type": "text",
        "text": (
            f"[Previous tool result for {meta['name']}: "
            f"{result_summary}]"
        ),
    }


def _tool_result_text(result_content: Any) -> str:
    """Join the text pieces of a ``tool_result``'s content (a list of text/image blocks or a scalar)."""
    if isinstance(result_content, list):
        # Anthropic tool_result content is a list of text/image blocks
        text_pieces = []
        for rc in result_content:
            if isinstance(rc, dict) and rc.get("type") == "text":
                text_pieces.append(str(rc.get("text", "")))
        return "\n".join(text_pieces)
    return str(result_content or "")


def _is_block_type(block: Any, btype: str) -> bool:
    return isinstance(block, dict) and block.get("type") == btype


def _tool_uses_to_tail(new_blocks: list) -> list:
    """Move surviving ``tool_use`` blocks after every other block, unless a thinking block pins the order."""
    from agent.anthropic_adapter import logger

    first_tu = next(i for i, b in enumerate(new_blocks) if _is_block_type(b, "tool_use"))
    last_non_tu = max(
        (i for i, b in enumerate(new_blocks) if not _is_block_type(b, "tool_use")),
        default=-1,
    )
    if first_tu >= last_non_tu:
        return new_blocks
    has_thinking = any(
        isinstance(b, dict)
        and b.get("type") in ("thinking", "redacted_thinking")
        for b in new_blocks
    )
    if has_thinking:
        logger.warning(
            "anthropic_adapter: assistant message has tool_use "
            "followed by non-tool_use blocks AND contains a "
            "thinking block; cannot reorder without invalidating "
            "the thinking signature. Anthropic may reject with "
            "a 400 about tool_use without tool_result.",
        )
        return new_blocks
    tool_uses = [b for b in new_blocks if _is_block_type(b, "tool_use")]
    other = [b for b in new_blocks if not _is_block_type(b, "tool_use")]
    return other + tool_uses


def _finalize_rewritten_blocks(role: Any, new_blocks: list) -> list:
    """Make a rewritten message's content valid again: non-empty and tool blocks in wire order."""
    # Empty content after rewrites — leave a placeholder so the
    # message still validates (Anthropic rejects empty content).
    if not new_blocks:
        new_blocks = [{"type": "text", "text": "(content removed)"}]
    # In a user message that's responding to an assistant tool_use,
    # Anthropic requires tool_result blocks to come BEFORE any other
    # content; otherwise the API 400s with
    #   `tool_use` ids were found without `tool_result` blocks
    #   immediately after: <id>
    # The in-place rewrite can leave leading text breadcrumbs
    # ahead of a surviving real tool_result (when some — but not all
    # — tool_results in the same user message were converted). Stable
    # partition restores the required ordering while preserving the
    # breadcrumbs after the live tool_results.
    if role == "user" and any(_is_block_type(b, "tool_result") for b in new_blocks):
        tool_results = [b for b in new_blocks if _is_block_type(b, "tool_result")]
        other = [b for b in new_blocks if not _is_block_type(b, "tool_result")]
        return tool_results + other
    # Symmetric fix on the assistant side: Anthropic rejects an
    # assistant message whose ``tool_use`` is followed by any other
    # block with the SAME 400 (``tool_use`` ids without
    # ``tool_result`` blocks immediately after). This rewrite can
    # produce that pattern when one tool_use survives (live tool) and
    # a later sibling tool_use becomes a text breadcrumb — leaving
    # ``[tool_use, text]`` in the same message. Move surviving
    # tool_use blocks to the tail to restore the contract.
    #
    # Thinking-signature safety: thinking blocks are signed against
    # their position in the response stream. If any thinking block
    # is present, reordering risks invalidating the signature; skip
    # and log instead so Anthropic surfaces the issue.
    if role == "assistant" and any(_is_block_type(b, "tool_use") for b in new_blocks):
        return _tool_uses_to_tail(new_blocks)
    return new_blocks
