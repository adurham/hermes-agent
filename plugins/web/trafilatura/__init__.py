"""Trafilatura direct-fetch web extract plugin — bundled, auto-loaded.

Extract-only (no search). Free, no API key, no account. Pair with a
search-only backend (brave-free, ddgs, searxng) for a fully free web
toolset on any provider (exo, ollama-cloud, anything non-Anthropic).
"""

from __future__ import annotations

from plugins.web.trafilatura.provider import TrafilaturaWebExtractProvider


def register(ctx) -> None:
    """Register the Trafilatura extract provider with the plugin context."""
    ctx.register_web_search_provider(TrafilaturaWebExtractProvider())
