"""Runtime wiring of the ``LEFT_CORE`` provider rows: the real ``resolve_runtime_provider`` installs the
plugin of a provider that left core for a home that uses it, once per process, honouring the failure
backoff and ``security.allow_lazy_installs``.

Real config files under a temp ``HERMES_HOME``; the catalog lookup and the network install are stand-ins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.left_core_migration as lcm


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(lcm, "_attempted", set())
    monkeypatch.setattr(lcm, "_undelivered", {})
    for name in ("HERMES_INFERENCE_PROVIDER", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    import hermes_cli.memory_provider_migration as mpm
    monkeypatch.setattr(mpm, "catalog_source", lambda name: name)  # never the network


@pytest.fixture
def installs(monkeypatch):
    calls = []

    def fake_install(identifier, *, force, enable, catalog_name=None, assume_deps_consent=None, **kw):
        calls.append((catalog_name, assume_deps_consent))
        return {"ok": False, "error": "fatal: unable to access the catalog repo"}
    import hermes_cli.plugins_cmd as pc
    monkeypatch.setattr(pc, "dashboard_install_plugin", fake_install)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)  # the harness's bridge; config decides
    return calls


def _copilot_home(tmp_path: Path, monkeypatch, config: str = "model:\n  provider: copilot\n") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _resolve(**kw):
    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(AuthError):  # the install failed: still an unknown provider, with the hint
        resolve_runtime_provider(**kw)


def test_runtime_resolution_installs_once_per_process_and_honours_the_backoff(tmp_path, monkeypatch, installs):
    _copilot_home(tmp_path, monkeypatch)
    for _ in range(3):
        _resolve()
    assert installs == [("github-copilot", True)]
    monkeypatch.setattr(lcm, "_attempted", set())  # a second process inside the failure backoff
    _resolve()
    assert len(installs) == 1


def test_runtime_resolution_without_lazy_installs_fetches_nothing(tmp_path, monkeypatch, installs):
    _copilot_home(tmp_path, monkeypatch,
                  "model:\n  provider: copilot\nsecurity:\n  allow_lazy_installs: false\n")
    _resolve()
    assert installs == []


def test_a_failed_attempt_is_not_rechecked_on_every_resolution(tmp_path, monkeypatch, installs):
    """After this process's attempt, a resolution of the provider costs a set lookup, not a re-read of the
    home's config, jobs, auth store and .env."""
    _copilot_home(tmp_path, monkeypatch)
    feature = next(f for f in lcm.LEFT_CORE if f.plugin == "github-copilot")
    calls = []

    def counted(home, **kw):
        calls.append(home)
        return lcm.copilot_in_use(home, **kw)
    from dataclasses import replace
    monkeypatch.setattr(lcm, "LEFT_CORE", tuple(replace(f, in_use=counted) if f is feature else f
                                                for f in lcm.LEFT_CORE))
    lcm.recover_for_provider("copilot")
    first = len(calls)
    for _ in range(5):
        lcm.recover_for_provider("copilot")
    assert len(calls) == first



@pytest.mark.parametrize("config", [
    "model:\n  provider: nous\ncron:\n  model_provider: copilot\n",
    "model:\n  provider: nous\ndelegation:\n  backends:\n    model_map:\n      options:\n"
    "        - name: fast\n          provider: copilot\n          model: gpt-5.4\n",
])
def test_a_route_configured_outside_the_main_model_installs_when_resolved(tmp_path, monkeypatch, installs, config):
    """A cron run or a routed delegate resolves the provider its config slot names: that is use."""
    _copilot_home(tmp_path, monkeypatch, config)
    _resolve(requested="copilot")
    assert installs == [("github-copilot", True)]
