"""/etc/hermes is always honored: user < $HERMES_MANAGED_DIR < /etc/hermes, per leaf.

The system directory is a monkeypatched temp dir (``_DEFAULT_MANAGED_DIR``; the real /etc/hermes is
never created) and the suite's ``_under_pytest`` guard, which hides the system dir from every other
test, is lifted only here.
"""
import os

import pytest


@pytest.fixture
def layers(tmp_path, monkeypatch):
    from hermes_cli import managed_scope
    import hermes_cli.config as cfg

    home, env_dir, system = tmp_path / "home", tmp_path / "env-managed", tmp_path / "etc-hermes"
    for d in (home, env_dir, system):
        d.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(env_dir))
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", system)
    monkeypatch.setattr(managed_scope, "_under_pytest", lambda: False)

    def reset():
        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        managed_scope.invalidate_managed_cache()

    reset()
    yield home, env_dir, system, reset
    reset()


def _write(path, text, reset):
    path.write_text(text, encoding="utf-8")
    reset()


def test_system_dir_wins_per_leaf_and_env_dir_fills_the_rest(layers):
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config

    home, env_dir, system, reset = layers
    _write(home / "config.yaml", "updates:\n  check: true\nlsp:\n  install_strategy: auto\n", reset)
    _write(env_dir / "config.yaml",
           "updates:\n  check: true\nlsp:\n  install_strategy: manual\nplugins:\n  disabled: [a, b]\n", reset)
    _write(system / "config.yaml", "updates:\n  check: false\nplugins:\n  disabled: [c]\n", reset)

    assert managed_scope.get_managed_dirs() == (env_dir, system)
    assert managed_scope.get_managed_dir() == system
    merged = managed_scope.load_managed_config()
    assert merged == {"updates": {"check": False}, "lsp": {"install_strategy": "manual"}, "plugins": {"disabled": ["c"]}}
    cfg = load_config()
    assert cfg["updates"]["check"] is False            # system beats env dir and user
    assert cfg["lsp"]["install_strategy"] == "manual"  # env dir beats user where system is silent
    assert cfg["plugins"]["disabled"] == ["c"]          # lists replace wholesale, system on top
    assert {"updates.check", "lsp.install_strategy", "plugins.disabled"} <= managed_scope.managed_config_keys()


def test_a_missing_env_dir_does_not_disable_the_system_dir(layers, monkeypatch):
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config

    _home, _env_dir, system, reset = layers
    _write(system / "config.yaml", "updates:\n  check: false\n", reset)
    for value in (str(system.parent / "no-such-dir"), "", "   "):
        monkeypatch.setenv("HERMES_MANAGED_DIR", value)
        reset()
        assert managed_scope.get_managed_dirs() == (system,)
        assert load_config()["updates"]["check"] is False
    monkeypatch.delenv("HERMES_MANAGED_DIR")
    reset()
    assert managed_scope.get_managed_dirs() == (system,)


def test_env_dir_cannot_shadow_the_system_dir(layers):
    """Pointing HERMES_MANAGED_DIR elsewhere (a user-controlled dir) leaves every system leaf in force."""
    from hermes_cli.config import load_config

    _home, env_dir, system, reset = layers
    _write(system / "config.yaml", "security:\n  allow_lazy_installs: false\n", reset)
    _write(env_dir / "config.yaml", "security:\n  allow_lazy_installs: true\n", reset)
    assert load_config()["security"]["allow_lazy_installs"] is False


def test_same_directory_named_twice_is_one_layer(layers, monkeypatch):
    from hermes_cli import managed_scope

    _home, _env_dir, system, reset = layers
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(system))
    reset()
    assert managed_scope.get_managed_dirs() == (system,)


def test_managed_env_layers_union_with_system_winning(layers, monkeypatch):
    from hermes_cli import managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv, resolve_home_dotenv

    home, env_dir, system, reset = layers
    for name in ("PROBE_LAYER_SHARED", "PROBE_LAYER_ENV_ONLY", "PROBE_LAYER_SYS_ONLY"):
        monkeypatch.setenv(name, "from-shell")  # restored by monkeypatch at teardown
    _write(home / ".env", "PROBE_LAYER_SHARED=user\n", reset)
    _write(env_dir / ".env", "PROBE_LAYER_SHARED=env-dir\nPROBE_LAYER_ENV_ONLY=env-dir\n", reset)
    _write(system / ".env", "PROBE_LAYER_SHARED=system\nPROBE_LAYER_SYS_ONLY=system\n", reset)

    assert managed_scope.load_managed_env() == {
        "PROBE_LAYER_SHARED": "system", "PROBE_LAYER_ENV_ONLY": "env-dir", "PROBE_LAYER_SYS_ONLY": "system"}
    assert managed_scope.is_env_managed("PROBE_LAYER_ENV_ONLY") and managed_scope.is_env_managed("PROBE_LAYER_SYS_ONLY")
    resolved = resolve_home_dotenv(home)
    assert (resolved["PROBE_LAYER_SHARED"], resolved["PROBE_LAYER_ENV_ONLY"]) == ("system", "env-dir")
    load_hermes_dotenv(hermes_home=str(home), load_external_secrets=False)
    assert os.environ["PROBE_LAYER_SHARED"] == "system"
    assert os.environ["PROBE_LAYER_ENV_ONLY"] == "env-dir"
    assert os.environ["PROBE_LAYER_SYS_ONLY"] == "system"


def test_editing_the_system_layer_invalidates_the_cached_config(layers):
    from hermes_cli.config import load_config

    _home, env_dir, system, reset = layers
    _write(env_dir / "config.yaml", "updates:\n  check: true\n", reset)
    assert load_config()["updates"]["check"] is True
    (system / "config.yaml").write_text("updates:\n  check: false\n", encoding="utf-8")  # no reset: cache key must see it
    assert load_config()["updates"]["check"] is False
    (system / "config.yaml").unlink()
    assert load_config()["updates"]["check"] is True


def test_refusal_messages_name_every_layer_that_pins(layers):
    from hermes_cli import managed_scope

    _home, env_dir, system, reset = layers
    _write(env_dir / ".env", "K=1\n", reset)
    _write(system / ".env", "K=2\n", reset)
    assert managed_scope.describe_source(".env") == f"{system / '.env'}, {env_dir / '.env'}"


def test_remote_mode_warning_sees_a_managed_config_in_any_layer(layers):
    """Remote mode ignores the managed config (D18) and warns about it: the warning must see every layer."""
    from plugins.config_backends.remote.backend import managed_config_file

    _home, env_dir, system, reset = layers
    assert managed_config_file() is None
    _write(env_dir / "config.yaml", "updates:\n  check: false\n", reset)
    assert managed_config_file() == env_dir / "config.yaml"
    _write(system / "config.yaml", "updates:\n  check: false\n", reset)
    assert managed_config_file() == system / "config.yaml"


def test_strict_still_refuses_a_missing_env_dir_even_with_a_system_dir(layers, monkeypatch):
    """HERMES_MANAGED_DIR names a directory that is not there: a broken deployment, not 'no override'."""
    from hermes_cli import managed_scope

    _home, _env_dir, system, reset = layers
    _write(system / "config.yaml", "updates:\n  check: false\n", reset)
    monkeypatch.setenv("HERMES_MANAGED_STRICT", "1")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(system.parent / "no-such-dir"))
    reset()
    with pytest.raises(managed_scope.ManagedScopeError, match="is not a directory"):
        managed_scope.check_strict()
    monkeypatch.delenv("HERMES_MANAGED_DIR")
    reset()
    managed_scope.check_strict()  # the system dir alone is a complete scope


def test_strict_requires_every_layer_to_be_complete(layers, monkeypatch):
    from hermes_cli import managed_scope

    _home, env_dir, system, reset = layers
    _write(env_dir / "config.yaml", "updates:\n  check: false\n", reset)
    _write(system / "config.yaml", "- broken\n", reset)
    monkeypatch.setenv("HERMES_MANAGED_STRICT", "1")
    reset()
    with pytest.raises(managed_scope.ManagedScopeError, match="etc-hermes/config.yaml is a list"):
        managed_scope.check_strict()


def test_pytest_guard_hides_the_system_dir_by_default(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    system = tmp_path / "etc-hermes"
    system.mkdir()
    (system / "config.yaml").write_text("updates:\n  check: false\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", system)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    assert managed_scope.get_managed_dirs() == ()
    assert managed_scope.load_managed_config() == {}
