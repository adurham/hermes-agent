"""``LEFT_CORE`` rows for a model provider that moved into a catalog plugin.

Real config/.env/auth.json/jobs.json files under a temp home. The generic seams (``providers``,
``profile_env``, ``notice``) run through a fake row; the GitHub Copilot row's predicate runs through its
own function, since which config slots count as "this home uses it" is that row's contract.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import hermes_cli.left_core_migration as lcm

ACME = lcm.LeftCoreFeature(
    plugin="acme-llm", label="Acme LLM", unchanged="your ACME_KEY is unchanged",
    in_use=lambda home, **kw: "acme" in lcm.providers_selected(home, ("acme", "acme-acp"), **kw),
    providers=("acme", "acme-acp"), profile_env=("ACME_CLI_PATH",),
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(lcm, "_attempted", set())
    monkeypatch.setattr(lcm, "_undelivered", {})
    for name in ("HERMES_INFERENCE_PROVIDER", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "ACME_CLI_PATH"):
        monkeypatch.delenv(name, raising=False)
    import hermes_cli.memory_provider_migration as mpm
    monkeypatch.setattr(mpm, "catalog_source", lambda name: name)  # never the network


def _home(tmp_path: Path, *, env: str = "", config: str = "", auth: dict | None = None,
          jobs: list | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    if env:
        (home / ".env").write_text(env, encoding="utf-8")
    if config:
        (home / "config.yaml").write_text(config, encoding="utf-8")
    if auth is not None:
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    if jobs is not None:
        (home / "cron").mkdir()
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return home


@pytest.mark.parametrize(("config", "env", "auth", "jobs"), [
    ("model:\n  provider: copilot\n  default: gpt-5.4\n", "", None, None),
    ("model:\n  provider: GitHub-Copilot\n", "", None, None),
    ("model:\n  provider: copilot-acp\n", "", None, None),
    ("fallback_providers:\n  - provider: copilot\n    model: gpt-5.4\n", "", None, None),
    ("fallback_model:\n  provider: github-models\n  model: gpt-4.1\n", "", None, None),
    ("auxiliary:\n  vision:\n    provider: copilot\n", "", None, None),
    ("delegation:\n  provider: copilot-acp\n", "", None, None),
    ("delegation:\n  fallback_providers:\n    - provider: copilot\n      model: gpt-5.4\n", "", None, None),
    ("moa:\n  presets:\n    mine:\n      reference_models:\n        - provider: copilot\n          model: gpt-5.4\n"
     "      aggregator:\n        provider: nous\n        model: hermes-4\n", "", None, None),
    ("", "HERMES_INFERENCE_PROVIDER=copilot\n", None, None),
    ("", "", {"active_provider": "copilot"}, None),
    ("", "", None, [{"id": "j1", "provider": "copilot", "model": "gpt-5.4"}]),
    ("cron:\n  model: gpt-5.4\n  model_provider: copilot\n", "", None, None),
    ("delegation:\n  backend: model_map\n  backends:\n    model_map:\n      options:\n        - name: fast\n"
     "          provider: copilot\n          model: gpt-5.4\n", "", None, None),
    ("delegation:\n  backends:\n    model_map:\n      options:\n        - name: deep\n          provider: nous\n"
     "          fallback_providers:\n            - provider: github-copilot\n              model: gpt-5.4\n",
     "", None, None),
])
def test_copilot_in_use_is_any_provider_slot_naming_it(tmp_path, config, env, auth, jobs):
    assert lcm.copilot_in_use(_home(tmp_path, config=config, env=env, auth=auth, jobs=jobs)) is True


@pytest.mark.parametrize(("config", "env", "auth"), [
    ("model:\n  provider: nous\n", "GH_TOKEN=gho_x\nCOPILOT_GITHUB_TOKEN=gho_y\n", None),
    ("model:\n  provider: openrouter\n  default: github/copilot-like\n", "", None),
    ("", "", {"credential_pool": {"copilot": [{"source": "gh_cli", "access_token": "t"}]}}),
    ("model: copilot\n", "", None),  # a bare model string names a model, not a provider
])
def test_copilot_credentials_alone_are_not_use(tmp_path, config, env, auth):
    assert lcm.copilot_in_use(_home(tmp_path, config=config, env=env, auth=auth)) is False


def test_process_env_provider_counts_only_for_the_launch_home(tmp_path, monkeypatch):
    home = _home(tmp_path, config="model:\n  provider: nous\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "copilot")
    assert lcm.copilot_in_use(home) is False
    assert lcm.copilot_in_use(home, process_env=True) is True


_GH_ONLY = {"credential_pool": {"copilot": [{"source": "gh_cli", "label": "gh auth token", "access_token": "t"}]}}


@pytest.mark.parametrize(("config", "env", "auth", "notice"), [
    ("model:\n  provider: copilot\n", "", _GH_ONLY, True),
    ("model:\n  provider: copilot\n", "", None, True),
    ("model:\n  provider: copilot\n", "GITHUB_TOKEN=gho_x\n", _GH_ONLY, False),
    ("model:\n  provider: copilot\n", "", {"credential_pool": {"copilot": [{"source": "manual"}]}}, False),
    ("model:\n  provider: copilot\n", "", {"credential_pool": {"copilot": [{"source": "manual:device_code"}]}},
     False),
    ("model:\n  provider: copilot-acp\n", "", None, False),  # the Copilot CLI keeps its own login
    ("model:\n  provider: nous\n", "", _GH_ONLY, False),
])
def test_relogin_notice_only_for_a_copilot_home_without_a_token_the_plugin_reads(tmp_path, config, env, auth,
                                                                                  notice):
    text = lcm.copilot_relogin_notice(_home(tmp_path, config=config, env=env, auth=auth))
    assert ("hermes auth add copilot" in text) is notice


def test_gh_only_home_is_told_to_log_in_on_every_migration_outcome(tmp_path):
    home = _home(tmp_path, config="model:\n  provider: copilot\n", auth=_GH_ONLY)
    said = []

    def install(name):
        (home / "plugins" / name).mkdir(parents=True)
        return {"ok": True}

    assert lcm.migrate_home(home, install=install, say=said.append) == ["github-copilot"]
    assert "✓ GitHub Copilot moved out of core" in said[0] and "run `hermes auth add copilot`" in said[0]
    other = _home(tmp_path / "b", config="model:\n  provider: copilot\n", auth=_GH_ONLY)
    said.clear()
    lcm.migrate_home(other, install=lambda name: {"ok": False, "error": "network down"}, say=said.append)
    assert "could not be installed automatically" in said[0] and "run `hermes auth add copilot`" in said[0]


# ── generic seams through a fake row ─────────────────────────────────────────────────────────────


@pytest.fixture
def acme_row(monkeypatch, tmp_path):
    monkeypatch.setattr(lcm, "LEFT_CORE", (ACME,))
    home = _home(tmp_path, config="model:\n  provider: acme\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: None)
    return home


def test_unknown_left_core_provider_names_the_install_on_every_surface(acme_row):
    from hermes_cli.auth import AuthError, resolve_provider
    from hermes_cli.auth_commands import _unknown_provider_exit
    from hermes_cli.model_switch import _unknown_provider_message

    with pytest.raises(AuthError) as err:
        resolve_provider("acme-acp")
    for text in (str(err.value), str(_unknown_provider_exit("acme")), _unknown_provider_message("ACME")):
        assert "Acme LLM moved out of core into the 'acme-llm' plugin" in text
        assert "hermes plugins install acme-llm" in text
    (acme_row / "plugins" / "acme-llm").mkdir(parents=True)  # installed but not loaded
    assert "hermes plugins enable acme-llm" in lcm.provider_install_hint("acme")
    assert lcm.provider_install_hint("other") == ""


def test_runtime_resolution_installs_the_plugin_first_for_a_home_using_it(acme_row, monkeypatch):
    calls = []
    monkeypatch.setattr(lcm, "recover_at_startup", lambda **kw: calls.append(kw) or [])
    lcm.recover_for_provider("acme")
    assert calls == [{}]
    lcm.recover_for_provider("openrouter")
    (acme_row / "config.yaml").write_text("model:\n  provider: nous\n", encoding="utf-8")
    lcm.recover_for_provider("acme")  # requested, but this home never chose it: no install
    assert len(calls) == 1
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: object())
    (acme_row / "config.yaml").write_text("model:\n  provider: acme\n", encoding="utf-8")
    lcm.recover_for_provider("acme")  # another plugin already serves it
    assert len(calls) == 1


def test_profile_env_is_refused_by_env_writers_only_while_the_plugin_is_absent(acme_row, monkeypatch):
    from hermes_cli.config import validate_env_var_name_for_write
    import providers
    monkeypatch.setattr(providers, "list_providers", list)
    with pytest.raises(ValueError, match="denylist"):
        validate_env_var_name_for_write("ACME_CLI_PATH")
    (acme_row / "plugins" / "acme-llm").mkdir(parents=True)
    validate_env_var_name_for_write("ACME_CLI_PATH")


def test_models_dev_never_stands_in_for_a_provider_whose_plugin_left_core(acme_row, monkeypatch):
    """models.dev lists the vendor's endpoint under the same id: resolving it there would send the user's
    raw token to that endpoint through a generic client the plugin never was."""
    from types import SimpleNamespace
    import hermes_cli.providers as hp
    monkeypatch.setattr(hp, "_models_dev_info", lambda name, allow_network=True: SimpleNamespace(
        env=("ACME_TOKEN",), name="Acme", api="https://api.acme.test", doc="") if name == "acme" else None)
    assert hp.get_provider("acme") is None
    assert hp.get_provider("acme-acp") is None
    import providers
    monkeypatch.setattr(providers, "get_provider_profile", lambda name: object())  # the plugin is loaded
    assert hp.get_provider("acme").base_url == "https://api.acme.test"


def test_a_row_without_notice_adds_nothing(tmp_path):
    feature = replace(ACME, notice=lcm._no_notice)
    assert lcm._noticed("x", tmp_path, feature, False) == "x"
