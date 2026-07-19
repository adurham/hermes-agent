#!/usr/bin/env python3
"""
cc_proxy_mcp.py — stdio MCP shim that proxies to Anthropic's claude.ai MCP
proxy (https://mcp-proxy.anthropic.com/v1/mcp/{server_id}) using the OAuth
bearer token Claude Code already manages in ~/.claude/.credentials.json.

Reuses Claude Code's auth without copying tokens around. Refreshes on demand
via POST https://platform.claude.com/v1/oauth/token when expired.

Usage (Hermes config.yaml) -- invoke as a module from the hermes-agent venv:

  mcp_servers:
    slack:
      command: python            # resolved against the hermes-agent venv PATH
      args:
        - -m
        - tools.bridges.cc_proxy_mcp
        - --connector
        - slack                  # or 'notion', 'pagerduty', etc.
      timeout: 180

If ``python`` is not on PATH for the MCP subprocess, point ``command`` at the
venv interpreter directly (e.g. ``/path/to/hermes-agent/.venv/bin/python``).
For ad-hoc / non-Hermes use, the script also runs standalone:

  python -m tools.bridges.cc_proxy_mcp --connector slack
  python /path/to/cc_proxy_mcp.py --connector slack

Resolution: matches connector by case-insensitive substring against the
display name returned by GET https://api.anthropic.com/v1/mcp_servers.
Pass --server-id <uuid> to skip resolution.

Prerequisite: Claude Code must be installed and logged in on this machine.
The shim reads its OAuth credentials from ~/.claude/.credentials.json (or the
macOS Keychain on Claude Code >=2.1.114) and reuses whichever connectors
Claude Code already has wired -- Slack, Notion, PagerDuty, Microsoft 365,
Stack Overflow Teams, internal MCP gateways, etc.

This script speaks the MCP Streamable-HTTP protocol upstream and bridges it
to Hermes via stdio. No tools are interpreted locally; we just pump frames
in both directions.

Wire format observed from Claude Code 2.1.109:
  - Auth: Authorization: Bearer <claude.ai access token>
  - Required header: X-Mcp-Client-Session-Id: <uuid>
  - Listing connectors needs: anthropic-beta: oauth-2025-04-20,mcp-servers-2025-12-04
                               anthropic-version: 2023-06-01
  - Proxying tool calls does NOT need anthropic-beta/anthropic-version.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

# --- config -----------------------------------------------------------------

CREDS_PATH = Path(os.environ.get("CLAUDE_CREDS_PATH",
                                 str(Path.home() / ".claude" / ".credentials.json")))
# macOS Keychain entry name used by Claude Code >=2.1.114 (in addition to
# or instead of the JSON file). When the file is missing, fall back to the
# Keychain so this shim works on machines where Claude Code only writes
# credentials to Keychain (default macOS behavior).
KEYCHAIN_SERVICE = "Claude Code-credentials"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # claude code prod client id
LIST_SERVERS_URL = "https://api.anthropic.com/v1/mcp_servers?limit=1000"
PROXY_URL_TMPL = "https://mcp-proxy.anthropic.com/v1/mcp/{server_id}"
OAUTH_BETA = "oauth-2025-04-20"
MCP_SERVERS_BETA = "mcp-servers-2025-12-04"

REFRESH_SKEW_SECS = 120  # refresh if token expires within 2 minutes

logging.basicConfig(
    level=os.environ.get("CC_PROXY_LOG_LEVEL", "WARNING"),
    format="cc_proxy_mcp [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("cc_proxy_mcp")


# --- credential management --------------------------------------------------

def _keychain_account() -> Optional[str]:
    """Return the account associated with the Claude Code keychain entry, or None."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # Output includes a line like:  "acct"<blob>="<account-name>"
    for line in result.stdout.splitlines():
        if "\"acct\"<blob>=" in line:
            try:
                return line.split("=", 1)[1].strip().strip('"')
            except (IndexError, ValueError):
                return None
    return None


class CredStore:
    """Reads/writes Claude Code OAuth credentials with token refresh.

    Two backends:
      - File at ``~/.claude/.credentials.json`` (Linux, older macOS Claude Code).
      - macOS Keychain entry "Claude Code-credentials" (Claude Code >=2.1.114
        on macOS — the default location now).

    File takes precedence when it exists (preserves existing behavior). When
    the file is missing on macOS, falls back to Keychain transparently.

    File-locked (fcntl) so multiple shim processes started concurrently don't
    double-refresh and clobber each other's writes. Cross-process races with
    Claude Code itself are an existing limitation — both processes refreshing
    in quick succession can invalidate each other's refresh tokens.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()  # in-process serialization
        self._lockfile = path.with_suffix(path.suffix + ".lock")
        # Decide backend at init time. File wins if it exists. Keychain is the
        # macOS fallback. Note: file existence is sticky for the life of this
        # process — if Claude Code creates the file mid-session we keep using
        # Keychain, which is fine since we're authoritative for our own
        # refreshes anyway.
        self._account = None
        if path.exists():
            self._backend = "file"
        elif platform.system() == "Darwin":
            self._account = _keychain_account()
            self._backend = "keychain" if self._account else "file"
        else:
            self._backend = "file"

    def _load_raw(self) -> dict:
        if self._backend == "keychain":
            return self._load_from_keychain()
        with self.path.open("r") as f:
            return json.load(f)

    def _load_from_keychain(self) -> dict:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise FileNotFoundError(
                f"Keychain read for '{KEYCHAIN_SERVICE}' failed: {e}"
            )
        if result.returncode != 0:
            raise FileNotFoundError(
                f"Keychain entry '{KEYCHAIN_SERVICE}' not found "
                f"(security exit {result.returncode}: {result.stderr.strip()})"
            )
        raw = result.stdout.strip()
        if not raw:
            raise FileNotFoundError(
                f"Keychain entry '{KEYCHAIN_SERVICE}' is empty"
            )
        return json.loads(raw)

    def _save_raw(self, data: dict) -> None:
        if self._backend == "keychain":
            self._save_to_keychain(data)
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def _save_to_keychain(self, data: dict) -> None:
        # ``-U`` updates the password in place if the (service, account) pair
        # already exists, otherwise creates a new entry. Match Claude Code's
        # account so we update in place rather than creating a duplicate.
        payload = json.dumps(data)
        account = self._account or os.environ.get("USER", "claude")
        try:
            result = subprocess.run(
                ["security", "add-generic-password",
                 "-U",
                 "-s", KEYCHAIN_SERVICE,
                 "-a", account,
                 "-w", payload],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(
                f"Keychain write for '{KEYCHAIN_SERVICE}' failed: {e}"
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"Keychain write for '{KEYCHAIN_SERVICE}' failed "
                f"(security exit {result.returncode}: {result.stderr.strip()})"
            )

    def _extract(self, data: dict) -> dict:
        # Claude Code stores under the "claudeAiOauth" key (observed structure).
        # Accept either nested or flat shapes for resilience.
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            return data["claudeAiOauth"]
        return data

    def _put_back(self, data: dict, updated: dict) -> dict:
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            data["claudeAiOauth"] = updated
        else:
            data.update(updated)
        return data

    async def get_access_token(self, force_refresh: bool = False) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._get_locked, force_refresh)

    def _get_locked(self, force_refresh: bool) -> str:
        """Synchronous body — runs under cross-process flock."""
        import fcntl  # POSIX only; macOS / Linux fine.

        with open(self._lockfile, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                raw = self._load_raw()
                tok = self._extract(raw)
                access = tok.get("accessToken") or tok.get("access_token")
                refresh = tok.get("refreshToken") or tok.get("refresh_token")
                expires_at = tok.get("expiresAt") or tok.get("expires_at") or 0
                now_ms = int(time.time() * 1000)
                fresh_enough = (
                    access
                    and expires_at
                    and (expires_at - now_ms) > REFRESH_SKEW_SECS * 1000
                )
                if fresh_enough and not force_refresh:
                    return access
                if not refresh:
                    if access and not force_refresh:
                        log.warning(
                            "No refreshToken in credentials; returning possibly-stale access token"
                        )
                        return access
                    raise RuntimeError(
                        "No refreshToken available to renew expired credentials"
                    )
                log.info(
                    "Refreshing Anthropic OAuth token (force=%s, expired=%s)",
                    force_refresh,
                    not fresh_enough,
                )
                with httpx.Client(timeout=30) as client:
                    resp = client.post(
                        TOKEN_URL,
                        json={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh,
                            "client_id": CLIENT_ID,
                        },
                        headers={"Content-Type": "application/json"},
                    )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Token refresh failed: {resp.status_code} {resp.text[:300]}"
                    )
                body = resp.json()
                new_access = body["access_token"]
                new_refresh = body.get("refresh_token", refresh)
                expires_in = int(body.get("expires_in", 3600))
                updated = dict(tok)
                updated["accessToken"] = new_access
                updated["refreshToken"] = new_refresh
                updated["expiresAt"] = now_ms + expires_in * 1000
                if "scopes" in body:
                    updated["scopes"] = body["scopes"]
                # Re-read just before writing in case another process refreshed
                # under the lock first (it didn't — we hold it — but be safe).
                raw_now = self._load_raw()
                raw_now = self._put_back(raw_now, updated)
                self._save_raw(raw_now)
                log.info("Token refreshed; new expiry in %ds", expires_in)
                return new_access
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


# --- connector resolution ---------------------------------------------------

async def list_servers(creds: CredStore) -> list[dict]:
    token = await creds.get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            LIST_SERVERS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": f"{OAUTH_BETA},{MCP_SERVERS_BETA}",
                "anthropic-version": "2023-06-01",
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"List servers failed: {resp.status_code} {resp.text[:500]}")
    body = resp.json()
    # Accept either {"servers": [...]} or {"data": [...]} or list directly.
    for key in ("servers", "data", "mcp_servers"):
        if isinstance(body, dict) and key in body and isinstance(body[key], list):
            return body[key]
    if isinstance(body, list):
        return body
    return []


# Cache of connector_name -> server_id, lives 24h. Skipping list_servers on
# cold start halves startup time for 6 concurrent proxies because that HTTP
# call is the single biggest contention point on the shared OAuth flock.
_RESOLVE_CACHE = Path.home() / ".hermes" / "cache" / "cc_proxy_servers.json"
_RESOLVE_CACHE_TTL = 24 * 3600


def _load_resolve_cache() -> dict:
    try:
        with _RESOLVE_CACHE.open("r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        if (time.time() - data.get("written_at", 0)) > _RESOLVE_CACHE_TTL:
            return {}
        return data.get("servers") or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_resolve_cache(servers: dict) -> None:
    try:
        _RESOLVE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RESOLVE_CACHE.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump({"written_at": time.time(), "servers": servers}, f)
        tmp.replace(_RESOLVE_CACHE)
    except OSError as e:
        log.warning("Could not write resolve cache: %s", e)


async def resolve_server_id(creds: CredStore, connector: str) -> str:
    needle = connector.strip().lower()
    cache = _load_resolve_cache()
    if needle in cache:
        log.info("Resolved %r -> %s (cached)", connector, cache[needle])
        return cache[needle]

    servers = await list_servers(creds)
    candidates = []
    for s in servers:
        name = (s.get("name") or s.get("display_name") or s.get("title") or "").lower()
        if needle in name:
            candidates.append(s)
    if not candidates:
        names = [s.get("name") or s.get("display_name") or "?" for s in servers]
        raise RuntimeError(f"No connector matched {connector!r}. Available: {names}")
    if len(candidates) > 1:
        names = [c.get("name") or c.get("display_name") for c in candidates]
        raise RuntimeError(f"Ambiguous connector {connector!r}; matched: {names}")
    sid = candidates[0].get("id") or candidates[0].get("server_id") or candidates[0].get("uuid")
    if not sid:
        raise RuntimeError(f"Matched connector but no id field present: {candidates[0]}")
    log.info("Resolved %r -> %s (%s)", connector, candidates[0].get("name"), sid)

    # Cache the full set: every connector that matched anything we've ever
    # asked for builds up over time, so next cold start skips list_servers.
    cache.setdefault(needle, sid)
    for s in servers:
        nm = (s.get("name") or s.get("display_name") or "").lower().strip()
        sid2 = s.get("id") or s.get("server_id") or s.get("uuid")
        if nm and sid2:
            cache.setdefault(nm, sid2)
    _save_resolve_cache(cache)
    return sid


# --- proxying ---------------------------------------------------------------

async def run_proxy(server_id: str, creds: CredStore) -> None:
    """Bridge: Hermes <-stdio-> us <-streamable-http-> mcp-proxy.anthropic.com."""
    proxy_url = PROXY_URL_TMPL.format(server_id=server_id)
    log.info("Connecting upstream: %s", proxy_url)

    class FreshBearerAuth(httpx.Auth):
        """Per-request: ask CredStore for a fresh (refreshed-if-needed) token."""

        requires_request_body = False
        requires_response_body = False

        def __init__(self, store: CredStore) -> None:
            self._store = store

        def sync_auth_flow(self, request):  # type: ignore[override]
            raise RuntimeError("Use async_auth_flow only")

        async def async_auth_flow(self, request):  # type: ignore[override]
            token = await self._store.get_access_token()
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
            # If proxy says token is bad, force a refresh and retry once.
            if response.status_code == 401:
                log.warning("Upstream returned 401; forcing token refresh and retrying")
                token = await self._store.get_access_token(force_refresh=True)
                request.headers["Authorization"] = f"Bearer {token}"
                yield request

    static_headers = {
        "X-Mcp-Client-Session-Id": str(uuid.uuid4()),
        "User-Agent": "cc-proxy-mcp/0.1 (hermes)",
    }

    async with streamablehttp_client(
        proxy_url,
        headers=static_headers,
        auth=FreshBearerAuth(creds),
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as upstream:
            await upstream.initialize()
            tools_resp = await upstream.list_tools()
            log.info("Upstream initialized; %d tool(s)", len(tools_resp.tools))

            local = Server("cc-proxy")

            @local.list_tools()
            async def _list_tools():  # type: ignore[no-redef]
                resp = await upstream.list_tools()
                return resp.tools

            @local.call_tool()
            async def _call_tool(name: str, arguments: dict[str, Any]):  # type: ignore[no-redef]
                resp = await upstream.call_tool(name, arguments or {})
                return resp.content

            async with stdio_server() as (in_stream, out_stream):
                init_opts = local.create_initialization_options()
                log.info("Bridging %d tool(s) over stdio", len(tools_resp.tools))
                await local.run(in_stream, out_stream, init_opts)


# --- entrypoint -------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    creds = CredStore(CREDS_PATH)
    if args.list:
        servers = await list_servers(creds)
        for s in servers:
            print(json.dumps({
                "id": s.get("id") or s.get("server_id") or s.get("uuid"),
                "name": s.get("name") or s.get("display_name"),
                "url": s.get("url"),
                "scopes": s.get("scopes"),
            }, indent=2))
        return 0

    server_id: Optional[str] = args.server_id
    if not server_id:
        if not args.connector:
            print("error: --connector or --server-id required", file=sys.stderr)
            return 2
        server_id = await resolve_server_id(creds, args.connector)

    await run_proxy(server_id, creds)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--connector", help="Connector display name (substring match), e.g. 'slack'")
    p.add_argument("--server-id", help="Explicit server UUID; skips resolution")
    p.add_argument("--list", action="store_true", help="Print all available connectors and exit")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        log.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
