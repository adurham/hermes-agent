"""browser_exec ``capture_screenshot()`` on a hidden tab (tools/browser_use_shot.py).

Chrome draws no frame for a hidden tab, and Browser Use cloud never answers a png
``Page.captureScreenshot`` for one, so the harness helper hung for its 60 s IPC timeout.
``browser_exec`` below runs the real harness CLI it launches in production; only the daemon transport
is faked.
"""
import json
import os
import sys
import textwrap

import pytest
from PIL import Image

import tools.browser_use_cli as bu_cli
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud

# sitecustomize on the harness's PYTHONPATH: the real browser_harness CLI runs, only its daemon transport is
# faked. A png Page.captureScreenshot on a hidden target raises the harness's own IPC timeout.
_FAKE_TRANSPORT = textwrap.dedent("""\
    import base64, io, json, os
    from PIL import Image
    import browser_harness.helpers as h

    LOG = os.environ["FAKE_CDP_LOG"]
    state = {"hidden": True, "activate_works": os.environ["FAKE_ACTIVATE_WORKS"] == "1"}

    def log(entry):
        with open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\\n")

    def image(fmt):
        buf = io.BytesIO()
        Image.new("RGB", (8, 6), "red").save(buf, format=fmt.upper())
        return base64.b64encode(buf.getvalue()).decode()

    def cdp(method, session_id=None, _response_timeout=5.0, **params):
        log({"method": method, "timeout": _response_timeout, **params})
        if method == "Runtime.evaluate":
            return {"result": {"value": "hidden" if state["hidden"] else "visible"}}
        if method == "Target.activateTarget":
            if not state["activate_works"]:
                raise RuntimeError("Target.activateTarget wasn't found")
            state["hidden"] = False
            return {}
        if method == "Page.captureScreenshot":
            if params.get("format") == "png" and state["hidden"]:
                raise h._IPCResponseTimeout(f"{method} timed out after {_response_timeout:g}s")
            return {"data": image(params["format"])}
        raise AssertionError(method)

    h.cdp = cdp
    h.current_tab = lambda: {"targetId": "T1", "target_id": "T1", "url": "about:blank", "title": ""}
    import browser_harness.admin as admin, browser_harness.telemetry as telemetry
    admin.ensure_daemon = admin.print_update_banner = lambda *a, **k: None
    admin.daemon_alive = lambda *a, **k: True
    telemetry.is_enabled = lambda: False
    telemetry.capture_cli_event = lambda *a, **k: None
""")


@pytest.fixture
def run_exec(monkeypatch, tmp_path):
    """browser_exec on the local route; the real harness CLI with the fake transport above."""
    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "")
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
    monkeypatch.setattr(bs, "get_session_info",
                        lambda key: {"cdp_url": f"ws://127.0.0.1:47000/devtools/browser/{key}",
                                     "features": {"local": True}})
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    monkeypatch.setattr(bu_cli, "_exec_route_is_sidecar", {}, raising=False)
    monkeypatch.setattr(bu_cli, "_driven_daemons", {})
    fake_dir = tmp_path / "fake_transport"
    fake_dir.mkdir()
    (fake_dir / "sitecustomize.py").write_text(_FAKE_TRANSPORT, encoding="utf-8")
    site_dir = bu_cli._harness_site_dir()
    monkeypatch.setattr(bu_cli, "_harness_site_dir", lambda: os.pathsep.join([str(fake_dir), site_dir]))
    # tests/tools/conftest.py hides the CLI; this is _find_cli()'s production command
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [sys.executable, "-m", "browser_harness.run"])
    log = tmp_path / "cdp.jsonl"

    def run(activate_works: bool):
        monkeypatch.setenv("FAKE_CDP_LOG", str(log))
        monkeypatch.setenv("FAKE_ACTIVATE_WORKS", "1" if activate_works else "0")
        shot = tmp_path / "shot.png"
        result = bu_cli.browser_exec(f"print(capture_screenshot({str(shot)!r}))", task_id="t", timeout_s=60)
        if not isinstance(result, str):  # a native screenshot result (image attached)
            result = json.dumps({"success": True, "screenshot_path": str(shot)})
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return json.loads(result), shot, calls

    return run


@pytest.mark.parametrize("activate_works", [True, False], ids=["activated", "activation-unsupported"])
def test_hidden_tab_screenshot_returns_a_png_instead_of_hanging(run_exec, activate_works):
    result, shot, calls = run_exec(activate_works)

    assert result.get("success") is True, result.get("stderr") or result
    with Image.open(shot) as img:
        assert img.format == "PNG"  # the path says .png, so a jpeg fallback is re-encoded
    shots = [c for c in calls if c["method"] == "Page.captureScreenshot"]
    assert shots[0]["format"] == "png" and shots[0]["timeout"] <= 10, shots
    if activate_works:  # the tab was brought to the front, so png answered first time
        assert [c["method"] for c in calls].index("Target.activateTarget") < calls.index(shots[0])
        assert len(shots) == 1
    else:  # png never comes for a hidden tab: one bounded png, then jpeg
        assert [s["format"] for s in shots] == ["png", "jpeg"]
