"""HERMES_MANAGED_STRICT=1: a managed scope that cannot be applied in full stops the process.

Without the switch every case below stays fail-open (the documented default): a broken managed
file is logged and ignored. With it, ``ManagedScopeError`` (a SystemExit, so the loaders'
``except Exception`` callers cannot swallow it) refuses to run, and the real CLI entry point
exits non-zero with the reason.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scope(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    def reset():
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        managed_scope.invalidate_managed_cache()

    reset()
    yield home, managed, reset
    reset()


def _strict(monkeypatch, on=True):
    if on:
        monkeypatch.setenv("HERMES_MANAGED_STRICT", "1")
    else:
        monkeypatch.delenv("HERMES_MANAGED_STRICT", raising=False)


# ── default (no switch): fail-open is unchanged ─────────────────────────────


def test_without_strict_a_malformed_file_is_ignored(scope, monkeypatch):
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch, on=False)
    (managed / "config.yaml").write_text("lsp: [unclosed\n", encoding="utf-8")
    assert managed_scope.load_managed_config() == {}
    managed_scope.check_strict()  # no-op


def test_without_strict_a_missing_dir_is_no_scope(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _strict(monkeypatch, on=False)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "nope"))
    managed_scope.invalidate_managed_cache()
    assert managed_scope.get_managed_dir() is None
    assert managed_scope.load_managed_config() == {}


def test_strict_is_off_for_falsy_values(monkeypatch):
    from hermes_cli import managed_scope

    for value in ("", "0", "false", "no"):
        monkeypatch.setenv("HERMES_MANAGED_STRICT", value)
        assert managed_scope.strict_mode() is False
    monkeypatch.setenv("HERMES_MANAGED_STRICT", "true")
    assert managed_scope.strict_mode() is True


# ── strict: every way the scope can be incomplete refuses ───────────────────


@pytest.mark.parametrize("body, needle", [
    ("lsp: [unclosed\n", "failed to parse"),
    ("- a\n- list\n", "is a list, not a mapping"),
    ("", "is an empty document, not a mapping"),
    ("# only a comment\n", "is an empty document, not a mapping"),
    ("just a string\n", "is a str, not a mapping"),
])
def test_strict_refuses_a_bad_config(scope, monkeypatch, body, needle):
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(managed_scope.ManagedScopeError) as exc:
        managed_scope.load_managed_config()
    assert isinstance(exc.value, SystemExit)
    assert needle in str(exc.value.code)
    assert str(managed / "config.yaml") in str(exc.value.code)
    with pytest.raises(managed_scope.ManagedScopeError):
        managed_scope.check_strict()


def test_strict_refuses_a_missing_config(scope, monkeypatch):
    from hermes_cli import managed_scope

    _strict(monkeypatch)
    with pytest.raises(managed_scope.ManagedScopeError, match="is missing"):
        managed_scope.check_strict()


def test_strict_refuses_a_managed_dir_that_is_not_a_directory(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _strict(monkeypatch)
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    for value in (tmp_path / "nope", not_a_dir):
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(value))
        managed_scope.invalidate_managed_cache()
        with pytest.raises(managed_scope.ManagedScopeError, match="is not a directory"):
            managed_scope.get_managed_dir()


def test_strict_without_any_configured_scope_enforces_nothing(monkeypatch):
    """No HERMES_MANAGED_DIR and no /etc/hermes: nothing is configured, so nothing is incomplete."""
    from hermes_cli import managed_scope

    _strict(monkeypatch)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    managed_scope.check_strict()
    assert managed_scope.get_managed_dirs() == ()
    assert managed_scope.load_managed_config() == {}


def test_strict_keeps_the_scope_when_its_directory_vanishes_later(scope, monkeypatch, caplog):
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("updates:\n  check: false\n", encoding="utf-8")
    assert managed_scope.load_managed_config() == {"updates": {"check": False}}
    monkeypatch.delenv("HERMES_MANAGED_DIR")  # the variable is gone mid-run
    with caplog.at_level(logging.CRITICAL, logger="hermes_cli.managed_scope"):
        assert managed_scope.load_managed_config() == {"updates": {"check": False}}
    assert any("no longer resolves" in r.getMessage() for r in caplog.records)


def test_strict_accepts_an_explicit_empty_mapping_and_no_env_file(scope, monkeypatch):
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("{}\n", encoding="utf-8")
    managed_scope.check_strict()
    assert managed_scope.load_managed_config() == {}
    assert managed_scope.load_managed_env() == {}


def test_strict_refuses_an_unreadable_env_file(scope, monkeypatch):
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("updates:\n  check: false\n", encoding="utf-8")
    (managed / ".env").write_bytes(b"KEY=\xff\xfe\xfa\n")  # not UTF-8
    with pytest.raises(managed_scope.ManagedScopeError, match=r"\.env failed to parse"):
        managed_scope.check_strict()


def test_strict_loads_a_good_scope_normally(scope, monkeypatch):
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("updates:\n  check: false\n", encoding="utf-8")
    (managed / ".env").write_text("PROBE_ONLY=1\n", encoding="utf-8")
    managed_scope.check_strict()
    assert load_config()["updates"]["check"] is False
    assert managed_scope.load_managed_env() == {"PROBE_ONLY": "1"}


def test_strict_refusal_is_not_swallowed_by_fail_open_callers(scope, monkeypatch):
    """apply_managed_overlay / load_config wrap managed reads in ``except Exception``."""
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config

    _home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("lsp: [unclosed\n", encoding="utf-8")
    with pytest.raises(managed_scope.ManagedScopeError):
        managed_scope.apply_managed_overlay({"a": 1})
    with pytest.raises(managed_scope.ManagedScopeError):
        load_config()


def test_strict_keeps_the_loaded_policy_when_the_file_breaks_later(scope, monkeypatch, caplog):
    """A running process never drops a policy it already enforced, and never raises mid-run."""
    from hermes_cli import managed_scope

    _home, managed, _reset = scope
    _strict(monkeypatch)
    cfg = managed / "config.yaml"
    cfg.write_text("updates:\n  check: false\n", encoding="utf-8")
    assert managed_scope.load_managed_config() == {"updates": {"check": False}}
    cfg.write_text("updates: [broken\n", encoding="utf-8")
    os.utime(cfg, ns=(1, 1))  # a different signature even on coarse-mtime filesystems
    with caplog.at_level(logging.CRITICAL, logger="hermes_cli.managed_scope"):
        assert managed_scope.load_managed_config() == {"updates": {"check": False}}
        assert managed_scope.load_managed_config() == {"updates": {"check": False}}
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "keeping the managed policy" in critical[0].getMessage()
    cfg.unlink()
    assert managed_scope.load_managed_config() == {"updates": {"check": False}}


def test_strict_dotenv_loader_refuses(scope, monkeypatch):
    """load_hermes_dotenv runs at import of every entry point (CLI, gateway, TUI, cron, ACP)."""
    from hermes_cli import managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed, _reset = scope
    _strict(monkeypatch)
    (managed / "config.yaml").write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(managed_scope.ManagedScopeError, match="not a mapping"):
        load_hermes_dotenv(hermes_home=home, load_external_secrets=False)


# ── the real CLI entry point, as a fresh offline process ────────────────────


def _run_cli(tmp_path, managed: Path, *, strict: bool) -> subprocess.CompletedProcess:
    home = tmp_path / "cli-home"
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
        "TMPDIR": str(tmp_path), "HERMES_MANAGED_DIR": str(managed), "PYTHONDONTWRITEBYTECODE": "1",
    }
    if strict:
        env["HERMES_MANAGED_STRICT"] = "1"
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "config", "path"], cwd=str(REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=180, check=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX entry-point environment")
@pytest.mark.skipif(Path("/etc/hermes").exists(), reason="a real /etc/hermes would join the child's scope")
def test_cli_entry_point_fails_closed_only_under_strict(tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("lsp: [unclosed\n", encoding="utf-8")

    open_run = _run_cli(tmp_path, managed, strict=False)
    assert open_run.returncode == 0, open_run.stderr[-2000:]
    assert "IGNORING this managed file" in open_run.stderr

    closed = _run_cli(tmp_path, managed, strict=True)
    assert closed.returncode != 0
    assert "refusing to run" in closed.stderr and "HERMES_MANAGED_STRICT=1" in closed.stderr
    assert str(managed / "config.yaml") in closed.stderr
    assert "Traceback" not in closed.stderr
    assert "config.yaml" not in closed.stdout  # the command itself never ran
