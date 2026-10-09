"""scripts/hermes_token_check.py: env-token check + native credential-store check.

The native-store check exists because the claude-subscription-directsdk provider
strips CLAUDE_CODE_OAUTH_TOKEN before spawning `claude`, so the CLI authenticates
from its own store — a path the env-token check alone cannot see (2026-09-30
incident: empty ~/.claude/.credentials.json husk, env check green, gateway broken).
"""

import importlib.util
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes_token_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_token_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_auth_status_variants():
    mod = _load()
    assert mod._parse_auth_status('{"loggedIn": true, "authMethod": "claude.ai"}') == (True, "claude.ai")
    assert mod._parse_auth_status('{"loggedIn": false, "authMethod": "none"}') == (False, "none")
    assert mod._parse_auth_status("not json at all") == (False, "")
    assert mod._parse_auth_status('{"loggedIn": "yes"}') == (False, "")


def test_native_env_strips_token_and_maps_config_dir(tmp_path, monkeypatch):
    mod = _load()
    envfile = tmp_path / ".env"
    envfile.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-secret\n"
        "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR=/tmp/cc-login\n"
        "OTHER=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "ENV_PATH", envfile)
    env = mod._native_env()
    # The plugin strips the token so native resolves its own store; a check
    # that leaked it would report logged-in on a husk file.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/cc-login"
    assert env["OTHER"] == "1"


def test_native_env_without_config_dir_override(tmp_path, monkeypatch):
    mod = _load()
    envfile = tmp_path / ".env"
    envfile.write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_PATH", envfile)
    env = mod._native_env()
    assert "CLAUDE_CONFIG_DIR" not in env


def _isolate_env_file(mod, tmp_path, monkeypatch):
    """Point ENV_PATH at an empty temp .env — native_login_state() reads it via
    _native_env(), and the suite's home I/O guard forbids touching the real home."""
    envfile = tmp_path / ".env"
    envfile.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "ENV_PATH", envfile)


def _fake_run(stdout: str):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    return run


def test_native_login_state_logged_out(tmp_path, monkeypatch):
    mod = _load()
    _isolate_env_file(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run('{"loggedIn": false, "authMethod": "none"}'))
    assert mod.native_login_state() == (False, "none", "")


def test_native_login_state_logged_in(tmp_path, monkeypatch):
    mod = _load()
    _isolate_env_file(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run('{"loggedIn": true, "authMethod": "claude.ai"}'))
    assert mod.native_login_state() == (True, "claude.ai", "")


def test_native_login_state_missing_cli(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    logged_in, method, detail = mod.native_login_state()
    assert logged_in is None
    assert method == ""
    assert "skipped" in detail


def test_native_login_state_survives_run_failure(tmp_path, monkeypatch):
    mod = _load()
    _isolate_env_file(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/claude")

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=20)

    monkeypatch.setattr(mod.subprocess, "run", boom)
    logged_in, _method, detail = mod.native_login_state()
    assert logged_in is None
    assert "failed to run" in detail
