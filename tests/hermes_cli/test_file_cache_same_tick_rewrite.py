"""Credential and allowlist file caches see a same-size in-place rewrite made in the tick of the cached read.

Kernels without multigrain timestamps (CI runners, older hosts) stamp mtime/ctime from a coarse clock,
so a file rewritten in place to a same-length value in the tick of an earlier read keeps mtime, ctime,
size and inode. A cache keyed on that stat signature served the older content for the rest of the
process: a revoked pairing kept its access, and a rotated key in the shared auth store never reached
the profiles reading it. ``coarse_clock`` reports both writes in one frozen tick, which is what such a
kernel returns, on this host too."""
import json
import sys
import time

import pytest


@pytest.fixture
def coarse_clock(monkeypatch):
    """Every stat signature reports one frozen mtime/ctime tick, like a coarse filesystem clock."""
    import utils
    real, tick = utils.file_signature, time.time_ns()

    def frozen(st):
        return (tick, *real(st)[1:3], tick)

    monkeypatch.setattr(utils, "file_signature", frozen)
    for module in list(sys.modules.values()):  # modules that bound the name at import
        if getattr(module, "file_signature", None) is real:
            monkeypatch.setattr(module, "file_signature", frozen)


def test_an_in_place_revocation_in_one_tick_is_not_served_from_the_pairing_cache(coarse_clock):
    from gateway.pairing import PairingStore
    store = PairingStore()
    path = store._approved_path("telegram")
    path.write_text(json.dumps({"111111": {"user_name": "a"}}))
    assert store.is_approved("telegram", "111111")
    path.write_text(json.dumps({"222222": {"user_name": "a"}}))  # same size, same inode, same tick
    assert not store.is_approved("telegram", "111111")
    assert store.is_approved("telegram", "222222")


def test_a_key_rotated_in_place_in_one_tick_reaches_global_auth_store_readers(coarse_clock, tmp_path, monkeypatch):
    import hermes_cli.auth as auth_mod
    path = tmp_path / "global-hermes" / "auth.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(auth_mod, "_global_auth_file_path", lambda: path)
    monkeypatch.setattr(auth_mod, "_global_auth_store_cache", None)

    def write(key):
        path.write_text(json.dumps({"version": 1, "providers": {"openrouter": {"api_key": key}}}))

    write("sk-old")
    assert auth_mod._load_global_auth_store()["providers"]["openrouter"]["api_key"] == "sk-old"
    write("sk-new")
    assert auth_mod._load_global_auth_store()["providers"]["openrouter"]["api_key"] == "sk-new"


def test_a_same_size_env_edit_in_one_tick_changes_the_served_profile_signature(coarse_clock, tmp_path):
    from gateway.run_profile_reconcile import profile_serve_signature
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-aaaa\n")
    before = profile_serve_signature(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-bbbb\n")
    assert profile_serve_signature(tmp_path) != before


def test_a_long_lived_signature_does_not_change_when_the_file_settles(tmp_path, monkeypatch):
    """``always_digest`` is for signatures compared much later (profile reconnects, parse-failure
    records): one taken while the file was fresh still matches the unchanged file once it settles."""
    import utils
    path = tmp_path / "config.yaml"
    path.write_text("model: a\n")
    fresh = utils.file_cache_version(path, always_digest=True)
    monkeypatch.setattr(utils, "mtime_is_racy", lambda _mtime_ns: False)
    assert utils.file_cache_version(path, always_digest=True) == fresh
    path.write_text("model: b\n")
    assert utils.file_cache_version(path, always_digest=True) != fresh
