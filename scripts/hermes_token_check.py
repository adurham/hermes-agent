#!/usr/bin/env python3
"""Check the hermes user's Claude Code OAuth token expiry.

Reads ~/.claude/.credentials.json (where ``claude setup-token`` and
hermes's refresh-rotation logic both store the active access token) and
logs days-until-expiry to journal. Exits non-zero when the token is
within ``--warn-days`` of expiry so a systemd timer can flag the unit
as failed and surface in `systemctl --failed`.

Designed to run once daily as a systemd timer on the gateway LXC.
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--warn-days", type=int, default=30,
                   help="Exit non-zero when token is within this many days of expiry (default: 30)")
    args = p.parse_args()

    if not CREDS_PATH.exists():
        print(f"ERROR: creds file missing at {CREDS_PATH}", file=sys.stderr)
        return 2

    try:
        data = json.load(open(CREDS_PATH))
    except Exception as e:
        print(f"ERROR: failed to parse {CREDS_PATH}: {e}", file=sys.stderr)
        return 2

    oauth = data.get("claudeAiOauth") or {}
    expires_at_ms = oauth.get("expiresAt")
    if not isinstance(expires_at_ms, (int, float)):
        print("ERROR: claudeAiOauth.expiresAt missing or non-numeric", file=sys.stderr)
        return 2

    expires_at = datetime.fromtimestamp(int(expires_at_ms) / 1000, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    delta = expires_at - now
    days = delta.total_seconds() / 86400.0

    sub = oauth.get("subscriptionType", "?")
    tier = oauth.get("rateLimitTier", "?")

    msg = (f"Claude OAuth token: {days:.1f} days until expiry "
           f"(expires {expires_at.isoformat()}, subscription={sub}, tier={tier})")

    if days < 0:
        print(f"CRITICAL: {msg} — token EXPIRED, bot inference will 401", file=sys.stderr)
        return 3
    if days < args.warn_days:
        # Stderr + non-zero exit: systemd will mark the unit as failed,
        # the user sees it in `systemctl --failed` or via a discord nudge
        # if we wire one later.
        print(f"WARNING: {msg} — rotate via `claude setup-token` on the LXC "
              f"and update vault_hermes_gw_claude_code_oauth_token", file=sys.stderr)
        return 1

    print(f"OK: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
