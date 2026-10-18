"""Buzz credentials autodiscovery resolves ``~/.config/buzz`` at call time, not import time."""

import json

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_buzz_mod = load_plugin_adapter("buzz")


def test_credentials_autodiscovery_follows_home_set_after_import(monkeypatch, tmp_path):
    for var in ("BUZZ_PRIVATE_KEY", "BUZZ_CREDENTIALS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    creds_dir = tmp_path / ".config" / "buzz"
    creds_dir.mkdir(parents=True)
    (creds_dir / "agent_credentials.json").write_text(json.dumps({"nsec": "nsec1fromhome"}), encoding="utf-8")

    assert _buzz_mod._resolve_private_key() == "nsec1fromhome"
