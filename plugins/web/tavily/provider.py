"""Tavily web search + content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Two
capabilities advertised:

- ``supports_search()``  -> True (Tavily ``/search``)
- ``supports_extract()`` -> True (Tavily ``/extract``)

Both are sync — the underlying call is ``httpx.post(...)``.

Config keys this provider responds to::

    web:
      search_backend: "tavily"     # explicit per-capability
      extract_backend: "tavily"    # explicit per-capability
      backend: "tavily"            # shared fallback for both

Env vars::

    TAVILY_API_KEY=...           # https://app.tavily.com/home (required)
    TAVILY_BASE_URL=...          # optional override of https://api.tavily.com

Tavily is a **keyed, opt-in** provider: it requires ``TAVILY_API_KEY`` and
never participates in the default-on keyless ring. Requests carry
``Authorization: Bearer TAVILY_API_KEY``; a missed key raises a clear error
rather
than sending an anonymous request.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_CLIENT_NAME = "hermes-agent"


def _tavily_headers(api_key: str) -> Dict[str, str]:
    """Build the Tavily request headers for keyed access only.

    ``api_key`` must be non-empty — :func:`_tavily_request` guarantees this
    before reaching the wire, so a Tavily call is never sent unauthenticated.
    """
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Client-Name": _CLIENT_NAME,
    }


def _tavily_request(
    endpoint: str, payload: Dict[str, Any], api_key: Optional[str] = None
) -> Dict[str, Any]:
    """POST to the Tavily API and return the parsed JSON response.

    Reads ``TAVILY_API_KEY`` via :func:`get_provider_env` unless *api_key* is
    passed explicitly. Raises ``ValueError`` when the key is missing so the
    caller surfaces a clear typed error — Tavily is keyed, opt-in only, so a
    request never goes out unauthenticated. Non-2xx responses raise
    ``ValueError`` with the response body so Tavily's error / rate-limit text
    reaches the model.
    """
    from agent.web_search_provider import get_provider_env

    if api_key is None:
        api_key = get_provider_env("TAVILY_API_KEY")
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY environment variable not set. "
            "Get your API key at https://app.tavily.com/home"
        )
    base_url = get_provider_env("TAVILY_BASE_URL") or "https://api.tavily.com"
    url = f"{base_url}/{endpoint.lstrip('/')}"
    logger.info("Tavily %s request to %s", endpoint, url)

    response = httpx.post(
        url,
        json=payload,
        timeout=60,
        headers=_tavily_headers(api_key),
    )
    if response.status_code >= 400:
        body = (response.text or "").strip()
        detail = body or f"HTTP {response.status_code}"
        raise ValueError(detail)
    return response.json()


def _normalize_tavily_search_results(response: Dict[str, Any]) -> Dict[str, Any]:
    """Map Tavily ``/search`` response to ``{success, data: {web: [...]}}``."""
    web_results = []
    for i, result in enumerate(response.get("results", [])):
        web_results.append(
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "description": result.get("content", ""),
                "position": i + 1,
            }
        )
    return {"success": True, "data": {"web": web_results}}


def _normalize_tavily_documents(
    response: Dict[str, Any], fallback_url: str = ""
) -> List[Dict[str, Any]]:
    """Map Tavily ``/extract`` response to standard documents.

    Documents follow the legacy LLM post-processing shape::

        {"url", "title", "content", "raw_content", "metadata"}

    Failures (``failed_results``, ``failed_urls``) become result entries
    with an ``error`` field rather than raising.
    """
    documents: List[Dict[str, Any]] = []
    for result in response.get("results", []):
        url = result.get("url", fallback_url)
        raw = result.get("raw_content", "") or result.get("content", "")
        documents.append(
            {
                "url": url,
                "title": result.get("title", ""),
                "content": raw,
                "raw_content": raw,
                "metadata": {"sourceURL": url, "title": result.get("title", "")},
            }
        )
    for fail in response.get("failed_results", []):
        documents.append(
            {
                "url": fail.get("url", fallback_url),
                "title": "",
                "content": "",
                "raw_content": "",
                "error": fail.get("error", "extraction failed"),
                "metadata": {"sourceURL": fail.get("url", fallback_url)},
            }
        )
    for fail_url in response.get("failed_urls", []):
        url_str = fail_url if isinstance(fail_url, str) else str(fail_url)
        documents.append(
            {
                "url": url_str,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "extraction failed",
                "metadata": {"sourceURL": url_str},
            }
        )
    return documents


class TavilyWebSearchProvider(WebSearchProvider):
    """Tavily search + extract provider (keyed, opt-in)."""

    @property
    def name(self) -> str:
        return "tavily"

    @property
    def display_name(self) -> str:
        return "Tavily"

    def is_available(self) -> bool:
        """Return True when ``TAVILY_API_KEY`` is set to a non-empty value."""
        from agent.web_search_provider import get_provider_env

        return bool(get_provider_env("TAVILY_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Tavily search via the keyed endpoint."""
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            logger.info("Tavily search: '%s' (limit=%d)", query, limit)
            raw = _tavily_request(
                "search",
                {
                    "query": query,
                    "max_results": min(limit, 20),
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            return _normalize_tavily_search_results(raw)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — including httpx errors
            logger.warning("Tavily search error: %s", exc)
            return {"success": False, "error": f"Tavily search failed: {exc}"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via Tavily.

        Sync — the underlying call is httpx.post(...). Returns the legacy
        list-of-results shape; per-URL failures become items with ``error``.
        """
        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [
                    {"url": u, "error": "Interrupted", "title": ""} for u in urls
                ]

            logger.info("Tavily extract: %d URL(s)", len(urls))
            raw = _tavily_request(
                "extract",
                {
                    "urls": urls,
                    "include_images": False,
                },
            )
            return _normalize_tavily_documents(
                raw, fallback_url=urls[0] if urls else ""
            )
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily extract error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"Tavily extract failed: {exc}"}
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Tavily",
            "badge": "paid",
            "tag": "Search + extract in one provider.",
            "env_vars": [
                {
                    "key": "TAVILY_API_KEY",
                    "prompt": "Tavily API key",
                    "url": "https://app.tavily.com/home",
                },
            ],
        }
