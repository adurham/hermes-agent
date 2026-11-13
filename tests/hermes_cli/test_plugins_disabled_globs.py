"""plugins.disabled accepts fnmatch patterns: ``platforms/*`` disables every platform plugin, including
one a later release adds. Exact names behave exactly as before; entries without ``*``/``?``/``[`` never
act as prefixes."""
import sys

import pytest

import hermes_yaml as yaml


def test_exact_names_and_patterns():
    from hermes_cli.plugins_discovery import disabled_match

    key = {"platforms/telegram", "telegram-platform", None}
    assert disabled_match(key, {"platforms/telegram"})            # exact, as before
    assert disabled_match(key, {"telegram-platform"})              # manifest name, as before
    assert disabled_match(key, {"platforms/*"})
    assert disabled_match(key, {"platforms/tele*"})
    assert disabled_match(key, {"platforms/[st]elegram"})
    assert not disabled_match(key, {"platforms/"})                 # no pattern chars: not a prefix
    assert not disabled_match(key, {"platforms"})
    assert not disabled_match(key, {"platforms/disc*", 7, None})   # non-string entries are ignored
    assert not disabled_match({"web/firecrawl"}, {"platforms/*"})


def _platform_manifest(key="platforms/probe_new"):
    from hermes_cli.plugins import PluginManifest

    return PluginManifest(name=f"{key.rsplit('/', 1)[-1]}-platform", key=key, source="bundled", kind="platform")


def test_a_glob_disables_a_bundled_platform_the_list_never_named():
    from hermes_cli.plugins_discovery import gate_manifest

    manifest = _platform_manifest()
    assert gate_manifest(manifest, set(), None).action == "defer"  # control: registers lazily
    verdict = gate_manifest(manifest, {"platforms/*"}, None)
    assert verdict.action == "placeholder" and verdict.error == "disabled via config"


def test_glob_disable_against_an_enable_is_still_the_loud_conflict():
    from hermes_cli.plugins_discovery import IN_BOTH_LISTS, gate_manifest

    verdict = gate_manifest(_platform_manifest(), {"platforms/*"}, {"platforms/probe_new"})
    assert verdict.error == IN_BOTH_LISTS


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    before = set(sys.modules)
    yield home, tmp_path
    for name in set(sys.modules) - before:
        if "probe_glob" in name:
            sys.modules.pop(name, None)


def _config(home, disabled):
    import hermes_cli.config as cfg
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"disabled": disabled}}), encoding="utf-8")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()


def _user_plugin(home, tmp_path, name, kind):
    d = home / "plugins" / name
    d.mkdir()
    (d / "plugin.yaml").write_text(yaml.safe_dump({"name": name, "kind": kind, "version": "0.1"}), encoding="utf-8")
    marker = tmp_path / f"imported-{name}"
    (d / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n# register_memory_provider\n",
                                   encoding="utf-8")
    return d, marker


def test_provider_discovery_honors_a_glob(home):
    import providers

    home, tmp_path = home
    plugin_dir, marker = _user_plugin(home, tmp_path, "probe_glob_provider", "model-provider")
    _config(home, ["probe_glob_*"])
    providers._import_plugin_dir(plugin_dir, "user", home_key="probe-glob")
    assert not marker.exists()
    _config(home, ["probe_other_*"])  # control
    providers._import_plugin_dir(plugin_dir, "user", home_key="probe-glob")
    assert marker.exists()


def test_memory_provider_honors_a_glob(home):
    from plugins.memory import _explicitly_disabled

    home, tmp_path = home
    plugin_dir, _marker = _user_plugin(home, tmp_path, "probe_glob_memory", "exclusive")
    _config(home, ["probe_glob_m*"])
    assert _explicitly_disabled("probe_glob_memory", plugin_dir) is True
    _config(home, ["probe_glob_memory_not"])
    assert _explicitly_disabled("probe_glob_memory", plugin_dir) is False
