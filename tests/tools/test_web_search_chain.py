"""Tests for the web.search_chain failover dispatcher.

Covers:
- _get_search_chain() config parsing (list, tuple, missing, malformed)
- _provider_failed() failure classification
- _run_search_chain() walk behavior:
  * success on first provider
  * 429 failover to second provider success
  * all-fail returns last error
  * unregistered provider skipped with warning
  * unavailable provider skipped
  * search-incapable provider skipped
  * provider raising .search() exception falls through
- web_search_tool integration: chain path vs single-provider path
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.tools.conftest import register_all_web_providers


# ---------------------------------------------------------------------------#
# Config parsing: _get_search_chain
# ---------------------------------------------------------------------------#


class TestGetSearchChain:
    def test_returns_empty_when_unset(self, monkeypatch):
        monkeypatch.setattr(
            "tools.web_tools._load_web_config", lambda: {}
        )
        from tools.web_tools import _get_search_chain
        assert _get_search_chain() == ()

    def test_returns_tuple_from_list(self, monkeypatch):
        monkeypatch.setattr(
            "tools.web_tools._load_web_config",
            lambda: {"search_chain": ["brave-free", "ddgs"]},
        )
        from tools.web_tools import _get_search_chain
        assert _get_search_chain() == ("brave-free", "ddgs")

    def test_normalizes_case_and_strips(self, monkeypatch):
        monkeypatch.setattr(
            "tools.web_tools._load_web_config",
            lambda: {"search_chain": [" Brave-Free ", "DDGS"]},
        )
        from tools.web_tools import _get_search_chain
        assert _get_search_chain() == ("brave-free", "ddgs")

    def test_filters_empty_entries(self, monkeypatch):
        monkeypatch.setattr(
            "tools.web_tools._load_web_config",
            lambda: {"search_chain": ["brave-free", "", "  ", "ddgs"]},
        )
        from tools.web_tools import _get_search_chain
        assert _get_search_chain() == ("brave-free", "ddgs")

    def test_non_list_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "tools.web_tools._load_web_config",
            lambda: {"search_chain": "brave-free"},
        )
        from tools.web_tools import _get_search_chain
        assert _get_search_chain() == ()


# ---------------------------------------------------------------------------#
# Failure classification: _provider_failed
# ---------------------------------------------------------------------------#


class TestProviderFailed:
    def test_success_true_is_not_failed(self):
        from tools.web_tools import _provider_failed
        assert _provider_failed({"success": True, "data": {"web": []}}) is False

    def test_success_false_is_failed(self):
        from tools.web_tools import _provider_failed
        assert _provider_failed({"success": False, "error": "HTTP 429"}) is True

    def test_missing_success_is_failed(self):
        from tools.web_tools import _provider_failed
        assert _provider_failed({"error": "rate limited"}) is True

    def test_non_dict_is_failed(self):
        from tools.web_tools import _provider_failed
        assert _provider_failed("not a dict") is True

    def test_none_is_failed(self):
        from tools.web_tools import _provider_failed
        assert _provider_failed(None) is True


# ---------------------------------------------------------------------------#
# Chain walk: _run_search_chain
# ---------------------------------------------------------------------------#


def _make_provider(name: str, *, supports_search=True, available=True, search_response=None, search_raises=None):
    """Build a mock provider with the WebSearchProvider-shaped surface."""
    p = MagicMock()
    p.name = name
    p.supports_search.return_value = supports_search
    p.is_available.return_value = available
    if search_raises is not None:
        p.search.side_effect = search_raises
    else:
        p.search.return_value = search_response or {"success": True, "data": {"web": []}}
    return p


class TestRunSearchChain:
    def test_success_on_first_provider(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider("brave-free", search_response={"success": True, "data": {"web": [{"title": "r1"}]}})
        ddgs = _make_provider("ddgs")
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is True
        brave.search.assert_called_once_with("query", 5)
        ddgs.search.assert_not_called()

    def test_failover_on_429_to_second(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider(
            "brave-free",
            search_response={"success": False, "error": "Brave Search returned HTTP 429"},
        )
        ddgs = _make_provider(
            "ddgs",
            search_response={"success": True, "data": {"web": [{"title": "r1"}]}},
        )
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is True
        brave.search.assert_called_once()
        ddgs.search.assert_called_once()

    def test_all_fail_returns_last_error(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider(
            "brave-free",
            search_response={"success": False, "error": "Brave Search returned HTTP 429"},
        )
        ddgs = _make_provider(
            "ddgs",
            search_response={"success": False, "error": "DDGS returned HTTP 429"},
        )
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is False
        assert "DDGS" in result["error"]

    def test_unregistered_provider_skipped(self, monkeypatch):
        from tools import web_tools

        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("nonexistent", "ddgs"), "query", 5)
        assert result["success"] is True
        ddgs.search.assert_called_once()

    def test_unavailable_provider_skipped(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider("brave-free", available=False)
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is True
        brave.search.assert_not_called()
        ddgs.search.assert_called_once()

    def test_search_incapable_provider_skipped(self, monkeypatch):
        from tools import web_tools

        extract_only = _make_provider("firecrawl", supports_search=False)
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"firecrawl": extract_only, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("firecrawl", "ddgs"), "query", 5)
        assert result["success"] is True
        ddgs.search.assert_called_once()

    def test_provider_raising_falls_through(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider("brave-free", search_raises=RuntimeError("network down"))
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is True
        ddgs.search.assert_called_once()

    def test_empty_chain_returns_synthesized_error(self, monkeypatch):
        from tools import web_tools
        result = web_tools._run_search_chain((), "query", 5)
        assert result["success"] is False
        assert "All providers" in result["error"]

    def test_is_available_raising_falls_through(self, monkeypatch):
        from tools import web_tools

        brave = _make_provider("brave-free")
        brave.is_available.side_effect = RuntimeError("boom")
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        result = web_tools._run_search_chain(("brave-free", "ddgs"), "query", 5)
        assert result["success"] is True
        ddgs.search.assert_called_once()


# ---------------------------------------------------------------------------#
# Integration: web_search_tool picks chain vs single path
# ---------------------------------------------------------------------------#


class TestWebSearchToolDispatch:
    def test_chain_path_used_when_configured(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_get_search_chain", lambda: ("brave-free", "ddgs"))
        monkeypatch.setattr(
            web_tools,
            "_run_search_chain",
            lambda chain, q, l: {"success": True, "data": {"web": [{"title": "chain-result"}]}},
        )
        single_called = []
        monkeypatch.setattr(
            web_tools,
            "_run_search_single",
            lambda q, l: single_called.append(1) or {"success": True, "data": {"web": []}},
        )

        import json
        result = json.loads(web_tools.web_search_tool("query", 5))
        assert result["data"]["web"][0]["title"] == "chain-result"
        assert single_called == []

    def test_single_path_used_when_no_chain(self, monkeypatch):
        from tools import web_tools

        monkeypatch.setattr(web_tools, "_get_search_chain", lambda: ())
        chain_called = []
        monkeypatch.setattr(
            web_tools,
            "_run_search_chain",
            lambda chain, q, l: chain_called.append(1) or {"success": True, "data": {"web": []}},
        )
        monkeypatch.setattr(
            web_tools,
            "_run_search_single",
            lambda q, l: {"success": True, "data": {"web": [{"title": "single-result"}]}},
        )

        import json
        result = json.loads(web_tools.web_search_tool("query", 5))
        assert result["data"]["web"][0]["title"] == "single-result"
        assert chain_called == []