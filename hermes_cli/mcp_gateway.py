"""
MCP server exposing the hermes gateway's /v1/runs API as tools.

Designed to be spawned by Claude Code (or any other MCP client) via
stdio. Lets the model submit fire-and-forget jobs at the gateway,
poll status, tail events, and stop runs — without ever asking the
user for a curl invocation.

Configuration (same precedence as `hermes submit`):
  HERMES_GATEWAY_URL          default: https://hermes-gw-01.tail19c543.ts.net
  HERMES_GATEWAY_API_KEY      bearer token (the "default" laptop principal)
  API_SERVER_KEY              legacy alias accepted as fallback

Wired up via Claude Code config:
  ~/.claude.json (mcp.servers.hermes_gw):
      {
        "command": "hermes",
        "args": ["mcp-gateway"],
        "env": {}
      }

Tools exposed:
  submit_task          POST /v1/runs               — start a run
  get_run_status       GET  /v1/runs/{id}          — poll status / output
  tail_run_events      GET  /v1/runs/{id}/events   — recent SSE events
  stop_run             POST /v1/runs/{id}/stop     — kill an in-flight run
  list_recent_runs                                  — recent audit-log entries
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

# The `mcp` package is in the hermes-agent venv (FastMCP-based servers).
from mcp.server.fastmcp import FastMCP


logger = logging.getLogger(__name__)


DEFAULT_GATEWAY_URL = "https://hermes-gw-01.tail19c543.ts.net"
DEFAULT_AUDIT_LOG_PATH = "/var/log/hermes-gateway/audit.log"
DEFAULT_SSH_HOST = "hermes-gw-01.tail19c543.ts.net"


def _resolve_base_url() -> str:
    return (os.getenv("HERMES_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")


def _resolve_bearer() -> str:
    """Bearer token — same chain `hermes submit` uses."""
    return (
        os.getenv("HERMES_GATEWAY_API_KEY")
        or os.getenv("API_SERVER_KEY")
        or _read_hermes_env("HERMES_GATEWAY_API_KEY")
        or _read_hermes_env("API_SERVER_KEY")
        or ""
    )


def _read_hermes_env(name: str) -> str:
    """Best-effort lookup in ~/.hermes/.env so the MCP server doesn't
    require Claude Code's env: block to be populated."""
    path = os.path.expanduser("~/.hermes/.env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _headers() -> Dict[str, str]:
    bearer = _resolve_bearer()
    out = {"Content-Type": "application/json"}
    if bearer:
        out["Authorization"] = f"Bearer {bearer}"
    return out


def _http_error(prefix: str, status: int, body: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": f"{prefix}: gateway returned HTTP {status}",
        "body": body[:500],
    }


# ─── FastMCP server with the gateway tools ──────────────────────────────────

mcp = FastMCP("hermes-gateway")


@mcp.tool()
def submit_task(prompt: str, instructions: Optional[str] = None) -> Dict[str, Any]:
    """Submit a fire-and-forget task to the remote hermes gateway.

    The run executes server-side on the LXC; this call returns
    immediately with a `run_id`. Use `get_run_status(run_id)` to poll
    until terminal, or `tail_run_events(run_id)` for the event stream.

    Args:
        prompt: The user message / task description for the agent.
        instructions: Optional ephemeral system-prompt override.

    Returns:
        On success: ``{"ok": true, "run_id": "run_…", "gateway": "...",
                       "status_url": "...", "events_url": "..."}``.
        On failure: ``{"ok": false, "error": "...", "body": "..."}``.
    """
    import httpx

    payload: Dict[str, Any] = {"input": prompt}
    if instructions:
        payload["instructions"] = instructions

    url = f"{_resolve_base_url()}/v1/runs"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"submit_task: {type(e).__name__}: {e}"}

    if r.status_code >= 400:
        return _http_error("submit_task", r.status_code, r.text)

    try:
        body = r.json()
    except json.JSONDecodeError:
        return _http_error("submit_task", r.status_code, r.text)

    run_id = body.get("id") or body.get("run_id")
    gw = _resolve_base_url()
    return {
        "ok": True,
        "run_id": run_id,
        "gateway": gw,
        "status_url": f"{gw}/v1/runs/{run_id}",
        "events_url": f"{gw}/v1/runs/{run_id}/events",
        "initial_status": body.get("status"),
    }


@mcp.tool()
def get_run_status(run_id: str) -> Dict[str, Any]:
    """Poll a run's status / output / token usage.

    Args:
        run_id: A run_id from `submit_task`.

    Returns:
        On success: the gateway's run record (status, output, usage,
        timestamps, etc.) plus ``"ok": true``. On failure: an error dict.
    """
    import httpx

    url = f"{_resolve_base_url()}/v1/runs/{run_id}"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"get_run_status: {type(e).__name__}: {e}"}

    if r.status_code == 404:
        return {"ok": False, "error": f"run_id {run_id} not found"}
    if r.status_code >= 400:
        return _http_error("get_run_status", r.status_code, r.text)

    try:
        return {"ok": True, **r.json()}
    except json.JSONDecodeError:
        return _http_error("get_run_status", r.status_code, r.text)


@mcp.tool()
def tail_run_events(run_id: str, max_events: int = 30, timeout_seconds: float = 10.0) -> Dict[str, Any]:
    """Pull recent SSE events from a run's event stream.

    The stream is unbounded server-side; this tool reads up to
    `max_events` events or until `timeout_seconds` elapses, then
    detaches. Useful for a quick check on a long-running job.

    Args:
        run_id: A run_id from `submit_task`.
        max_events: Cap on events to return (default 30).
        timeout_seconds: Stop reading after this many seconds (default 10).

    Returns:
        On success: ``{"ok": true, "events": [...]}`` where each event
        is the parsed JSON payload from a `data:` line. On failure:
        an error dict.
    """
    import time as _time

    import httpx

    url = f"{_resolve_base_url()}/v1/runs/{run_id}/events"
    headers = {**_headers(), "Accept": "text/event-stream"}
    events: List[Any] = []
    deadline = _time.time() + max(0.1, timeout_seconds)

    try:
        with httpx.stream("GET", url, headers=headers, timeout=timeout_seconds + 1) as r:
            if r.status_code >= 400:
                return _http_error("tail_run_events", r.status_code, r.read().decode("utf-8", "replace"))
            for raw in r.iter_lines():
                if _time.time() >= deadline:
                    break
                if len(events) >= max_events:
                    break
                if not raw or not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    events.append({"raw": payload})
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"tail_run_events: {type(e).__name__}: {e}", "events": events}

    return {"ok": True, "events": events, "stopped_at_cap": len(events) >= max_events}


@mcp.tool()
def stop_run(run_id: str) -> Dict[str, Any]:
    """Stop an in-flight run.

    Sends POST /v1/runs/{id}/stop. The run becomes ``cancelled`` and
    the agent process is asked to terminate. Idempotent — calling on
    an already-terminal run returns success without effect.

    Args:
        run_id: A run_id from `submit_task`.

    Returns:
        ``{"ok": true, "status": "..."}`` on success, else an error dict.
    """
    import httpx

    url = f"{_resolve_base_url()}/v1/runs/{run_id}/stop"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(url, headers=_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"stop_run: {type(e).__name__}: {e}"}

    if r.status_code >= 400:
        return _http_error("stop_run", r.status_code, r.text)

    try:
        return {"ok": True, **r.json()}
    except json.JSONDecodeError:
        return {"ok": True, "status": "stop_requested"}


@mcp.tool()
def list_recent_runs(limit: int = 20) -> Dict[str, Any]:
    """Read recent /v1/runs submissions from the gateway's audit log.

    The audit log is server-side at /var/log/hermes-gateway/audit.log.
    We tail it via SSH using the same Tailscale hostname the rest of
    the tools use. Each line is one submission; the model gets
    (timestamp, principal, run_id, prompt_sha256, remote).

    Args:
        limit: Most recent N entries to return (default 20).

    Returns:
        ``{"ok": true, "entries": [...]}`` on success — each entry is
        a parsed JSON record. ``{"ok": false, "error": "..."}`` on
        failure (ssh/permissions/etc.).
    """
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        DEFAULT_SSH_HOST,
        f"tail -n {int(limit)} {DEFAULT_AUDIT_LOG_PATH}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": f"list_recent_runs: {type(e).__name__}: {e}"}

    if result.returncode != 0:
        return {
            "ok": False,
            "error": f"ssh exited {result.returncode}",
            "stderr": result.stderr.strip()[:500],
        }

    entries = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"raw": line})
    return {"ok": True, "entries": entries}


def main() -> None:
    """Entry point — runs the stdio MCP server until the client disconnects."""
    mcp.run()


if __name__ == "__main__":
    main()
