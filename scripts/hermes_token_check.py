#!/usr/bin/env python3
"""Live auth check for the hermes user's Claude Code OAuth token.

Two checks, because the gateway has TWO auth consumption paths:

1. Env-token check — hits Anthropic's /v1/models with the token from
   CLAUDE_CODE_OAUTH_TOKEN (process env first, $HERMES_HOME/.env second,
   then the credentials file — the same precedence hermes uses). The
   endpoint is free, returns the model list on 2xx, and 401s when the
   token is expired or revoked.

2. Native credential-store check — runs `claude auth status` in the env
   the claude-subscription-directsdk provider hands its spawned CLI
   child: the plugin's own .env vars, with CLAUDE_CODE_OAUTH_TOKEN
   stripped (the plugin deliberately drops it so native uses its own
   store) and CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR mapped to
   CLAUDE_CONFIG_DIR. A "logged out" here means every Claude call through
   that provider fails with "Failed to authenticate" even while check 1
   is green (incident 2026-09-30: ~/.claude/.credentials.json was an
   empty husk while the .env token was current).

Setup-tokens (the long-lived `claude setup-token` flavor we deploy)
don't carry a queryable expiry, so the only reliable signal is
"does the auth surface accept it right now". Run daily via systemd
timer; non-zero exit code surfaces in `systemctl --failed`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CREDS_PATH = Path(os.environ.get("HERMES_CREDS_PATH",
                                 Path.home() / ".claude" / ".credentials.json"))
ENV_PATH = Path(os.environ.get("HERMES_ENV_PATH",
                               Path.home() / ".hermes" / ".env"))

CLAUDE_BIN = os.environ.get("HERMES_CLAUDE_BIN", "claude")

# Lightweight: just lists available models. ~50 bytes outbound, ~2K
# inbound, no token consumption. Anthropic's docs treat /v1/models as
# free, just authenticated.
MODELS_URL = "https://api.anthropic.com/v1/models"


def _resolve_token() -> tuple[str, str]:
    """Return (token, source) — same precedence the gateway uses."""
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token, "env CLAUDE_CODE_OAUTH_TOKEN"

    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                if tok:
                    return tok, f"file {ENV_PATH}"

    if CREDS_PATH.exists():
        try:
            with open(CREDS_PATH, encoding="utf-8-sig") as f:
                data = json.load(f)
            tok = (data.get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return tok, f"file {CREDS_PATH}"
        except Exception:
            pass

    return "", "(no token found)"


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader for a Hermes .env — the same parse shape the
    DirectSDK plugin uses to resolve its own declared vars."""
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError:
        pass
    return values


def _native_env() -> dict[str, str]:
    """Reconstruct the env the DirectSDK provider hands its spawned CLI.

    The provider merges its own $HERMES_HOME/.env vars over the process env,
    maps CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR -> CLAUDE_CONFIG_DIR, and
    strips CLAUDE_CODE_OAUTH_TOKEN so native falls back to its own credential
    store (keychain on macOS, ~/.claude/.credentials.json elsewhere).
    Reproduce that resolution exactly — this is the auth path a broken or
    empty store silently kills while the env-token check stays green.
    """
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", ""),
    }
    env.update({k: v for k, v in _read_env_file(ENV_PATH).items() if v})
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    config_dir = env.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", "")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def _parse_auth_status(stdout: str) -> tuple[bool, str]:
    """(logged_in, auth_method) from `claude auth status` stdout."""
    try:
        data = json.loads(stdout) if stdout.strip().startswith("{") else {}
    except ValueError:
        data = {}
    return data.get("loggedIn") is True, str(data.get("authMethod") or "")


def native_login_state() -> tuple[bool | None, str, str]:
    """(logged_in, auth_method, detail) for the CLI's OWN credential store.

    ``logged_in`` is None when the state cannot be determined (CLI not
    installed or failed to run) — callers treat that as advisory-only.
    """
    exe = os.environ.get("HERMES_CLAUDE_BIN") or shutil.which(CLAUDE_BIN)
    if not exe:
        return None, "", f"`{CLAUDE_BIN}` not found on PATH; native-store check skipped"
    try:
        run = subprocess.run([exe, "auth", "status"], env=_native_env(),
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", f"native-store check failed to run: {e}"
    logged_in, method = _parse_auth_status(run.stdout)
    return logged_in, method, ""


def _check_env_token(timeout: int) -> int:
    """The original /v1/models check. Returns its exit code."""
    token, source = _resolve_token()
    if not token:
        print(f"ERROR: no token found (checked env + {ENV_PATH} + {CREDS_PATH})",
              file=sys.stderr)
        return 2

    # Same beta header set the gateway uses on the OAuth path so the
    # check exercises the same auth surface as real inference. Without
    # `oauth-2025-04-20` an OAuth token gets rejected by /v1/models.
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "hermes-token-check/1.0",
    }

    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            r = client.get(MODELS_URL, headers=headers)
    except Exception as e:
        print(f"ERROR: HTTP request failed: {e}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if r.status_code == 401:
        print(f"CRITICAL ({now}): token from {source} returned 401 — "
              f"rotate via `claude setup-token` on the LXC, then update "
              f"vault_hermes_gw_claude_code_oauth_token.",
              file=sys.stderr)
        return 3

    if r.status_code == 403:
        # 403 on /v1/models for OAuth tokens is normal for some scopes
        # (setup-tokens may not have the models:read scope). Auth itself
        # worked — the token was decoded — so treat as OK and note it.
        print(f"OK ({now}): token from {source} authenticated (403 on /v1/models "
              f"is expected for setup-tokens — auth itself succeeded)")
        return 0

    if r.status_code >= 400:
        print(f"WARN ({now}): /v1/models returned {r.status_code}: "
              f"{r.text[:200]}", file=sys.stderr)
        return 1

    # 2xx — count models in response as a sanity check.
    try:
        body = r.json()
        n = len(body.get("data", []))
    except Exception:
        n = -1
    print(f"OK ({now}): token from {source} authenticated, "
          f"/v1/models returned {n} models")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeout", type=int, default=10,
                   help="HTTP timeout (default: 10s)")
    p.add_argument("--skip-native", action="store_true",
                   help="skip the native credential-store check (hosts without `claude`)")
    args = p.parse_args()

    rc = _check_env_token(args.timeout)

    if not args.skip_native:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        logged_in, method, detail = native_login_state()
        if logged_in is None:
            print(f"SKIP ({now}): {detail}")
        elif logged_in:
            print(f"OK ({now}): native credential store logged in "
                  f"(authMethod={method or 'unknown'})")
        else:
            print(f"CRITICAL ({now}): the spawned `claude` CLI has NO usable login "
                  f"in its own credential store (authMethod={method or 'none'}). "
                  f"The claude-subscription-directsdk provider strips "
                  f"CLAUDE_CODE_OAUTH_TOKEN before spawning it, so every Claude "
                  f"call through that provider fails even while the env-token "
                  f"check above passes. Ensure {CREDS_PATH} carries the current "
                  f"setup-token (homelab role task 'Render Claude Code Native "
                  f"Credentials' — re-run deploy_hermes_gateway.yml) or rotate "
                  f"via `claude setup-token`.", file=sys.stderr)
            if rc == 0:
                rc = 4

    return rc


if __name__ == "__main__":
    sys.exit(main())
