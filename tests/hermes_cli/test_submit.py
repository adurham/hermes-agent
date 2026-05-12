"""Unit tests for hermes_cli.submit — the `hermes submit` subcommand."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import submit as submit_mod


def _args(**kw):
    """Build a SimpleNamespace mirroring argparse output, with sane defaults."""
    defaults = {
        "prompt": [],
        "file": None,
        "instructions": None,
        "gateway_url": None,
        "api_key": None,
        "tail": False,
        "tail_run": None,
        "quiet": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ─── _resolve_target precedence ─────────────────────────────────────────────

def test_resolve_target_uses_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    with patch("hermes_cli.config.get_env_value", return_value=""):
        target = submit_mod._resolve_target(_args())
    assert target.base_url == submit_mod.DEFAULT_GATEWAY_URL
    assert target.api_key == ""
    assert "default" in target.source


def test_resolve_target_flag_beats_env(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://from-env:9000")
    with patch("hermes_cli.config.get_env_value", return_value=""):
        target = submit_mod._resolve_target(
            _args(gateway_url="http://from-flag:8000", api_key="k")
        )
    assert target.base_url == "http://from-flag:8000"
    assert target.api_key == "k"
    assert target.source == "--gateway-url"


def test_resolve_target_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_URL", raising=False)
    with patch("hermes_cli.config.get_env_value", return_value=""):
        target = submit_mod._resolve_target(_args(gateway_url="http://x:1/"))
    assert target.base_url == "http://x:1"


def test_resolve_target_env_beats_hermes_dotenv(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://env-wins:1")
    with patch("hermes_cli.config.get_env_value", return_value="http://dotenv-loses:2"):
        target = submit_mod._resolve_target(_args())
    assert target.base_url == "http://env-wins:1"
    assert "env" in target.source


def test_resolve_target_api_key_falls_back_through_chain(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("API_SERVER_KEY", "from-api-server-key")
    with patch("hermes_cli.config.get_env_value", return_value=""):
        target = submit_mod._resolve_target(_args())
    assert target.api_key == "from-api-server-key"


# ─── _read_prompt sources ──────────────────────────────────────────────────

def test_read_prompt_joins_positional_args():
    assert submit_mod._read_prompt(_args(prompt=["do", "the", "thing"])) == "do the thing"


def test_read_prompt_reads_file(tmp_path):
    p = tmp_path / "task.md"
    p.write_text("do this from a file\n")
    assert submit_mod._read_prompt(_args(file=str(p))) == "do this from a file\n"


def test_read_prompt_errors_when_no_source_and_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit) as exc:
        submit_mod._read_prompt(_args())
    assert "no prompt provided" in str(exc.value)


# ─── _post_run HTTP behavior ───────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class _FakeClient:
    def __init__(self, *, response: _FakeResp):
        self._response = response
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, *, json, headers):
        self.calls.append({"url": url, "json": json, "headers": dict(headers)})
        return self._response


def test_post_run_includes_bearer_when_key_set():
    fake = _FakeClient(response=_FakeResp(200, {"id": "run_xyz"}))
    target = submit_mod._GatewayTarget(base_url="http://gw:8642", api_key="sekret", source="t")
    with patch("httpx.Client", return_value=fake):
        out = submit_mod._post_run(target, "hello", instructions=None)
    assert out == {"id": "run_xyz"}
    assert fake.calls[0]["url"] == "http://gw:8642/v1/runs"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sekret"
    assert fake.calls[0]["json"] == {"input": "hello"}


def test_post_run_omits_authorization_when_no_key():
    fake = _FakeClient(response=_FakeResp(200, {"id": "r"}))
    target = submit_mod._GatewayTarget(base_url="http://gw:8642", api_key="", source="t")
    with patch("httpx.Client", return_value=fake):
        submit_mod._post_run(target, "hi", instructions=None)
    assert "Authorization" not in fake.calls[0]["headers"]


def test_post_run_passes_instructions():
    fake = _FakeClient(response=_FakeResp(200, {"id": "r"}))
    target = submit_mod._GatewayTarget(base_url="http://gw:8642", api_key="", source="t")
    with patch("httpx.Client", return_value=fake):
        submit_mod._post_run(target, "p", instructions="be terse")
    assert fake.calls[0]["json"] == {"input": "p", "instructions": "be terse"}


def test_post_run_401_gives_actionable_error():
    fake = _FakeClient(response=_FakeResp(401, "unauthorized"))
    target = submit_mod._GatewayTarget(base_url="http://gw:8642", api_key="bad", source="t")
    with patch("httpx.Client", return_value=fake):
        with pytest.raises(SystemExit) as exc:
            submit_mod._post_run(target, "p", instructions=None)
    assert "401" in str(exc.value)
    assert "API_SERVER_KEY" in str(exc.value)


def test_post_run_5xx_propagates_status_and_body():
    fake = _FakeClient(response=_FakeResp(503, "gateway down"))
    target = submit_mod._GatewayTarget(base_url="http://gw:8642", api_key="", source="t")
    with patch("httpx.Client", return_value=fake):
        with pytest.raises(SystemExit) as exc:
            submit_mod._post_run(target, "p", instructions=None)
    assert "503" in str(exc.value)
    assert "gateway down" in str(exc.value)


# ─── submit_command end-to-end (mocked) ────────────────────────────────────

def test_submit_command_prints_run_id_and_returns_zero(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    fake = _FakeClient(response=_FakeResp(202, {"id": "run_abc123"}))
    with patch("httpx.Client", return_value=fake), \
         patch("hermes_cli.config.get_env_value", return_value=""):
        rc = submit_mod.submit_command(_args(prompt=["do", "x"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "run_abc123" in out
    assert "gateway:" in out


def test_submit_command_quiet_prints_only_run_id(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_URL", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    fake = _FakeClient(response=_FakeResp(202, {"id": "run_q"}))
    with patch("httpx.Client", return_value=fake), \
         patch("hermes_cli.config.get_env_value", return_value=""):
        rc = submit_mod.submit_command(_args(prompt=["x"], quiet=True))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "run_q"
