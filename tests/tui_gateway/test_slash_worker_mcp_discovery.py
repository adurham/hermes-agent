"""Integration coverage for profile-local MCP discovery in slash workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import textwrap
import threading

import pytest
import yaml

pytest.importorskip("mcp.server.fastmcp")


def test_profile_local_mcp_tool_is_visible_in_slash_worker(tmp_path):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    marker = "profile-local-61922"
    server = tmp_path / "fastmcp_probe.py"
    server.write_text(
        textwrap.dedent(
            f"""
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("profileprobe")

            @mcp.tool()
            def hermes_61922_profile_probe() -> str:
                return {marker!r}

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "profileprobe": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                },
                # Default mcp_discovery_timeout (1.5s, see hermes_cli/config.py
                # DEFAULT_CONFIG) is tuned for real-world servers that are
                # already warm; spawning a fresh Python interpreter + fastmcp
                # subprocess from cold start here routinely exceeds it, so
                # wait_for_mcp_discovery() in the worker returns to the first
                # /tools call before discovery has actually registered the
                # probe tool. Widen it so this test's assertion reflects a
                # completed discovery, not a race against the worker's
                # startup wait bound.
                "mcp_discovery_timeout": 15.0,
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            env.pop(key)
    # Strip pytest's own PYTEST_CURRENT_TEST marker before it leaks into the
    # spawned worker. cli.py checks this env var (see `_arm_exit_watchdog` /
    # `_install_cleanup_skip_handler`) to detect "running under pytest" and
    # changes its own shutdown behavior — but this IS the production worker
    # binary being exercised as a real subprocess, not an in-process pytest
    # call, so it should behave exactly like it would for a real user.
    env.pop("PYTEST_CURRENT_TEST", None)
    env["HERMES_HOME"] = str(profile_home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["HERMES_SLASH_WATCHDOG_GRACE_S"] = "0"
    env["HERMES_SLASH_WATCHDOG_POLL_S"] = "0.05"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "tui_gateway.slash_worker",
            "--session-key",
            "agent:main:tui:dm:mcp-profile-test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    output: queue.Queue[str] = queue.Queue()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdout = proc.stdout
        threading.Thread(
            target=lambda: output.put(stdout.readline()),
            daemon=True,
        ).start()
        proc.stdin.write(json.dumps({"id": 1, "command": "/tools"}) + "\n")
        proc.stdin.flush()
        try:
            # Bound raised past mcp_discovery_timeout (15s, set above) plus
            # margin for interpreter/fastmcp subprocess spawn — see the
            # comment on mcp_discovery_timeout for why the previous default
            # (10s here vs the old 1.5s discovery bound) was a race, not a
            # generous margin.
            line = output.get(timeout=25)
        except queue.Empty:
            pytest.fail("slash worker produced no /tools response within 25 seconds")
        response = json.loads(line)
        assert response["ok"] is True
        # This fork registers MCP tools WITHOUT the upstream "mcp_" prefix
        # (see tools/mcp_tool.py::is_mcp_tool_parallel_safe docstring):
        # tools are named "{server}_{tool}" here, not upstream's
        # "mcp__{server}__{tool}". This assertion used the upstream naming
        # convention and never matched anything this fork actually registers.
        assert "profileprobe_hermes_61922_profile_probe" in response["output"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
