"""Regression tests for the ``auto`` → main-model-first policy.

Prior to this change, aggregator users (OpenRouter / Nous Portal) had aux
tasks routed through a cheap provider-side default (Gemini Flash) while
non-aggregator users got their main model.  This made behavior inconsistent
and surprising — users picked Claude but got Gemini Flash summaries.

The current policy: ``auto`` means "use my main chat model" for every user,
regardless of provider type.  Explicit per-task overrides in ``config.yaml``
(``auxiliary.<task>.provider``) still win.  The cheap fallback chain only
runs when the main provider has no working client.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── Text aux tasks — _resolve_auto ──────────────────────────────────────────


class TestResolveAutoMainFirst:
    """_resolve_auto() must prefer main provider + main model for every user."""

    def test_openrouter_main_uses_main_model_for_aux(self, monkeypatch):
        """OpenRouter main user → aux uses their picked OR model, not Gemini Flash."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")

        with patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="openrouter",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="anthropic/claude-sonnet-4.6",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "anthropic/claude-sonnet-4.6")

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is mock_client
        assert model == "anthropic/claude-sonnet-4.6"
        # Verify it asked resolve_provider_client for the MAIN provider+model,
        # not a fallback-chain provider
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.args[0] == "openrouter"
        assert mock_resolve.call_args.args[1] == "anthropic/claude-sonnet-4.6"

    def test_moa_main_resolves_aux_to_aggregator(self, monkeypatch, tmp_path):
        """MoA main user → aux runs on the aggregator slot, NOT the preset name.

        provider='moa'/model='opus-gpt' would otherwise send the preset name
        'opus-gpt' as the model id and 400 ("not a valid model ID"). Aux tasks
        don't need the reference fan-out — they use the aggregator (the preset's
        acting model). The virtual moa://local base_url + placeholder key must
        be dropped so the aggregator resolves via its own provider credentials.
        """
        import yaml

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "moa": {
                        "default_preset": "opus-gpt",
                        "presets": {
                            "opus-gpt": {
                                "enabled": True,
                                "reference_models": [{"provider": "openrouter", "model": "openai/gpt-5.5"}],
                                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                            }
                        },
                    }
                }
            )
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        with patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve, patch(
            "agent.auxiliary_client._is_provider_unhealthy", return_value=False
        ):
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "anthropic/claude-opus-4.8")

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto(
                main_runtime={
                    "provider": "moa",
                    "model": "opus-gpt",
                    "base_url": "moa://local",
                    "api_key": "moa-virtual-provider",
                    "api_mode": "chat_completions",
                },
                task="title_generation",
            )

        assert client is mock_client
        # Resolved to the aggregator's real provider+model, not the preset name.
        assert mock_resolve.call_args.args[0] == "openrouter"
        assert mock_resolve.call_args.args[1] == "anthropic/claude-opus-4.8"
        # The virtual moa://local endpoint must not be forwarded as the
        # aggregator's base_url.
        assert mock_resolve.call_args.kwargs.get("explicit_base_url") in (None, "")

    def test_nous_main_uses_main_model_for_aux(self, monkeypatch):
        """Nous Portal main user → aux uses their picked Nous model, not free-tier MiMo."""
        # No OPENROUTER_API_KEY → ensures if main failed we'd fall to chain
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="nous",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="anthropic/claude-opus-4.6",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "anthropic/claude-opus-4.6")

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is mock_client
        assert model == "anthropic/claude-opus-4.6"
        assert mock_resolve.call_args.args[0] == "nous"

    def test_non_aggregator_main_still_uses_main(self, monkeypatch):
        """Non-aggregator main (DeepSeek) → unchanged behavior, main model used."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="deepseek",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="deepseek-chat",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "deepseek-chat")

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is mock_client
        assert model == "deepseek-chat"
        assert mock_resolve.call_args.args[0] == "deepseek"

    def test_main_unavailable_falls_through_to_chain(self, monkeypatch):
        """Main provider with no working client → fall back to aux chain."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        chain_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="anthropic",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="claude-opus",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(None, None),  # main provider has no client
        ), patch(
            "agent.auxiliary_client._try_openrouter",
            return_value=(chain_client, "google/gemini-3-flash-preview"),
        ):
            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is chain_client
        assert model == "google/gemini-3-flash-preview"

    def test_main_unavailable_uses_task_fallback_chain_before_builtin_chain(self):
        """Auto aux resolution honors auxiliary.<task>.fallback_chain before built-ins."""
        task_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="nvidia",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="qwen/qwen3.5-122b-a10b",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(None, None),  # main provider has no client
        ), patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(task_client, "task-free-model", "fallback_chain[0](openrouter)"),
        ) as mock_task_chain, patch(
            "agent.auxiliary_client._try_main_fallback_chain",
        ) as mock_main_chain, patch(
            "agent.auxiliary_client._try_openrouter",
        ) as mock_openrouter:
            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto(task="title_generation")

        assert client is task_client
        assert model == "task-free-model"
        mock_task_chain.assert_called_once_with(
            "title_generation", "nvidia", reason="main provider unavailable")
        mock_main_chain.assert_not_called()
        mock_openrouter.assert_not_called()

    def test_main_unavailable_uses_main_fallback_chain_before_builtin_chain(self):
        """Auto aux resolution honors top-level fallback_providers before built-ins."""
        main_fallback_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="nvidia",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="qwen/qwen3.5-122b-a10b",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(None, None),  # main provider has no client
        ), patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(None, None, ""),
        ), patch(
            "agent.auxiliary_client._try_main_fallback_chain",
            return_value=(main_fallback_client, "inclusionai/ring-2.6-1t:free", "openrouter"),
        ) as mock_main_chain, patch(
            "agent.auxiliary_client._try_openrouter",
        ) as mock_openrouter:
            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto(task="title_generation")

        assert client is main_fallback_client
        assert model == "inclusionai/ring-2.6-1t:free"
        mock_main_chain.assert_called_once_with(
            "title_generation", "nvidia", reason="main provider unavailable")
        mock_openrouter.assert_not_called()

    def test_no_main_config_uses_chain_directly(self):
        """No main provider configured → skip step 1, use chain (no regression)."""
        chain_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="",
        ), patch(
            "agent.auxiliary_client._try_openrouter",
            return_value=(chain_client, "google/gemini-3-flash-preview"),
        ):
            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is chain_client

    def test_runtime_override_wins_over_config(self, monkeypatch):
        """main_runtime kwarg overrides config-read main provider (anthropic wins
        over the config-read openrouter provider).  For Anthropic specifically,
        the aux MODEL is claude-sonnet-4-6 — not the runtime main model — because
        provider-matched aux substitution applies to anthropic sessions."""
        from agent.auxiliary_client import _ANTHROPIC_DEFAULT_AUX_MODEL

        with patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="openrouter",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="config-model",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_resolve.return_value = (MagicMock(), _ANTHROPIC_DEFAULT_AUX_MODEL)

            from agent.auxiliary_client import _resolve_auto

            _resolve_auto(main_runtime={
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "base_url": "",
                "api_key": "",
                "api_mode": "",
            })

        # Runtime provider override wins (anthropic, not config-read openrouter).
        assert mock_resolve.call_args.args[0] == "anthropic"
        # Aux model is sonnet-4-6, not the main runtime opus model.
        assert mock_resolve.call_args.args[1] == _ANTHROPIC_DEFAULT_AUX_MODEL

    def test_resolve_provider_auto_returns_runtime_model_not_stale_config_default(self):
        """Blank auto aux requests must not pair a stale config model with live fallback provider."""
        runtime_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_model",
            return_value="claude-opus-4-8",
        ) as mock_read_main_model, patch(
            "agent.auxiliary_client._resolve_auto",
            return_value=(runtime_client, "gpt-5.5"),
        ) as mock_resolve_auto:
            from agent.auxiliary_client import resolve_provider_client

            client, model = resolve_provider_client(
                "auto",
                main_runtime={
                    "provider": "openai-codex",
                    "model": "gpt-5.5",
                    "base_url": "",
                    "api_key": "",
                    "api_mode": "codex_responses",
                },
            )

        assert client is runtime_client
        assert model == "gpt-5.5"
        mock_read_main_model.assert_not_called()
        mock_resolve_auto.assert_called_once()

    def test_runtime_base_url_passed_for_named_api_key_provider(self):
        """Named API-key providers inherit the live session endpoint for aux work."""
        token_plan_url = "https://token-plan-sgp.xiaomimimo.com/v1"
        with patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="openrouter",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="config-model",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_resolve.return_value = (MagicMock(), "mimo-v2.5-pro")

            from agent.auxiliary_client import _resolve_auto

            _resolve_auto(main_runtime={
                "provider": "xiaomi",
                "model": "mimo-v2.5-pro",
                "base_url": token_plan_url,
                "api_key": "tp-test-key",
                "api_mode": "chat_completions",
            })

        assert mock_resolve.call_args.args[0] == "xiaomi"
        assert mock_resolve.call_args.args[1] == "mimo-v2.5-pro"
        assert mock_resolve.call_args.kwargs["explicit_base_url"] == token_plan_url
        assert mock_resolve.call_args.kwargs["explicit_api_key"] == "tp-test-key"
        assert mock_resolve.call_args.kwargs["api_mode"] == "chat_completions"


# ── Vision — resolve_vision_provider_client ─────────────────────────────────


class TestResolveVisionMainFirst:
    """Vision auto-detection prefers the main provider first."""

    def test_openrouter_main_vision_uses_main_model(self, monkeypatch):
        """OpenRouter main with vision-capable model → aux vision uses main model."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="openrouter",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="anthropic/claude-sonnet-4.6",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve, patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ):
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "anthropic/claude-sonnet-4.6")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "openrouter"
        assert client is mock_client
        assert model == "anthropic/claude-sonnet-4.6"
        # Verify it did NOT call the strict vision backend for OpenRouter
        # (which would have used a cheap gemini-flash-preview default)
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.args[0] == "openrouter"
        assert mock_resolve.call_args.args[1] == "anthropic/claude-sonnet-4.6"
        assert mock_resolve.call_args.kwargs.get("is_vision") is True

    def test_nous_main_vision_uses_paid_nous_vision_backend(self):
        """Paid Nous main → aux vision uses the dedicated Nous vision backend."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="nous",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="openai/gpt-5",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client._resolve_strict_vision_backend",
            return_value=(MagicMock(), "google/gemini-3-flash-preview"),
        ):
            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "nous"
        assert client is not None
        assert model == "google/gemini-3-flash-preview"

    def test_nous_main_vision_uses_free_tier_nous_vision_backend(self):
        """Free-tier Nous main → aux vision uses MiMo omni, not the text main model."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="nous",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="xiaomi/mimo-v2-pro",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client._resolve_strict_vision_backend",
            return_value=(MagicMock(), "xiaomi/mimo-v2-omni"),
        ):
            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "nous"
        assert client is not None
        assert model == "xiaomi/mimo-v2-omni"

    def test_exotic_provider_with_vision_override_preserved(self):
        """xiaomi → mimo-v2.5 override still wins over main_model."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="xiaomi",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="mimo-v2-pro",  # text model
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve, patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ):
            mock_resolve.return_value = (MagicMock(), "mimo-v2.5")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "xiaomi"
        # Should use mimo-v2.5 (vision override), not mimo-v2-pro (text main)
        assert mock_resolve.call_args.args[1] == "mimo-v2.5"
        assert mock_resolve.call_args.kwargs.get("is_vision") is True

    def test_copilot_vision_sets_vision_header(self, monkeypatch):
        """Copilot vision requests include the header required for vision routing."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_test-token")

        captured = {}

        def fake_headers(*, is_agent_turn=False, is_vision=False):
            captured["is_agent_turn"] = is_agent_turn
            captured["is_vision"] = is_vision
            return {"Copilot-Vision-Request": "true"} if is_vision else {}

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="copilot",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="configured-copilot-model",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client.OpenAI",
        ) as mock_openai, patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "provider": "copilot",
                "api_key": "copilot-api-token",
                "base_url": "https://api.githubcopilot.com",
            },
        ), patch(
            "hermes_cli.copilot_auth.copilot_request_headers",
            side_effect=fake_headers,
        ):
            mock_client = MagicMock()
            mock_openai.return_value = mock_client

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "copilot"
        assert client is mock_client
        assert model == "configured-copilot-model"
        assert captured == {"is_agent_turn": True, "is_vision": True}
        assert mock_openai.call_args.kwargs["default_headers"]["Copilot-Vision-Request"] == "true"

    def test_text_copilot_does_not_set_vision_header(self, monkeypatch):
        """Text Copilot requests keep the vision-only header off."""
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_test-token")

        captured = {}

        def fake_headers(*, is_agent_turn=False, is_vision=False):
            captured["is_agent_turn"] = is_agent_turn
            captured["is_vision"] = is_vision
            return {"Copilot-Vision-Request": "true"} if is_vision else {}

        with patch(
            "agent.auxiliary_client.OpenAI",
        ) as mock_openai, patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "provider": "copilot",
                "api_key": "copilot-api-token",
                "base_url": "https://api.githubcopilot.com",
            },
        ), patch(
            "hermes_cli.copilot_auth.copilot_request_headers",
            side_effect=fake_headers,
        ):
            mock_client = MagicMock()
            mock_openai.return_value = mock_client

            from agent.auxiliary_client import resolve_provider_client

            client, model = resolve_provider_client("copilot", "gpt-5-mini")

        assert client is mock_client
        assert model == "gpt-5-mini"
        assert captured == {"is_agent_turn": True, "is_vision": False}
        assert "default_headers" not in mock_openai.call_args.kwargs

    def test_main_unavailable_vision_falls_through_to_aggregators(self):
        """Main provider fails → fall back to OpenRouter/Nous strict backends."""
        fallback_client = MagicMock()
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="deepseek",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="deepseek-chat",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(None, None),
        ), patch(
            "agent.auxiliary_client._resolve_strict_vision_backend",
            return_value=(fallback_client, "google/gemini-3-flash-preview"),
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ):
            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert client is fallback_client
        assert provider in {"openrouter", "nous"}

    def test_explicit_provider_override_still_wins(self):
        """Explicit config override bypasses main-first policy."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="openrouter",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="anthropic/claude-opus-4.6",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("nous", None, None, None, None),  # explicit override
        ), patch(
            "agent.auxiliary_client._resolve_strict_vision_backend"
        ) as mock_strict:
            mock_strict.return_value = (MagicMock(), "nous-default-model")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        # Explicit "nous" override → uses strict backend, NOT main model path
        assert provider == "nous"
        mock_strict.assert_called_once_with("nous", None)


# ── Vision — custom provider endpoint credential passthrough ────────────────


class TestResolveVisionCustomProvider:
    """Custom-endpoint mains must forward base_url/api_key to Step 1.

    Regression: a ``custom:<name>`` main provider resolves to the bare
    runtime provider id ``"custom"``.  ``resolve_provider_client("custom")``
    has no built-in endpoint, so without forwarding the live base_url/api_key
    it returns ``(None, None)`` and vision falls through to OpenRouter / Nous,
    which an offline / aggregator-less user has never configured — breaking
    vision entirely with ``No LLM provider configured for task=vision
    provider=auto``.  The fix recovers the live endpoint that
    ``set_runtime_main()`` recorded for the turn.
    """

    def test_custom_main_forwards_runtime_endpoint(self, monkeypatch):
        """custom main with recorded runtime endpoint → Step 1 builds a client."""
        import agent.auxiliary_client as aux

        monkeypatch.setattr(aux, "_RUNTIME_MAIN_BASE_URL", "https://my.endpoint.example/v1")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_KEY", "sk-runtime-key")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_MODE", "anthropic_messages")

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="custom",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "claude-opus-4-8")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "custom"
        assert client is mock_client
        assert model == "claude-opus-4-8"
        # The endpoint credentials recorded for the turn MUST be forwarded,
        # otherwise resolve_provider_client("custom") returns (None, None).
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("explicit_base_url") == "https://my.endpoint.example/v1"
        assert kwargs.get("explicit_api_key") == "sk-runtime-key"
        assert kwargs.get("is_vision") is True

    def test_custom_prefixed_main_forwards_runtime_endpoint(self, monkeypatch):
        """A ``custom:<name>`` provider id also forwards the runtime endpoint."""
        import agent.auxiliary_client as aux

        monkeypatch.setattr(aux, "_RUNTIME_MAIN_BASE_URL", "https://named.example/v1")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_KEY", "sk-named")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_MODE", "")

        with patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="custom:copilot-gateway",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "claude-opus-4-8")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert provider == "custom:copilot-gateway"
        assert client is mock_client
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("explicit_base_url") == "https://named.example/v1"
        assert kwargs.get("explicit_api_key") == "sk-named"
        assert kwargs.get("is_vision") is True

    def test_custom_main_no_runtime_falls_back_to_configured_endpoint(self, monkeypatch):
        """No recorded runtime endpoint → resolve the configured custom endpoint."""
        import agent.auxiliary_client as aux

        monkeypatch.setattr(aux, "_RUNTIME_MAIN_BASE_URL", "")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_KEY", "")
        monkeypatch.setattr(aux, "_RUNTIME_MAIN_API_MODE", "")

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="custom",
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client._resolve_custom_runtime",
            return_value=("https://configured.example/v1", "sk-configured", "chat_completions"),
        ), patch(
            "agent.auxiliary_client.resolve_provider_client"
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "claude-opus-4-8")

            from agent.auxiliary_client import resolve_vision_provider_client

            import agent.auxiliary_client as _aux_mod
            _aux_mod._clear_vision_resolution_cache()
            provider, client, model = resolve_vision_provider_client()

        assert client is mock_client
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("explicit_base_url") == "https://configured.example/v1"
        assert kwargs.get("explicit_api_key") == "sk-configured"


# ── Constant cleanup ────────────────────────────────────────────────────────


def test_aggregator_providers_constant_removed():
    """The dead _AGGREGATOR_PROVIDERS constant should no longer live in the module.

    Removed when the main-first policy made the aggregator-skip guard obsolete.
    """
    import agent.auxiliary_client as aux_mod

    assert not hasattr(aux_mod, "_AGGREGATOR_PROVIDERS"), (
        "_AGGREGATOR_PROVIDERS was removed when _resolve_auto stopped "
        "treating aggregators specially. If you re-added it, the main-first "
        "policy may have regressed."
    )


# ── Exo-scoped auxiliary delegation (_resolve_task_provider_model) ────────────


class TestExoScopedAuxDelegation:
    """When ``auxiliary.<task>`` is configured to target the exo cluster
    (``provider: exo`` / ``custom:exo`` / matching ``base_url``), that
    override is honored ONLY when the active main provider is itself exo.
    Non-exo sessions (Claude, OpenRouter, Ollama, ...) drop it and fall
    through to ``"auto"`` so aux tasks follow the main provider instead
    of pulling the cluster into the request.

    Mirrors the exo-only delegate scoping in ``agent/image_routing.py``
    (``_provider_is_exo``).
    """

    def test_exo_main_honors_exo_aux_override(self):
        """Main=exo + ``auxiliary.compression.provider=exo`` -> returns
        (``"exo"``, ``qwen_model``) -- the override is forwarded to
        ``resolve_provider_client``."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="exo",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="mlx-community/DeepSeek-V4-Flash",
        ), patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
                "api_key": "not-needed",
            },
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "mlx-community/Qwen3.6-35B-A3B-8bit")

            from agent.auxiliary_client import _resolve_task_provider_model

            provider, model, base_url, api_key, api_mode = (
                _resolve_task_provider_model(task="compression")
            )

        # When both base_url + api_key are configured, the resolver returns
        # ``"custom"`` with the endpoint inlined (see the cfg_base_url +
        # cfg_api_key branch).  That's the correct, working path -- the
        # caller (get_text_auxiliary_client / resolve_vision_provider_client)
        # hands (provider, model, base_url, api_key) to
        # resolve_provider_client, which builds the OpenAI client against the
        # exo base_url.  The key invariant is that we did NOT fall through to
        # ``"auto"`` (which would route aux to the main DeepSeek-V4-Flash
        # model instead of Qwen3.6).
        assert provider == "custom"
        assert model == "mlx-community/Qwen3.6-35B-A3B-8bit"
        assert base_url == "http://192.168.86.201:52415/v1"
        assert api_key == "not-needed"

    def test_non_exo_main_drops_exo_aux_override(self):
        """Anthropic main + ``auxiliary.compression.provider=exo`` -> guard
        drops the override and falls through to ``"auto"``."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="anthropic",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="claude-sonnet-4-6",
        ), patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={
                "provider": "exo",
                "model": "mlx-community/Qwen3.6-35B-A3B-8bit",
                "base_url": "http://192.168.86.201:52415/v1",
                "api_key": "not-needed",
            },
        ):
            from agent.auxiliary_client import _resolve_task_provider_model

            provider, model, base_url, api_key, api_mode = (
                _resolve_task_provider_model(task="compression")
            )

        assert provider == "auto", (
            f"expected 'auto' for non-exo main with exo-configured aux, "
            f"got {provider!r}"
        )
        assert model is None
        assert base_url is None
        assert api_key is None


# ── Anthropic 401 fix + provider-matched sonnet-4-6 aux ──────────────────────


class TestAnthropicAuxModel:
    """When the main provider is Anthropic, auxiliary tasks must use
    claude-sonnet-4-6 (not the main Opus model, not Haiku) and must not be
    poisoned by a foreign placeholder key like "not-needed".

    Fork-only feature — 2026-06-21.
    """

    def test_anthropic_main_aux_ignores_foreign_placeholder_key(self):
        """main=anthropic, runtime api_key="not-needed" → resolved aux client
        uses the real OAuth token, NOT "not-needed"."""
        real_token = "sk-ant-oat01-" + "x" * 88

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="anthropic",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client._select_pool_entry",
            return_value=(False, None),
        ), patch(
            # resolve_anthropic_token is lazily imported inside _try_anthropic
            # from agent.anthropic_adapter, so patch it at the source module.
            "agent.anthropic_adapter.resolve_anthropic_token",
            return_value=real_token,
        ) as mock_resolve_token, patch(
            "agent.anthropic_adapter.build_anthropic_client",
        ) as mock_build:
            mock_real_client = MagicMock()
            mock_build.return_value = mock_real_client

            from agent.auxiliary_client import _try_anthropic

            client, model = _try_anthropic(explicit_api_key="not-needed")

        # The placeholder key must have been discarded; resolve_anthropic_token
        # must have been consulted for the real credential.
        mock_resolve_token.assert_called_once()
        # build_anthropic_client must have been called with the real token, NOT
        # with "not-needed".
        assert mock_build.call_args is not None
        called_api_key = mock_build.call_args[0][0]  # first positional arg
        assert called_api_key == real_token, (
            f"Expected real token, got {called_api_key!r}"
        )
        assert client is not None

    def test_anthropic_main_aux_uses_sonnet_not_opus(self):
        """main=anthropic/claude-opus-4-8, empty auxiliary.compression →
        resolved aux model is claude-sonnet-4-6, not the main opus model."""
        from agent.auxiliary_client import _ANTHROPIC_DEFAULT_AUX_MODEL

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="anthropic",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, _ANTHROPIC_DEFAULT_AUX_MODEL)

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is mock_client
        assert model == _ANTHROPIC_DEFAULT_AUX_MODEL, (
            f"Expected {_ANTHROPIC_DEFAULT_AUX_MODEL!r}, got {model!r}"
        )
        # Verify resolve_provider_client was asked for sonnet, NOT for opus.
        mock_resolve.assert_called_once()
        called_model_arg = mock_resolve.call_args[0][1]  # second positional arg
        assert called_model_arg == _ANTHROPIC_DEFAULT_AUX_MODEL, (
            f"_resolve_auto Step-1 should request {_ANTHROPIC_DEFAULT_AUX_MODEL!r} "
            f"for anthropic aux, got {called_model_arg!r}"
        )

    def test_anthropic_per_task_model_override_wins(self):
        """auxiliary.compression.model set explicitly → that model is used,
        NOT claude-sonnet-4-6."""
        from agent.auxiliary_client import _ANTHROPIC_DEFAULT_AUX_MODEL

        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="anthropic",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="claude-opus-4-8",
        ), patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"model": "claude-haiku-4-5-20251001"},
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
        ) as mock_resolve:
            mock_client = MagicMock()
            # _resolve_auto returns sonnet; the per-task model is applied at the
            # outer resolve_provider_client("auto") layer.
            mock_resolve.return_value = (mock_client, _ANTHROPIC_DEFAULT_AUX_MODEL)

            from agent.auxiliary_client import _resolve_task_provider_model

            provider, model, base_url, api_key, api_mode = (
                _resolve_task_provider_model(task="compression")
            )

        # Provider should be "auto" (no explicit provider in the task config).
        assert provider == "auto"
        # The per-task model override must survive as the returned model so the
        # outer caller can apply it.
        assert model == "claude-haiku-4-5-20251001", (
            f"Per-task model override should be preserved, got {model!r}"
        )

    def test_non_anthropic_main_unaffected(self):
        """main=exo → no sonnet-4-6 substitution; Step-1 still uses the
        configured exo main model (existing exo-scoping behavior intact)."""
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="exo",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="mlx-community/DeepSeek-V4-Flash",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
        ) as mock_resolve:
            mock_client = MagicMock()
            mock_resolve.return_value = (mock_client, "mlx-community/DeepSeek-V4-Flash")

            from agent.auxiliary_client import _resolve_auto

            client, model = _resolve_auto()

        assert client is mock_client
        # The exo main model must be forwarded unchanged — no sonnet substitution.
        mock_resolve.assert_called_once()
        called_model_arg = mock_resolve.call_args[0][1]
        assert called_model_arg == "mlx-community/DeepSeek-V4-Flash", (
            f"Non-anthropic providers must pass their main model unchanged, "
            f"got {called_model_arg!r}"
        )

    def test_anthropic_aux_client_carries_1m_context_beta(self):
        """_try_anthropic() with the default aux model (sonnet-4-6) must build
        a client that has the 1M-context beta in its request headers.

        build_anthropic_client gates the beta on _model_supports_1m_context,
        and claude-sonnet-4-6 is in that allowlist, so the beta must be present
        when the aux model is sonnet-4-6.
        """
        from agent.auxiliary_client import _ANTHROPIC_DEFAULT_AUX_MODEL
        from agent.anthropic_adapter import _model_supports_1m_context, _CONTEXT_1M_BETA

        # Verify the constant itself qualifies for 1M context.
        assert _model_supports_1m_context(_ANTHROPIC_DEFAULT_AUX_MODEL), (
            f"{_ANTHROPIC_DEFAULT_AUX_MODEL!r} must be in _model_supports_1m_context "
            f"allowlist so the compression aux client gets the 1M-context beta"
        )

        captured_kwargs: dict = {}

        def fake_build(api_key, base_url=None, timeout=None, *, drop_context_1m_beta=False, model=None):
            from agent.anthropic_adapter import _common_betas_for_base_url
            betas = _common_betas_for_base_url(
                base_url, drop_context_1m_beta=drop_context_1m_beta, model=model,
            )
            captured_kwargs["betas"] = betas
            captured_kwargs["model"] = model
            return MagicMock()

        real_token = "sk-ant-oat01-" + "x" * 88

        with patch(
            "agent.auxiliary_client._select_pool_entry",
            return_value=(False, None),
        ), patch(
            "agent.anthropic_adapter.resolve_anthropic_token",
            return_value=real_token,
        ), patch(
            "agent.anthropic_adapter.build_anthropic_client",
            side_effect=fake_build,
        ):
            from agent.auxiliary_client import _try_anthropic

            _try_anthropic()

        assert captured_kwargs.get("model") == _ANTHROPIC_DEFAULT_AUX_MODEL
        assert _CONTEXT_1M_BETA in captured_kwargs.get("betas", []), (
            f"Expected {_CONTEXT_1M_BETA!r} in beta headers for "
            f"{_ANTHROPIC_DEFAULT_AUX_MODEL!r} aux client, got {captured_kwargs.get('betas')}"
        )

    def test_compression_task_anthropic_main_resolves_sonnet_e2e(self):
        """Canonical regression: set_runtime_main(anthropic, opus-4-8, not-needed) →
        resolve_provider_client(*_resolve_task_provider_model('compression')) returns
        claude-sonnet-4-6 with an OAuth token and 1M-context beta support.

        This exercises the REAL resolve_provider_client path — resolve_provider_client
        is NOT mocked — so the `caller_model` fix is validated end-to-end.  This is
        the test that would have caught the bypass where the auto-fill fallback
        (model = _read_main_model() = 'claude-opus-4-8') won over the
        provider-matched model returned by _resolve_auto.
        """
        from agent.auxiliary_client import (
            set_runtime_main,
            clear_runtime_main,
            _resolve_task_provider_model,
            resolve_provider_client,
            _ANTHROPIC_DEFAULT_AUX_MODEL,
        )
        from agent.anthropic_adapter import _model_supports_1m_context

        real_token = "sk-ant-oat01-" + "x" * 88
        mock_built_client = MagicMock()

        try:
            set_runtime_main("anthropic", "claude-opus-4-8", api_key="not-needed")

            with patch(
                "agent.auxiliary_client._select_pool_entry",
                return_value=(False, None),
            ), patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value={},
            ), patch(
                # Patched at source so the lazy import inside _try_anthropic picks it up.
                "agent.anthropic_adapter.resolve_anthropic_token",
                return_value=real_token,
            ), patch(
                "agent.anthropic_adapter.build_anthropic_client",
                return_value=mock_built_client,
            ) as mock_build:
                prov, model, *_ = _resolve_task_provider_model(task="compression")
                client, resolved = resolve_provider_client(prov, model)

            assert resolved == _ANTHROPIC_DEFAULT_AUX_MODEL, (
                f"Expected {_ANTHROPIC_DEFAULT_AUX_MODEL!r}, got {resolved!r} — "
                "resolve_provider_client auto-fill must not override the "
                "provider-matched model returned by _resolve_auto"
            )
            # build_anthropic_client must have been called with the real OAuth
            # token, not the 'not-needed' placeholder.
            assert mock_build.call_count >= 1
            called_token = mock_build.call_args_list[-1][0][0]
            assert called_token == real_token, (
                f"Expected real OAuth token, got {called_token!r}"
            )
            assert _model_supports_1m_context(resolved), (
                f"{resolved!r} must qualify for the 1M-context beta"
            )
        finally:
            clear_runtime_main()

    def test_exo_pinned_task_with_fallback_model_uses_haiku_on_anthropic_main(self):
        """A task pinned to exo with fallback_model=haiku: exo-main keeps Qwen,
        anthropic-main drops the exo pin and uses the fallback (Haiku), NOT the
        global Sonnet default.

        This is the cost-saving path — trivial aux tasks (title, mcp, etc.) run
        free on the local exo cluster when main=exo, and on cheap Haiku (3x under
        Sonnet) when an Anthropic session follows main.
        """
        from agent.auxiliary_client import (
            set_runtime_main,
            clear_runtime_main,
            _resolve_task_provider_model,
        )

        EXO_URL = "http://192.168.86.201:52415/v1"
        QWEN = "mlx-community/Qwen3.6-35B-A3B-8bit"
        cheap_cfg = {
            "provider": "exo",
            "base_url": EXO_URL,
            "api_key": "x",
            "model": QWEN,
            "fallback_model": "claude-haiku-4-5-20251001",
        }

        # main=exo → exo pin honored, Qwen unchanged.
        try:
            set_runtime_main("exo", QWEN, api_key="not-needed", base_url=EXO_URL)
            with patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value=dict(cheap_cfg),
            ):
                prov, model, base_url, _ak, _am = _resolve_task_provider_model(
                    task="title_generation"
                )
            assert prov == "exo" or base_url == EXO_URL, (
                f"exo-main must keep the exo pin, got provider={prov!r} base_url={base_url!r}"
            )
            assert model == QWEN, (
                f"exo-main must keep the configured exo model, got {model!r}"
            )
        finally:
            clear_runtime_main()

        # main=anthropic → exo pin dropped, fallback_model (Haiku) selected.
        try:
            set_runtime_main("anthropic", "claude-opus-4-8", api_key="not-needed")
            with patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value=dict(cheap_cfg),
            ):
                prov, model, base_url, _ak, _am = _resolve_task_provider_model(
                    task="title_generation"
                )
            assert prov == "auto", (
                f"anthropic-main must drop the exo pin to auto, got {prov!r}"
            )
            assert base_url is None, (
                f"anthropic-main must drop the exo base_url, got {base_url!r}"
            )
            assert model == "claude-haiku-4-5-20251001", (
                f"anthropic-main must use fallback_model (Haiku), got {model!r}"
            )
        finally:
            clear_runtime_main()

    def test_exo_pinned_task_without_fallback_model_uses_sonnet_on_anthropic_main(self):
        """A task pinned to exo with NO fallback_model: anthropic-main drops the
        exo pin and clears the model so the provider-default aux model (Sonnet)
        applies — the unchanged behavior for quality-critical tasks like
        compression/vision/curator/memory_extraction.
        """
        from agent.auxiliary_client import (
            set_runtime_main,
            clear_runtime_main,
            _resolve_task_provider_model,
        )

        EXO_URL = "http://192.168.86.201:52415/v1"
        QWEN = "mlx-community/Qwen3.6-35B-A3B-8bit"
        quality_cfg = {
            "provider": "exo",
            "base_url": EXO_URL,
            "api_key": "x",
            "model": QWEN,
        }

        try:
            set_runtime_main("anthropic", "claude-opus-4-8", api_key="not-needed")
            with patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value=dict(quality_cfg),
            ):
                prov, model, base_url, _ak, _am = _resolve_task_provider_model(
                    task="compression"
                )
            assert prov == "auto", f"expected auto, got {prov!r}"
            assert base_url is None, f"exo base_url must be dropped, got {base_url!r}"
            # No fallback_model → model cleared → provider-default (Sonnet) applies
            # downstream in resolve_provider_client.
            assert model is None, (
                f"without fallback_model the model must be cleared, got {model!r}"
            )
        finally:
            clear_runtime_main()

    def test_haiku_fallback_client_does_not_carry_1m_beta_e2e(self):
        """Regression for the HTTP 400 "long context beta is not yet available
        for this subscription" bug (2026-06-22).

        A cheap aux task with fallback_model=Haiku resolves to
        provider='auto', model='claude-haiku-4-5...' on an anthropic-main
        session. The bug: _resolve_auto Step 1 built the Anthropic SDK client
        for the per-provider aux DEFAULT (Sonnet, 1M-capable), which baked the
        context-1m-2025-08-07 beta into the client's default headers; the
        request was then sent as Haiku (no 1M tier) → Anthropic 400.

        The fix threads the caller's per-task model through _resolve_auto →
        _try_anthropic → build_anthropic_client so the client is built for the
        model that actually serves the request. This exercises the REAL
        resolve_provider_client + real build_anthropic_client path (only the
        SDK constructor and token are stubbed) and asserts the baked betas
        match the request model: Haiku → no context-1m, Sonnet → context-1m.

        All 8 cheap fallback_model tasks share this single code path, so this
        one test guards the whole class of them.
        """
        from agent.auxiliary_client import (
            set_runtime_main,
            clear_runtime_main,
            resolve_provider_client,
        )
        from agent.anthropic_adapter import _CONTEXT_1M_BETA

        captured: list = []

        def fake_build(api_key, base_url=None, timeout=None, *, drop_context_1m_beta=False, model=None):
            # Reproduce the real beta-baking decision without an SDK/network call.
            from agent.anthropic_adapter import _common_betas_for_base_url
            betas = _common_betas_for_base_url(
                base_url, drop_context_1m_beta=drop_context_1m_beta, model=model,
            )
            captured.append({"model": model, "betas": list(betas)})
            return MagicMock()

        real_token = "***" + "x" * 88

        try:
            set_runtime_main("anthropic", "claude-opus-4-8", api_key="not-needed")
            with patch(
                "agent.auxiliary_client._select_pool_entry",
                return_value=(False, None),
            ), patch(
                "agent.anthropic_adapter.resolve_anthropic_token",
                return_value=real_token,
            ), patch(
                "agent.anthropic_adapter.build_anthropic_client",
                side_effect=fake_build,
            ):
                # Cheap task path: explicit Haiku model (what fallback_model
                # produces). Client MUST be built for Haiku and MUST NOT carry
                # the context-1m beta.
                captured.clear()
                _client, resolved = resolve_provider_client(
                    "auto", "claude-haiku-4-5-20251001",
                )
                assert resolved == "claude-haiku-4-5-20251001", (
                    f"expected Haiku to serve the request, got {resolved!r}"
                )
                assert captured, "build_anthropic_client was never called"
                haiku_build = captured[-1]
                assert haiku_build["model"] == "claude-haiku-4-5-20251001", (
                    "client must be built for the request model (Haiku), got "
                    f"{haiku_build['model']!r} — the model/client mismatch is the bug"
                )
                assert _CONTEXT_1M_BETA not in haiku_build["betas"], (
                    f"Haiku client must NOT carry {_CONTEXT_1M_BETA!r} (no 1M tier) — "
                    f"this is the HTTP 400 cause. Got betas={haiku_build['betas']}"
                )

                # Quality task path: no explicit model → provider-default Sonnet,
                # which DOES have a 1M tier and must keep the beta.
                captured.clear()
                _client2, resolved2 = resolve_provider_client("auto", None)
                assert resolved2 == "claude-sonnet-5", (
                    f"expected Sonnet aux default, got {resolved2!r}"
                )
                sonnet_build = captured[-1]
                assert sonnet_build["model"] == "claude-sonnet-5"
                assert _CONTEXT_1M_BETA in sonnet_build["betas"], (
                    f"Sonnet client must keep {_CONTEXT_1M_BETA!r} (has 1M tier). "
                    f"Got betas={sonnet_build['betas']}"
                )
        finally:
            clear_runtime_main()


class TestProviderScopedFallbackModels:
    """Provider-scoped ``auxiliary.<task>.fallback_models`` map (fork 2026-06-24).

    The map keys a main-provider id to the aux model used when the exo pin is
    dropped. Resolution on drop: scoped entry → legacy scalar → cleared.
    """

    EXO_URL = "http://192.168.86.201:52415/v1"
    QWEN = "mlx-community/Qwen3.6-35B-A3B-8bit"

    def _cfg(self, **extra):
        base = {
            "provider": "exo",
            "base_url": self.EXO_URL,
            "api_key": "x",
            "model": self.QWEN,
        }
        base.update(extra)
        return base

    def _resolve_with_main(self, main_provider, cfg, task="title_generation"):
        from agent.auxiliary_client import (
            set_runtime_main,
            clear_runtime_main,
            _resolve_task_provider_model,
        )
        try:
            if main_provider == "exo":
                set_runtime_main("exo", self.QWEN, api_key="not-needed", base_url=self.EXO_URL)
            else:
                set_runtime_main(main_provider, "x-main-model", api_key="not-needed")
            with patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value=dict(cfg),
            ):
                return _resolve_task_provider_model(task=task)
        finally:
            clear_runtime_main()

    def test_scoped_map_selects_per_main_provider(self):
        """Same task, different main providers → different aux models."""
        cfg = self._cfg(fallback_models={
            "anthropic": "claude-sonnet-4-6",
            "openrouter": "anthropic/claude-3.5-haiku",
        })
        _p, model, base_url, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert base_url is None and model == "claude-sonnet-4-6", (
            f"anthropic-main must use scoped anthropic model, got {model!r}"
        )
        _p, model, _bu, _ak, _am = self._resolve_with_main("openrouter", cfg)
        assert model == "anthropic/claude-3.5-haiku", (
            f"openrouter-main must use scoped openrouter model, got {model!r}"
        )

    def test_scoped_map_wins_over_legacy_scalar(self):
        """When both present, the provider-scoped entry takes precedence."""
        cfg = self._cfg(
            fallback_model="claude-haiku-4-5-20251001",
            fallback_models={"anthropic": "claude-sonnet-4-6"},
        )
        _p, model, _bu, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert model == "claude-sonnet-4-6", (
            f"scoped entry must win over scalar, got {model!r}"
        )

    def test_falls_back_to_scalar_when_provider_not_in_map(self):
        """Main provider absent from map → legacy scalar applies."""
        cfg = self._cfg(
            fallback_model="claude-haiku-4-5-20251001",
            fallback_models={"openrouter": "anthropic/claude-3.5-haiku"},
        )
        _p, model, _bu, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert model == "claude-haiku-4-5-20251001", (
            f"missing scoped entry must fall back to scalar, got {model!r}"
        )

    def test_no_match_no_scalar_clears_to_provider_default(self):
        """Neither scoped entry nor scalar → model cleared (provider default)."""
        cfg = self._cfg(fallback_models={"openrouter": "x/y"})
        prov, model, base_url, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert prov == "auto" and base_url is None and model is None, (
            f"no match + no scalar must clear model, got prov={prov!r} model={model!r}"
        )

    def test_exo_main_ignores_scoped_map_and_keeps_pin(self):
        """exo-main keeps the exo pin regardless of fallback_models."""
        cfg = self._cfg(fallback_models={"anthropic": "claude-sonnet-4-6"})
        prov, model, base_url, _ak, _am = self._resolve_with_main("exo", cfg)
        assert (prov == "exo" or base_url == self.EXO_URL) and model == self.QWEN, (
            f"exo-main must keep the exo pin, got prov={prov!r} model={model!r}"
        )

    def test_scoped_key_match_is_case_insensitive(self):
        """Map keys match the main provider id case-insensitively."""
        cfg = self._cfg(fallback_models={"Anthropic": "claude-sonnet-4-6"})
        _p, model, _bu, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert model == "claude-sonnet-4-6", (
            f"scoped key match must be case-insensitive, got {model!r}"
        )

    def test_malformed_map_falls_back_to_scalar(self):
        """A non-dict fallback_models is ignored; scalar still applies."""
        cfg = self._cfg(
            fallback_model="claude-haiku-4-5-20251001",
            fallback_models="not-a-dict",
        )
        _p, model, _bu, _ak, _am = self._resolve_with_main("anthropic", cfg)
        assert model == "claude-haiku-4-5-20251001", (
            f"malformed map must fall back to scalar, got {model!r}"
        )