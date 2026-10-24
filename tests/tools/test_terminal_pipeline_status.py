"""The bash wrapper reports each pipeline segment's own exit status."""

import subprocess

import pytest

from tools.environments.base_session_env import _wrap_command_script

CWD_MARKER = "__HERMES_CWD_test__"
PIPE_MARKER = "__HERMES_PIPE_test__"


def _script(command: str) -> str:
    return _wrap_command_script(
        command, quoted_cwd="/tmp", quoted_snap="/nonexistent-snapshot",
        snap_tmp_template="/tmp/hermes-snap.XXXXXX", passthrough_names=(),
        snapshot_ready=False, cwd_marker=CWD_MARKER, pipe_marker=PIPE_MARKER,
    )


def _run_result(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", _script(command)], capture_output=True, text=True, check=False)


def _run(command: str) -> str:
    return _run_result(command).stdout


def test_pipeline_status_captures_the_leading_command():
    output = _run("false | true")

    assert f"{PIPE_MARKER}1 0{PIPE_MARKER}" in output


def test_plain_command_reports_one_status():
    output = _run("false")

    assert f"{PIPE_MARKER}1{PIPE_MARKER}" in output


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("if false; then echo yes; fi", 0),
        ("while read line; do echo \"$line\"; done < /dev/null", 0),
        ("set -o pipefail; false | true", 1),
        ("! false", 0),
        ("! true", 1),
        ("echo 'unbalanced", 2),
        ("echo hi \\", 0),
        ("exit 3", 3),
    ],
)
def test_wrapper_preserves_shell_exit_code(command, expected):
    """``PIPESTATUS`` is side data: the wrapper must return the shell's own ``$?``."""
    assert _run_result(command).returncode == expected


def test_wrapper_does_not_leak_internals_on_syntax_error():
    result = _run_result("echo 'unbalanced")

    assert "__hermes_ps" not in result.stderr


def test_wrapper_does_not_leak_internals_after_a_trailing_backslash():
    result = _run_result("echo hi \\")

    assert result.returncode == 0
    assert "__hermes_ps" not in result.stdout
    assert "__hermes_ps" not in result.stderr
