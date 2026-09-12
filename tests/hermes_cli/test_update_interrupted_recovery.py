"""An interrupted old updater must retry with a complete source handoff.

The historical .update-incomplete writer is a relaunch shim, not a state
protocol. PM recovery tests own dependency repair; this boundary must refuse
completion when an old in-flight caller cannot provide the new request.
"""

import pytest

from hermes_cli import main as cli_main, update_cmd


def test_incomplete_handoff_requires_explicit_update_retry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        update_cmd, "run_completion",
        lambda request: pytest.fail("cannot launch completion without captured update state"),
    )

    with pytest.raises(SystemExit) as error:
        update_cmd._complete_source_update(None)

    assert error.value.code == 1
    output = capsys.readouterr()
    assert "run `hermes update` again" in output.err
    assert "Update complete" not in output.out
    assert not (tmp_path / ".update-incomplete").exists()
    assert not (tmp_path / ".lazy-refresh-incomplete").exists()
