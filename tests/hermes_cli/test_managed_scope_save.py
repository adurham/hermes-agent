"""save_config never changes what the USER file says about a managed leaf.

Callers save ``load_config()`` output, which carries the managed overlay. The user's file must keep
its own value for every managed leaf (present stays present with its old value, absent stays
absent): a process started without the overlay — a service unit missing HERMES_MANAGED_DIR, a plain
venv — reads that file and must not silently fall back to wider defaults, and administrator policy
must never be copied into the user's file.
"""
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
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


def _write(path, body, reset):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    reset()


def _user_file(home) -> dict:
    from utils import fast_safe_load

    return fast_safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


USER = """\
    lsp:
      install_strategy: auto
    agent:
      disabled_toolsets: [moa]
    updates:
      check: true
"""
MANAGED = """\
    lsp:
      install_strategy: manual
    agent:
      disabled_toolsets: [tts, computer_use]
    plugins:
      disabled: [platforms/telegram]
    tools:
      connectors:
        enabled: false
"""


def test_a_save_keeps_the_users_own_values_for_managed_leaves(homes, capsys):
    from hermes_cli.config import load_config, save_config

    home, managed, reset = homes
    _write(home / "config.yaml", USER, reset)
    _write(managed / "config.yaml", MANAGED, reset)
    cfg = load_config()
    assert cfg["lsp"]["install_strategy"] == "manual"  # the overlay is what the process runs on
    cfg["updates"]["check"] = False                      # an ordinary, unmanaged edit
    save_config(cfg)

    on_disk = _user_file(home)
    assert on_disk["lsp"]["install_strategy"] == "auto"          # kept, not deleted, not replaced
    assert on_disk["agent"]["disabled_toolsets"] == ["moa"]
    assert on_disk["updates"]["check"] is False                  # the real edit landed
    assert "were not saved" not in capsys.readouterr().err       # nothing was refused


def test_a_save_never_writes_managed_values_into_the_user_file(homes):
    from hermes_cli.config import load_config, save_config

    home, managed, reset = homes
    _write(home / "config.yaml", USER, reset)
    _write(managed / "config.yaml", MANAGED, reset)
    save_config(load_config())

    on_disk = _user_file(home)
    assert "disabled" not in (on_disk.get("plugins") or {})
    assert "connectors" not in (on_disk.get("tools") or {})  # no empty branch either
    assert on_disk["agent"]["disabled_toolsets"] == ["moa"]


def test_a_changed_managed_leaf_is_refused_and_reported(homes, capsys):
    from hermes_cli.config import load_config, save_config

    home, managed, reset = homes
    _write(home / "config.yaml", USER, reset)
    _write(managed / "config.yaml", MANAGED, reset)
    cfg = load_config()
    cfg["lsp"]["install_strategy"] = "off"
    cfg["plugins"]["disabled"] = ["something-else"]
    save_config(cfg)

    on_disk = _user_file(home)
    assert on_disk["lsp"]["install_strategy"] == "auto"
    assert "disabled" not in (on_disk.get("plugins") or {})
    err = capsys.readouterr().err
    assert "2 managed setting(s) were not saved" in err
    assert "lsp.install_strategy" in err and "plugins.disabled" in err


def test_the_overlay_still_wins_after_the_save(homes):
    from hermes_cli.config import load_config, save_config

    home, managed, reset = homes
    _write(home / "config.yaml", USER, reset)
    _write(managed / "config.yaml", MANAGED, reset)
    save_config(load_config())
    reset()
    cfg = load_config()
    assert cfg["lsp"]["install_strategy"] == "manual"
    assert cfg["agent"]["disabled_toolsets"] == ["tts", "computer_use"]


def test_without_a_managed_scope_saves_are_unchanged(tmp_path, monkeypatch):
    from hermes_cli import managed_scope
    import hermes_cli.config as cfgmod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    (home / "config.yaml").write_text(textwrap.dedent(USER), encoding="utf-8")
    cfgmod._LOAD_CONFIG_CACHE.clear()
    cfgmod._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    cfg = cfgmod.load_config()
    cfg["lsp"]["install_strategy"] = "manual"
    cfgmod.save_config(cfg)
    assert _user_file(home)["lsp"]["install_strategy"] == "manual"
