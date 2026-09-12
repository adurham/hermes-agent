"""All source selection routes hand off once, without old-process maintenance."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import update_cmd, update_cmd_zip


@pytest.mark.parametrize("hook,args,kwargs", [
    (update_cmd._prepare_updated_checkout, ("unused",), {"desktop": False}),
    (update_cmd._reload_config_modules, (), {}),
    (update_cmd._reload_process_scan_modules, (), {}),
    (update_cmd._run_pending_fleet_restart, (), {}),
])
def test_historical_completion_hook_never_reports_success(hook, args, kwargs, capsys):
    with pytest.raises(SystemExit) as error:
        hook(*args, **kwargs)
    assert error.value.code != 0
    assert "update" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("route", ["pulled", "current", "zip"])
def test_every_route_hands_off_once(route, tmp_path, monkeypatch):
    request = {"branch": "main", "receipt": {"update_id": "c" * 32}}
    handed_off = []
    monkeypatch.setattr(update_cmd, "_complete_source_update", handed_off.append, raising=False)
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        PROJECT_ROOT=tmp_path, _resolve_update_branch=lambda args: "main"))
    monkeypatch.setattr(update_cmd, "_verify_head_after_pull", lambda *a, **kw: "new-sha")
    monkeypatch.setattr(update_cmd, "_prepare_updated_checkout", lambda *a, **kw: pytest.fail("old-process preparation"))
    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", lambda **kw: None)
    monkeypatch.setattr(update_cmd, "_sweep_bytecode_after_update", lambda *a: None)
    monkeypatch.setattr(update_cmd_zip, "_abort_zip_update_if_dirty_tree", lambda: None)
    swap = Mock()
    monkeypatch.setattr(update_cmd_zip, "_download_and_swap_zip", swap)
    plan = SimpleNamespace(in_place_update=False, auto_stash_ref=None, parked_branch_switched=False,
                           upstream_checked=True)
    opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, pre_update_version="old")
    if route == "pulled":
        update_cmd._apply_pulled_update(
            ["git"], "main", "old-sha", plan, opts, is_fork=False, _windows_gateway_resume=None,
            completion_request=request)
    elif route == "current":
        update_cmd._finish_already_up_to_date(
            ["git"], "main", "main", plan, gw_input_fn=None, completion_request=request)
    else:
        assert update_cmd_zip._update_via_zip(SimpleNamespace(), completion_request=request) is True
        swap.assert_called_once()
    assert handed_off == [request]
