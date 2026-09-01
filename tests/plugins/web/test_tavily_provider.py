"""Hermetic smoke tests for the Tavily web search/extract provider.

Covers:
- TavilyWebSearchProvider satisfies the WebSearchProvider ABC (all abstract
  methods concrete, correct name/display_name/capability flags).
- is_available() reflects TAVILY_API_KEY presence; is_keyless_available()
  reflects the keyless tier (enabled + not pinned paid).
- search() happy path against a MOCKED httpx response (no live network, no
  real API key) — keyed and keyless header shapes.
- extract() happy path + per-URL failure shape against a mocked response.
- The keyless ring actually includes tavily (search_with_failover /
  extract_with_failover can route to Tavily's own keyless endpoint).

Per the dev skill: these tests use *real* imports from the plugin module —
no mocking of the provider class itself — so the test catches drift in the
ABC interface, the registry, and the plugin glue layer simultaneously.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from agent.web_search_provider import WebSearchProvider
from plugins.web.tavily.provider import (
    TavilyWebSearchProvider,
    _tavily_headers,
    _tavily_request,
    _normalize_tavily_search_results,
    _normalize_tavily_documents,
)


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


class TestTavilyProviderContract:
    def test_implements_web_search_provider(self):
        assert issubclass(TavilyWebSearchProvider, WebSearchProvider)

    def test_all_abstract_methods_concrete(self):
        """The provider must implement every abstract member of the ABC."""
        abstract = set(WebSearchProvider.__abstractmethods__)
        concrete = {
            m for m in abstract if not getattr(
                getattr(TavilyWebSearchProvider, m), "__isabstractmethod__", False
            )
        }
        assert concrete == abstract, f"missing concrete impls: {abstract - concrete}"

    def test_name_and_display_name(self):
        p = TavilyWebSearchProvider()
        assert p.name == "tavily"
        assert p.display_name == "Tavily"

    def test_supports_both_search_and_extract(self):
        p = TavilyWebSearchProvider()
        assert p.supports_search() is True
        assert p.supports_extract() is True

    def test_get_setup_schema_has_tavily_key(self):
        schema = TavilyWebSearchProvider().get_setup_schema()
        assert schema["name"] == "Tavily"
        keys = [e["key"] for e in schema["env_vars"]]
        assert "TAVILY_API_KEY" in keys


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestTavilyAvailability:
    def test_is_available_false_without_key(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        assert TavilyWebSearchProvider().is_available() is False

    def test_is_available_true_with_key(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
        assert TavilyWebSearchProvider().is_available() is True

    def test_is_keyless_available_when_enabled(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch("plugins.web.keyless_mcp.keyless_enabled", return_value=True), \
             patch("plugins.web.keyless_mcp.provider_tier", return_value="auto"):
            assert TavilyWebSearchProvider().is_keyless_available() is True

    def test_is_keyless_available_false_when_paid(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch("plugins.web.keyless_mcp.keyless_enabled", return_value=True), \
             patch("plugins.web.keyless_mcp.provider_tier", return_value="paid"):
            assert TavilyWebSearchProvider().is_keyless_available() is False


# ---------------------------------------------------------------------------
# Header construction
# ---------------------------------------------------------------------------


class TestTavilyHeaders:
    def test_keyed_uses_bearer(self):
        h = _tavily_headers("sk-test")
        assert h["Authorization"] == "Bearer sk-test"
        assert "X-Tavily-Access-Mode" not in h
        assert h["X-Client-Name"] == "hermes-agent"

    def test_keyless_sets_access_mode(self):
        h = _tavily_headers("")
        assert "Authorization" not in h
        assert h["X-Tavily-Access-Mode"] == "keyless"
        assert h["X-Client-Name"] == "hermes-agent"


# ---------------------------------------------------------------------------
# search() against a mocked HTTP response
# ---------------------------------------------------------------------------


class TestTavilySearch:
    def test_search_happy_path_keyed(self, monkeypatch):
        """Keyed search maps Tavily /search results to the legacy shape."""
        monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {
            "results": [
                {"title": "A", "url": "https://a.example", "content": "desc a"},
                {"title": "B", "url": "https://b.example", "content": "desc b"},
            ]
        }
        with patch("plugins.web.tavily.provider.httpx.post", return_value=fake) as post:
            out = TavilyWebSearchProvider().search("hello", limit=2)
        assert out["success"] is True
        assert len(out["data"]["web"]) == 2
        assert out["data"]["web"][0] == {
            "title": "A", "url": "https://a.example",
            "description": "desc a", "position": 1,
        }
        # Keyed request must carry the Bearer header.
        sent_headers = post.call_args.kwargs["headers"]
        assert sent_headers["Authorization"] == "Bearer sk-test"

    def test_search_happy_path_keyless(self, monkeypatch):
        """Keyless request (api_key='') sends X-Tavily-Access-Mode: keyless."""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {"results": []}
        with patch("plugins.web.tavily.provider.httpx.post", return_value=fake) as post:
            out = _tavily_request("search", {"query": "hello"}, api_key="")
        assert out == {"results": []}
        sent_headers = post.call_args.kwargs["headers"]
        assert sent_headers["X-Tavily-Access-Mode"] == "keyless"
        assert "Authorization" not in sent_headers

    def test_search_http_error_returns_failure(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
        fake = MagicMock()
        fake.status_code = 429
        fake.text = "rate limited"
        with patch("plugins.web.tavily.provider.httpx.post", return_value=fake):
            out = TavilyWebSearchProvider().search("hello")
        assert out["success"] is False
        assert "rate limited" in out["error"]


# ---------------------------------------------------------------------------
# extract() against a mocked HTTP response
# ---------------------------------------------------------------------------


class TestTavilyExtract:
    def test_extract_happy_path(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "sk-test")
        fake = MagicMock()
        fake.status_code = 200
        fake.json.return_value = {
            "results": [
                {"url": "https://a.example", "title": "A", "raw_content": "body a"},
            ]
        }
        with patch("plugins.web.tavily.provider.httpx.post", return_value=fake):
            docs = TavilyWebSearchProvider().extract(["https://a.example"])
        assert len(docs) == 1
        assert docs[0]["url"] == "https://a.example"
        assert docs[0]["content"] == "body a"
        assert docs[0]["raw_content"] == "body a"

    def test_extract_failed_results_become_error_entries(self):
        """Tavily failed_results map to per-URL error entries, not raises."""
        docs = _normalize_tavily_documents(
            {"results": [], "failed_results": [{"url": "https://x", "error": "blocked"}]},
            fallback_url="",
        )
        assert len(docs) == 1
        assert docs[0]["url"] == "https://x"
        assert docs[0]["error"] == "blocked"


# ---------------------------------------------------------------------------
# Keyless ring wiring
# ---------------------------------------------------------------------------


class TestTavilyKeylessRing:
    def test_tavily_is_a_ring_member(self):
        """The keyless ring must include tavily so a keyless Tavily request
        routes to Tavily's own keyless endpoint (not silently to a sibling)."""
        from plugins.web.keyless_mcp import (
            _KEYLESS_RING,
            _KEYLESS_SEARCHERS,
            _KEYLESS_EXTRACTORS,
        )
        assert "tavily" in _KEYLESS_RING
        assert "tavily" in _KEYLESS_SEARCHERS
        assert "tavily" in _KEYLESS_EXTRACTORS

    def test_keyless_search_routes_through_tavily(self, monkeypatch):
        """A pinned keyless Tavily search calls the Tavily keyless searcher."""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        from plugins.web import keyless_mcp
        monkeypatch.setattr(keyless_mcp, "_vendor_pinned", lambda n: n == "tavily")
        with patch.object(
            keyless_mcp, "tavily_search_keyless",
            return_value={"success": True, "data": {"web": []}},
        ) as keyless:
            out = TavilyWebSearchProvider().search("q")
        keyless.assert_called_once()
        assert out["success"] is True
