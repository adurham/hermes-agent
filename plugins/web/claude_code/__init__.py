"""Claude Code CLI web provider — bundled, auto-loaded.

Delegates web_search/web_extract to the Claude Code CLI's WebSearch/WebFetch
tools by shelling out to ``claude -p``. Uses the user's existing Anthropic
auth so no extra API keys are required.
"""

from __future__ import annotations

from plugins.web.claude_code.provider import ClaudeCodeWebProvider


def register(ctx) -> None:
    """Register the Claude Code provider with the plugin context."""
    ctx.register_web_search_provider(ClaudeCodeWebProvider())
