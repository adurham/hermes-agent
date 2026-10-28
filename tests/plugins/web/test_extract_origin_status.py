"""web_extract must report a target page's HTTP >= 400 as an error, on every extract provider.

Scraping vendors answer 200 and tuck the target's own status into the payload; reading only the
content made a 403/404 look like a successful (often blank) page.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.web import keyless_mcp
from plugins.web.exa import provider as exa_provider
from plugins.web.firecrawl import provider as firecrawl_provider
from plugins.web.parallel import provider as parallel_provider

STUB_HTML = "<!DOCTYPE html><html><body></body></html>"


def _scrape(payload: dict) -> dict:
    client = SimpleNamespace(scrape=lambda **_kw: payload)
    with patch.object(firecrawl_provider, "_get_firecrawl_client", return_value=client), \
            patch.object(firecrawl_provider, "check_website_access", return_value=None), \
            patch.object(firecrawl_provider, "is_safe_url", return_value=True):
        return asyncio.run(firecrawl_provider._scrape_one("https://t.test/x", ["markdown", "html"], None))


@pytest.mark.parametrize("metadata, expected", [
    # Firecrawl SDK v4 model_dump (direct key and Nous Tool Gateway): snake_case, stub body.
    ({"status_code": 403, "error": "Forbidden", "source_url": "https://t.test/x"}, "HTTP 403 from origin: Forbidden"),
    # REST payload (keyless / self-hosted): camelCase.
    ({"statusCode": 404, "sourceURL": "https://t.test/x"}, "HTTP 404 from origin"),
])
def test_firecrawl_scrape_flags_origin_error_status(metadata, expected):
    entry = _scrape({"markdown": "", "html": STUB_HTML, "metadata": metadata})
    assert entry["error"] == expected
    assert entry["content"] == STUB_HTML  # the error page body is kept, not discarded


def test_firecrawl_scrape_success_has_no_error():
    entry = _scrape({"markdown": "# Hi", "metadata": {"status_code": 200, "title": "Hi"}})
    assert entry["content"] == "# Hi"
    assert "error" not in entry


def test_keyless_firecrawl_flags_origin_error_status():
    class _Client:
        def scrape(self, **_kw):
            return {"success": True, "data": {"markdown": "", "metadata": {"statusCode": 403, "error": "Forbidden"}}}

    with patch.object(firecrawl_provider, "_KeylessFirecrawlClient", _Client):
        [entry] = keyless_mcp.firecrawl_extract_keyless(["https://t.test/x"])
    assert entry["error"] == "HTTP 403 from origin: Forbidden"


def test_keyless_parallel_names_origin_status():
    payload = json.dumps({"results": [], "errors": [
        {"url": "https://t.test/x", "error_type": "http_error", "http_status_code": 403, "content": None},
    ]})
    with patch.object(keyless_mcp, "mcp_call", return_value=payload):
        [entry] = keyless_mcp.parallel_extract_keyless(["https://t.test/x"])
    assert entry["error"] == "HTTP 403 from origin"


def test_parallel_sdk_names_origin_status():
    error = SimpleNamespace(url="https://t.test/x", error_type="http_error", http_status_code=404, content="nope")
    response = SimpleNamespace(results=[], errors=[error])

    async def _extract(**_kw):
        return response

    client = SimpleNamespace(beta=SimpleNamespace(extract=_extract))
    with patch.object(parallel_provider, "use_keyless", return_value=False), \
            patch.object(parallel_provider, "_get_async_client", return_value=client):
        [entry] = asyncio.run(parallel_provider.ParallelWebSearchProvider().extract(["https://t.test/x"]))
    assert entry["error"] == "HTTP 404 from origin"
    assert entry["content"] == "nope"


def test_exa_sdk_reports_failed_urls_from_statuses():
    ok = SimpleNamespace(url="https://t.test/ok", title="OK", text="body")
    statuses = [
        SimpleNamespace(id="https://t.test/ok", status="success", error=None),
        SimpleNamespace(id="https://t.test/x", status="error",
                        error=SimpleNamespace(tag="CRAWL_HTTP_403", http_status_code=403)),
    ]
    client = SimpleNamespace(get_contents=lambda *_a, **_kw: SimpleNamespace(results=[ok], statuses=statuses))
    with patch.object(exa_provider, "use_keyless", return_value=False), \
            patch.object(exa_provider, "_get_exa_client", return_value=client):
        out = exa_provider.ExaWebSearchProvider().extract(["https://t.test/ok", "https://t.test/x"])
    assert [r.get("error") for r in out] == [None, "HTTP 403 from origin"]
    assert out[1]["url"] == "https://t.test/x"
