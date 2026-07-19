"""Claude Code CLI web provider — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Delegates
``web_search`` and ``web_extract`` to the Claude Code CLI's built-in
``WebSearch`` and ``WebFetch`` tools by shelling out to ``claude -p`` with
``--output-format json`` and a ``--json-schema`` constraint so the model
returns results in a predictable shape.

Uses the user's existing Anthropic auth (via ``claude auth login``) so there
are no extra API keys to manage — search/extract becomes "free" for anyone
already paying for a Claude Code subscription.

Configuration::

    # ~/.hermes/config.yaml
    web:
      backend: "claude-code"          # or search_backend / extract_backend

Requirements:
    * ``claude`` CLI on ``PATH`` (https://claude.com/claude-code)
    * ``claude auth status`` exits 0 (i.e. logged in)

The previous in-tree implementation lived at
``tools.web_providers.claude_code`` and used the legacy in-tree ABCs; this
file is the canonical replacement after PR #25182 moved every web provider
to the plugin layout.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


# ─── Auth detection (cached) ──────────────────────────────────────────────────

_AUTH_CACHE: Optional[bool] = None


def _reset_auth_cache() -> None:
    """Clear the cached auth-status result. Used by tests."""
    global _AUTH_CACHE
    _AUTH_CACHE = None


def _is_configured() -> bool:
    """Return True when ``claude`` is on PATH AND ``claude auth status`` exits 0.

    Result is cached process-wide; call :func:`_reset_auth_cache` to
    invalidate (e.g. from tests, or after a user logs in/out).
    """
    global _AUTH_CACHE
    if _AUTH_CACHE is not None:
        return _AUTH_CACHE

    binary = shutil.which("claude")
    if not binary:
        _AUTH_CACHE = False
        return False

    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("claude auth status check failed: %s", exc)
        _AUTH_CACHE = False
        return False

    _AUTH_CACHE = proc.returncode == 0
    return _AUTH_CACHE


# ─── Shared JSON schemas / system prompts ─────────────────────────────────────

_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "url", "description"],
            },
        }
    },
    "required": ["results"],
}

_SEARCH_SYSTEM_PROMPT = (
    "You are a web search backend. Run a single WebSearch for the user's "
    "query. Return the top results as JSON matching the provided schema. "
    "Do not summarize, do not visit URLs — just call WebSearch once and "
    "return its results structured as the schema requires."
)

_EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["url", "title", "content"],
            },
        }
    },
    "required": ["pages"],
}

_EXTRACT_SYSTEM_PROMPT = (
    "You are a web content extraction backend. For each URL the user "
    "provides, call WebFetch exactly once and capture the page's title and "
    "main textual content. Return one entry per input URL as JSON matching "
    "the provided schema. Do not summarize aggressively — preserve the "
    "page's prose. Do not invent URLs that were not in the input."
)


def _parse_claude_json(stdout: str, inner_key: str) -> List[Dict[str, Any]]:
    """Extract the inner list from a ``claude -p --output-format json`` envelope.

    Handles three shapes:
      1. Single-object envelope: ``{"structured_output": {...}, "result": "..."}``
      2. Single-object with structured payload nested in ``result`` (string JSON).
      3. Event-array log (when the CLI emits the full turn stream as a JSON
         array). We scan for any element that carries our schema-validated
         payload — preferring ``type == "result"`` envelopes — then fall back
         to any tool_use input/output that contains ``inner_key``.
    """
    parsed = json.loads(stdout)

    def _from_obj(obj: Dict[str, Any]) -> Any:
        if not isinstance(obj, dict):
            return None
        structured = obj.get("structured_output")
        if isinstance(structured, dict) and isinstance(structured.get(inner_key), list):
            return structured[inner_key]
        result_field = obj.get("result")
        if isinstance(result_field, str) and result_field.strip():
            try:
                inner = json.loads(result_field)
            except json.JSONDecodeError:
                inner = None
            if isinstance(inner, dict) and isinstance(inner.get(inner_key), list):
                return inner[inner_key]
        if isinstance(result_field, dict) and isinstance(result_field.get(inner_key), list):
            return result_field[inner_key]
        return None

    if isinstance(parsed, dict):
        value = _from_obj(parsed)
        if value is not None:
            return value
        raise ValueError(
            f"claude JSON envelope missing '{inner_key}' "
            f"(keys present: {sorted(parsed.keys())[:8]})"
        )

    if isinstance(parsed, list):
        for ev in reversed(parsed):
            if isinstance(ev, dict) and ev.get("type") == "result":
                v = _from_obj(ev)
                if v is not None:
                    return v
        for ev in parsed:
            if not isinstance(ev, dict):
                continue
            v = _from_obj(ev)
            if v is not None:
                return v
            msg = ev.get("message")
            if isinstance(msg, dict):
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    inp = block.get("input")
                    if isinstance(inp, dict) and isinstance(inp.get(inner_key), list):
                        return inp[inner_key]
        raise ValueError(
            f"claude event stream missing '{inner_key}' "
            f"({len(parsed)} events scanned)"
        )

    raise ValueError(
        f"claude JSON envelope is {type(parsed).__name__}, expected dict or list"
    )


class ClaudeCodeWebProvider(WebSearchProvider):
    """Web search + extract via ``claude -p`` + the WebSearch/WebFetch tools.

    A single provider class advertises both capabilities. Both shell out to
    the same ``claude`` binary with different ``--allowedTools`` /
    ``--json-schema`` arguments and parse the structured output envelope.
    """

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    def is_available(self) -> bool:
        """Return True when the claude CLI is installed and logged in.

        Performs a subprocess call to ``claude auth status`` on first call
        per process, then caches the result. Must not raise.
        """
        return _is_configured()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        binary = shutil.which("claude")
        if not binary:
            return {"success": False, "error": "claude CLI not found on PATH"}

        args = [
            binary,
            "-p", query,
            "--allowedTools", "WebSearch",
            "--output-format", "json",
            "--max-turns", "6",
            "--json-schema", json.dumps(_SEARCH_SCHEMA),
            "--system-prompt", _SEARCH_SYSTEM_PROMPT,
        ]

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            logger.warning("claude-code search timed out after 60s for query=%r", query)
            return {"success": False, "error": "claude-code search timed out after 60s"}
        except (FileNotFoundError, OSError) as exc:
            logger.warning("claude-code search failed to launch: %s", exc)
            return {"success": False, "error": f"Could not launch claude CLI: {exc}"}

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip() or "(no stderr)"
            logger.warning("claude-code search exited %d: %s", proc.returncode, stderr)
            return {
                "success": False,
                "error": f"claude CLI exited {proc.returncode}: {stderr[:500]}",
            }

        try:
            raw_results = _parse_claude_json(proc.stdout, "results")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("claude-code search JSON parse error: %s", exc)
            return {
                "success": False,
                "error": f"Could not parse claude CLI JSON output: {exc}",
            }

        web_results = []
        for i, r in enumerate(raw_results[:limit]):
            if not isinstance(r, dict):
                continue
            web_results.append({
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("description", "")),
                "position": i + 1,
            })

        logger.info(
            "claude-code search '%s': %d results (from %d raw, limit %d)",
            query, len(web_results), len(raw_results), limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via the Claude Code WebFetch tool.

        Returns a list of dicts shaped for the legacy LLM post-processing
        pipeline. On per-URL or whole-batch failure, results carry an
        ``error`` field rather than raising.
        """
        binary = shutil.which("claude")
        if not binary:
            return [{"url": u, "error": "claude CLI not found on PATH"} for u in urls]

        if not urls:
            return []

        numbered = "\n".join(f"{i + 1}. {u}" for i, u in enumerate(urls))
        prompt = f"Extract content from these URLs:\n{numbered}"

        # WebFetch is approximately one tool call per URL; give Claude a
        # little headroom (e.g. retries / a final structured-output turn).
        max_turns = 2 * len(urls) + 2

        args = [
            binary,
            "-p", prompt,
            "--allowedTools", "WebFetch",
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--json-schema", json.dumps(_EXTRACT_SCHEMA),
            "--system-prompt", _EXTRACT_SYSTEM_PROMPT,
        ]

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            logger.warning("claude-code extract timed out after 90s for %d URL(s)", len(urls))
            return [{"url": u, "error": "claude-code extract timed out after 90s"} for u in urls]
        except (FileNotFoundError, OSError) as exc:
            logger.warning("claude-code extract failed to launch: %s", exc)
            return [{"url": u, "error": f"Could not launch claude CLI: {exc}"} for u in urls]

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip() or "(no stderr)"
            logger.warning("claude-code extract exited %d: %s", proc.returncode, stderr)
            return [
                {"url": u, "error": f"claude CLI exited {proc.returncode}: {stderr[:500]}"}
                for u in urls
            ]

        try:
            raw_pages = _parse_claude_json(proc.stdout, "pages")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("claude-code extract JSON parse error: %s", exc)
            return [
                {"url": u, "error": f"Could not parse claude CLI JSON output: {exc}"}
                for u in urls
            ]

        documents: List[Dict[str, Any]] = []
        for page in raw_pages:
            if not isinstance(page, dict):
                continue
            content = str(page.get("content", ""))
            documents.append({
                "url": str(page.get("url", "")),
                "title": str(page.get("title", "")),
                "content": content,
                "raw_content": content,
                "metadata": {"source": "claude-code"},
            })

        logger.info(
            "claude-code extract: %d page(s) returned for %d requested URL(s)",
            len(documents), len(urls),
        )

        return documents

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Claude Code",
            "badge": "free · uses Anthropic subscription",
            "tag": "Delegates to the Claude Code CLI's built-in WebSearch/WebFetch — no extra API keys",
            "env_vars": [],
        }
