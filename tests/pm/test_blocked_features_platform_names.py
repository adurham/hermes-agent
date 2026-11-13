"""security.blocked_features: ``platform.<name>`` and bare ``<name>`` are the same feature.

A platform adapter asks pm for its bare extra (``telegram``) while configs commonly list the
platform spelling (``platform.telegram``); an exact-match veto let every such entry through.
Exercised through the public sync_venv() lazy path with a real config.yaml read.
"""

from __future__ import annotations

import pytest

from pm import receipt
from pm.package import InstallError
from tests.pm.test_blocked_features import _Reached, _stop_after_veto, _write_config

BLOCKED_MESSAGE = "blocked by security.blocked_features in config.yaml"


@pytest.mark.parametrize(("listed", "requested"), [
    ("platform.telegram", "telegram"),
    ("telegram", "platform.telegram"),
    ("platform.telegram", "platform.telegram"),
])
def test_either_spelling_blocks_the_other(monkeypatch, listed, requested):
    install = _stop_after_veto(monkeypatch)
    _write_config(f"security:\n  blocked_features: [{listed}]\n")

    with pytest.raises(InstallError) as info:
        install.sync_venv([requested])

    assert BLOCKED_MESSAGE in str(info.value)
    assert f"remove {listed!r} from security.blocked_features" in str(info.value)  # names the real entry
    assert receipt.latest()["refusal"]["code"] == "blocked-feature"


def test_no_over_matching_and_explicit_still_overrides(monkeypatch):
    install = _stop_after_veto(monkeypatch)
    _write_config("security:\n  blocked_features: [platform.telegram]\n")
    for other in ("telegram-bot", "platform.teams", "web"):
        with pytest.raises(_Reached):
            install.sync_venv([other])
    with pytest.raises(_Reached):
        install.sync_venv(["telegram"], explicit=True)
