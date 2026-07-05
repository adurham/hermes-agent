"""Tests for agent/web_search_registry.py — fork's provider-scoped web config.

Verifies the _read_web_config_key function that supports
web.by_provider.<current_provider> overrides.
"""

import pytest


class TestWebSearchRegistryFork:
    """Tests for the fork's _read_web_config_key in web_search_registry."""

    def test_read_web_config_key_falls_back_to_top_level(self, monkeypatch):
        """When no by_provider config exists, falls back to top-level web config."""
        from agent.web_search_registry import _read_web_config_key

        # Mock no main provider
        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "",
        )

        # Mock load_config to return a simple web config
        def fake_load_config():
            return {"web": {"search_backend": "brave-free"}}

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "brave-free"

    def test_read_web_config_key_uses_by_provider(self, monkeypatch):
        """When by_provider config exists for the current provider, it's used."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "anthropic",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "anthropic": {"search_backend": "anthropic-native"},
                    },
                    "search_backend": "brave-free",
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "anthropic-native"

    def test_read_web_config_key_by_provider_shared_backend(self, monkeypatch):
        """by_provider block can use a shared 'backend' key."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "exo",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "exo": {"backend": "searxng"},
                    },
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "searxng"

    def test_read_web_config_key_by_provider_wins_over_top_level(self, monkeypatch):
        """by_provider takes precedence over top-level web.backend."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "anthropic",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "anthropic": {"search_backend": "anthropic-native"},
                    },
                    "backend": "firecrawl",
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "anthropic-native"

    def test_read_web_config_key_no_main_provider(self, monkeypatch):
        """When no main provider is set, falls through to top-level config."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "anthropic": {"search_backend": "anthropic-native"},
                    },
                    "search_backend": "tavily",
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "tavily"

    def test_read_web_config_key_extract_capability(self, monkeypatch):
        """_read_web_config_key works for the 'extract' capability too."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "anthropic",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "anthropic": {"extract_backend": "jina"},
                    },
                    "extract_backend": "firecrawl",
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("extract")
        assert result == "jina"

    def test_read_web_config_key_by_provider_not_matching(self, monkeypatch):
        """When by_provider has no entry for the current provider, falls back."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "openrouter",
        )

        def fake_load_config():
            return {
                "web": {
                    "by_provider": {
                        "anthropic": {"search_backend": "anthropic-native"},
                    },
                    "search_backend": "ddgs",
                }
            }

        monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)

        result = _read_web_config_key("search")
        assert result == "ddgs"

    def test_read_web_config_key_exception_safety(self, monkeypatch):
        """Exceptions in config reading don't crash — fall through to top-level."""
        from agent.web_search_registry import _read_web_config_key

        monkeypatch.setattr(
            "agent.auxiliary_client._read_main_provider",
            lambda: "anthropic",
        )

        def broken_load():
            raise RuntimeError("config broken")

        monkeypatch.setattr("hermes_cli.config.load_config", broken_load)

        # Should not raise — the function catches exceptions internally
        result = _read_web_config_key("search")
        # Falls through to top-level config read which also fails
        assert result is None or isinstance(result, str)