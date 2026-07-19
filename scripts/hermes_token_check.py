#!/usr/bin/env python3
"""Live auth check for the hermes user's Claude Code OAuth token.

Hits Anthropic's /v1/models endpoint with the token used by the
gateway (CLAUDE_CODE_OAUTH_TOKEN env var first, .credentials.json
file second — same precedence hermes uses). The endpoint is free,
returns the model list on 2xx, and 401s when the token is expired
or revoked.

Setup-tokens (the long-lived `claude setup-token` flavor we deploy)
don't carry a queryable expiry, so the only reliable signal is
"does Anthropic accept this token right now". Run daily via systemd
timer; non-zero exit code surfaces in `systemctl --failed`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CREDS_PATH = Path(os.environ.get("HERMES_CREDS_PATH",
                                 Path.home() / ".claude" / ".credentials.json"))
ENV_PATH = Path(os.environ.get("HERMES_ENV_PATH",
                               Path.home() / ".hermes" / ".env"))

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
        for raw in ENV_PATH.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                if tok:
                    return tok, f"file {ENV_PATH}"

    if CREDS_PATH.exists():
        try:
            data = json.load(open(CREDS_PATH))
            tok = (data.get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return tok, f"file {CREDS_PATH}"
        except Exception:
            pass

    return "", "(no token found)"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeout", type=int, default=10,
                   help="HTTP timeout (default: 10s)")
    args = p.parse_args()

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
        with httpx.Client(timeout=args.timeout) as client:
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


if __name__ == "__main__":
    sys.exit(main())
