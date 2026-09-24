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
        # Bucketed fetch (5 → 10), same as _memoized_search, so near-identical limits share a memo entry.
        brave.search.assert_called_once_with("query", 10)
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
# Chain caching: _run_search_chain shares the web_result_cache TTL memo
# ---------------------------------------------------------------------------#


class TestChainMemoCache:
    """The chain path shares upstream's TTL memo with the single-provider path.

    Regression for the de-fork audit defect: ``_run_search_chain`` used to call
    ``provider.search()`` directly, bypassing the ``web.cache_enabled`` /
    ``web.cache_ttl_minutes`` contract that ``_memoized_search`` honors. Both
    paths now key the same ``search_memo`` on (provider name, normalized query,
    bucketed limit).
    """

    def test_chained_search_populates_the_memo(self, monkeypatch):
        from tools import web_tools
        from tools.web_result_cache import search_memo

        search_memo.clear()
        brave = _make_provider("brave-free", search_response={"success": True, "data": {"web": [{"title": "r1"}]}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave}.get(n))

        result = web_tools._run_search_chain(("brave-free",), "cached query", 5)
        assert result["success"] is True

        hit = search_memo.lookup("brave-free", "cached query", 5)
        assert hit is not None, "a successful chained search must populate the shared memo"
        assert hit["data"]["web"][0]["title"] == "r1"

    def test_repeated_chained_search_served_from_the_memo(self, monkeypatch):
        """Second identical walk is answered from the memo — no second paid call."""
        from tools import web_tools
        from tools.web_result_cache import search_memo

        search_memo.clear()
        brave = _make_provider("brave-free", search_response={"success": True, "data": {"web": [{"title": "r1"}]}})
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": [{"title": "r2"}]}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        first = web_tools._run_search_chain(("brave-free", "ddgs"), "repeat query", 5)
        second = web_tools._run_search_chain(("brave-free", "ddgs"), "repeat query", 5)

        assert first == second
        brave.search.assert_called_once(), "the second walk must not re-hit the vendor"
        ddgs.search.assert_not_called()

    def test_chain_memo_respects_case_and_limit_buckets(self, monkeypatch):
        """The chain keys the memo exactly like _memoized_search: case-folded query, bucketed limit."""
        from tools import web_tools
        from tools.web_result_cache import search_memo

        search_memo.clear()
        brave = _make_provider("brave-free", search_response={"success": True, "data": {"web": [{"title": "r1"}]}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave}.get(n))

        web_tools._run_search_chain(("brave-free",), "Mixed CASE query", 5)
        # Same bucket (<=10) + case-folded query: a memo hit, no second vendor call.
        web_tools._run_search_chain(("brave-free",), "  mixed case   QUERY ", 8)
        brave.search.assert_called_once()

    def test_chain_memo_disabled_by_config(self, monkeypatch):
        """web.cache_enabled: false disables the memo for the chain path too."""
        from tools import web_tools
        import tools.web_result_cache as wrc

        search_memo = wrc.search_memo
        search_memo.clear()
        monkeypatch.setattr(wrc, "_web_config", lambda: {"cache_enabled": False})
        brave = _make_provider("brave-free", search_response={"success": True, "data": {"web": []}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave}.get(n))

        web_tools._run_search_chain(("brave-free",), "q", 5)
        web_tools._run_search_chain(("brave-free",), "q", 5)
        assert brave.search.call_count == 2

    def test_chain_never_memoizes_failures(self, monkeypatch):
        """A failed member must be re-tried next walk — failures are not sticky."""
        from tools import web_tools
        from tools.web_result_cache import search_memo

        search_memo.clear()
        brave = _make_provider("brave-free", search_response={"success": False, "error": "HTTP 429"})
        ddgs = _make_provider("ddgs", search_response={"success": True, "data": {"web": [{"title": "r2"}]}})
        monkeypatch.setattr(web_tools, "_resolve_search_provider", lambda n: {"brave-free": brave, "ddgs": ddgs}.get(n))

        web_tools._run_search_chain(("brave-free", "ddgs"), "q failure", 5)
        web_tools._run_search_chain(("brave-free", "ddgs"), "q failure", 5)

        assert brave.search.call_count == 2, "the failed first member must be re-tried"
        assert ddgs.search.call_count == 1, "the successful member is memoized under its own name"
        assert search_memo.lookup("brave-free", "q failure", 5) is None


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