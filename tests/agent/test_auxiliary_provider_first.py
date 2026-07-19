"""Provider-first ``auxiliary`` schema — fork feature (2026-06-24).

The ``auxiliary`` config block supports two interchangeable shapes:

  * **task-first** (legacy / upstream): top-level keys are task names whose
    values are flat routing dicts (``{provider, model, base_url, …}``).
  * **provider-first** (this fork): top-level keys are provider ids + a
    ``defaults`` block. The model a task uses is selected from the provider
    block matching the active *main* provider, with a per-block ``default``
    model for unlisted tasks and a shared ``defaults`` block for
    provider-independent per-task settings (timeout, extra_body, …).

These tests assert the *invariant* that both schemas resolve to the same
``(provider, model, base_url, api_mode, timeout)`` for every task given the
same main provider — they are NOT change-detector snapshots of a model list.
Everything runs against a real ``load_config()`` + temp ``HERMES_HOME`` so the
DEFAULT_CONFIG deep-merge (which injects task-key pollution on top of a
provider-first user config) is exercised for real, not mocked away.
"""

from __future__ import annotations

import textwrap

import pytest

from agent import auxiliary_client as ac


ALL_TASKS = [
    "vision", "web_extract", "compression", "skills_hub", "approval", "mcp",
    "title_generation", "tts_audio_tags", "triage_specifier",
    "kanban_decomposer", "profile_describer", "curator", "monitor",
    "session_search", "memory_extraction",
]

# Adam's real-world intent: exo cluster when main==exo (compression on a
# dedicated model), cheap Haiku on anthropic-main with the heavier
# reasoning tasks bumped to Sonnet.
_EXO_BASE = "http://192.168.86.201:52415/v1"
_QWEN = "mlx-community/Qwen3.6-35B-A3B-8bit"
_DEEPSEEK = "mlx-community/DeepSeek-V4-Flash"
_SONNET_TASKS = {"vision", "compression", "curator", "memory_extraction"}

PROVIDER_FIRST_CONFIG = textwrap.dedent(f"""\
model:
  provider: anthropic
  model: claude-opus-4-8
providers:
  exo:
    base_url: {_EXO_BASE}
auxiliary:
  defaults:
    vision: {{timeout: 120, download_timeout: 30}}
    web_extract: {{timeout: 360}}
    compression: {{timeout: 120}}
    skills_hub: {{timeout: 180}}
    approval: {{timeout: 180}}
    mcp: {{timeout: 180}}
    title_generation: {{timeout: 180, language: ''}}
    tts_audio_tags: {{timeout: 30}}
    triage_specifier: {{timeout: 120}}
    kanban_decomposer: {{timeout: 180}}
    profile_describer: {{timeout: 60}}
    curator: {{timeout: 600}}
    monitor: {{timeout: 60}}
    session_search: {{timeout: 180, max_concurrency: 3}}
    memory_extraction: {{timeout: 180, max_tokens_per_turn: 1024}}
  exo:
    provider: custom:exo
    base_url: {_EXO_BASE}
    api_key: not-needed
    api_mode: chat_completions
    default: {_QWEN}
    compression: {_DEEPSEEK}
  anthropic:
    default: claude-haiku-4-5
    vision: claude-sonnet-4-6
    compression: claude-sonnet-4-6
    curator: claude-sonnet-4-6
    memory_extraction: claude-sonnet-4-6
""")


@pytest.fixture
def hermes_home_pf(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(PROVIDER_FIRST_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Bust the path-keyed load_config cache so a prior test's config can't leak.
    # (Each test uses a fresh tmp_path so collisions are unlikely, but clearing
    # is cheap insurance.)
    try:
        from hermes_cli import config as _cfg
        if hasattr(_cfg, "_LOAD_CONFIG_CACHE"):
            _cfg._LOAD_CONFIG_CACHE.clear()
        if hasattr(_cfg, "_RAW_CONFIG_CACHE"):
            _cfg._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass
    yield tmp_path
    ac.clear_runtime_main()


def _resolve_all():
    out = {}
    for t in ALL_TASKS:
        prov, model, base_url, _key, api_mode = ac._resolve_task_provider_model(t)
        out[t] = {
            "provider": prov, "model": model,
            "base_url": base_url, "api_mode": api_mode,
        }
    return out


# ── Schema detection ──────────────────────────────────────────────────

def test_detector_provider_first_with_defaults_key():
    assert ac._aux_schema_is_provider_first({"defaults": {}, "anthropic": {}})


def test_detector_provider_first_survives_task_key_pollution():
    """The DEFAULT_CONFIG merge injects task keys on top of a provider-first
    config — detection must still classify it provider-first."""
    polluted = {
        "anthropic": {"default": "claude-haiku-4-5"},
        "exo": {"provider": "custom:exo"},
        # Pollution injected by the deep-merge against task-first DEFAULT_CONFIG:
        "vision": {"provider": "auto", "model": ""},
        "compression": {"provider": "auto", "model": ""},
    }
    assert ac._aux_schema_is_provider_first(polluted)


def test_detector_task_first_legacy():
    legacy = {
        "vision": {"provider": "auto", "model": ""},
        "compression": {"provider": "auto", "model": "claude-sonnet-4-6"},
    }
    assert not ac._aux_schema_is_provider_first(legacy)


def test_detector_empty_is_task_first():
    assert not ac._aux_schema_is_provider_first({})


# ── Resolution equivalence (the core invariant) ───────────────────────

def test_anthropic_main_models(hermes_home_pf):
    ac.set_runtime_main("anthropic", "claude-opus-4-8")
    resolved = _resolve_all()
    for task in ALL_TASKS:
        expected = "claude-sonnet-4-6" if task in _SONNET_TASKS else "claude-haiku-4-5"
        assert resolved[task]["model"] == expected, (task, resolved[task])
        # Model-only anthropic block defers to the main-provider auto path.
        assert resolved[task]["provider"] == "auto", (task, resolved[task])


def test_exo_main_models(hermes_home_pf):
    ac.set_runtime_main(
        "custom:exo", _DEEPSEEK,
        base_url=_EXO_BASE, api_key="not-needed", api_mode="chat_completions",
    )
    resolved = _resolve_all()
    for task in ALL_TASKS:
        expected = _DEEPSEEK if task == "compression" else _QWEN
        assert resolved[task]["model"] == expected, (task, resolved[task])
        assert resolved[task]["provider"] == "custom", (task, resolved[task])
        assert resolved[task]["base_url"] == _EXO_BASE, (task, resolved[task])
        assert resolved[task]["api_mode"] == "chat_completions", (task, resolved[task])


def test_per_task_timeouts_preserved_via_defaults(hermes_home_pf):
    """Per-task settings live in the shared ``defaults`` block and must survive
    regardless of which provider serves the model."""
    ac.set_runtime_main("anthropic", "claude-opus-4-8")
    assert ac._get_task_timeout("curator") == 600.0
    assert ac._get_task_timeout("web_extract") == 360.0
    assert ac._get_task_timeout("vision") == 120.0
    assert ac._get_task_timeout("monitor") == 60.0


def test_unlisted_task_uses_block_default(hermes_home_pf):
    """A task with no explicit entry in a provider block falls back to that
    block's ``default`` model (the 'assume from main aux config' rule)."""
    ac.set_runtime_main("anthropic", "claude-opus-4-8")
    # 'mcp' is not explicitly listed under anthropic → block default.
    _p, model, _b, _k, _m = ac._resolve_task_provider_model("mcp")
    assert model == "claude-haiku-4-5"


def test_flatten_unit_anthropic_block():
    """Unit-level flatten: model-only block emits provider=auto + model."""
    aux = {
        "defaults": {"vision": {"timeout": 120}},
        "anthropic": {"default": "claude-haiku-4-5", "vision": "claude-sonnet-4-6"},
        "exo": {"provider": "custom:exo", "base_url": _EXO_BASE, "default": _QWEN},
    }
    flat = ac._aux_flatten_provider_first("vision", aux, "anthropic", None)
    assert flat["provider"] == "auto"
    assert flat["model"] == "claude-sonnet-4-6"
    assert flat["timeout"] == 120


def test_flatten_unit_exo_block_routing():
    """Unit-level flatten: block with base_url emits explicit custom endpoint."""
    aux = {
        "exo": {
            "provider": "custom:exo", "base_url": _EXO_BASE,
            "api_key": "not-needed", "api_mode": "chat_completions",
            "default": _QWEN, "compression": _DEEPSEEK,
        },
    }
    flat = ac._aux_flatten_provider_first("compression", aux, "custom:exo", None)
    assert flat["provider"] == "custom:exo"
    assert flat["model"] == _DEEPSEEK
    assert flat["base_url"] == _EXO_BASE
    assert flat["api_mode"] == "chat_completions"


# ── Migration converter (task-first → provider-first) ─────────────────

def test_converter_collapses_to_provider_blocks():
    from hermes_cli.config import convert_auxiliary_to_provider_first
    task_first = {
        "vision": {
            "provider": "custom:exo", "model": _QWEN, "base_url": _EXO_BASE,
            "api_key": "not-needed", "api_mode": "chat_completions",
            "timeout": 120, "fallback_models": {"anthropic": "claude-sonnet-4-6"},
        },
        "compression": {
            "provider": "custom:exo", "model": _DEEPSEEK, "base_url": _EXO_BASE,
            "api_key": "not-needed", "api_mode": "chat_completions",
            "timeout": 120, "fallback_models": {"anthropic": "claude-sonnet-4-6"},
        },
        "mcp": {
            "provider": "custom:exo", "model": _QWEN, "base_url": _EXO_BASE,
            "api_key": "not-needed", "api_mode": "chat_completions",
            "timeout": 180, "fallback_models": {"anthropic": "claude-haiku-4-5"},
        },
    }
    out = convert_auxiliary_to_provider_first(task_first)
    # exo block: Qwen is the most-common model → block default; DeepSeek override.
    assert out["exo"]["provider"] == "custom:exo"
    assert out["exo"]["base_url"] == _EXO_BASE
    assert out["exo"]["default"] == _QWEN
    assert out["exo"]["compression"] == _DEEPSEEK
    assert "vision" not in out["exo"]  # equals default → not restated
    # anthropic block from fallback_models: sonnet is most-common (2 of 3) →
    # block default; mcp=haiku is the lone override. vision/compression equal
    # the default and are not restated.
    assert out["anthropic"]["default"] == "claude-sonnet-4-6"
    assert out["anthropic"]["mcp"] == "claude-haiku-4-5"
    assert "vision" not in out["anthropic"]
    assert "compression" not in out["anthropic"]
    # per-task settings moved to defaults.
    assert out["defaults"]["vision"]["timeout"] == 120
    assert out["defaults"]["mcp"]["timeout"] == 180


def test_converter_idempotent():
    from hermes_cli.config import convert_auxiliary_to_provider_first
    provider_first = {
        "defaults": {"vision": {"timeout": 120}},
        "exo": {"provider": "custom:exo", "default": _QWEN},
        "anthropic": {"default": "claude-haiku-4-5"},
    }
    assert convert_auxiliary_to_provider_first(dict(provider_first)) == provider_first


# ── save_config pollution stripping ──────────────────────────────────

def test_save_strips_provider_first_task_pollution():
    """A provider-first config carrying DEFAULT_CONFIG task-key pollution must
    be cleaned on save — no write path may persist stale auxiliary.<task>."""
    from hermes_cli.config import _strip_provider_first_aux_pollution
    polluted = {
        "auxiliary": {
            "defaults": {"vision": {"timeout": 120}},
            "exo": {"provider": "custom:exo", "default": _QWEN},
            "anthropic": {"default": "claude-haiku-4-5", "vision": "claude-sonnet-4-6"},
            # Pollution injected by the DEFAULT_CONFIG deep-merge:
            "vision": {"provider": "auto", "model": ""},
            "compression": {"provider": "auto", "model": ""},
            "mcp": {"provider": "auto", "model": ""},
        }
    }
    out = _strip_provider_first_aux_pollution(polluted)
    assert sorted(out["auxiliary"].keys()) == ["anthropic", "defaults", "exo"]


def test_save_strip_noop_on_task_first():
    """A genuine task-first config must be left untouched (not detected as
    provider-first, so its task blocks survive)."""
    from hermes_cli.config import _strip_provider_first_aux_pollution
    task_first = {
        "auxiliary": {
            "vision": {"provider": "auto", "model": "", "timeout": 120},
            "compression": {"provider": "auto", "model": "claude-sonnet-4-6"},
        }
    }
    before = {k: dict(v) for k, v in task_first["auxiliary"].items()}
    out = _strip_provider_first_aux_pollution(task_first)
    assert out["auxiliary"] == before


def test_first_migrate_to_v31_leaves_no_pollution(tmp_path, monkeypatch):
    """A single migrate of a fresh v<31 task-first config must produce a clean
    provider-first config — no leftover task-key pollution (regression for the
    DEFAULT_CONFIG re-merge that the first migrate originally persisted)."""
    import textwrap as _tw
    cfg = _tw.dedent(f"""\
        _config_version: 29
        model:
          provider: anthropic
          model: claude-opus-4-8
        providers:
          exo:
            base_url: {_EXO_BASE}
        auxiliary:
          vision:
            provider: custom:exo
            model: {_QWEN}
            base_url: {_EXO_BASE}
            api_key: not-needed
            api_mode: chat_completions
            timeout: 120
            fallback_models: {{anthropic: claude-sonnet-4-6}}
          compression:
            provider: custom:exo
            model: {_DEEPSEEK}
            base_url: {_EXO_BASE}
            api_key: not-needed
            api_mode: chat_completions
            timeout: 120
            fallback_models: {{anthropic: claude-sonnet-4-6}}
          mcp:
            provider: custom:exo
            model: {_QWEN}
            base_url: {_EXO_BASE}
            api_key: not-needed
            api_mode: chat_completions
            timeout: 30
            fallback_models: {{anthropic: claude-haiku-4-5}}
    """)
    (tmp_path / "config.yaml").write_text(cfg)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        from hermes_cli import config as _cfg
        if hasattr(_cfg, "_LOAD_CONFIG_CACHE"):
            _cfg._LOAD_CONFIG_CACHE.clear()
        if hasattr(_cfg, "_RAW_CONFIG_CACHE"):
            _cfg._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass

    from hermes_cli.config import migrate_config, read_raw_config
    migrate_config(interactive=False, quiet=True)
    aux = read_raw_config().get("auxiliary", {})
    assert sorted(aux.keys()) == ["anthropic", "defaults", "exo"], sorted(aux.keys())
    # conversion correctness
    assert aux["exo"]["default"] == _QWEN
    assert aux["exo"]["compression"] == _DEEPSEEK
    assert aux["anthropic"]["default"] == "claude-sonnet-4-6"
    assert aux["anthropic"]["mcp"] == "claude-haiku-4-5"


# ── Explicit top-level task pins (fork fix, 2026-07-11) ───────────────

CONSULT_PIN_CONFIG = PROVIDER_FIRST_CONFIG + (
    "  consult:\n"
    "    provider: anthropic\n"
    "    model: claude-fable-5\n"
    "    timeout: 300\n"
)


@pytest.fixture
def hermes_home_consult_pin(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONSULT_PIN_CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        from hermes_cli import config as _cfg
        if hasattr(_cfg, "_LOAD_CONFIG_CACHE"):
            _cfg._LOAD_CONFIG_CACHE.clear()
        if hasattr(_cfg, "_RAW_CONFIG_CACHE"):
            _cfg._RAW_CONFIG_CACHE.clear()
    except Exception:
        pass
    yield tmp_path
    ac.clear_runtime_main()


def test_task_pin_wins_over_provider_first_exo_main(hermes_home_consult_pin):
    """An explicit ``auxiliary.consult`` pin must be honored even in a
    provider-first schema — previously it was parsed as a provider block
    named "consult" that nothing selected, so consult silently resolved to
    the exo block's default model (Qwen answering as the "Fable" consult)."""
    ac.set_runtime_main(
        "custom:exo", _DEEPSEEK,
        base_url=_EXO_BASE, api_key="not-needed", api_mode="chat_completions",
    )
    prov, model, base_url, _key, _mode = ac._resolve_task_provider_model("consult")
    assert prov == "anthropic", (prov, model)
    assert model == "claude-fable-5", (prov, model)
    # The exo block's base_url must NOT leak under the pin's provider —
    # a leaked base_url would force the provider to "custom" downstream.
    assert not base_url, base_url
    assert ac._get_task_timeout("consult") == 300.0


def test_task_pin_wins_over_provider_first_anthropic_main(hermes_home_consult_pin):
    ac.set_runtime_main("anthropic", "claude-opus-4-8")
    prov, model, _b, _k, _m = ac._resolve_task_provider_model("consult")
    assert prov == "anthropic", (prov, model)
    assert model == "claude-fable-5", (prov, model)


def test_pollution_task_key_is_not_a_pin(hermes_home_pf):
    """The deep-merge's inert ``{provider: auto, model: ''}`` task keys must
    NOT be treated as pins — block resolution still applies."""
    ac.set_runtime_main(
        "custom:exo", _DEEPSEEK,
        base_url=_EXO_BASE, api_key="not-needed", api_mode="chat_completions",
    )
    # 'mcp' has no real pin; exo-main should still resolve it to the exo block.
    prov, model, base_url, _k, _m = ac._resolve_task_provider_model("mcp")
    assert model == _QWEN, (prov, model)
    assert base_url == _EXO_BASE, base_url
