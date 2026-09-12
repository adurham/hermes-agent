"""Update retries use the same dependency transaction as a newly pulled tree."""

import json
import subprocess
import venv
from types import SimpleNamespace

import pytest

import pm
from hermes_cli import main, update_cmd






@pytest.mark.parametrize("failure", [
    pm.InstallError("venv", "conflict"),
    subprocess.CalledProcessError(23, ["python", "-m", "hermes_cli.source_build"]),
])
def test_command_reports_failed_preparation_and_releases_lock(tmp_path, monkeypatch, capsys, failure):
    from hermes_cli import update_lock, update_receipt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    finalized = []
    monkeypatch.setattr(main, "_finalize_update_output", finalized.append)

    def fail(args, gateway_mode):
        update_receipt.begin_update_receipt()
        raise failure

    monkeypatch.setattr(update_cmd, "_cmd_update_impl", fail)
    with pytest.raises(SystemExit) as error:
        main.cmd_update(SimpleNamespace(gateway=True))
    assert error.value.code == 1
    receipt = update_receipt.read_latest_receipt()
    assert receipt is not None
    assert receipt["exit_code"] == 1
    assert receipt["outcome"] == "failed"
    assert (tmp_path / ".update_exit_code").read_text().strip() == "1"
    assert finalized == [None]
    lock = update_lock.UpdateLock()
    assert lock.acquire()
    lock.release()
    assert "Update failed" in capsys.readouterr().out
