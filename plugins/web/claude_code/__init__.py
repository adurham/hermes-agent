"""Claude Code CLI web provider (search via WebSearch, extract via WebFetch) — bundled, auto-loaded."""
from __future__ import annotations
from plugins.web.claude_code.provider import ClaudeCodeWebProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(ClaudeCodeWebProvider())
