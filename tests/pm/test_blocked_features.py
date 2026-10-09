"""security.blocked_features: the per-feature veto on LAZY installs.

Narrower than security.allow_lazy_installs: the listed pm packages / extras
are never pulled in on demand (refused with an InstallError naming the
config key + a 'blocked-feature' refusal receipt), while everything else
still lazy-installs. An explicit ``hermes pm install`` is the override.
Exercised through the public ensure() / sync_venv() paths with a real
config.yaml read by the real config loader.
"""

from __future__ import annotations

import pytest

from hermes_cli.config import get_config_path
from pm import receipt
from pm.package import InstallError
from tests.pm.test_pm_core import pm_env as pm_env, served as served  # noqa: F401  (fixtures)

BLOCKED_MESSAGE = "blocked by security.blocked_features in config.yaml"


def _write_config(text: str) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lazy_ensure_of_blocked_package_is_refused_and_receipted(pm_env):
    import pm.install as install

    _write_config("security:\n  blocked_features:\n    - faketool\n")
    token = receipt.begin("sync")
    try:
        with pytest.raises(InstallError) as info:
            install.ensure("faketool", base_env={})
        recorded = receipt.snapshot()
    finally:
        receipt.finalize("failed", 1, token=token)

    assert BLOCKED_MESSAGE in str(info.value)
    assert "remove 'faketool' from security.blocked_features" in str(info.value)
    assert "hermes pm install" in str(info.value)
    assert recorded["refusal"]["code"] == "blocked-feature"
    assert BLOCKED_MESSAGE in recorded["refusal"]["detail"]
    assert not install.is_installed("faketool")

    # The explicit install is the documented override.
    install.ensure("faketool", explicit=True, base_env={})
    assert install.is_installed("faketool")


def test_lazy_ensure_of_unlisted_package_still_installs(pm_env):
    import pm.install as install

    _write_config("security:\n  blocked_features:\n    - tts-premium\n")
    token = receipt.begin("sync")
    try:
        install.ensure("faketool", base_env={})
        recorded = receipt.snapshot()
    finally:
        receipt.finalize("ok", 0, token=token)

    assert install.is_installed("faketool")
    assert recorded["refusal"] is None


class _Reached(Exception):
    """Sentinel: sync_venv got past the veto into feature-policy resolution."""


def _stop_after_veto(monkeypatch):
    import pm.install as install

    def reached(*args, **kwargs):
        raise _Reached()

    monkeypatch.setattr(install, "_feature_policy", reached)
    monkeypatch.setattr(install, "lazy_installs_allowed", lambda: True)
    return install


def test_lazy_sync_of_blocked_extra_is_refused_with_receipt(monkeypatch):
    install = _stop_after_veto(monkeypatch)
    _write_config("security:\n  blocked_features: [tts-premium]\n")

    with pytest.raises(InstallError) as info:
        install.sync_venv(["tts-premium"])

    assert BLOCKED_MESSAGE in str(info.value)
    assert "remove 'tts-premium' from security.blocked_features" in str(info.value)
    saved = receipt.latest()
    assert saved["refusal"]["code"] == "blocked-feature"
    assert "tts-premium" in saved["refusal"]["detail"]
    assert saved["outcome"] == "failed" and saved["exit_code"] != 0


def test_unblocked_or_explicit_sync_is_not_vetoed(monkeypatch):
    install = _stop_after_veto(monkeypatch)

    _write_config("security:\n  blocked_features: [tts-premium]\n")
    with pytest.raises(_Reached):
        install.sync_venv(["tts-premium"], explicit=True)  # explicit override
    with pytest.raises(_Reached):
        install.sync_venv(["web"])  # unlisted extra

    _write_config("{}\n")
    with pytest.raises(_Reached):
        install.sync_venv(["tts-premium"])  # nothing blocked
    saved = receipt.latest()
    assert saved["refusal"] is None
