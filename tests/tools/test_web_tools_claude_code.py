"""Tests for the Claude Code CLI web provider.

Covers:
- ``is_configured()`` — claude on PATH + ``claude auth status`` exit code
- ``ClaudeCodeSearchProvider.search()`` — happy path, timeout, malformed JSON
- ``ClaudeCodeExtractProvider.extract()`` — happy path
- Integration: ``_is_backend_available("claude-code")``
- Integration: ``_get_search_backend()`` returns ``claude-code`` when configured
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_auth_cache():
    """Clear the module-level auth cache between tests."""
    from tools.web_providers.claude_code import _reset_auth_cache as reset
    reset()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess-like MagicMock for subprocess.run."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def setup_method(self):
        _reset_auth_cache()

    def teardown_method(self):
        _reset_auth_cache()

    def test_returns_true_when_claude_on_path_and_auth_ok(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout='{"loggedIn": true}')):
            from tools.web_providers.claude_code import is_configured
            assert is_configured() is True

    def test_returns_false_when_claude_not_on_path(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value=None):
            from tools.web_providers.claude_code import is_configured
            assert is_configured() is False

    def test_returns_false_when_auth_status_exits_nonzero(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=1, stderr="not logged in")):
            from tools.web_providers.claude_code import is_configured
            assert is_configured() is False

    def test_returns_false_when_auth_status_times_out(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10)):
            from tools.web_providers.claude_code import is_configured
            assert is_configured() is False

    def test_result_is_cached(self):
        """Second call should NOT re-shell to ``claude auth status``."""
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0)) as mock_run:
            from tools.web_providers.claude_code import is_configured
            assert is_configured() is True
            assert is_configured() is True
            assert mock_run.call_count == 1

    def test_reset_auth_cache_invalidates(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0)) as mock_run:
            from tools.web_providers.claude_code import is_configured, _reset_auth_cache
            is_configured()
            _reset_auth_cache()
            is_configured()
            assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# ClaudeCodeSearchProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeSearchProvider:
    def setup_method(self):
        _reset_auth_cache()

    def teardown_method(self):
        _reset_auth_cache()

    def test_provider_name(self):
        from tools.web_providers.claude_code import ClaudeCodeSearchProvider
        assert ClaudeCodeSearchProvider().provider_name() == "claude-code"

    def test_implements_web_search_provider(self):
        from tools.web_providers.base import WebSearchProvider
        from tools.web_providers.claude_code import ClaudeCodeSearchProvider
        assert issubclass(ClaudeCodeSearchProvider, WebSearchProvider)

    def test_search_happy_path_structured_output(self):
        """Preferred path: results come through ``parsed["structured_output"]``."""
        envelope = {
            "structured_output": {
                "results": [
                    {"title": "Result A", "url": "https://a.example.com", "description": "Desc A"},
                    {"title": "Result B", "url": "https://b.example.com", "description": "Desc B"},
                ]
            }
        }
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout=json.dumps(envelope))):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("test query", limit=5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 2
        assert web[0] == {
            "title": "Result A",
            "url": "https://a.example.com",
            "description": "Desc A",
            "position": 1,
        }
        assert web[1]["position"] == 2

    def test_search_happy_path_fallback_to_result_field(self):
        """When ``structured_output`` is absent, parse JSON from ``result``."""
        envelope = {
            "result": json.dumps({
                "results": [
                    {"title": "T", "url": "https://x.example.com", "description": "D"},
                ]
            })
        }
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout=json.dumps(envelope))):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=5)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
        assert result["data"]["web"][0]["url"] == "https://x.example.com"

    def test_search_truncates_to_limit(self):
        envelope = {
            "structured_output": {
                "results": [
                    {"title": f"R{i}", "url": f"https://r{i}.example.com", "description": f"D{i}"}
                    for i in range(10)
                ]
            }
        }
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout=json.dumps(envelope))):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=3)

        assert result["success"] is True
        assert len(result["data"]["web"]) == 3
        assert [r["position"] for r in result["data"]["web"]] == [1, 2, 3]

    def test_search_timeout_returns_error_dict(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=5)

        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_search_nonzero_exit_returns_error_dict(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=2, stderr="boom")):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=5)

        assert result["success"] is False
        assert "exited 2" in result["error"]
        assert "boom" in result["error"]

    def test_search_malformed_json_returns_error_dict(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout="not json at all")):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=5)

        assert result["success"] is False
        assert "parse" in result["error"].lower()

    def test_search_returns_error_when_claude_missing_from_path(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value=None):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            result = ClaudeCodeSearchProvider().search("q", limit=5)

        assert result["success"] is False
        assert "claude" in result["error"].lower()

    def test_search_builds_correct_args(self):
        """Spot-check the subprocess args contain the documented flags."""
        envelope = {"structured_output": {"results": []}}
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _completed(returncode=0, stdout=json.dumps(envelope))

        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run", side_effect=fake_run):
            from tools.web_providers.claude_code import ClaudeCodeSearchProvider
            ClaudeCodeSearchProvider().search("hello world", limit=5)

        args = captured["args"]
        assert args[0] == "/usr/local/bin/claude"
        assert "-p" in args
        assert "hello world" in args
        assert "--bare" not in args  # removed: --bare requires API key, breaks subscription auth
        assert "--allowedTools" in args
        # WebSearch only (not WebFetch)
        web_search_idx = args.index("--allowedTools") + 1
        assert args[web_search_idx] == "WebSearch"
        assert "--output-format" in args
        assert "json" in args
        assert "--json-schema" in args
        assert "--system-prompt" in args
        assert "--max-turns" in args
        assert captured["kwargs"].get("timeout") == 60


# ---------------------------------------------------------------------------
# ClaudeCodeExtractProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeExtractProvider:
    def setup_method(self):
        _reset_auth_cache()

    def teardown_method(self):
        _reset_auth_cache()

    def test_provider_name(self):
        from tools.web_providers.claude_code import ClaudeCodeExtractProvider
        assert ClaudeCodeExtractProvider().provider_name() == "claude-code"

    def test_implements_web_extract_provider(self):
        from tools.web_providers.base import WebExtractProvider
        from tools.web_providers.claude_code import ClaudeCodeExtractProvider
        assert issubclass(ClaudeCodeExtractProvider, WebExtractProvider)

    def test_extract_happy_path(self):
        envelope = {
            "structured_output": {
                "pages": [
                    {"url": "https://a.example.com", "title": "Page A", "content": "Body A"},
                    {"url": "https://b.example.com", "title": "Page B", "content": "Body B"},
                ]
            }
        }
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout=json.dumps(envelope))):
            from tools.web_providers.claude_code import ClaudeCodeExtractProvider
            result = ClaudeCodeExtractProvider().extract([
                "https://a.example.com", "https://b.example.com",
            ])

        assert result["success"] is True
        docs = result["data"]
        assert len(docs) == 2
        first = docs[0]
        assert first["url"] == "https://a.example.com"
        assert first["title"] == "Page A"
        assert first["content"] == "Body A"
        assert first["raw_content"] == "Body A"
        assert first["metadata"] == {"source": "claude-code"}

    def test_extract_empty_url_list_returns_empty_data(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"):
            from tools.web_providers.claude_code import ClaudeCodeExtractProvider
            result = ClaudeCodeExtractProvider().extract([])
        assert result == {"success": True, "data": []}

    def test_extract_timeout_returns_error_dict(self):
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=90)):
            from tools.web_providers.claude_code import ClaudeCodeExtractProvider
            result = ClaudeCodeExtractProvider().extract(["https://x.example.com"])
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_extract_max_turns_scales_with_url_count(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _completed(returncode=0, stdout=json.dumps({"structured_output": {"pages": []}}))

        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run", side_effect=fake_run):
            from tools.web_providers.claude_code import ClaudeCodeExtractProvider
            ClaudeCodeExtractProvider().extract([
                "https://a.example.com", "https://b.example.com", "https://c.example.com",
            ])

        args = captured["args"]
        # 2 * len(urls) + 2 = 8 for 3 URLs
        idx = args.index("--max-turns") + 1
        assert args[idx] == "8"
        # WebFetch (not WebSearch)
        tools_idx = args.index("--allowedTools") + 1
        assert args[tools_idx] == "WebFetch"


# ---------------------------------------------------------------------------
# Integration: web_tools registry
# ---------------------------------------------------------------------------


class TestWebToolsIntegration:
    def setup_method(self):
        _reset_auth_cache()

    def teardown_method(self):
        _reset_auth_cache()

    def test_is_backend_available_wires_up_claude_code(self):
        from tools.web_tools import _is_backend_available
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0)):
            assert _is_backend_available("claude-code") is True

    def test_is_backend_available_false_when_claude_missing(self):
        from tools.web_tools import _is_backend_available
        with patch("tools.web_providers.claude_code.shutil.which", return_value=None):
            assert _is_backend_available("claude-code") is False

    def test_get_search_backend_returns_claude_code_when_configured(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "claude-code"})
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0)):
            assert web_tools._get_search_backend() == "claude-code"

    def test_get_extract_backend_returns_claude_code_when_configured(self, monkeypatch):
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "claude-code"})
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0)):
            assert web_tools._get_extract_backend() == "claude-code"

    def test_web_search_tool_dispatches_to_claude_code(self, monkeypatch):
        """End-to-end: web_search_tool routes to ClaudeCodeSearchProvider."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "claude-code"})
        monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False, raising=False)

        envelope = {
            "structured_output": {
                "results": [
                    {"title": "T", "url": "https://x.example.com", "description": "D"},
                ]
            }
        }
        with patch("tools.web_providers.claude_code.shutil.which", return_value="/usr/local/bin/claude"), \
             patch("tools.web_providers.claude_code.subprocess.run",
                   return_value=_completed(returncode=0, stdout=json.dumps(envelope))):
            result = json.loads(web_tools.web_search_tool("hello", limit=5))

        assert result["success"] is True
        assert result["data"]["web"][0]["url"] == "https://x.example.com"
