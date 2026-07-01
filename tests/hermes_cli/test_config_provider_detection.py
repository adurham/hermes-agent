"""Regression: bare ``/model <name>`` resolves user-declared provider models.

Root cause (2026-06-30): ``detect_provider_for_model`` consulted only static +
OpenRouter catalogs, never the user's ``providers.<name>.models`` config. So a
cold-start session on Anthropic that switched to an exo/ollama model via bare
``/model <name>`` (no ``--provider``) kept provider='anthropic'. That mislabel
cascaded into auxiliary routing — a Claude model name shipped to the exo
endpoint → recurring 404 on title_generation.

Fix: ``_detect_config_provider_for_model`` matches the model against configured
providers first, so bare-switch behaves like the explicit ``--provider`` form.
"""
import pytest

from hermes_cli import models as m


@pytest.fixture
def cfg_with_providers(monkeypatch):
    fake_cfg = {
        "model": {"provider": "anthropic", "default": "claude-opus-4-8"},
        "providers": {
            "exo": {
                "base_url": "http://192.168.86.201:52415/v1",
                "models": {  # dict form
                    "mlx-community/DeepSeek-V4-Flash": {"context_length": 1048576},
                    "mlx-community/Qwen3.6-35B-A3B-8bit": {"context_length": 262144},
                },
            },
            "ollama-launch": {
                "api": "http://127.0.0.1:11434/v1",
                "models": ["glm-5.2:cloud", "deepseek-v4-flash:cloud"],  # list form
            },
        },
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda *a, **k: fake_cfg, raising=False
    )
    return fake_cfg


class TestConfigProviderDetection:
    def test_exo_model_dict_form_from_anthropic(self, cfg_with_providers):
        got = m.detect_provider_for_model(
            "mlx-community/DeepSeek-V4-Flash", "anthropic"
        )
        assert got == ("exo", "mlx-community/DeepSeek-V4-Flash")

    def test_ollama_cloud_model_list_form_from_anthropic(self, cfg_with_providers):
        got = m.detect_provider_for_model("deepseek-v4-flash:cloud", "anthropic")
        assert got == ("ollama-launch", "deepseek-v4-flash:cloud")

    def test_case_insensitive_match(self, cfg_with_providers):
        got = m.detect_provider_for_model(
            "MLX-Community/DeepSeek-V4-Flash", "anthropic"
        )
        assert got == ("exo", "mlx-community/DeepSeek-V4-Flash")

    def test_no_switch_when_already_on_that_provider(self, cfg_with_providers):
        # Already on exo → config match must not fire (no no-op switch); the
        # downstream catalog logic handles same-provider.
        got = m._detect_config_provider_for_model(
            "mlx-community/DeepSeek-V4-Flash", "exo"
        )
        assert got is None

    def test_unknown_model_returns_none(self, cfg_with_providers):
        assert (
            m._detect_config_provider_for_model("totally-made-up-model", "anthropic")
            is None
        )

    def test_helper_handles_missing_providers_gracefully(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda *a, **k: {}, raising=False
        )
        assert m._detect_config_provider_for_model("anything", "anthropic") is None
