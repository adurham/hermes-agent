"""Trafilatura web extract — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Extract-only
(``supports_search()`` is False — pair with brave-free / ddgs / searxng for
``web_search``).

No API key, no account, no self-hosted service required — this is the free
extract backend for setups (exo / ollama-cloud / any non-Anthropic provider)
that have a search backend (brave-free, ddgs) but no extract-capable one,
since ddgs/brave-free/searxng are all search-only and the remaining extract
backends (firecrawl, tavily, exa, parallel) all need a paid/API-key account.

Fetches each URL directly via ``httpx`` and runs the open-source
``trafilatura`` library (boilerplate/nav/ad stripping, main-content
extraction) to produce clean markdown — the same content shape the paid
extract backends return.

Security note on redirects: ``httpx`` is called with
``follow_redirects=False`` and this provider walks each redirect hop
manually (capped at ``_MAX_REDIRECTS``), re-checking ``is_safe_url()`` and
``check_website_access()`` on every hop BEFORE requesting it. Letting
httpx auto-follow redirects would fetch attacker-controlled hops (e.g. a
302 to a private/internal address) before any SSRF check ever ran on them.

Config keys this provider responds to::

    web:
      extract_backend: "trafilatura"   # explicit per-capability
      backend: "trafilatura"           # shared fallback

No env vars required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

# Realistic browser UA — httpx's default UA (`python-httpx/x.y`) gets
# blocked (403/Cloudflare) by a large fraction of real sites.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Per-hop connect/read timeouts. Overall wall-clock budget for a single URL
# (including redirect hops) is bounded separately by _MAX_REDIRECTS * this.
_TIMEOUT = 20.0
_MAX_REDIRECTS = 5
# Abort a response body past this size — avoids a slow-loris / huge-page
# DoS on the agent process and skips parsing content nobody will read.
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB


async def _fetch_with_guarded_redirects(url: str) -> Dict[str, Any]:
    """Fetch *url*, manually walking redirects with a per-hop SSRF/policy re-check.

    Returns one of:
      * ``{"ok": True, "final_url": str, "html": str, "content_type": str}``
      * ``{"ok": False, "error": str, "blocked_by_policy": dict (optional)}``
    """
    import httpx

    current = url
    timeout = httpx.Timeout(connect=5.0, read=_TIMEOUT, write=10.0, pool=5.0)

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, headers={"User-Agent": _USER_AGENT}
    ) as client:
        for hop in range(_MAX_REDIRECTS + 1):
            if not await asyncio.get_event_loop().run_in_executor(
                None, is_safe_url, current
            ):
                return {
                    "ok": False,
                    "error": "Blocked: URL targets a private or internal network address",
                }
            blocked = check_website_access(current)
            if blocked:
                return {
                    "ok": False,
                    "error": blocked["message"],
                    "blocked_by_policy": {
                        "host": blocked["host"],
                        "rule": blocked["rule"],
                        "source": blocked["source"],
                    },
                }

            try:
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            return {
                                "ok": False,
                                "error": f"HTTP {resp.status_code} redirect with no Location header",
                            }
                        next_url = urljoin(current, location)
                        parsed = urlparse(next_url)
                        if parsed.scheme not in ("http", "https"):
                            return {
                                "ok": False,
                                "error": f"Blocked: redirect to disallowed scheme '{parsed.scheme}'",
                            }
                        current = next_url
                        continue

                    if resp.status_code >= 400:
                        return {
                            "ok": False,
                            "error": f"HTTP {resp.status_code} fetching {current}",
                        }

                    content_type = resp.headers.get("content-type", "")
                    if "html" not in content_type and "xml" not in content_type and content_type:
                        return {
                            "ok": False,
                            "error": (
                                f"Unsupported content-type '{content_type}' — "
                                "trafilatura extracts HTML pages only"
                            ),
                        }

                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_BODY_BYTES:
                            return {
                                "ok": False,
                                "error": f"Page exceeds {_MAX_BODY_BYTES // (1024 * 1024)}MB size cap",
                            }
                    html = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
                    return {
                        "ok": True,
                        "final_url": str(resp.url),
                        "html": html,
                        "content_type": content_type,
                    }
            except httpx.TimeoutException:
                return {"ok": False, "error": f"Timed out after {_TIMEOUT}s fetching {current}"}
            except httpx.RequestError as exc:
                return {"ok": False, "error": f"Could not reach {current}: {exc}"}

        return {"ok": False, "error": f"Too many redirects (> {_MAX_REDIRECTS})"}


def _parse_with_trafilatura(html: str, final_url: str, fmt: str | None) -> Dict[str, Any]:
    """Run trafilatura extraction + metadata in a worker thread (CPU-bound lxml work)."""
    import trafilatura

    output_format = "html" if fmt == "html" else "markdown"
    content = trafilatura.extract(
        html,
        output_format=output_format,
        url=final_url,
        include_links=True,
        include_images=True,
        favor_recall=True,
    )
    title = ""
    metadata: Dict[str, Any] = {"sourceURL": final_url}
    try:
        meta = trafilatura.extract_metadata(html, default_url=final_url)
        if meta is not None:
            title = meta.title or ""
            metadata.update(
                {
                    k: v
                    for k, v in {
                        "title": meta.title,
                        "author": meta.author,
                        "hostname": meta.hostname,
                        "description": meta.description,
                        "sitename": meta.sitename,
                        "date": meta.date,
                    }.items()
                    if v
                }
            )
    except Exception as exc:  # noqa: BLE001 — metadata is best-effort
        logger.debug("trafilatura metadata extraction failed for %s: %s", final_url, exc)

    return {"content": content or "", "title": title, "metadata": metadata}


class TrafilaturaWebExtractProvider(WebSearchProvider):
    """Extract-only provider: direct httpx fetch + trafilatura content extraction.

    No API key or account needed. Pair with brave-free / ddgs / searxng for
    ``web_search`` — this provider does not implement search.
    """

    @property
    def name(self) -> str:
        return "trafilatura"

    @property
    def display_name(self) -> str:
        return "Trafilatura (direct fetch)"

    def is_available(self) -> bool:
        """Return True when the ``trafilatura`` package is importable.

        No network I/O, no credentials — always available once the
        (optional) package is installed.
        """
        try:
            import trafilatura  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch + extract clean content from one or more URLs.

        Per-URL failures (timeout, SSRF block, policy block, non-HTML,
        parse failure) become items with an ``error`` field rather than
        raising or failing the whole batch.
        """
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": "trafilatura package is not installed — run `pip install trafilatura`",
                }
                for u in urls
            ]

        from tools.interrupt import is_interrupted

        fmt = kwargs.get("format")
        results: List[Dict[str, Any]] = []

        for url in urls:
            if is_interrupted():
                results.append({"url": url, "title": "", "content": "", "raw_content": "", "error": "Interrupted"})
                continue

            fetch_result = await _fetch_with_guarded_redirects(url)
            if not fetch_result["ok"]:
                item = {
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": fetch_result["error"],
                }
                if "blocked_by_policy" in fetch_result:
                    item["blocked_by_policy"] = fetch_result["blocked_by_policy"]
                results.append(item)
                continue

            final_url = fetch_result["final_url"]
            try:
                parsed = await asyncio.to_thread(
                    _parse_with_trafilatura, fetch_result["html"], final_url, fmt
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("trafilatura parse failed for %s: %s", final_url, exc)
                results.append(
                    {
                        "url": final_url,
                        "title": "",
                        "content": "",
                        "raw_content": "",
                        "error": f"Content parse failed: {exc}",
                    }
                )
                continue

            content = parsed["content"]
            if not content:
                results.append(
                    {
                        "url": final_url,
                        "title": parsed["title"],
                        "content": "",
                        "raw_content": "",
                        "error": (
                            "No extractable content found — the page may be "
                            "JavaScript-rendered (try browser_navigate instead) "
                            "or have no main content trafilatura could isolate."
                        ),
                        "metadata": parsed["metadata"],
                    }
                )
                continue

            results.append(
                {
                    "url": final_url,
                    "title": parsed["title"],
                    "content": content,
                    "raw_content": content,
                    "metadata": parsed["metadata"],
                }
            )

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Trafilatura (direct fetch)",
            "badge": "free · no key · no account",
            "tag": "Direct httpx fetch + open-source content extraction — no API key, no self-hosting.",
            "env_vars": [],
            # Trigger `_run_post_setup("trafilatura")` after the user picks
            # this row so the trafilatura Python package gets pip-installed
            # on first selection (mirrors the ddgs post_setup pattern).
            "post_setup": "trafilatura",
        }
