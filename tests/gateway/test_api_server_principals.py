"""Tests for the multi-principal auth + audit log on api_server."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


# ─── _load_principals_map ───────────────────────────────────────────────────


class TestLoadPrincipalsMap:
    def test_empty_path_returns_empty(self):
        assert APIServerAdapter._load_principals_map("") == {}

    def test_missing_file_returns_empty(self, tmp_path):
        assert APIServerAdapter._load_principals_map(str(tmp_path / "nope.yaml")) == {}

    def test_json_map(self, tmp_path):
        f = tmp_path / "keys.json"
        f.write_text(json.dumps({"laptop-adam": "key1", "discord-adapter": "key2"}))
        got = APIServerAdapter._load_principals_map(str(f))
        assert got == {"laptop-adam": "key1", "discord-adapter": "key2"}

    def test_yaml_map(self, tmp_path):
        pytest.importorskip("yaml")
        f = tmp_path / "keys.yaml"
        f.write_text('laptop-adam: "k1"\ndiscord-adapter: "k2"\n')
        got = APIServerAdapter._load_principals_map(str(f))
        assert got == {"laptop-adam": "k1", "discord-adapter": "k2"}

    def test_non_mapping_top_level_rejected(self, tmp_path):
        f = tmp_path / "keys.json"
        f.write_text(json.dumps(["not", "a", "map"]))
        assert APIServerAdapter._load_principals_map(str(f)) == {}

    def test_skips_non_string_entries(self, tmp_path):
        f = tmp_path / "keys.json"
        f.write_text(json.dumps({"good": "tok", "bad-empty": "", "bad-int": 42}))
        got = APIServerAdapter._load_principals_map(str(f))
        assert got == {"good": "tok"}


# ─── _check_auth resolution ─────────────────────────────────────────────────


def _adapter(*, api_key="", principals=None) -> APIServerAdapter:
    cfg = PlatformConfig()
    if api_key:
        cfg.extra["key"] = api_key
    a = APIServerAdapter(cfg)
    if principals is not None:
        a._principals = principals
    return a


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request used by _check_auth.

    Implements just headers (for the Authorization lookup) and the
    dict-like state slots the adapter uses to attach `principal`.
    """

    def __init__(self, *, bearer=None):
        self.headers = {}
        if bearer is not None:
            self.headers["Authorization"] = f"Bearer {bearer}"
        self._state: dict = {}

    def __setitem__(self, k, v):
        self._state[k] = v

    def __getitem__(self, k):
        return self._state[k]

    def get(self, k, default=None):
        return self._state.get(k, default)


def _request(*, bearer=None):
    return _FakeRequest(bearer=bearer)


class TestCheckAuth:
    def test_no_keys_at_all_allows_and_tags_anonymous(self):
        a = _adapter()
        r = _request()
        assert a._check_auth(r) is None
        assert r._state["principal"] == "anonymous"

    def test_legacy_single_key_match_sets_default_principal(self):
        a = _adapter(api_key="secret")
        r = _request(bearer="secret")
        assert a._check_auth(r) is None
        assert r._state["principal"] == "default"

    def test_legacy_single_key_mismatch_401(self):
        a = _adapter(api_key="secret")
        r = _request(bearer="wrong")
        resp = a._check_auth(r)
        assert resp is not None
        assert resp.status == 401

    def test_principals_match_sets_name(self):
        a = _adapter(principals={"laptop-adam": "k1", "discord-adapter": "k2"})
        r = _request(bearer="k2")
        assert a._check_auth(r) is None
        assert r._state["principal"] == "discord-adapter"

    def test_principals_match_with_legacy_key_also_set(self):
        # Both API_SERVER_KEY and KEYS_FILE active — legacy hit wins as "default".
        a = _adapter(api_key="legacy", principals={"alice": "k1"})
        r1 = _request(bearer="legacy")
        a._check_auth(r1)
        assert r1._state["principal"] == "default"
        r2 = _request(bearer="k1")
        a._check_auth(r2)
        assert r2._state["principal"] == "alice"

    def test_unknown_bearer_against_principals_only_401(self):
        a = _adapter(principals={"alice": "k1"})
        resp = a._check_auth(_request(bearer="not-a-key"))
        assert resp is not None
        assert resp.status == 401

    def test_missing_bearer_with_keys_configured_401(self):
        a = _adapter(principals={"alice": "k1"})
        resp = a._check_auth(_request())
        assert resp is not None
        assert resp.status == 401


# ─── _write_audit ───────────────────────────────────────────────────────────


class TestWriteAudit:
    def test_noop_when_path_unset(self):
        a = _adapter()
        a._audit_log_path = ""
        # Should not raise.
        a._write_audit(event="run.submitted", run_id="run_x", principal="alice")

    def test_writes_jsonl_entry(self, tmp_path):
        a = _adapter()
        a._audit_log_path = str(tmp_path / "audit.log")
        a._write_audit(
            event="run.submitted",
            run_id="run_abc",
            principal="laptop-adam",
            prompt_sha256="dead" * 16,
            remote="100.119.249.49",
        )
        lines = Path(a._audit_log_path).read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "run.submitted"
        assert rec["run_id"] == "run_abc"
        assert rec["principal"] == "laptop-adam"
        assert rec["prompt_sha256"] == "dead" * 16
        assert rec["remote"] == "100.119.249.49"
        assert "ts" in rec

    def test_appends_multiple(self, tmp_path):
        a = _adapter()
        a._audit_log_path = str(tmp_path / "audit.log")
        a._write_audit(event="run.submitted", run_id="r1", principal="a")
        a._write_audit(event="run.submitted", run_id="r2", principal="b")
        lines = Path(a._audit_log_path).read_text().splitlines()
        assert [json.loads(l)["run_id"] for l in lines] == ["r1", "r2"]

    def test_write_failure_is_swallowed(self, tmp_path):
        a = _adapter()
        # Point at a directory — open(... "a") will fail.
        a._audit_log_path = str(tmp_path)
        # Should not raise even though write fails.
        a._write_audit(event="run.submitted", run_id="run_x", principal="a")


# ─── init wires env correctly ───────────────────────────────────────────────


class TestInitFromEnv:
    def test_keys_file_env_loads_principals(self, tmp_path, monkeypatch):
        f = tmp_path / "keys.json"
        f.write_text(json.dumps({"alice": "k1"}))
        monkeypatch.setenv("API_SERVER_KEYS_FILE", str(f))
        a = APIServerAdapter(PlatformConfig())
        assert a._principals == {"alice": "k1"}

    def test_audit_log_env_wired(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_SERVER_AUDIT_LOG", str(tmp_path / "a.log"))
        a = APIServerAdapter(PlatformConfig())
        assert a._audit_log_path == str(tmp_path / "a.log")

    def test_no_env_means_empty_defaults(self, monkeypatch):
        monkeypatch.delenv("API_SERVER_KEYS_FILE", raising=False)
        monkeypatch.delenv("API_SERVER_AUDIT_LOG", raising=False)
        a = APIServerAdapter(PlatformConfig())
        assert a._principals == {}
        assert a._audit_log_path == ""
