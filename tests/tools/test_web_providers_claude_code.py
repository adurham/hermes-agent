"""Tests for the Claude Code CLI web search/extract provider.

Covers:
- ClaudeCodeWebProvider.is_available() — claude CLI on PATH + auth status
- ClaudeCodeWebProvider.name / display_name / supports_* contract
- ClaudeCodeWebProvider.search() — happy path, missing binary, non-zero exit,
  timeout, malformed JSON, structured_output envelope handling
- ClaudeCodeWebProvider.extract() — happy path, per-URL error shape, empty
  URL list, batch failure surfaces an error per URL rather than raising
- _parse_claude_json — single-object envelope, result-string envelope,
  event-array envelope, missing-key error
- Plugin registration — plugin.yaml provides_web_providers contains
  "claude-code"; agent.web_search_registry discovers it
- _is_backend_available("claude-code") integration in tools.web_tools
"""
from __future__ import annotations

import json
import subprocess
import yaml
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Auth-cache utility — reset between tests so one test's mocked auth state
# can't leak into the next via the process-global cache.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_claude_auth_cache():
    """Clear the cached ``_is_configured()`` result before each test."""
    from plugins.web.claude_code.provider import _reset_auth_cache
    _reset_auth_cache()
    yield
    _reset_auth_cache()


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


class TestClaudeCodeProviderContract:
    def test_provider_name(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        assert ClaudeCodeWebProvider().name == "claude-code"

    def test_display_name(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        assert ClaudeCodeWebProvider().display_name == "Claude Code"

    def test_implements_web_search_provider(self):
        from agent.web_search_provider import WebSearchProvider
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        assert issubclass(ClaudeCodeWebProvider, WebSearchProvider)

    def test_supports_both_search_and_extract(self):
        """Unlike search-only providers (brave-free, ddgs, searxng), the
        Claude Code provider exposes both capabilities from one class."""
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        p = ClaudeCodeWebProvider()
        assert p.supports_search() is True
        assert p.supports_extract() is True

    def test_get_setup_schema_has_zero_env_vars(self):
        """Claude Code re-uses the user's existing ``claude auth login`` so
        the setup wizard must not prompt for any API keys."""
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        schema = ClaudeCodeWebProvider().get_setup_schema()
        assert schema["env_vars"] == []
        assert schema["name"] == "Claude Code"


# ---------------------------------------------------------------------------
# is_available() — gated on claude CLI presence + auth status
# ---------------------------------------------------------------------------


class TestClaudeCodeIsAvailable:
    def test_returns_false_when_binary_missing(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which", return_value=None):
            assert ClaudeCodeWebProvider().is_available() is False

    def test_returns_true_when_auth_status_exits_zero(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=MagicMock(returncode=0)):
            assert ClaudeCodeWebProvider().is_available() is True

    def test_returns_false_when_auth_status_exits_nonzero(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=MagicMock(returncode=1)):
            assert ClaudeCodeWebProvider().is_available() is False

    def test_returns_false_on_auth_status_timeout(self):
        """A hanging ``claude auth status`` must not block tool registration."""
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("claude", 10)):
            assert ClaudeCodeWebProvider().is_available() is False

    def test_returns_false_on_oserror(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   side_effect=OSError("permission denied")):
            assert ClaudeCodeWebProvider().is_available() is False

    def test_auth_result_is_cached(self):
        """Subsequent calls must not re-spawn ``claude auth status``."""
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=MagicMock(returncode=0)) as mock_run:
            p = ClaudeCodeWebProvider()
            p.is_available()
            p.is_available()
            p.is_available()
            # First call probes; remaining calls hit the cache.
            assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# _parse_claude_json — envelope normalization
# ---------------------------------------------------------------------------


class TestParseClaudeJson:
    def test_single_object_structured_output_envelope(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps({
            "structured_output": {"results": [{"title": "A", "url": "u", "description": "d"}]},
            "result": "ignored",
        })
        out = _parse_claude_json(stdout, "results")
        assert out == [{"title": "A", "url": "u", "description": "d"}]

    def test_result_as_json_string_envelope(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps({
            "result": json.dumps({"results": [{"title": "B", "url": "u2", "description": "d2"}]}),
        })
        out = _parse_claude_json(stdout, "results")
        assert out == [{"title": "B", "url": "u2", "description": "d2"}]

    def test_result_as_inline_dict_envelope(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps({
            "result": {"results": [{"title": "C", "url": "u3", "description": "d3"}]},
        })
        out = _parse_claude_json(stdout, "results")
        assert out == [{"title": "C", "url": "u3", "description": "d3"}]

    def test_event_array_terminal_result_envelope(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"role": "assistant", "content": []}},
            {
                "type": "result",
                "structured_output": {"pages": [{"url": "u", "title": "t", "content": "body"}]},
            },
        ])
        out = _parse_claude_json(stdout, "pages")
        assert out == [{"url": "u", "title": "t", "content": "body"}]

    def test_event_array_tool_use_input_fallback(self):
        """When the terminal ``result`` event lacks ``structured_output``, the
        parser scans tool_use blocks for the structured payload."""
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps([
            {"type": "system"},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "input": {"results": [{"title": "X", "url": "u", "description": "d"}]},
                    }],
                },
            },
            {"type": "result"},  # no structured_output
        ])
        out = _parse_claude_json(stdout, "results")
        assert out == [{"title": "X", "url": "u", "description": "d"}]

    def test_missing_inner_key_raises(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        stdout = json.dumps({"result": "{}"})
        with pytest.raises(ValueError, match="missing 'results'"):
            _parse_claude_json(stdout, "results")

    def test_non_json_stdout_raises(self):
        from plugins.web.claude_code.provider import _parse_claude_json
        with pytest.raises(json.JSONDecodeError):
            _parse_claude_json("not json", "results")


# ---------------------------------------------------------------------------
# search() — full subprocess.run mocking
# ---------------------------------------------------------------------------


def _ok_proc(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _err_proc(returncode: int, stderr: str) -> MagicMock:
    return MagicMock(returncode=returncode, stdout="", stderr=stderr)


class TestClaudeCodeSearch:
    def test_happy_path_returns_normalized_results(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        envelope = json.dumps({
            "structured_output": {
                "results": [
                    {"title": "T1", "url": "https://a", "description": "d1"},
                    {"title": "T2", "url": "https://b", "description": "d2"},
                ]
            }
        })
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_ok_proc(envelope)):
            result = ClaudeCodeWebProvider().search("hermes", limit=5)
        assert result["success"] is True
        assert len(result["data"]["web"]) == 2
        assert result["data"]["web"][0] == {
            "title": "T1", "url": "https://a", "description": "d1", "position": 1,
        }
        assert result["data"]["web"][1]["position"] == 2

    def test_respects_limit(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        envelope = json.dumps({
            "structured_output": {
                "results": [
                    {"title": f"R{i}", "url": f"https://{i}", "description": ""}
                    for i in range(10)
                ]
            }
        })
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_ok_proc(envelope)):
            result = ClaudeCodeWebProvider().search("q", limit=3)
        assert len(result["data"]["web"]) == 3

    def test_missing_binary_returns_failure(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value=None):
            result = ClaudeCodeWebProvider().search("q")
        assert result["success"] is False
        assert "not found on PATH" in result["error"]

    def test_non_zero_exit_returns_failure(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_err_proc(2, "auth failed")):
            result = ClaudeCodeWebProvider().search("q")
        assert result["success"] is False
        assert "exited 2" in result["error"]
        assert "auth failed" in result["error"]

    def test_timeout_returns_failure(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("claude", 60)):
            result = ClaudeCodeWebProvider().search("q")
        assert result["success"] is False
        assert "timed out" in result["error"]

    def test_malformed_json_returns_failure(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_ok_proc("not json at all")):
            result = ClaudeCodeWebProvider().search("q")
        assert result["success"] is False
        assert "parse" in result["error"].lower() or "json" in result["error"].lower()

    def test_launch_oserror_returns_failure(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   side_effect=OSError("permission denied")):
            result = ClaudeCodeWebProvider().search("q")
        assert result["success"] is False
        assert "Could not launch" in result["error"]


# ---------------------------------------------------------------------------
# extract() — list-of-dicts return shape, per-URL error surfacing
# ---------------------------------------------------------------------------


class TestClaudeCodeExtract:
    def test_happy_path_returns_one_doc_per_url(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        envelope = json.dumps({
            "structured_output": {
                "pages": [
                    {"url": "https://a", "title": "A", "content": "body A"},
                    {"url": "https://b", "title": "B", "content": "body B"},
                ]
            }
        })
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_ok_proc(envelope)):
            docs = ClaudeCodeWebProvider().extract(["https://a", "https://b"])
        assert isinstance(docs, list)
        assert len(docs) == 2
        assert docs[0]["url"] == "https://a"
        assert docs[0]["title"] == "A"
        assert docs[0]["content"] == "body A"
        assert docs[0]["raw_content"] == "body A"
        assert docs[0]["metadata"] == {"source": "claude-code"}

    def test_empty_urls_returns_empty_list(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        # No subprocess call should happen; if it does, the test fails loudly.
        with patch("plugins.web.claude_code.provider.subprocess.run") as mock_run:
            docs = ClaudeCodeWebProvider().extract([])
        assert docs == []
        mock_run.assert_not_called()

    def test_missing_binary_returns_per_url_errors(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value=None):
            docs = ClaudeCodeWebProvider().extract(["https://a", "https://b"])
        assert len(docs) == 2
        for d in docs:
            assert "error" in d
            assert "not found" in d["error"]

    def test_non_zero_exit_returns_per_url_errors(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_err_proc(3, "rate limited")):
            docs = ClaudeCodeWebProvider().extract(["https://a"])
        assert len(docs) == 1
        assert "exited 3" in docs[0]["error"]
        assert "rate limited" in docs[0]["error"]

    def test_timeout_returns_per_url_errors(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("claude", 90)):
            docs = ClaudeCodeWebProvider().extract(["https://a", "https://b"])
        assert len(docs) == 2
        assert all("timed out" in d["error"] for d in docs)

    def test_malformed_json_returns_per_url_errors(self):
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=_ok_proc("definitely not json")):
            docs = ClaudeCodeWebProvider().extract(["https://a"])
        assert len(docs) == 1
        assert "error" in docs[0]


# ---------------------------------------------------------------------------
# Plugin registration + registry wiring
# ---------------------------------------------------------------------------


class TestClaudeCodePluginRegistration:
    def test_plugin_yaml_declares_claude_code_provider(self):
        """``plugins/web/claude_code/plugin.yaml`` must advertise the
        ``claude-code`` provider so the bundled plugin loader picks it up."""
        repo_root = Path(__file__).resolve().parents[2]
        plugin_yaml = repo_root / "plugins" / "web" / "claude_code" / "plugin.yaml"
        assert plugin_yaml.exists()
        spec = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
        assert spec["kind"] == "backend"
        assert "claude-code" in spec["provides_web_providers"]

    def test_plugin_init_exposes_register_hook(self):
        """The bundled plugin loader looks for a top-level ``register(ctx)``
        function and would silently skip the plugin without it."""
        import plugins.web.claude_code as plugin
        assert callable(getattr(plugin, "register", None))

    def test_register_attaches_provider_to_context(self):
        """``register(ctx)`` must call ``ctx.register_web_search_provider``
        with a ClaudeCodeWebProvider instance."""
        import plugins.web.claude_code as plugin
        from plugins.web.claude_code.provider import ClaudeCodeWebProvider
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_web_search_provider.assert_called_once()
        registered = ctx.register_web_search_provider.call_args.args[0]
        assert isinstance(registered, ClaudeCodeWebProvider)


# ---------------------------------------------------------------------------
# tools.web_tools._is_backend_available integration
# ---------------------------------------------------------------------------


class TestClaudeCodeBackendAvailability:
    def test_is_backend_available_true_when_authed(self):
        """``_is_backend_available("claude-code")`` defers to the plugin's
        ``_is_configured`` helper. With claude installed + authed, return True."""
        from tools import web_tools
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=MagicMock(returncode=0)):
            assert web_tools._is_backend_available("claude-code") is True

    def test_is_backend_available_false_when_unauthed(self):
        from tools import web_tools
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value=None):
            assert web_tools._is_backend_available("claude-code") is False

    def test_check_web_api_key_true_when_claude_code_configured(self, monkeypatch):
        """``web.backend = claude-code`` + authenticated CLI is sufficient for
        ``check_web_api_key()`` to gate web tools as available."""
        from tools import web_tools
        monkeypatch.setattr(web_tools, "_load_web_config",
                            lambda: {"backend": "claude-code"})
        with patch("plugins.web.claude_code.provider.shutil.which",
                   return_value="/usr/local/bin/claude"), \
             patch("plugins.web.claude_code.provider.subprocess.run",
                   return_value=MagicMock(returncode=0)):
            assert web_tools.check_web_api_key() is True


# ---------------------------------------------------------------------------
# Setup wizard plumbing — claude-code must appear in the web-backend picker
# ---------------------------------------------------------------------------


class TestClaudeCodeSetupWizardEntry:
    def test_claude_code_is_listed_in_web_setup_options(self):
        """``hermes_cli.tools_config.TOOL_CATEGORIES['web']`` must include a
        claude-code option so users can pick it from the interactive picker."""
        from hermes_cli.tools_config import TOOL_CATEGORIES
        web_options = TOOL_CATEGORIES["web"]["providers"]
        backends = [o.get("web_backend") for o in web_options]
        assert "claude-code" in backends

    def test_claude_code_option_requires_no_env_vars(self):
        """No env-var prompts — Claude Code re-uses ``claude auth login``."""
        from hermes_cli.tools_config import TOOL_CATEGORIES
        web_options = TOOL_CATEGORIES["web"]["providers"]
        cc_opt = next(o for o in web_options if o.get("web_backend") == "claude-code")
        assert cc_opt.get("env_vars", []) == []
