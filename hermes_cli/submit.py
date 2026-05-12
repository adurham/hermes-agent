"""
Submit subcommand for hermes CLI — fire a prompt at a remote gateway.

Posts to ``POST /v1/runs`` on the configured gateway, prints the
returned ``run_id``, and exits. Lets the laptop close while the run
continues server-side. Status updates can be tailed locally via
``--tail`` (SSE stream) or watched in the gateway's discord channel
when the discord adapter is the one that submitted the run (separate
code path on the gateway side — out of scope for this CLI).

Configuration (in precedence order):
  * --gateway-url / --api-key flags
  * HERMES_GATEWAY_URL / HERMES_GATEWAY_API_KEY env vars
  * ~/.hermes/.env values for the same names
  * Defaults: http://172.16.0.50:8642 and no key (will fail auth on
    a network-bound gateway because of the bind_guard in
    gateway/platforms/api_server.py:3372)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional


DEFAULT_GATEWAY_URL = "http://172.16.0.50:8642"


@dataclass
class _GatewayTarget:
    base_url: str
    api_key: str
    source: str  # diagnostic: where the URL came from


def _resolve_target(args) -> _GatewayTarget:
    """Pick the gateway URL + API key from flags / env / hermes home env."""
    # Lazy import so `hermes --help` etc. don't pay for hermes_cli.config.
    from hermes_cli.config import get_env_value

    base_url = (
        getattr(args, "gateway_url", None)
        or os.environ.get("HERMES_GATEWAY_URL")
        or get_env_value("HERMES_GATEWAY_URL")
        or DEFAULT_GATEWAY_URL
    ).rstrip("/")

    api_key = (
        getattr(args, "api_key", None)
        or os.environ.get("HERMES_GATEWAY_API_KEY")
        or os.environ.get("API_SERVER_KEY")
        or get_env_value("HERMES_GATEWAY_API_KEY")
        or get_env_value("API_SERVER_KEY")
        or ""
    )

    if getattr(args, "gateway_url", None):
        source = "--gateway-url"
    elif os.environ.get("HERMES_GATEWAY_URL"):
        source = "env HERMES_GATEWAY_URL"
    elif get_env_value("HERMES_GATEWAY_URL"):
        source = "~/.hermes/.env HERMES_GATEWAY_URL"
    else:
        source = f"default ({DEFAULT_GATEWAY_URL})"

    return _GatewayTarget(base_url=base_url, api_key=api_key, source=source)


def _read_prompt(args) -> str:
    """Resolve the prompt: positional arg, --file path, or stdin."""
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8") as f:
            return f.read()
    parts = getattr(args, "prompt", None) or []
    if parts:
        return " ".join(parts)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(
        "submit: no prompt provided. Pass it as a positional, via --file PATH, "
        "or on stdin (e.g. `cat task.md | hermes submit`)."
    )


def _post_run(target: _GatewayTarget, prompt: str, *, instructions: Optional[str]) -> dict:
    """POST /v1/runs and return the parsed JSON response."""
    import httpx

    payload = {"input": prompt}
    if instructions:
        payload["instructions"] = instructions

    headers = {"Content-Type": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"

    url = f"{target.base_url}/v1/runs"
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise SystemExit(f"submit: HTTP request to {url} failed: {e}")

    if r.status_code == 401:
        raise SystemExit(
            f"submit: 401 from {url} — set HERMES_GATEWAY_API_KEY (or "
            f"API_SERVER_KEY) to the gateway's API_SERVER_KEY value, "
            f"or pass --api-key."
        )
    if r.status_code >= 400:
        raise SystemExit(
            f"submit: gateway returned {r.status_code}: {r.text[:500]}"
        )

    try:
        return r.json()
    except json.JSONDecodeError:
        raise SystemExit(
            f"submit: gateway returned non-JSON (status {r.status_code}): "
            f"{r.text[:500]}"
        )


def _tail_events(target: _GatewayTarget, run_id: str) -> int:
    """Stream the SSE event feed for `run_id` until end-of-stream.

    Returns the exit code: 0 on clean completion, non-zero on
    server-side error events.
    """
    import httpx

    headers = {"Accept": "text/event-stream"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"

    url = f"{target.base_url}/v1/runs/{run_id}/events"
    rc = 0
    try:
        with httpx.stream("GET", url, headers=headers, timeout=None) as r:
            if r.status_code >= 400:
                print(f"submit: tail failed with {r.status_code}", file=sys.stderr)
                return 1
            for raw in r.iter_lines():
                if not raw:
                    continue
                # SSE lines look like `data: {...}` or `event: foo`.
                if raw.startswith("data:"):
                    payload = raw[5:].strip()
                    print(payload)
                    # Best-effort failure detection — server-side schema may
                    # vary, so don't be strict; just bump rc on obvious errors.
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    et = evt.get("type") or evt.get("event")
                    if et in ("error", "run.failed"):
                        rc = 1
                else:
                    print(raw)
    except KeyboardInterrupt:
        print("\nsubmit: detached from event stream (run continues on gateway)",
              file=sys.stderr)
    except httpx.HTTPError as e:
        print(f"submit: tail dropped: {e}", file=sys.stderr)
        rc = 1
    return rc


def submit_command(args) -> int:
    """Entry point wired from main.cmd_submit."""
    target = _resolve_target(args)
    prompt = _read_prompt(args)

    response = _post_run(target, prompt, instructions=getattr(args, "instructions", None))
    run_id = response.get("id") or response.get("run_id") or "<unknown>"

    if not getattr(args, "quiet", False):
        print(f"run_id: {run_id}")
        print(f"gateway: {target.base_url}  ({target.source})")
        print(f"status:  curl -H 'Authorization: Bearer …' {target.base_url}/v1/runs/{run_id}")
        print(f"tail:    hermes submit --tail-run {run_id}")
    else:
        print(run_id)

    if getattr(args, "tail", False):
        return _tail_events(target, run_id)

    return 0


def tail_only_command(args) -> int:
    """Entry point for `hermes submit --tail-run <id>` (no submission)."""
    target = _resolve_target(args)
    run_id = args.tail_run
    return _tail_events(target, run_id)
