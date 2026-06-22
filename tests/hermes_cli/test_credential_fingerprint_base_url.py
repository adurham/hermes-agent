"""Regression: the provider-model cache fingerprint must track the config-level
``model.base_url`` / ``model.api_key`` endpoint override.

The /model picker caches each provider's live ``/v1/models`` catalog on disk
(``provider_models_cache.json``), keyed by provider slug and validated by a
``_credential_fingerprint(provider)``. ``provider_model_ids("anthropic")``
honors the inline ``model.base_url`` / ``model.api_key`` from config.yaml when
the main ``model.provider`` is anthropic — so it can be pointed at any
OpenAI/Anthropic-compatible endpoint.

The bug: the fingerprint hashed only PROVIDER_REGISTRY env vars + OAuth-file
mtimes, NOT those inline config fields. So a corrupted config (provider=anthropic
left pinned at an exo base_url) fetched exo's MLX catalog and cached it under the
"anthropic" key. After the base_url was later blanked on disk, the fingerprint
was UNCHANGED, the poisoned entry validated as fresh, and the picker kept showing
``mlx-community/DeepSeek-*`` under the Anthropic provider — even in brand-new
sessions — until the cache was manually wiped.

These tests drive the real fingerprint helper against a temp HERMES_HOME and
assert that changing the inline endpoint fields changes the fingerprint (busting
the stale cache), while unrelated config stays stable.
"""

from __future__ import annotations

import importlib

import yaml
import pytest


@pytest.fixture()
def models_mod(tmp_path, monkeypatch):
    """Import hermes_cli.models with HERMES_HOME at a temp dir."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: hermes_home)

    import hermes_cli.config as cfg_mod
    importlib.reload(cfg_mod)
    import hermes_cli.models as M
    importlib.reload(M)
    return M, hermes_home


def _seed(home, model_block):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": model_block, "providers": {}}),
        encoding="utf-8",
    )


def test_base_url_change_busts_anthropic_fingerprint(models_mod):
    """The real-world bug: provider=anthropic pinned at an exo base_url, then
    blanked, must yield a DIFFERENT fingerprint so the stale (exo) catalog
    cached under the anthropic key is invalidated."""
    M, home = models_mod

    _seed(home, {
        "provider": "anthropic",
        "default": "claude-opus-4-8",
        "base_url": "http://192.168.86.201:52415/v1",  # exo endpoint
        "api_key": "not-needed",
    })
    fp_poisoned = M._credential_fingerprint("anthropic")

    # The corrective edit: blank the stale endpoint fields.
    _seed(home, {
        "provider": "anthropic",
        "default": "claude-opus-4-8",
        "base_url": "",
        "api_key": "",
    })
    # config.load_config caches; clear it so the second read sees disk.
    import hermes_cli.config as cfg_mod
    if hasattr(cfg_mod, "_config_cache"):
        cfg_mod._config_cache = None
    importlib.reload(cfg_mod)
    importlib.reload(M)

    fp_clean = M._credential_fingerprint("anthropic")
    assert fp_poisoned != fp_clean, (
        "fingerprint ignored the inline model.base_url change — a stale "
        "poisoned catalog would survive the on-disk correction"
    )


def test_api_key_change_busts_fingerprint(models_mod):
    """Rotating the inline api_key for the matching provider also busts it."""
    M, home = models_mod

    _seed(home, {"provider": "anthropic", "base_url": "https://x/v1", "api_key": "key-a"})
    fp_a = M._credential_fingerprint("anthropic")

    _seed(home, {"provider": "anthropic", "base_url": "https://x/v1", "api_key": "key-b"})
    import hermes_cli.config as cfg_mod
    importlib.reload(cfg_mod)
    importlib.reload(M)
    fp_b = M._credential_fingerprint("anthropic")

    assert fp_a != fp_b


def test_inline_fields_ignored_when_provider_mismatch(models_mod):
    """The inline fields only feed provider_model_ids when model.provider
    matches the slug being fetched. A base_url under provider=exo must NOT
    perturb the anthropic fingerprint (no false cache busts for unrelated
    providers)."""
    M, home = models_mod

    _seed(home, {"provider": "exo", "base_url": "http://192.168.86.201:52415/v1"})
    fp_1 = M._credential_fingerprint("anthropic")

    _seed(home, {"provider": "exo", "base_url": "http://10.0.0.9:9999/v1"})
    import hermes_cli.config as cfg_mod
    importlib.reload(cfg_mod)
    importlib.reload(M)
    fp_2 = M._credential_fingerprint("anthropic")

    assert fp_1 == fp_2, (
        "changing an exo-scoped base_url perturbed the anthropic fingerprint; "
        "inline fields must only count under a provider match"
    )
