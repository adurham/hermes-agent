"""Hermetic smoke tests for the Tavily web search/extract provider.

Covers:
- TavilyWebSearchProvider satisfies the WebSearchProvider ABC (all abstract
  methods concrete, correct name/display_name/capability flags).
- Keyed contract: is_available() reflects TAVILY_API_KEY presence, and
  is_keyless_available() is always False (Tavily never enters the default-on
  keyless ring — it is a keyed, opt-in provider only).
- A missing TAVILY_API_KEY yields the explicit "environment variable not
  set" error rather than an anonymous request.
- search() happy path against a MOCKED httpx response (no live network, no
  real API key) — keyed header shape.
- extract() happy path + per-URL failure shape against a mocked response.

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

    def test_is_keyless_available_is_always_false(self):
        """Tavily must NEVER be keyless-available.

        It relies on the ABC default (``return False``): Tavily is a keyed,
        opt-in provider and must not enter the default-on keyless ring —
        keyless availability would change baseline data egress for zero-config
        users to a vendor they never picked.
        """
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

    def test_search_missing_key_returns_explicit_error(self, monkeypatch):
        """Without TAVILY_API_KEY, search fails loudly rather than anonymous."""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch("plugins.web.tavily.provider.httpx.post") as post:
            out = TavilyWebSearchProvider().search("hello")
        post.assert_not_called()
        assert out["success"] is False
        assert "TAVILY_API_KEY environment variable not set" in out["error"]

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

    def test_extract_missing_key_returns_explicit_error(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        with patch("plugins.web.tavily.provider.httpx.post") as post:
            docs = TavilyWebSearchProvider().extract(["https://a.example"])
        post.assert_not_called()
        assert len(docs) == 1
        assert docs[0]["url"] == "https://a.example"
        assert "TAVILY_API_KEY environment variable not set" in docs[0]["error"]

    def test_extract_failed_results_become_error_entries(self):
        """Tavily failed_results map to per-URL error entries, not raises."""
        docs = _normalize_tavily_documents(
            {"results": [], "failed_results": [{"url": "https://x", "error": "blocked"}]},
            fallback_url="",
        )
        assert len(docs) == 1
        assert docs[0]["url"] == "https://x"
        assert docs[0]["error"] == "blocked"
