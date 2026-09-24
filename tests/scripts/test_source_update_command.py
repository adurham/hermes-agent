"""Source E2E updates must follow the staged git branch when supported."""

import os
from pathlib import Path
import subprocess

import pytest


HELPER = Path(__file__).resolve().parents[1] / "install/e2e-assets/source-update-command.sh"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        ("update [--yes] [--branch NAME]", "update --yes --branch main"),
        ("update [--yes]", "update --yes"),
        ("update [--switch-branch]", "update"),
        ("update", "update"),
    ],
)
def test_source_update_follows_staged_main_if_installed_cli_accepts_branch(tmp_path, help_text, expected):
    cli = tmp_path / "installed hermes"
    cli.write_text('''#!/usr/bin/env bash
if [[ "$*" == *"--branch main"* ]]; then
  printf 'selected staged main: %s\\n' "$*"
elif [[ "$EXPECT_BRANCH" == 1 ]]; then
  printf 'Channel object not found: releases/channels/main.json\\n' >&2
  exit 1
else
  printf 'legacy updater: %s\\n' "$*"
fi
''', encoding="utf-8")
    cli.chmod(0o755)
    result = subprocess.run(
        ["bash", "-euc", 'source "$HELPER"; build_source_update_command "$CLI" "$HELP_TEXT"; "${update_cmd[@]}"'],
        env=dict(os.environ, HELPER=str(HELPER), CLI=str(cli), HELP_TEXT=help_text,
                 EXPECT_BRANCH="1" if "--branch NAME" in help_text else "0"),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(expected)
    assert "Channel object not found" not in result.stderr
