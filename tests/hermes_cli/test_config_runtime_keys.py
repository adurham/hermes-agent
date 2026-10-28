"""`hermes config get/set` must recognize keys the runtime reads but DEFAULT_CONFIG leaves unseeded:
core provider selections (``browser.cloud_provider``, ``<category>.use_gateway``) and keys a plugin
declares through ``config_keys`` in its manifest."""

import argparse
import json
import os
from unittest.mock import patch

import pytest

from hermes_cli.config import config_command, set_config_value

NOTICE = "not a recognized config key"


@pytest.fixture(autouse=True)
def hermes_home(tmp_path):
    (tmp_path / ".env").touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def test_config_get_browser_selection_keys_print_value_without_notice(hermes_home, capsys):
    (hermes_home / "config.yaml").write_text(
        json.dumps({"browser": {"cloud_provider": "browser-use", "use_gateway": True}}))

    for key, expected in (("browser.cloud_provider", "browser-use"), ("browser.use_gateway", True)):
        config_command(argparse.Namespace(config_command="get", key=key, json=True))
        captured = capsys.readouterr()
        assert json.loads(captured.out) == expected
        assert NOTICE not in captured.err, key


@pytest.mark.parametrize("key,value", [
    ("browser.cloud_provider", "browserbase"),   # core: tools.tool_backend_helpers.read_selection
    ("web.use_gateway", "true"),                 # core: legacy managed-gateway flag, every category
    ("memory.retaindb.project", "p1"),           # plugin manifest: plugins/memory/retaindb
    ("dashboard.kanban.default_tenant", "t1"),   # dashboard manifest: plugins/kanban/dashboard
])
def test_config_set_runtime_read_key_is_silent(key, value, hermes_home, capsys):
    set_config_value(key, value)
    out = capsys.readouterr().out
    assert f"Set {key}" in out
    assert NOTICE not in out


def test_config_set_browser_cloud_provider_is_what_the_runtime_selects(hermes_home, capsys):
    from tools.tool_backend_helpers import read_selection

    set_config_value("browser.cloud_provider", "browserbase")
    assert read_selection("browser") == "browserbase"
    set_config_value("browser.cloud_provider", "browser-use")
    assert read_selection("browser") == "browser-use"
    assert NOTICE not in capsys.readouterr().out


def test_user_plugin_declares_its_config_keys_in_plugin_yaml(hermes_home, capsys):
    plugin = hermes_home / "plugins" / "widget-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(json.dumps({"name": "widget-plugin", "config_keys": ["display.widget"]}))

    set_config_value("display.widget.size", "3")
    assert NOTICE not in capsys.readouterr().out
    # A sibling the plugin did not declare, and a typo of a core selection key, still warn.
    set_config_value("display.widgetx", "3")
    assert NOTICE in capsys.readouterr().out
    set_config_value("browser.cloud_providr", "browserbase")
    assert NOTICE in capsys.readouterr().out


def test_enabled_project_plugin_declares_its_config_keys(hermes_home, tmp_path, monkeypatch, capsys):
    plugin = tmp_path / "proj" / ".hermes" / "plugins" / "widget-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(json.dumps({"name": "widget-plugin", "config_keys": ["display.widget"]}))
    monkeypatch.chdir(tmp_path / "proj")

    set_config_value("display.widget.size", "3")
    assert NOTICE in capsys.readouterr().out  # project plugins are opt-in
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    set_config_value("display.widget.size", "4")
    assert NOTICE not in capsys.readouterr().out
