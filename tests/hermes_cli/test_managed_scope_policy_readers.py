"""Policy gates that read the user's raw config.yaml must still honor the managed overlay.

Each of these readers skips DEFAULT_CONFIG on purpose (an absent key means "unset"), so it read the
user file raw — and an administrator's pin was silently ignored. With no managed scope every reader
behaves exactly as before; with one, the pinned value wins.
"""
import subprocess
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    def reset(with_managed=True):
        if with_managed:
            monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        else:
            monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        managed_scope.invalidate_managed_cache()

    yield home, managed, reset
    reset(with_managed=False)


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_passive_update_check_honors_a_managed_opt_out(homes, tmp_path, monkeypatch):
    """The startup/banner check (passive) must stop before any network when the admin pins updates.check off."""
    from hermes_cli import source_check

    home, managed, reset = homes
    root = tmp_path / "checkout"
    root.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], cwd=root, check=True)
    monkeypatch.delenv("HERMES_REVISION", raising=False)
    requests = []

    def offline(url, accept, token):
        requests.append(url)
        raise OSError("offline test")

    monkeypatch.setattr(source_check, "_request_with", offline)
    _write(home / "config.yaml", "updates:\n  check: true\n")
    _write(managed / "config.yaml", "updates:\n  check: false\n")

    reset(with_managed=False)
    unmanaged = source_check.check_for_updates(install_root=root, home=home, passive=True, force=True)
    assert unmanaged.get("reason") != "disabled"  # control: the user's own setting lets it run

    reset(with_managed=True)
    requests.clear()
    managed_result = source_check.check_for_updates(install_root=root, home=home, passive=True, force=True)
    assert managed_result.get("reason") == "disabled"
    assert requests == []


def test_private_url_allowance_honors_a_managed_deny(homes, monkeypatch):
    from tools import url_safety

    home, managed, reset = homes
    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    _write(home / "config.yaml", "security:\n  allow_private_urls: true\n")
    reset(with_managed=False)
    assert url_safety._resolve_allow_private_urls() is True
    _write(managed / "config.yaml", "security:\n  allow_private_urls: false\n")
    reset(with_managed=True)
    assert url_safety._resolve_allow_private_urls() is False


def test_website_blocklist_applies_a_managed_policy_even_without_a_user_file(homes):
    from tools import website_policy

    home, managed, reset = homes
    _write(managed / "config.yaml", """\
        security:
          website_blocklist:
            enabled: true
            domains: [blocked.example]
    """)
    reset(with_managed=True)
    policy = website_policy._load_policy_config(home / "config.yaml")  # the user file does not exist
    assert policy["enabled"] is True and policy["domains"] == ["blocked.example"]
    reset(with_managed=False)
    assert website_policy._load_policy_config(home / "config.yaml")["enabled"] is False


def test_sandbox_env_passthrough_honors_a_managed_list(homes):
    from tools import env_passthrough

    home, managed, reset = homes
    _write(home / "config.yaml", "terminal:\n  env_passthrough: [PROBE_USER_VAR]\n")
    reset(with_managed=False)
    env_passthrough._config_passthrough.clear()
    assert "PROBE_USER_VAR" in env_passthrough._load_config_passthrough()
    _write(managed / "config.yaml", "terminal:\n  env_passthrough: []\n")
    reset(with_managed=True)
    env_passthrough._config_passthrough.clear()
    assert "PROBE_USER_VAR" not in env_passthrough._load_config_passthrough()
    env_passthrough._config_passthrough.clear()


def test_terminal_env_bridge_lets_a_managed_backend_beat_a_user_env_value(homes):
    """A user .env TERMINAL_ENV must not undo an administrator's terminal.backend pin."""
    from hermes_cli.config import apply_terminal_config_to_env

    home, managed, reset = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")  # no terminal section of its own
    reset(with_managed=False)
    assert apply_terminal_config_to_env(env={"TERMINAL_ENV": "local"})["TERMINAL_ENV"] == "local"
    _write(managed / "config.yaml", "terminal:\n  backend: docker\n")
    reset(with_managed=True)
    assert apply_terminal_config_to_env(env={"TERMINAL_ENV": "local"})["TERMINAL_ENV"] == "docker"


def test_coding_posture_keeps_a_managed_disabled_mcp_server_out(homes):
    from agent import coding_context

    home, managed, reset = homes
    _write(home / "config.yaml", "mcp_servers:\n  corp:\n    command: corp-mcp\n")
    reset(with_managed=False)
    assert coding_context._enabled_mcp_servers(None) == ["corp"]
    _write(managed / "config.yaml", "mcp_servers:\n  corp:\n    enabled: false\n")
    reset(with_managed=True)
    assert coding_context._enabled_mcp_servers(None) == []
