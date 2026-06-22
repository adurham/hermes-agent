"""Regression: CLI ``/model --global`` must not leave a stale base_url behind.

The CLI's global model-switch persistence (``_persist_global_model_switch`` in
cli.py, used by both the typed ``/model`` path and the prompt_toolkit picker)
historically wrote only ``model.default`` + ``model.provider``. When switching
FROM a custom-endpoint provider (exo / ollama / custom) TO a built-in provider
(anthropic), the PREVIOUS provider's ``model.base_url`` and ``model.api_key``
were left in config.yaml. The main model still worked (Anthropic OAuth hardcodes
its own URL), but auxiliary tasks honor the literal base_url and 404'd against
the wrong box::

    ⚠ Auxiliary title generation failed:
      HTTP 404: No instance found for model claude-haiku-4-5-20251001

(the Haiku title-gen fallback was sent to the exo cluster at
192.168.86.201:52415, which only serves MLX models).

These tests drive the real persistence helper against a temp HERMES_HOME and
assert the endpoint fields are reconciled on a provider switch.
"""

from __future__ import annotations

import importlib

import yaml
import pytest


@pytest.fixture()
def cli_mod(tmp_path, monkeypatch):
    """Import cli with HERMES_HOME pointed at a temp dir, seeded config.yaml."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)

    import cli as cli_module
    importlib.reload(cli_module)
    # save_config_value resolves the path from cli._hermes_home at call time.
    monkeypatch.setattr(cli_module, "_hermes_home", hermes_home, raising=False)
    return cli_module, hermes_home


def _seed(home, model_block):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": model_block, "providers": {}}),
        encoding="utf-8",
    )


def _result(**kw):
    from hermes_cli.model_switch import ModelSwitchResult

    base = dict(success=True, new_model="claude-opus-4-8", target_provider="anthropic",
                provider_changed=True, api_key="", base_url="", api_mode="",
                provider_label="Anthropic", is_global=True)
    base.update(kw)
    return ModelSwitchResult(**base)


def test_exo_to_anthropic_clears_stale_endpoint(cli_mod):
    """The real-world bug: exo main → anthropic must drop the exo base_url and
    dummy api_key, or aux tasks 404 against the exo cluster."""
    cli_module, home = cli_mod
    _seed(home, {
        "default": "mlx-community/DeepSeek-V4-Flash",
        "provider": "exo",
        "base_url": "http://192.168.86.201:52415/v1",
        "api_key": "not-needed",
    })

    cli_module._persist_global_model_switch(_result())

    written = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["model"]
    assert written["default"] == "claude-opus-4-8"
    assert written["provider"] == "anthropic"
    # The stale exo endpoint must be gone (empty == unset for resolution).
    assert not written.get("base_url"), f"stale base_url survived: {written.get('base_url')!r}"
    assert not written.get("api_key"), f"stale api_key survived: {written.get('api_key')!r}"


def test_switch_to_custom_endpoint_persists_new_base_url(cli_mod):
    """Switching TO a custom endpoint must persist its base_url/api_key, not blank
    them — the clear is only for built-in providers."""
    cli_module, home = cli_mod
    _seed(home, {"default": "old", "provider": "anthropic"})

    cli_module._persist_global_model_switch(_result(
        new_model="some-local-model",
        target_provider="custom",
        base_url="http://127.0.0.1:8080/v1",
        api_key="sk-local",
    ))

    written = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["model"]
    assert written["default"] == "some-local-model"
    assert written["provider"] == "custom"
    assert written["base_url"] == "http://127.0.0.1:8080/v1"
    assert written["api_key"] == "sk-local"


def test_no_provider_change_leaves_endpoint_untouched(cli_mod):
    """A same-provider model bump (provider_changed=False) must NOT touch the
    endpoint fields — only the default model id changes."""
    cli_module, home = cli_mod
    _seed(home, {
        "default": "old-custom",
        "provider": "custom",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "sk-keep",
    })

    cli_module._persist_global_model_switch(_result(
        new_model="new-custom",
        target_provider="custom",
        provider_changed=False,
    ))

    written = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["model"]
    assert written["default"] == "new-custom"
    assert written["base_url"] == "http://127.0.0.1:8080/v1"
    assert written["api_key"] == "sk-keep"
