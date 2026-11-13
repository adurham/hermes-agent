"""plugins.allow_user_dir / plugins.user_dir_allowlist: closing <HERMES_HOME>/plugins.

With ``allow_user_dir: false`` nothing in the user plugins directory is imported — whatever its kind and
whatever ``plugins.enabled`` says — unless the allowlist names its directory. "Not imported" is proved
by an import-time marker file each fixture plugin writes. Unset, the directory behaves as before.
"""
import logging
import sys

import pytest

import hermes_yaml as yaml


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    markers = tmp_path / "markers"
    markers.mkdir()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    from hermes_cli import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
    before = set(sys.modules)
    yield home, markers
    for name in set(sys.modules) - before:  # fixture plugin modules never leak into later tests
        if "probe_ud" in name:
            sys.modules.pop(name, None)


def _plugin(home, markers, name, *, kind=None, extra_source=""):
    d = home / "plugins" / name
    d.mkdir(parents=True)
    manifest = {"name": name, "version": "0.1.0", "description": "allowlist probe"}
    if kind:
        manifest["kind"] = kind
    (d / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    marker = markers / f"imported-{name}"
    (d / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('imported')\n{extra_source}\ndef register(ctx):\n    pass\n",
        encoding="utf-8")
    return d


def _config(home, **plugins):
    import hermes_cli.config as cfg
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": plugins}), encoding="utf-8")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()


def _imported(markers):
    return sorted(p.name.removeprefix("imported-") for p in markers.iterdir())


KINDS = {"probe_ud_standalone": None, "probe_ud_backend": "backend", "probe_ud_platform": "platform",
         "probe_ud_provider": "model-provider", "probe_ud_memory": "exclusive"}


def test_an_open_directory_behaves_as_before(env):
    from hermes_cli.plugins import PluginManager

    home, markers = env
    _plugin(home, markers, "probe_ud_standalone")
    _config(home, enabled=["probe_ud_standalone"])
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins["probe_ud_standalone"].enabled is True
    assert _imported(markers) == ["probe_ud_standalone"]


def test_a_closed_directory_imports_nothing_unlisted_of_any_kind(env):
    from hermes_cli.plugins import PluginManager

    home, markers = env
    for name, kind in KINDS.items():
        _plugin(home, markers, name, kind=kind)
    _config(home, enabled=list(KINDS), allow_user_dir=False, user_dir_allowlist=["something-else"])
    manager = PluginManager()
    manager.discover_and_load()
    assert _imported(markers) == []
    for name in KINDS:
        state = manager._plugins[name]
        assert state.enabled is False and "plugins.allow_user_dir is false" in (state.error or ""), name


def test_an_allowlisted_plugin_still_loads(env):
    from hermes_cli.plugins import PluginManager

    home, markers = env
    _plugin(home, markers, "probe_ud_standalone")
    _plugin(home, markers, "probe_ud_kept")
    _config(home, enabled=["probe_ud_standalone", "probe_ud_kept"], allow_user_dir=False,
            user_dir_allowlist=["probe_ud_kept"])
    manager = PluginManager()
    manager.discover_and_load()
    assert _imported(markers) == ["probe_ud_kept"]
    assert manager._plugins["probe_ud_kept"].enabled is True


def test_provider_discovery_never_imports_a_closed_model_provider(env):
    """providers/ imports flat user model-provider plugins on its own (not via PluginManager)."""
    import providers

    home, markers = env
    plugin_dir = _plugin(home, markers, "probe_ud_provider", kind="model-provider")
    _config(home, allow_user_dir=False)
    providers._import_plugin_dir(plugin_dir, "user", home_key="probe-home")
    assert _imported(markers) == []
    _config(home, allow_user_dir=False, user_dir_allowlist=["probe_ud_provider"])
    providers._import_plugin_dir(plugin_dir, "user", home_key="probe-home")
    assert _imported(markers) == ["probe_ud_provider"]


def test_category_loader_refuses_a_closed_user_directory(env):
    """context engines, cron and computer-use providers (and memory's in-process path) import here."""
    from plugins import plugin_loader

    home, markers = env
    plugin_dir = _plugin(home, markers, "probe_ud_engine")
    _config(home, allow_user_dir=False)
    log = logging.getLogger("probe_ud")
    def load():  # the context engine's own call shape
        return plugin_loader.load_plugin_module(
            "plugins.context_engine.probe_ud_engine", plugin_dir, parents=("plugins", "plugins.context_engine"),
            logger=log)

    assert load() is None
    assert _imported(markers) == []
    _config(home)  # control: the same directory, open, is imported
    assert load() is not None
    assert _imported(markers) == ["probe_ud_engine"]


def test_memory_provider_refused_before_any_load_path(env):
    from plugins.memory import load_memory_provider

    home, markers = env
    _plugin(home, markers, "probe_ud_memory", kind="exclusive",
            extra_source="from agent.memory_provider import MemoryProvider  # register_memory_provider")
    _config(home, allow_user_dir=False)
    assert load_memory_provider("probe_ud_memory") is None
    assert _imported(markers) == []
    _config(home)  # control: open, the provider's module is imported (it defines no provider: None again)
    load_memory_provider("probe_ud_memory")
    assert _imported(markers) == ["probe_ud_memory"]


def test_names_are_paths_under_the_user_directory(env, tmp_path):
    from hermes_cli.plugins_discovery import user_plugin_dir_refusal

    home, _markers = env
    real = tmp_path / "elsewhere" / "real-plugin"
    real.mkdir(parents=True)
    (home / "plugins" / "linked").symlink_to(real, target_is_directory=True)
    (home / "plugins" / "model-providers" / "nested").mkdir(parents=True)
    _config(home, allow_user_dir="false", user_dir_allowlist=["linked", "model-providers/nested/"])
    assert user_plugin_dir_refusal(home / "plugins" / "linked") is None  # judged by its entry, not its target
    assert user_plugin_dir_refusal(home / "plugins" / "model-providers" / "nested") is None
    assert "'other'" in user_plugin_dir_refusal(home / "plugins" / "other")
    assert user_plugin_dir_refusal(real) is None  # outside the user dir: not this gate's business
    _config(home, allow_user_dir=True, user_dir_allowlist=[])
    assert user_plugin_dir_refusal(home / "plugins" / "other") is None
