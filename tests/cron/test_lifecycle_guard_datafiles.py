"""Oversized DATA files referenced by a command are not unscannable scripts.

The referenced-script walk tokenizes a Python heredoc body with shell rules, so
``LogReader(os.path.expanduser('~/route/rlog.zst'))`` puts ``~/route/rlog.zst`` in command
position. A >1 MiB compressed log then failed closed as an "oversized script" and blocked a
read-only analysis command. Only a file that provably cannot execute is exempt; scripts and
executables above the scan cap still fail closed.
"""

from __future__ import annotations

import os

import pytest

from cron import lifecycle_guard

_OVER_CAP = lifecycle_guard._MAX_REFERENCED_SCRIPT_BYTES + 1
# zstd frame magic + header: a NUL in the first line, like real openpilot rlog.zst files.
_ZSTD_PREFIX = b"\x28\xb5\x2f\xfd\x00\x60\x64\x95"


def _write(path, prefix: bytes, *, executable: bool = False):
    path.write_bytes(prefix + b"\n" + b"A" * _OVER_CAP)
    os.chmod(path, 0o755 if executable else 0o644)
    return path


def _analysis_command(target) -> str:
    """The real blocked shape: an env-prefixed Python heredoc reading the file."""
    return (
        f"cd {target.parent}\n"
        "env -u PYTHONPATH python3 - <<'EOF'\n"
        "import os\n"
        "from openpilot.tools.lib.logreader import LogReader\n"
        f"lr=LogReader(os.path.expanduser('{target}'))\n"
        "EOF"
    )


def test_oversized_zst_input_is_allowed(tmp_path):
    rlog = _write(tmp_path / "rlog.zst", _ZSTD_PREFIX)
    assert lifecycle_guard.scan_gateway_lifecycle(
        _analysis_command(rlog), cwd=str(tmp_path)
    ) == (False, None)


def test_oversized_data_input_with_lifecycle_command_in_command_text_still_blocks(tmp_path):
    """The exemption only skips the unreadable file; the command itself is still scanned."""
    rlog = _write(tmp_path / "rlog.zst", _ZSTD_PREFIX)
    command = _analysis_command(rlog) + "\nhermes gateway restart"
    assert lifecycle_guard.scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


@pytest.mark.parametrize("form", ["bash {p}", "sh {p}", ". {p}", "source {p}", "{p}"])
def test_oversized_shell_script_still_fails_closed(tmp_path, form):
    script = _write(tmp_path / "big.sh", b"#!/bin/sh", executable=form == "{p}")
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(form.format(p=script), cwd=str(tmp_path))
    assert unsafe is True and "scan cap" in refusal


def test_shell_read_of_nul_prefixed_data_still_fails_closed(tmp_path):
    """A shell READS its operand whatever the mode/prefix: never exempt `bash file`."""
    blob = _write(tmp_path / "rlog.zst", _ZSTD_PREFIX)
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(f"bash {blob}", cwd=str(tmp_path))
    assert unsafe is True and "scan cap" in refusal


@pytest.mark.parametrize(
    "name, prefix",
    [
        ("big.py", b"\x00import os"),  # .py suffix wins even over a NUL prefix
        ("analyze", b"#!/usr/bin/env python3"),  # extensionless shebang script, no x bit
    ],
)
def test_oversized_python_script_referenced_as_argument_still_fails_closed(tmp_path, name, prefix):
    script = _write(tmp_path / name, prefix)
    command = (
        "env -u PYTHONPATH python3 - <<'EOF'\n"
        f"import runpy\nrunpy.run_path('{script}')\nEOF"
    )
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe is True and "scan cap" in refusal


@pytest.mark.parametrize(
    "prefix, executable",
    [
        (_ZSTD_PREFIX, True),  # execute bit set: binary-looking or not, it may run
        (b"hermes gateway restart", False),  # text: a later `chmod +x` makes it a shell script
        (b"A" * 80 + b"\x00", False),  # NUL past the 80-byte window bash/dash inspect
    ],
)
def test_unusual_extension_that_could_execute_still_fails_closed(tmp_path, prefix, executable):
    tool = _write(tmp_path / "payload.dat", prefix, executable=executable)
    for command in (f"{tool}", f"chmod +x {tool}; {tool}", _analysis_command(tool)):
        unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe is True and "scan cap" in refusal, command
