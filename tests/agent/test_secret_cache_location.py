"""Secret-source disk caches live directly in the profile home.

A sandbox that hides credential stores by path can hide a file safely only at the top of the
profile home: a writable folder above a hidden file can be renamed to expose it, and the
sandboxed backend has to write ``cache/``. Earlier releases kept these caches in ``cache/``; the
first read moves such a copy to the new location, and every path keeps a single live copy.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from agent.secret_sources import bitwarden as bw
from agent.secret_sources import onepassword as op
from agent.secret_sources._cache import CachedFetch, DiskCache


@pytest.fixture(autouse=True)
def _reset_caches():
    bw._reset_cache_for_tests()
    op._reset_cache_for_tests()
    yield
    bw._reset_cache_for_tests()
    op._reset_cache_for_tests()


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def _payload(key: str, secrets: dict) -> str:
    return json.dumps({"key": key, "secrets": secrets, "fetched_at": time.time()})


def _previous(home: Path, name: str) -> Path:
    path = home / "cache" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _refuse_rename(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "rename", refuse)


def test_caches_live_at_the_top_of_the_profile_home(home):
    assert bw._disk_cache_path(home) == home / "bws_cache.json"
    assert bw._encrypted_disk_cache_path(home) == home / "bws_cache.enc.json"
    assert op._STORE.disk.path(home) == home / "op_cache.json"


def test_a_write_lands_in_the_profile_home(home):
    cache = DiskCache("probe.json", key_serializer=str)
    cache.write("k", CachedFetch(secrets={"A": "b"}, fetched_at=time.time()), 60, home)

    assert json.loads((home / "probe.json").read_text(encoding="utf-8"))["secrets"] == {"A": "b"}
    assert not (home / "cache").exists()


def test_the_first_read_moves_a_cache_from_the_previous_location(home):
    cache = DiskCache("probe.json", key_serializer=str)
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "b"}), encoding="utf-8")

    entry = cache.read("k", 60, home)

    assert entry is not None and entry.secrets == {"A": "b"}
    assert (home / "probe.json").is_file()
    assert not previous.exists()


@pytest.mark.platforms("posix")  # POSIX permission bits
def test_the_move_keeps_the_file_mode(home):
    cache = DiskCache("probe.json", key_serializer=str)
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "b"}), encoding="utf-8")
    previous.chmod(0o600)
    old_umask = os.umask(0o022)  # a copy instead of a rename would come out 0o644
    try:
        assert cache.read("k", 60, home) is not None
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((home / "probe.json").stat().st_mode) == 0o600


def test_a_current_cache_wins_and_the_previous_copy_is_removed(home):
    cache = DiskCache("probe.json", key_serializer=str)
    (home / "probe.json").write_text(_payload("k", {"A": "current"}), encoding="utf-8")
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "previous"}), encoding="utf-8")

    entry = cache.read("k", 60, home)

    assert entry is not None and entry.secrets == {"A": "current"}
    assert not previous.exists()


def test_a_failed_move_keeps_working_from_the_previous_copy(home, monkeypatch):
    cache = DiskCache("probe.json", key_serializer=str)
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "b"}), encoding="utf-8")
    _refuse_rename(monkeypatch)

    entry = cache.read("k", 60, home)
    assert entry is not None and entry.secrets == {"A": "b"}
    assert previous.is_file()

    cache.write("k", CachedFetch(secrets={"A": "c"}, fetched_at=time.time()), 60, home)
    copies = [p for p in (home / "probe.json", previous) if p.exists()]
    assert len(copies) == 1, copies
    assert json.loads(copies[0].read_text(encoding="utf-8"))["secrets"] == {"A": "c"}


def test_a_move_another_process_finished_reads_the_new_location(home, monkeypatch):
    cache = DiskCache("probe.json", key_serializer=str)
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "b"}), encoding="utf-8")
    real_rename = os.rename

    def raced(src, dst):
        real_rename(src, dst)  # a concurrent reader moved it first
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(os, "rename", raced)

    entry = cache.read("k", 60, home)

    assert entry is not None and entry.secrets == {"A": "b"}


def test_clear_removes_both_locations(home):
    cache = DiskCache("probe.json", key_serializer=str)
    (home / "probe.json").write_text(_payload("k", {"A": "b"}), encoding="utf-8")
    previous = _previous(home, "probe.json")
    previous.write_text(_payload("k", {"A": "b"}), encoding="utf-8")

    cache.clear(home)

    assert not (home / "probe.json").exists()
    assert not previous.exists()


def test_onepassword_cache_moves_from_the_previous_location(home):
    key = ("auth-fp", "account", str(home), "refs-fp")
    previous = _previous(home, "op_cache.json")
    previous.write_text(_payload(op._disk_key_str(key), {"OP_KEY": "v"}), encoding="utf-8")

    entry = op._STORE.disk.read(key, 60, home)

    assert entry is not None and entry.secrets == {"OP_KEY": "v"}
    assert (home / "op_cache.json").is_file()
    assert not previous.exists()


def _seed_encrypted(home: Path) -> tuple[tuple[str, str, str], Path]:
    pytest.importorskip("cryptography")
    cache_key = (bw._token_fingerprint("0.t"), "proj-1", "")
    bw._write_encrypted_disk_cache(cache_key=cache_key, access_token="0.t",
                                   entry=CachedFetch(secrets={"K": "v"}, fetched_at=time.time()),
                                   home_path=home)
    previous = _previous(home, "bws_cache.enc.json")
    os.replace(home / "bws_cache.enc.json", previous)
    return cache_key, previous


def test_bitwarden_encrypted_cache_moves_from_the_previous_location(home):
    cache_key, previous = _seed_encrypted(home)

    entry = bw._read_encrypted_disk_cache(cache_key=cache_key, access_token="0.t",
                                          max_age_seconds=3600, home_path=home)

    assert entry is not None and entry.secrets == {"K": "v"}
    assert (home / "bws_cache.enc.json").is_file()
    assert not previous.exists()


def test_bitwarden_encrypted_cache_failed_move_reads_the_previous_copy(home, monkeypatch):
    cache_key, previous = _seed_encrypted(home)
    _refuse_rename(monkeypatch)

    entry = bw._read_encrypted_disk_cache(cache_key=cache_key, access_token="0.t",
                                          max_age_seconds=3600, home_path=home)

    assert entry is not None and entry.secrets == {"K": "v"}
    assert previous.is_file()
    assert not (home / "bws_cache.enc.json").exists()


def test_bitwarden_clear_caches_removes_previous_copies(home):
    for name in ("bws_cache.json", "bws_cache.enc.json"):
        _previous(home, name).write_text("{}", encoding="utf-8")
        (home / name).write_text("{}", encoding="utf-8")

    bw.clear_caches(home)

    leftovers = [p for p in (*home.iterdir(), *(home / "cache").iterdir()) if p.is_file()]
    assert leftovers == []
