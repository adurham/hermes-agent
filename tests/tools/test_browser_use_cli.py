"""Tests for the Browser Use CLI 3.0 backend (tools/browser_use_cli.py).

Covers the three seams the integration relies on:

* Mode detection — ``browser.backend: browser-use`` in config (set via the
  ``hermes tools`` picker); off by default.
* Tool-surface swap — when the mode is on, ``check_browser_requirements``
  returns False so every legacy ``browser_*`` tool (including
  browser_cdp/browser_dialog, whose check_fns funnel through it) is hidden,
  and ``browser_exec`` is advertised instead.
* ``browser_exec`` execution — code is piped on stdin, ``session`` becomes
  ``BU_NAME``, bad session names and a missing CLI produce actionable errors.
"""
import json
import os
import stat
import shutil
import subprocess
import sys
import time

import pytest

import tools.browser_use_cli as bu_cli
from tools import browser_tool_install as bt_install
from tools import browser_tool_cloud as bt_cloud
from tools import browser_tool_session as bt_session


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _fake_managed_chromium(monkeypatch):
    """The local-engine route asks agent-browser for the packaged Chromium's CDP url; never launch
    a real browser from a unit test. Records every ``get cdp-url`` session key in ``.calls``."""
    calls = []

    def fake_run(task_id, command, args=None, timeout=None, _engine_override=None):
        calls.append((task_id, command, tuple(args or [])))
        return {"success": True, "data": {"cdpUrl": f"ws://127.0.0.1:47000/devtools/browser/{task_id}"}}

    monkeypatch.setattr(bt_session, "_run_browser_command", fake_run)
    return calls


@pytest.fixture(autouse=True)
def _fake_supervisor_registry(monkeypatch):
    """browser_exec attaches the vault supervisor to the resolved CDP endpoint; the fake endpoint above is
    not a real browser, so record the attach instead of opening a WebSocket (15 s start timeout)."""
    from tools import browser_supervisor

    attached = []

    class _Registry:
        def get_or_start(self, task_id, cdp_url, **kw):
            attached.append((task_id, cdp_url))

    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", _Registry())
    return attached


def _fake_cli(tmp_path, body):
    """Write an executable fake browser-use CLI and return its path."""
    script = tmp_path / "browser-use"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":
        wrapper = script.with_suffix(".cmd")
        wrapper.write_text(f'@"{shutil.which("bash")}" "{script.as_posix()}" %*\n')
        return str(wrapper)
    return str(script)


@pytest.mark.parametrize("config,installed,key,camofox,expected", [
    ({}, True, False, False, True), ({}, False, False, False, False),
    ({"backend": "off"}, True, True, False, False),
    ({"backend": False}, True, True, False, False),
    ({"backend": "browser-use"}, False, False, False, True),
    ({"backend": "other"}, True, True, False, False),
    ({"cloud_provider": "browser-use"}, False, True, False, True),
    ({"cloud_provider": "browser-use", "use_gateway": True}, False, True, False, False),
    ({"cloud_provider": "browser-use"}, False, False, False, False),
    ({}, True, True, True, False),
    ({"backend": "browser-use"}, True, True, True, False),
    ({"cloud_provider": "browser-use", "backend": "other"}, True, True, False, False),
    ({"cloud_provider": "browserbase"}, False, True, False, False),
    ({"cloud_provider": "local"}, False, True, False, False),
    ({}, False, True, False, True),
    (None, False, False, False, False),
])
def test_mode_precedence(config, installed, key, camofox, expected, monkeypatch):
    def read():
        if config is None:
            raise OSError("config unreadable")
        return {"browser": config}

    monkeypatch.setattr("hermes_cli.config.read_raw_config", read)
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: camofox)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["browser-use"] if installed else None)
    monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key" if key else "")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-key")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")
    assert bu_cli.is_browser_use_cli_mode() is expected


class TestSubprocessEnvironment:
    @pytest.mark.platforms("posix")
    def test_subprocess_env_floors_version_manager_only_path(self, monkeypatch):
        """Profile workers (kanban bots, cron) can inherit a PATH of only
        version-manager dirs (observed in the wild: one nvm dir repeated
        7x). The uv browser-use trampoline resolves dirname/realpath
        through PATH, so /usr/bin must be guaranteed or the CLI dies
        'realpath: not found' (exit 127) before its Python starts."""
        import sys
        from types import ModuleType

        browser_tool = ModuleType("tools.browser_tool")
        browser_tool._build_browser_env = lambda: {
            "PATH": os.pathsep.join(
                ["/home/u/.nvm/versions/node/v24.18.0/bin"] * 7
            ),
        }
        monkeypatch.setitem(sys.modules, "tools.browser_tool", browser_tool)

        env = bu_cli._base_subprocess_env()

        parts = env["PATH"].split(os.pathsep)
        assert "/usr/bin" in parts
        assert "/bin" in parts

    @pytest.mark.platforms("posix")
    def test_floor_preserves_existing_entries_and_order(self):
        """The floor only adds dirs — never drops or reorders what the
        caller's environment already had."""
        original = "/opt/toolchain/bin:/usr/bin:/snap/bin"
        merged = bu_cli._floor_subprocess_path(original).split(os.pathsep)

        assert set(original.split(os.pathsep)) <= set(merged)
        positions = [merged.index(p) for p in original.split(os.pathsep)]
        assert positions == sorted(positions)

    @pytest.mark.platforms("posix")
    def test_floor_survives_missing_sibling_helper(self, monkeypatch):
        """If browser_tool stops exporting _merge_browser_path, the floor
        degrades to appending FHS bin dirs instead of vanishing."""
        import sys
        from types import ModuleType

        browser_tool = ModuleType("tools.browser_tool")
        browser_tool._build_browser_env = lambda: {
            "PATH": "/home/u/.nvm/versions/node/v24.18.0/bin"
        }
        monkeypatch.setitem(sys.modules, "tools.browser_tool", browser_tool)

        env = bu_cli._base_subprocess_env()

        parts = env["PATH"].split(os.pathsep)
        assert "/usr/bin" in parts
        assert "/home/u/.nvm/versions/node/v24.18.0/bin" in parts


class TestToolSurfaceSwap:
    def test_legacy_browser_tools_hidden_in_cli_mode(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_is_browser_use_cli_mode", lambda: True)
        assert bt_install.check_browser_requirements() is False
        assert bt_install.check_browser_vision_requirements() is False

    def test_browser_exec_registered_with_mode_check(self):
        from tools.registry import registry

        entry = registry.get_entry("browser_exec")
        assert entry is not None
        assert entry.check_fn is bu_cli.is_browser_use_cli_mode
        assert entry.toolset == "browser-use"

    def test_browser_exec_in_browser_toolsets(self):
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

        assert "browser_exec" in _HERMES_CORE_TOOLS
        assert "browser_exec" in TOOLSETS["browser"]["tools"]
        assert "browser_exec" in TOOLSETS["coding"]["tools"]

    def test_browser_exec_stripped_without_terminal(self, monkeypatch):
        """Sessions without the terminal surface must not regain host code
        execution through browser_exec (arbitrary Python via the CLI)."""
        monkeypatch.setattr(bu_cli, "is_browser_use_cli_mode", lambda: True)
        from tools.registry import registry

        entry = registry.get_entry("browser_exec")
        monkeypatch.setattr(entry, "check_fn", lambda: True)
        import model_tools

        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["browser"], quiet_mode=False
        )
        names = {t["function"]["name"] for t in defs}
        assert "browser_exec" not in names

    def test_browser_exec_present_with_terminal(self, monkeypatch):
        monkeypatch.setattr(bu_cli, "is_browser_use_cli_mode", lambda: True)
        from tools.registry import registry

        entry = registry.get_entry("browser_exec")
        monkeypatch.setattr(entry, "check_fn", lambda: True)
        import model_tools

        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["browser", "terminal"], quiet_mode=False
        )
        names = {t["function"]["name"] for t in defs}
        assert "browser_exec" in names


class TestVaultSupervisorAttach:
    def test_exec_attaches_supervisor_to_the_browser_it_drives(self, tmp_path, monkeypatch, _fake_supervisor_registry):
        """browser_vault_fill injects secrets only over the supervisor's CDP WebSocket. Without this attach the
        default (Browser Use) backend had no supervisor at all and every fill failed with supervisor_required."""
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": {"backend": "browser-use"}})
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho ok\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", lambda url: url)

        result = json.loads(bu_cli.browser_exec("print(1)", task_id="t-vault"))

        assert result["success"] is True
        assert _fake_supervisor_registry == [("t-vault", "ws://127.0.0.1:47000/devtools/browser/t-vault")]


class TestLegacyCloudMigration:
    """Pre-CLI direct-API Browser Use cloud configs (cloud_provider:
    "browser-use" + BROWSER_USE_API_KEY) auto-route to the CLI backend;
    Nous-gateway users stay on the legacy provider path."""

    _LEGACY = {"browser": {"cloud_provider": "browser-use"}}


    @pytest.mark.platforms("linux")
    def test_migrated_config_gets_bu_autospawn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: self._LEGACY)
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho "autospawn:$BU_AUTOSPAWN"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)"))
        assert "autospawn:1" in result["output"]

    @pytest.mark.platforms("linux")
    def test_explicit_backend_does_not_set_bu_autospawn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"backend": "browser-use"}},
        )
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho "autospawn:[$BU_AUTOSPAWN]"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)"))
        assert "autospawn:[]" in result["output"]

    def test_picker_highlights_cli_row_for_migrated_config(self, monkeypatch):
        from hermes_cli.tools_config import TOOL_CATEGORIES, _is_provider_active

        cli_row = next(
            r for r in TOOL_CATEGORIES["browser"]["providers"] if r.get("browser_backend")
        )
        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert _is_provider_active(cli_row, dict(self._LEGACY)) is True
        monkeypatch.delenv("BROWSER_USE_API_KEY")
        assert _is_provider_active(cli_row, dict(self._LEGACY)) is False


@pytest.mark.parametrize("operator,override,provider,gateway,engine,session,endpoint,expected_key,expected_env,private", [
    ("ws://operator/x", "http://override", "cloud", False, True, "", "http://engine", None, {"BU_CDP_WS": "ws://operator/x"}, False),
    ("", "http://override", "cloud", False, True, "", "http://engine", None, {"BU_CDP_URL": "http://override"}, False),
    ("", "wss://override/x", None, False, True, "named", "http://engine", None, {"BU_CDP_WS": "wss://override/x"}, False),
    ("", "", "cloud", False, True, "", "wss://cloud/x", "task", {"BU_CDP_WS": "wss://cloud/x"}, False),
    ("", "", "cloud", False, True, "research", "wss://cloud/x", "bu-named-research", {"BU_CDP_WS": "wss://cloud/x"}, True),
    ("", "", "browser-use", False, False, "research", "unused", None, {}, True),
    ("", "", "browser-use", True, False, "research", "wss://gateway/x", "bu-named-research", {"BU_CDP_WS": "wss://gateway/x"}, True),
    ("", "", None, False, True, "", "http://engine", "task", {"BU_CDP_URL": "http://engine"}, True),
    ("", "", None, False, True, "research", "http://engine", "bu-named-research", {"BU_CDP_URL": "http://engine"}, True),
    ("", "", None, False, False, "", "unused", None, {"BU_CDP_WS": "ws://127.0.0.1:47000/devtools/browser/task"}, True),
    ("", "", None, False, False, "research", "unused", None, {"BU_CDP_WS": "ws://127.0.0.1:47000/devtools/browser/bu-named-research"}, True),
])
def test_exec_cdp_precedence(monkeypatch, operator, override, provider, gateway, engine,
                             session, endpoint, expected_key, expected_env, private):
    from types import SimpleNamespace

    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {"browser": {"use_gateway": gateway}})
    monkeypatch.setenv("BU_CDP_WS", operator)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: override)
    monkeypatch.setattr("tools.browser_tool_cdp._resolve_cdp_override", lambda url: url)
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: SimpleNamespace(name=provider) if provider else None)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: engine)
    seen = []
    monkeypatch.setattr(bt_session, "_get_session_info", lambda key: seen.append(key) or {"cdp_url": endpoint})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["fixture"])
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda cmd, code, env, timeout:
                        subprocess.CompletedProcess(cmd, 0, json.dumps({"code": code, "env": env}), ""))
    tasks = ["task", "followup"] if session else ["task"]
    for task in tasks:
        result = json.loads(bu_cli.browser_exec("print('payload')", session=session, task_id=task))
        assert result["success"], result
        child = json.loads(result["output"])
        assert {k: v for k, v in child["env"].items() if k in {"BU_CDP_URL", "BU_CDP_WS"} and v} == expected_env
        assert "_HERMES_BU_PRIVATE_BROWSER" not in child["env"]
        assert ("_hermes_ensure_own_tab" in child["code"]) is bool(session and not private)
        assert child["code"].endswith("print('payload')")
        compile(child["code"], "browser-payload", "exec")
    assert seen == ([expected_key] * len(tasks) if expected_key else [])


def test_exec_without_task_uses_default_backend_identity(monkeypatch, _fake_managed_chromium):
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["fixture"])
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda cmd, *a:
                        subprocess.CompletedProcess(cmd, 0, "ok", ""))
    assert json.loads(bu_cli.browser_exec("print(1)"))["success"]
    assert _fake_managed_chromium == [("browser-exec-default", "get", ("cdp-url",))]


@pytest.mark.parametrize("provider,engine,fault,expected", [
    (True, False, "raises", "api down"), (True, False, "missing", "no CDP endpoint"),
    (False, True, "raises", "browser.engine"), (False, True, "missing", "no CDP endpoint"),
    (False, False, "missing", "Chromium browser not installed"),
])
def test_exec_backend_failure_never_launches_cli(monkeypatch, provider, engine, fault, expected):
    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: object() if provider else None)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: engine)
    def info(key):
        if fault == "raises":
            raise RuntimeError("api down")
        return {"cdp_url": None}
    monkeypatch.setattr(bt_session, "_get_session_info", info)
    monkeypatch.setattr(bt_session, "_run_browser_command", lambda *a, **kw:
                        {"success": False, "error": "Chromium browser not installed"})
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["fixture"])
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", lambda *a: pytest.fail("launched on routing failure"))
    assert expected in json.loads(bu_cli.browser_exec("print(1)"))["error"]


class TestProviderPickerIntegration:
    """The `hermes tools` Browser Automation picker row (browser_backend
    marker) must enter/leave CLI mode cleanly and highlight correctly."""

    def _rows(self):
        from hermes_cli.tools_config import TOOL_CATEGORIES

        return TOOL_CATEGORIES["browser"]["providers"]

    def test_picker_has_browser_use_cli_row(self):
        row = next(r for r in self._rows() if r.get("browser_backend"))
        assert row["browser_backend"] == "browser-use"
        assert row["name"] == "Browser Use"

    def test_picker_row_names_stay_unique(self):
        """The CLI row is named "Browser Use"; the legacy plugin API row must
        keep a distinct name — apply_provider_selection matches by name."""
        from hermes_cli.tools_config import TOOL_CATEGORIES, _plugin_browser_providers

        names = [r["name"] for r in TOOL_CATEGORIES["browser"]["providers"]]
        names += [r["name"] for r in _plugin_browser_providers()]
        assert len(names) == len(set(names))

    def test_selecting_cli_row_writes_backend_and_keeps_cloud_provider(self):
        from hermes_cli.tools_config import _write_provider_config

        row = next(r for r in self._rows() if r.get("browser_backend"))
        config = {"browser": {"cloud_provider": "browserbase"}}
        assert row["name"] == "Browser Use"
        _write_provider_config(row, config, managed_feature=None)
        assert config["browser"]["backend"] == "browser-use"
        assert config["browser"]["cloud_provider"] == "browserbase"

    def test_selecting_provider_row_keeps_cli_mode(self):
        """Backend composes with the provider: switching browser source
        (local/Browserbase/Firecrawl/gateway) keeps the driver choice."""
        from hermes_cli.tools_config import _write_provider_config

        local_row = next(
            r for r in self._rows() if r.get("browser_provider") == "local"
        )
        config = {"browser": {"backend": "browser-use"}}
        _write_provider_config(local_row, config, managed_feature=None)
        assert config["browser"]["backend"] == "browser-use"
        assert config["browser"]["cloud_provider"] == "local"

    def test_provider_row_stays_active_alongside_cli_mode(self, monkeypatch):
        from hermes_cli.tools_config import _is_provider_active

        cli_row = next(r for r in self._rows() if r.get("browser_backend"))
        local_row = next(
            r for r in self._rows() if r.get("browser_provider") == "local"
        )
        cli_config = {"browser": {"cloud_provider": "local", "backend": "browser-use"}}
        assert _is_provider_active(cli_row, cli_config) is True
        # Provider row remains highlighted: it supplies the browser the CLI
        # driver attaches to.
        assert _is_provider_active(local_row, cli_config) is True

        # Explicit off: the CLI row must not highlight even with the CLI
        # installed (default-on only applies while backend is unset).
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
        off_config = {"browser": {"cloud_provider": "local", "backend": "off"}}
        assert _is_provider_active(cli_row, off_config) is False
        assert _is_provider_active(local_row, off_config) is True

        # Backend unset: default-on — the CLI row highlights when the CLI
        # is runnable, and not when it isn't.
        default_config = {"browser": {"cloud_provider": "local"}}
        assert _is_provider_active(cli_row, default_config) is True
        assert _is_provider_active(local_row, default_config) is True
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        assert _is_provider_active(cli_row, default_config) is False


class TestBrowserUseSlashCommand:
    """/browser use [off] toggles browser.backend and resets the session,
    mirroring the /tools enable/disable flow."""

    class _Stub:
        def __init__(self):
            self.session_resets = 0

        def new_session(self):
            self.session_resets += 1

    def _run(self, cmd, config, monkeypatch):
        import hermes_cli.config as hc
        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        saved = {}
        monkeypatch.setattr(hc, "load_config", lambda: config)
        monkeypatch.setattr(hc, "save_config", lambda c: saved.update(c))
        stub = self._Stub()
        CLICommandsMixin._handle_browser_command(stub, cmd)
        return stub, saved

    def test_use_enables_backend_and_resets_session(self, monkeypatch):
        stub, saved = self._run("/browser use", {}, monkeypatch)
        assert saved["browser"]["backend"] == "browser-use"
        assert stub.session_resets == 1

    def test_use_off_pins_backend_off(self, monkeypatch):
        """`off` must be written explicitly (BACKEND_DISABLED), not removed:
        with the key merely deleted, is_legacy_browser_use_cloud_config()
        would re-activate CLI mode on the next start for anyone with
        BROWSER_USE_API_KEY set, so /browser use off wouldn't stick."""
        config = {"browser": {"backend": "browser-use"}}
        stub, saved = self._run("/browser use off", config, monkeypatch)
        assert saved["browser"]["backend"] == bu_cli.BACKEND_DISABLED
        assert stub.session_resets == 1

    def test_use_bad_arg_prints_usage_without_writing(self, monkeypatch):
        stub, saved = self._run("/browser use whatever", {}, monkeypatch)
        assert saved == {}
        assert stub.session_resets == 0


class TestNativeScreenshots:
    """Screenshots printed by capture_screenshot() attach directly to the
    model's context when it has native vision — no aux vision-LLM detour."""

    def _shot(self, tmp_path):
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        return str(shot)

    def test_find_screenshot_returns_last_fresh_path(self, tmp_path):
        a, b = self._shot(tmp_path), str(tmp_path / "b.png")
        (tmp_path / "b.png").write_bytes(b"\x89PNG fake2")
        out = f"step one saved {a}\nthen saved {b}\n"
        assert bu_cli._find_screenshot(out, since=time.time() - 5) == b

    def test_find_screenshot_rejects_stale_and_missing(self, tmp_path):
        stale = self._shot(tmp_path)
        os.utime(stale, (time.time() - 900, time.time() - 900))
        out = f"{stale}\n/nonexistent/dir/x.png\n"
        assert bu_cli._find_screenshot(out, since=time.time()) is None

    @pytest.mark.platforms("linux")
    def test_vision_model_gets_multimodal_envelope(self, tmp_path, monkeypatch):
        shot = self._shot(tmp_path)
        cli = _fake_cli(tmp_path, f'cat > /dev/null\necho "{shot}"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
        )
        monkeypatch.setattr(
            "tools.vision_tools._resize_image_for_vision",
            lambda p, **kw: "data:image/png;base64,QUJD",
        )
        result = bu_cli.browser_exec("print(capture_screenshot())")
        assert isinstance(result, dict) and result["_multimodal"] is True
        kinds = [part["type"] for part in result["content"]]
        assert kinds == ["text", "image_url"]
        assert result["meta"]["screenshot_path"] == shot
        assert shot in result["text_summary"]

    @pytest.mark.platforms("linux")
    def test_text_only_model_gets_plain_result_with_path(self, tmp_path, monkeypatch):
        shot = self._shot(tmp_path)
        cli = _fake_cli(tmp_path, f'cat > /dev/null\necho "{shot}"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: False
        )
        result = json.loads(bu_cli.browser_exec("print(capture_screenshot())"))
        assert result["screenshot_path"] == shot

    def test_no_screenshot_keeps_string_result(self, tmp_path, monkeypatch):
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho "no images here"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)"))
        assert "screenshot_path" not in result


class TestStepLabels:
    """browser_exec code leads with a `# …` comment (per the tool
    description); the TUI surfaces it as the step label and keeps the code
    collapsed behind display.tool_preview_length."""

    _CODE = "# Searching Amazon for paper towels\nnew_tab('https://amazon.com')\nwait_for_load()"

    def test_leading_comment_becomes_step_label(self):
        from agent.display import _browser_exec_step_label

        assert _browser_exec_step_label({"code": self._CODE}) == "Searching Amazon for paper towels"

    def test_no_comment_returns_none(self):
        from agent.display import _browser_exec_step_label

        assert _browser_exec_step_label({"code": "new_tab('x')"}) is None
        assert _browser_exec_step_label({"code": ""}) is None
        assert _browser_exec_step_label({"code": "#   "}) is None

    def test_label_hard_capped_regardless_of_global_setting(self):
        from agent.display import _browser_exec_step_label

        long = "# " + "x" * 200
        label = _browser_exec_step_label({"code": long})
        assert len(label) <= 80 and label.endswith("…")

    def test_preview_prefers_comment_over_code(self):
        from agent.display import build_tool_preview

        assert build_tool_preview("browser_exec", {"code": self._CODE}) == (
            "Searching Amazon for paper towels"
        )
        assert "new_tab" in build_tool_preview("browser_exec", {"code": "new_tab('x')"})

    def test_progress_line_shows_label(self):
        from agent.display import get_cute_tool_message

        line = get_cute_tool_message("browser_exec", {"code": self._CODE}, 1.2)
        assert "Searching Amazon for paper towels" in line
        assert "new_tab" not in line

@pytest.mark.parametrize("vision,lightpanda,expected", [
    (True, False, "attached to your context automatically"),
    (False, False, "cannot view images"), (True, True, "Lightpanda"),
])
def test_schema_is_dynamic_without_cli_io(monkeypatch, vision, lightpanda, expected):
    monkeypatch.setattr("tools.vision_tools._should_use_native_vision_fast_path", lambda: vision)
    monkeypatch.setattr("tools.browser_tool_lightpanda_fallback.lightpanda_engine_status", lambda: (lightpanda, ""))
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: pytest.fail("schema invoked CLI"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("schema spawned child"))
    assert expected in bu_cli._dynamic_schema_overrides()["description"]


class TestBrowserExec:
    def test_missing_cli_returns_install_hint(self, monkeypatch):
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        result = json.loads(bu_cli.browser_exec("print(page_info())"))
        assert "hermes tools" in result["error"]

    def test_empty_code_rejected(self):
        result = json.loads(bu_cli.browser_exec("   "))
        assert "error" in result

    @pytest.mark.platforms("linux")
    def test_code_piped_on_stdin(self, tmp_path, monkeypatch):
        cli = _fake_cli(tmp_path, 'code=$(cat)\necho "got:$code"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec('print("hi")'))
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert 'got:print("hi")' in result["output"]
        assert "session" not in result

    @pytest.mark.platforms("linux")
    def test_session_sets_bu_name(self, tmp_path, monkeypatch):
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho "bu:$BU_NAME"\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)", session="r7k2"))
        assert "bu:r7k2" in result["output"]
        assert result["session"] == "r7k2"

    def test_invalid_session_name_rejected(self, monkeypatch, tmp_path):
        cli = _fake_cli(tmp_path, "cat > /dev/null\n")
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)", session="bad name!"))
        assert "error" in result
        assert "session" in result["error"].lower()

    @pytest.mark.platforms("linux")
    def test_nonzero_exit_reports_failure_and_stderr(self, tmp_path, monkeypatch):
        cli = _fake_cli(tmp_path, 'cat > /dev/null\necho "boom" >&2\nexit 3\n')
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        result = json.loads(bu_cli.browser_exec("print(1)"))
        assert result["success"] is False
        assert result["exit_code"] == 3
        assert "boom" in result["stderr"]

    @pytest.mark.platforms("linux")
    def test_timeout_returns_actionable_error(self, tmp_path, monkeypatch):
        cli = _fake_cli(tmp_path, "cat > /dev/null\nsleep 30\n")
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])
        monkeypatch.setattr(bu_cli, "_MIN_TIMEOUT_S", 2)
        result = json.loads(bu_cli.browser_exec("print(1)", timeout_s=2))
        assert "timed out" in result["error"]


class TestDefaultDowngradeNotice:
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})

    def test_notice_when_default_and_cli_missing(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        notice = bu_cli.default_downgrade_notice()
        assert notice is not None
        assert "hermes tools" in notice

    def test_rate_limited_within_24h(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        assert bu_cli.default_downgrade_notice() is not None
        assert bu_cli.default_downgrade_notice() is None

    def test_no_notice_when_cli_runnable(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/usr/bin/browser-use"])
        assert bu_cli.default_downgrade_notice() is None

    def test_no_notice_on_explicit_backend(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(
            "hermes_cli.config.read_raw_config",
            lambda: {"browser": {"backend": bu_cli.BACKEND_DISABLED}},
        )
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: None)
        assert bu_cli.default_downgrade_notice() is None


class TestLightpandaHeader:
    def test_lightpanda_header_is_text_first_even_for_vision_models(self, monkeypatch):
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
        )
        monkeypatch.setattr(
            "tools.browser_tool_lightpanda_fallback.lightpanda_engine_status", lambda: (True, "used")
        )
        header = bu_cli._description_header()
        assert header.startswith(bu_cli._HEADER_BASE)
        assert header.endswith(bu_cli._HEADER_LIGHTPANDA)
        assert "goto_url(url)" in header
        assert "attached to your context automatically" not in header
        overrides = bu_cli._dynamic_schema_overrides()
        assert overrides["description"].startswith(bu_cli._HEADER_BASE)
        assert overrides["description"].endswith(bu_cli._HELPERS_DIGEST)

    def test_shadowed_engine_keeps_default_header(self, monkeypatch):
        monkeypatch.setattr(
            "tools.vision_tools._should_use_native_vision_fast_path", lambda: True
        )
        monkeypatch.setattr(
            "tools.browser_tool_lightpanda_fallback.lightpanda_engine_status", lambda: (False, "cloud")
        )
        assert bu_cli._description_header() == bu_cli._HEADER_BASE + bu_cli._HEADER_VISION


class TestLightpandaPickerRow:
    def _rows(self):
        from hermes_cli.tools_config import TOOL_CATEGORIES

        return TOOL_CATEGORIES["browser"]["providers"]

    def _row(self, name):
        return next(r for r in self._rows() if r["name"] == name)

    def test_lightpanda_row_shape(self):
        row = self._row("Lightpanda")
        assert row["browser_provider"] == "local"
        assert row["browser_engine"] == "lightpanda"
        assert row["post_setup"] == "lightpanda"
        assert row["env_vars"] == []
        # Local Browser stays the default-highlighted first row.
        assert self._rows()[0]["name"] == "Local Browser"

    def test_selecting_lightpanda_writes_engine_and_local_keeps_backend(self):
        from hermes_cli.tools_config import _write_provider_config

        config = {"browser": {"backend": "browser-use", "cloud_provider": "browserbase"}}
        _write_provider_config(self._row("Lightpanda"), config, managed_feature=None)
        assert config["browser"]["cloud_provider"] == "local"
        assert config["browser"]["engine"] == "lightpanda"
        assert config["browser"]["backend"] == "browser-use"

    def test_selecting_local_browser_resets_engine(self):
        from hermes_cli.tools_config import _write_provider_config

        config = {"browser": {"cloud_provider": "local", "engine": "lightpanda"}}
        _write_provider_config(self._row("Local Browser"), config, managed_feature=None)
        assert config["browser"]["engine"] == "auto"

    def test_active_row_follows_engine(self):
        from hermes_cli.tools_config import _is_provider_active

        lp_row, local_row = self._row("Lightpanda"), self._row("Local Browser")
        lp_cfg = {"browser": {"cloud_provider": "local", "engine": "lightpanda"}}
        assert _is_provider_active(lp_row, lp_cfg) is True
        assert _is_provider_active(local_row, lp_cfg) is False
        default_cfg = {"browser": {"cloud_provider": "local"}}
        assert _is_provider_active(lp_row, default_cfg) is False
        assert _is_provider_active(local_row, default_cfg) is True
        cloud_cfg = {"browser": {"cloud_provider": "browserbase", "engine": "lightpanda"}}
        assert _is_provider_active(lp_row, cloud_cfg) is False


class TestLightpandaStatusLine:
    def _status(self, monkeypatch, *, used, reason, binary="/opt/lightpanda"):
        import contextlib
        import io

        from hermes_cli.cli_commands_mixin import CLICommandsMixin

        monkeypatch.setattr(bu_cli, "is_browser_use_cli_mode", lambda: True)
        monkeypatch.setattr("tools.browser_tool_lightpanda_fallback._using_lightpanda_engine", lambda: True)
        monkeypatch.setattr("tools.browser_tool_lightpanda_fallback.lightpanda_engine_status", lambda: (used, reason))
        monkeypatch.setattr("tools.browser_lightpanda.find_lightpanda_binary", lambda: binary)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            CLICommandsMixin._handle_browser_command(object(), "/browser status")
        return buf.getvalue()

    def test_status_reports_lightpanda_in_use(self, monkeypatch):
        out = self._status(monkeypatch, used=True, reason="Browser Use mode: Hermes spawns `lightpanda serve` per session")
        assert "Engine: Lightpanda" in out
        assert "spawns `lightpanda serve`" in out
        assert "Binary: /opt/lightpanda" in out

    def test_status_reports_missing_binary(self, monkeypatch):
        out = self._status(monkeypatch, used=True, reason="x", binary=None)
        assert "lightpanda binary not found" in out

    def test_status_reports_shadowed_engine(self, monkeypatch):
        out = self._status(monkeypatch, used=False, reason="cloud provider Browserbase is selected")
        assert "NOT in use" in out
        assert "Browserbase" in out


class TestTimeoutProcessGroupKill:
    """#106244: on timeout the whole CLI process group must die. A pipe-holding
    grandchild (browser_harness daemon / Chrome helper) otherwise keeps communicate()
    blocked forever, and the wedged call's activity heartbeat pins the session at
    "now" in the sidebar indefinitely."""

    @pytest.mark.platforms("posix")
    def test_timeout_kills_grandchild_and_returns_promptly(self, tmp_path, monkeypatch):
        """A grandchild that outlives the direct child and holds the inherited stdout
        pipe must not keep browser_exec blocked past the timeout (it wedged permanently
        before the group kill)."""
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {})
        pid_file = tmp_path / "grandchild.pid"
        cli = _fake_cli(tmp_path, (
            "cat > /dev/null\n"
            "sleep 60 &\n"
            "echo $! > \"" + str(pid_file) + "\"\n"
            "sleep 60\n"
        ))
        monkeypatch.setattr(bu_cli, "_find_cli", lambda: [cli])

        start = time.time()
        result = json.loads(bu_cli.browser_exec("print(1)", timeout_s=bu_cli._MIN_TIMEOUT_S))
        elapsed = time.time() - start

        assert "timed out" in result["error"]
        # Pre-fix, this hangs until the 60s sleeps expire — and forever with a daemon child.
        assert elapsed < 30
        # The pipe-holding grandchild died with the group instead of leaking.
        pid = int(pid_file.read_text().strip())
        time.sleep(0.5)
        with pytest.raises(OSError):
            os.kill(pid, 0)

    def test_post_kill_drain_is_bounded(self, monkeypatch):
        """If even the post-kill drain misses its deadline, give up instead of wedging."""

        class _StuckProc:
            pid = 424243
            returncode = None

            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired("browser-use", timeout)

        monkeypatch.setattr(bu_cli.subprocess, "Popen", lambda *a, **k: _StuckProc())
        monkeypatch.setattr(bu_cli, "_kill_cli_process_group", lambda proc: None)
        with pytest.raises(subprocess.TimeoutExpired):
            bu_cli._run_cli_killing_process_group(["x"], "code", {}, 5)
