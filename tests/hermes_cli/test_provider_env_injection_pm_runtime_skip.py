"""The import-time provider env injection skips PM's private runtime.

The injection feeds interactive surfaces (setup/config UI); inside the PM worker
(minimal runtime, no app dependency tree) the provider-plugin scan can only fail
noisily — solstice importing httpx printed "Failed to load bundled provider
plugin solstice: No module named 'httpx'" once per worker spawn.
"""

import hermes_cli.config as config_mod


def test_provider_env_injection_skipped_inside_pm_runtime(monkeypatch):
    import pm.runtime
    import providers

    calls = []
    monkeypatch.setattr(pm.runtime, "is_runtime", lambda: True)
    monkeypatch.setattr(providers, "list_providers", lambda: calls.append(True) or [])

    config_mod._inject_profile_env_vars()

    assert calls == []


def test_provider_env_injection_runs_outside_pm_runtime(monkeypatch):
    import pm.runtime
    import providers

    calls = []
    monkeypatch.setattr(pm.runtime, "is_runtime", lambda: False)
    monkeypatch.setattr(providers, "list_providers", lambda: calls.append(True) or [])

    config_mod._inject_profile_env_vars()

    assert calls == [True]
