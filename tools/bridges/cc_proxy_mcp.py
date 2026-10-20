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
try:
    # mcp >= 1.24.0 renamed the entry point; mcp 2.0 dropped the old
    # `streamablehttp_client` alias entirely (see tools/mcp_tool.py's
    # _ensure_mcp_sdk / sdk_httpx for the same compat shims used by the
    # core agent). mcp 2.0 also moved the HTTP transport from `httpx` to
    # a separate `httpx2` distribution with the same public API — any
    # AsyncClient/Auth object we hand to the new client must come from
    # that module, not our own pinned `httpx`.
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_LEGACY_HTTP = True
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
    _MCP_LEGACY_HTTP = False
    from mcp.client import streamable_http as _sdk_transport_mod
    httpx2 = _sdk_transport_mod.httpx2  # the SDK's own httpx-compatible module
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


# --- keychain discovery -----------------------------------------------------

def _resolve_keychain_service() -> Optional[str]:
    """Find the actual Keychain service name for Claude Code's credentials.

    Claude Code >=2.1.114 on some macOS installs suffixes the service name
    with an account/workspace-specific hash (observed:
    "Claude Code-credentials-3775e6c9"), not the bare "Claude
    Code-credentials" the constant assumes. An exact ``-s`` match against
    the un-suffixed name silently finds nothing on those installs, which
    this shim previously mis-handled as "no keychain entry -> fall back to
    file", producing a stale-file-not-found or stale-token error instead of
    ever reading the real (working) credential.

    ``security dump-keychain`` (no ``-a``) lists entry metadata without
    triggering a per-item unlock/access prompt, so this is cheap and silent.
    """
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    candidates = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"svce"<blob>='):
            value = line.split("=", 1)[1].strip().strip('"')
            if value == KEYCHAIN_SERVICE or value.startswith(KEYCHAIN_SERVICE + "-"):
                candidates.append(value)
    if not candidates:
        return None
    # Prefer the exact legacy (un-suffixed) name if present.
    if KEYCHAIN_SERVICE in candidates:
        return KEYCHAIN_SERVICE
    # Every /login can leave behind a new suffixed entry; dump order is
    # arbitrary, so taking the first match picks stale (dead refresh token)
    # entries. Choose the most recently modified one.
    def _mdat(service: str) -> str:
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
        for ln in out.splitlines():
            if '"mdat"<timedate>=' in ln:
                return ln.rsplit('"', 2)[-2] if ln.count('"') >= 2 else ""
        return ""
    return max(dict.fromkeys(candidates), key=_mdat)


def _keychain_account(service: str) -> Optional[str]:
    """Return the account associated with a Claude Code keychain entry, or None."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5,
            check=False,
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


# --- failure classification -------------------------------------------------
#
# Every way this bridge can fail is mapped to ONE of these kinds so the
# message the agent / user sees names the actual fix. The default before was a
# bare process exit, which Hermes could only report as "Connection closed";
# the connector's tools then silently vanished from the session and the agent
# concluded the capability did not exist.

KIND_REAUTH = "reauth"                    # refresh token dead / keychain entry missing
KIND_NETWORK = "network"                  # cannot reach Anthropic (offline, VPN, DNS, timeout)
KIND_CONNECTOR_UNAUTHORIZED = "connector_unauthorized"  # claude.ai connector's own OAuth revoked
KIND_UPSTREAM = "upstream"                # any other upstream HTTP/MCP failure
KIND_CONFIG = "config"                    # bad --connector, ambiguous match, ...

# Stub tool name per kind: Hermes exposes it as ``<server>_<name>`` (e.g.
# ``slack_auth_expired_relogin_required``) so the failure is visible in the
# tool list itself, and tool_search for the service name finds it.
STUB_TOOL_NAMES = {
    KIND_REAUTH: "auth_expired_relogin_required",
    KIND_NETWORK: "network_unreachable",
    KIND_CONNECTOR_UNAUTHORIZED: "connector_reauthorize_required",
    KIND_UPSTREAM: "upstream_error",
    KIND_CONFIG: "config_error",
}
DEGRADED_META_KEY = "hermes/connector_degraded"

# Refresh POST timeout. A refresh that the server completed but whose response
# we abandoned rotates the chain to a token nobody holds (observed: a ~30s
# timeout immediately followed by permanent invalid_grant), so wait long.
REFRESH_TIMEOUT_SECS = 120
# Cross-process floor between FORCED refreshes of one chain. Every forced
# refresh rotates the refresh token; 7 bridges each forcing on every 401 used
# to rotate the shared chain thousands of times a day.
FORCED_REFRESH_COOLDOWN_SECS = 60
SAVE_RETRIES = 3



def _state_dir() -> Path:
    """Shared lock/state dir for every bridge process on this machine."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "cache" / "cc_proxy"


class ConnectorError(RuntimeError):
    """A classified bridge failure; ``str()`` never contains token material."""

    def __init__(self, kind: str, detail: str, *, service: Optional[str] = None) -> None:
        super().__init__(f"[{kind}] {detail}")
        self.kind = kind
        self.detail = detail
        self.service = service


def _redact(text: str) -> str:
    """Strip anything token-shaped from upstream text before it reaches a log."""
    import re
    return re.sub(r"[A-Za-z0-9_\-.]{32,}", "***", text or "")


def _fingerprint(secret: str) -> str:
    """Irreversible short id for a refresh token (to remember 'this one is dead')."""
    import hashlib
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def _config_dir_candidates() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        out.append(Path(env_dir).expanduser())
    out.append(home / ".claude")
    out.extend(sorted(p for p in home.glob(".claude-*") if p.is_dir()))
    out.append(home / ".hermes" / "claude-proxy")
    out.append(home / ".config" / "claude")
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def config_dir_for_service(service: Optional[str]) -> Optional[Path]:
    """The CLAUDE_CONFIG_DIR whose `claude auth login` writes ``service``.

    Claude Code names its keychain entry ``Claude Code-credentials-<8 hex>``
    where the suffix is sha256(config dir path)[:8]; the bare name belongs to
    the default config dir. Returns None when no local dir matches."""
    import hashlib
    if not service or service == KEYCHAIN_SERVICE:
        return Path.home() / ".claude"
    prefix = KEYCHAIN_SERVICE + "-"
    if not service.startswith(prefix):
        return None
    suffix = service[len(prefix):]
    for cand in _config_dir_candidates():
        if hashlib.sha256(str(cand).encode()).hexdigest()[:8] == suffix:
            return cand
    return None


def relogin_command(service: Optional[str]) -> str:
    cfg = config_dir_for_service(service)
    if cfg is None:
        return (f"CLAUDE_CONFIG_DIR=<the dir that owns keychain entry '{service}'> "
                f"claude auth login")
    if cfg == Path.home() / ".claude" and (not service or service == KEYCHAIN_SERVICE):
        return "claude auth login"
    return f"CLAUDE_CONFIG_DIR={cfg} claude auth login"


def _network_exc_types() -> tuple:
    types: list = [httpx.TransportError, ConnectionError, TimeoutError]
    if not _MCP_LEGACY_HTTP:
        types.append(httpx2.TransportError)
    import socket
    types.append(socket.gaierror)
    return tuple(types)


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in exc.exceptions:
            out.extend(_leaf_exceptions(sub))
        return out
    return [exc]


def classify_exception(exc: BaseException, *, auth_hint: Optional[ConnectorError] = None,
                       service: Optional[str] = None) -> ConnectorError:
    """Map any startup/runtime exception (incl. anyio TaskGroup wrappers) to a ConnectorError.

    ``auth_hint`` is the auth flow's own verdict on an upstream 401 -- it wins
    over the SDK's opaque ``MCPError: Server returned an error response``."""
    leaves = _leaf_exceptions(exc)
    for leaf in leaves:
        if isinstance(leaf, ConnectorError):
            return leaf
    if auth_hint is not None:
        return auth_hint
    net = _network_exc_types()
    for leaf in leaves:
        if isinstance(leaf, net):
            return ConnectorError(KIND_NETWORK, f"{type(leaf).__name__}: {_redact(str(leaf))[:200]}",
                                  service=service)
    leaf = leaves[0] if leaves else exc
    return ConnectorError(KIND_UPSTREAM, f"{type(leaf).__name__}: {_redact(str(leaf))[:300]}",
                          service=service)


def human_message(err: ConnectorError, connector: str) -> str:
    """One plain paragraph: what is broken and the exact command that fixes it."""
    label = connector.strip() or "Connector"
    label = label[:1].upper() + label[1:]
    reload_hint = "then run /reload-mcp in Hermes (or restart Hermes)"
    if err.kind == KIND_REAUTH:
        return (f"{label} connector auth expired ({err.detail}). The Claude Code login this "
                f"bridge uses (keychain entry '{err.service}') must be renewed. Fix: run "
                f"`{relogin_command(err.service)}` in a terminal, {reload_hint}. "
                f"The {label} tools exist but are unavailable until then.")
    if err.kind == KIND_NETWORK:
        return (f"{label} connector unreachable: network error talking to Anthropic "
                f"({err.detail}). Check the network / corporate VPN, {reload_hint}. "
                f"The {label} tools exist but are unavailable until then.")
    if err.kind == KIND_CONNECTOR_UNAUTHORIZED:
        return (f"{label} connector's own authorization at claude.ai was revoked or expired "
                f"({err.detail}). The Claude login itself is fine. Fix: reconnect the "
                f"'{connector}' connector in claude.ai Settings > Connectors, {reload_hint}.")
    if err.kind == KIND_CONFIG:
        return f"{label} connector misconfigured: {err.detail}. Fix the mcp_servers entry, {reload_hint}."
    return (f"{label} connector failed upstream ({err.detail}). Retry with /reload-mcp; if it "
            f"persists check https://status.anthropic.com. The {label} tools exist but are "
            f"unavailable until then.")


# --- credential management --------------------------------------------------

class CredStore:
    """Reads/writes Claude Code OAuth credentials with token refresh.

    Two backends:
      - File at ``~/.claude/.credentials.json`` (Linux, older macOS Claude Code).
      - macOS Keychain entry "Claude Code-credentials[-<hash>]" (Claude Code
        >=2.1.114 on macOS -- the default location now).

    ``CC_PROXY_KEYCHAIN_SERVICE`` pins one keychain entry (a dedicated
    ``CLAUDE_CONFIG_DIR=<dir> claude auth login``) so the bridge never shares
    a refresh chain with interactive ``claude`` -- refresh tokens ROTATE, so
    two uncoordinated refreshers on one chain kill it.

    Concurrency: every bridge process on the same chain serializes refreshes
    on one flock keyed by the keychain service (or the creds file), and keeps a
    small shared state file next to it (last forced refresh, fingerprint of a
    refresh token the server rejected) so sibling processes neither re-rotate
    in a storm nor replay a known-dead token. Claude Code itself does not take
    this lock: that is why the pinned entry must not be the interactive one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()  # in-process serialization
        self._account = None
        self._service = KEYCHAIN_SERVICE
        pinned = os.environ.get("CC_PROXY_KEYCHAIN_SERVICE")
        if pinned and platform.system() == "Darwin":
            self._service = pinned
            self._account = _keychain_account(pinned)
            if not self._account:
                raise ConnectorError(
                    KIND_REAUTH,
                    f"keychain entry '{pinned}' (CC_PROXY_KEYCHAIN_SERVICE) not found",
                    service=pinned)
            self._backend = "keychain"
        elif path.exists():
            self._backend = "file"
        elif platform.system() == "Darwin":
            resolved_service = _resolve_keychain_service()
            if resolved_service:
                self._service = resolved_service
                self._account = _keychain_account(resolved_service)
            self._backend = "keychain" if self._account else "file"
        else:
            self._backend = "file"
        if self._backend == "keychain":
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self._service)
            state_dir = _state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            self._lockfile = state_dir / f"{safe}.lock"
            self._statefile = state_dir / f"{safe}.state.json"
        else:
            self._lockfile = path.with_suffix(path.suffix + ".lock")
            self._statefile = path.with_suffix(path.suffix + ".cc_proxy_state.json")

    @property
    def service(self) -> Optional[str]:
        return self._service if self._backend == "keychain" else None

    # -- raw storage ---------------------------------------------------------

    def _load_raw(self) -> dict:
        if self._backend == "keychain":
            return self._load_from_keychain()
        try:
            with self.path.open("r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise ConnectorError(KIND_REAUTH, f"credentials file {self.path} not found")

    def _load_from_keychain(self) -> dict:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", self._service, "-w"],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ConnectorError(KIND_REAUTH, f"keychain read for '{self._service}' failed: {e}",
                                 service=self._service)
        if result.returncode != 0:
            raise ConnectorError(
                KIND_REAUTH,
                f"keychain entry '{self._service}' not found "
                f"(security exit {result.returncode})", service=self._service)
        raw = result.stdout.strip()
        if not raw:
            raise ConnectorError(KIND_REAUTH, f"keychain entry '{self._service}' is empty",
                                 service=self._service)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ConnectorError(KIND_REAUTH, f"keychain entry '{self._service}' is not valid JSON",
                                 service=self._service)

    def _save_raw(self, data: dict) -> None:
        if self._backend == "keychain":
            self._save_to_keychain(data)
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def _save_to_keychain(self, data: dict) -> None:
        # ``-U`` updates the password in place if the (service, account) pair
        # already exists. Match Claude Code's account so we update in place.
        payload = json.dumps(data)
        account = self._account or os.environ.get("USER", "claude")
        try:
            result = subprocess.run(
                ["security", "add-generic-password",
                 "-U",
                 "-s", self._service,
                 "-a", account,
                 "-w", payload],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"Keychain write for '{self._service}' failed: {e}")
        if result.returncode != 0:
            raise RuntimeError(
                f"Keychain write for '{self._service}' failed "
                f"(security exit {result.returncode}: {result.stderr.strip()[:200]})")

    def _extract(self, data: dict) -> dict:
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            return data["claudeAiOauth"]
        return data

    def _put_back(self, data: dict, updated: dict) -> dict:
        if "claudeAiOauth" in data and isinstance(data["claudeAiOauth"], dict):
            data["claudeAiOauth"] = updated
        else:
            data.update(updated)
        return data

    @staticmethod
    def _fields(tok: dict) -> tuple[Optional[str], Optional[str], int]:
        return (tok.get("accessToken") or tok.get("access_token"),
                tok.get("refreshToken") or tok.get("refresh_token"),
                int(tok.get("expiresAt") or tok.get("expires_at") or 0))

    # -- shared cross-process state (no secrets: timestamps + fingerprints) --

    def _read_state(self) -> dict:
        try:
            with self._statefile.open("r") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, **updates: Any) -> None:
        state = self._read_state()
        state.update(updates)
        try:
            tmp = self._statefile.with_suffix(".tmp")
            with tmp.open("w") as f:
                json.dump(state, f)
            tmp.replace(self._statefile)
        except OSError as e:
            log.warning("Could not write bridge state %s: %s", self._statefile, e)

    # -- token access ---------------------------------------------------------

    async def get_access_token(self, force_refresh: bool = False,
                               rejected_access: Optional[str] = None) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._get_locked, force_refresh, rejected_access)

    def _get_locked(self, force_refresh: bool, rejected_access: Optional[str] = None) -> str:
        """Synchronous body -- runs under the cross-process flock."""
        import fcntl  # POSIX only; macOS / Linux fine.

        with open(self._lockfile, "w", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                return self._get_under_lock(force_refresh, rejected_access)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _get_under_lock(self, force_refresh: bool, rejected_access: Optional[str]) -> str:
        raw = self._load_raw()
        tok = self._extract(raw)
        access, refresh, expires_at = self._fields(tok)
        now_ms = int(time.time() * 1000)
        fresh_enough = bool(access and expires_at and (expires_at - now_ms) > REFRESH_SKEW_SECS * 1000)

        # Another process already refreshed since the caller's token was minted:
        # use the stored one instead of rotating the chain again.
        if rejected_access and access and access != rejected_access and fresh_enough:
            log.info("Stored token differs from the rejected one; using it without refreshing")
            return access
        if fresh_enough and not force_refresh:
            return access
        if not refresh:
            if access and not force_refresh:
                log.warning("No refreshToken in credentials; returning possibly-stale access token")
                return access
            raise ConnectorError(KIND_REAUTH, "no refresh token stored", service=self.service)

        state = self._read_state()
        if state.get("dead_refresh_fp") == _fingerprint(refresh):
            # A sibling already proved this refresh token dead: replaying it
            # cannot succeed and only adds noise at the token endpoint.
            raise ConnectorError(KIND_REAUTH, "refresh token rejected: invalid_grant (cached verdict)",
                                 service=self.service)
        if force_refresh and fresh_enough:
            last = float(state.get("last_forced_refresh", 0) or 0)
            if time.time() - last < FORCED_REFRESH_COOLDOWN_SECS:
                log.warning("Forced refresh suppressed: chain was force-refreshed %.0fs ago",
                            time.time() - last)
                return access

        log.info("Refreshing Anthropic OAuth token (force=%s, expired=%s)", force_refresh, not fresh_enough)
        body = self._refresh_with_retry(refresh)
        new_access = body["access_token"]
        new_refresh = body.get("refresh_token", refresh)
        expires_in = int(body.get("expires_in", 3600))
        updated = dict(tok)
        updated["accessToken"] = new_access
        updated["refreshToken"] = new_refresh
        updated["expiresAt"] = now_ms + expires_in * 1000
        if "scopes" in body:
            updated["scopes"] = body["scopes"]
        self._persist_rotated(updated, new_refresh)
        if force_refresh:
            self._write_state(last_forced_refresh=time.time())
        log.info("Token refreshed; new expiry in %ds", expires_in)
        return new_access

    def _post_refresh(self, refresh: str) -> httpx.Response:
        try:
            with httpx.Client(timeout=REFRESH_TIMEOUT_SECS) as client:
                return client.post(
                    TOKEN_URL,
                    json={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID},
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException as e:
            # The server may have rotated the chain without us seeing the new
            # token; say so, because the next attempt may hit invalid_grant.
            raise ConnectorError(
                KIND_NETWORK,
                f"token refresh timed out after {REFRESH_TIMEOUT_SECS}s ({type(e).__name__}); if this is "
                f"followed by invalid_grant the login must be renewed", service=self.service)
        except httpx.TransportError as e:
            raise ConnectorError(KIND_NETWORK, f"token refresh failed: {type(e).__name__}: {_redact(str(e))[:200]}",
                                 service=self.service)

    @staticmethod
    def _is_invalid_grant(resp: httpx.Response) -> bool:
        if resp.status_code not in (400, 401):
            return False
        try:
            return (resp.json() or {}).get("error") == "invalid_grant"
        except ValueError:
            return "invalid_grant" in resp.text

    def _refresh_with_retry(self, refresh: str) -> dict:
        resp = self._post_refresh(refresh)
        if resp.status_code == 200:
            return resp.json()
        if self._is_invalid_grant(resp):
            # Race: another writer (one not using our lock) rotated the chain
            # between our read and our POST. Re-read once; retry only with a
            # DIFFERENT token -- never replay the one just rejected.
            tok2 = self._extract(self._load_raw())
            access2, refresh2, exp2 = self._fields(tok2)
            if refresh2 and refresh2 != refresh:
                log.warning("invalid_grant, but the stored refresh token changed meanwhile; retrying once")
                if access2 and exp2 - int(time.time() * 1000) > REFRESH_SKEW_SECS * 1000:
                    return {"access_token": access2, "refresh_token": refresh2,
                            "expires_in": max(1, (exp2 - int(time.time() * 1000)) // 1000)}
                resp = self._post_refresh(refresh2)
                if resp.status_code == 200:
                    return resp.json()
                if self._is_invalid_grant(resp):
                    self._write_state(dead_refresh_fp=_fingerprint(refresh2), dead_at=time.time())
                    raise ConnectorError(KIND_REAUTH, "refresh token rejected: invalid_grant",
                                         service=self.service)
            else:
                self._write_state(dead_refresh_fp=_fingerprint(refresh), dead_at=time.time())
                raise ConnectorError(KIND_REAUTH, "refresh token rejected: invalid_grant",
                                     service=self.service)
        raise ConnectorError(KIND_UPSTREAM,
                             f"token refresh failed: HTTP {resp.status_code} {_redact(resp.text)[:200]}",
                             service=self.service)

    def _persist_rotated(self, updated: dict, new_refresh: str) -> None:
        """Save the rotated chain BEFORE handing out the new access token, and
        read it back: an unsaved rotated refresh token means the chain dies at
        the next refresh, so this is the one write that must not fail quietly."""
        last_err: Optional[Exception] = None
        for attempt in range(1, SAVE_RETRIES + 1):
            try:
                raw_now = self._load_raw()
                self._save_raw(self._put_back(raw_now, dict(updated)))
                _, stored_refresh, _ = self._fields(self._extract(self._load_raw()))
                if stored_refresh == new_refresh:
                    return
                last_err = RuntimeError("read-back mismatch after save")
            except Exception as e:
                last_err = e
            log.warning("Saving rotated credentials failed (attempt %d/%d): %s",
                        attempt, SAVE_RETRIES, _redact(str(last_err)))
            time.sleep(0.2 * attempt)
        log.error("ROTATED REFRESH TOKEN COULD NOT BE SAVED to %s; this session works until the "
                  "access token expires, then the login must be renewed: %s",
                  self._service if self._backend == "keychain" else self.path, _redact(str(last_err)))


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
    if resp.status_code == 401:
        raise ConnectorError(KIND_REAUTH, "access token rejected listing connectors (HTTP 401)",
                             service=creds.service)
    if resp.status_code != 200:
        raise ConnectorError(KIND_UPSTREAM,
                             f"list connectors failed: HTTP {resp.status_code} {_redact(resp.text)[:300]}")
    body = resp.json()
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
        raise ConnectorError(KIND_CONFIG, f"no claude.ai connector matched {connector!r}; available: {names}")
    if len(candidates) > 1:
        names = [c.get("name") or c.get("display_name") for c in candidates]
        raise ConnectorError(KIND_CONFIG, f"ambiguous connector {connector!r}; matched: {names}")
    sid = candidates[0].get("id") or candidates[0].get("server_id") or candidates[0].get("uuid")
    if not sid:
        raise ConnectorError(KIND_CONFIG, f"matched connector {candidates[0].get('name')!r} has no id field")
    log.info("Resolved %r -> %s (%s)", connector, candidates[0].get("name"), sid)

    cache.setdefault(needle, sid)
    for s in servers:
        nm = (s.get("name") or s.get("display_name") or "").lower().strip()
        sid2 = s.get("id") or s.get("server_id") or s.get("uuid")
        if nm and sid2:
            cache.setdefault(nm, sid2)
    _save_resolve_cache(cache)
    return sid


# --- upstream 401 handling --------------------------------------------------

class _AuthVerdict:
    """Shared between the auth flow and the caller: the classified reason for
    the most recent upstream 401 (the SDK itself only reports an opaque
    ``MCPError``)."""

    def __init__(self) -> None:
        self.error: Optional[ConnectorError] = None


def _parse_401(body: bytes) -> tuple[str, str]:
    """(error_code, message) from an mcp-proxy 401 body; empty strings if absent."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "", ""
    err = (data or {}).get("error") or {}
    if not isinstance(err, dict):
        return "", ""
    details = err.get("details") or {}
    code = details.get("error_code", "") if isinstance(details, dict) else ""
    return str(code or ""), str(err.get("message") or "")


# mcp-proxy answers a VALID Claude token with this code when the claude.ai
# connector's own upstream OAuth (e.g. Tanium Provisioner's) is invalidated.
# Refreshing the Claude token cannot fix it -- and doing so rotated the shared
# refresh chain on every spawn (thousands of times a day), which is what killed
# it. Verified 2026-10-20: same bearer -> slack 200, provisioner 401 with this code.
CONNECTOR_TOKEN_INVALID_CODES = frozenset({"mcp_invalid_oauth_token"})


async def _auth_flow(store: CredStore, verdict: _AuthVerdict, request):
    """Body shared by both SDK generations' Auth classes."""
    token = await store.get_access_token()
    request.headers["Authorization"] = f"Bearer {token}"
    response = yield request
    if response.status_code != 401:
        return
    try:
        await response.aread()
        code, message = _parse_401(response.content)
    except Exception:
        code, message = "", ""
    if code in CONNECTOR_TOKEN_INVALID_CODES:
        verdict.error = ConnectorError(
            KIND_CONNECTOR_UNAUTHORIZED, f"mcp-proxy 401 {code}: {message}".strip(), service=store.service)
        log.warning("Upstream 401 %s: connector-level auth failure; NOT refreshing the Claude token", code)
        return
    log.warning("Upstream returned 401 (%s); refreshing token (unless a sibling already did) and retrying",
                code or message or "no detail")
    token = await store.get_access_token(force_refresh=True, rejected_access=token)
    request.headers["Authorization"] = f"Bearer {token}"
    response = yield request
    if response.status_code == 401:
        verdict.error = ConnectorError(
            KIND_REAUTH, "access token still rejected (HTTP 401) after refresh", service=store.service)


# --- proxying ---------------------------------------------------------------

async def run_proxy(server_id: str, creds: CredStore, verdict: Optional[_AuthVerdict] = None) -> None:
    """Bridge: Hermes <-stdio-> us <-streamable-http-> mcp-proxy.anthropic.com."""
    proxy_url = PROXY_URL_TMPL.format(server_id=server_id)
    log.info("Connecting upstream: %s", proxy_url)
    verdict = verdict or _AuthVerdict()

    class FreshBearerAuthLegacy(httpx.Auth):
        """Per-request fresh token; mcp < 1.24.0 (pinned httpx)."""
        requires_request_body = False
        requires_response_body = False

        def __init__(self, store: CredStore) -> None:
            self._store = store

        def sync_auth_flow(self, request):  # type: ignore[override]
            raise RuntimeError("Use async_auth_flow only")

        async def async_auth_flow(self, request):  # type: ignore[override]
            flow = _auth_flow(self._store, verdict, request)
            response = None
            try:
                while True:
                    req = await flow.asend(response)
                    response = yield req
            except StopAsyncIteration:
                return

    static_headers = {
        "X-Mcp-Client-Session-Id": str(uuid.uuid4()),
        "User-Agent": "cc-proxy-mcp/0.2 (hermes)",
    }

    if _MCP_LEGACY_HTTP:
        http_ctx = streamablehttp_client(
            proxy_url,
            headers=static_headers,
            auth=FreshBearerAuthLegacy(creds),
        )
    else:
        # mcp >= 1.24.0 (incl. 2.0): caller-owned client; every object crossing
        # the SDK boundary must come from the SDK's own httpx2 module.
        class FreshBearerAuthNew(httpx2.Auth):
            requires_request_body = False
            requires_response_body = False

            def __init__(self, store: CredStore) -> None:
                self._store = store

            async def async_auth_flow(self, request):
                flow = _auth_flow(self._store, verdict, request)
                response = None
                try:
                    while True:
                        req = await flow.asend(response)
                        response = yield req
                except StopAsyncIteration:
                    return

        http_client = httpx2.AsyncClient(
            headers=static_headers,
            auth=FreshBearerAuthNew(creds),
        )

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _http_ctx_cm():
            async with http_client:
                async with streamablehttp_client(proxy_url, http_client=http_client) as streams:
                    yield (streams[0], streams[1], None)

        http_ctx = _http_ctx_cm()

    async with http_ctx as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as upstream:
            await upstream.initialize()
            tools_resp = await upstream.list_tools()
            log.info("Upstream initialized; %d tool(s)", len(tools_resp.tools))

            async def _relay_call(name: str, arguments: dict[str, Any]):
                # A token that dies MID-session must still say why, not just
                # surface as an opaque transport error on every call.
                try:
                    return await upstream.call_tool(name, arguments or {})
                except Exception as exc:
                    err = classify_exception(exc, auth_hint=verdict.error, service=creds.service)
                    log.error("Tool call %s failed: %s", name, err)
                    return _error_result(human_message(err, _CONNECTOR_LABEL))

            if _MCP_LEGACY_HTTP:
                local = Server("cc-proxy")

                @local.list_tools()
                async def _list_tools():  # type: ignore[no-redef]
                    resp = await upstream.list_tools()
                    return resp.tools

                @local.call_tool()
                async def _call_tool(name: str, arguments: dict[str, Any]):  # type: ignore[no-redef]
                    return await _relay_call(name, arguments)
            else:
                # upstream.call_tool() already returns a CallToolResult from the
                # same SDK generation on_call_tool expects; returning it directly
                # preserves isError/structuredContent (see TestCallToolErrorPropagation).
                import mcp_types as _types

                async def _on_list_tools(_ctx, _params):
                    resp = await upstream.list_tools()
                    return _types.ListToolsResult(tools=resp.tools)

                async def _on_call_tool(_ctx, params):
                    return await _relay_call(params.name, params.arguments or {})

                local = Server(
                    "cc-proxy",
                    on_list_tools=_on_list_tools,
                    on_call_tool=_on_call_tool,
                )

            async with stdio_server() as (in_stream, out_stream):
                init_opts = local.create_initialization_options()
                log.info("Bridging %d tool(s) over stdio", len(tools_resp.tools))
                await local.run(in_stream, out_stream, init_opts)


# Display name for messages; set by main_async from --connector.
_CONNECTOR_LABEL = "connector"


def _error_result(text: str):
    import mcp_types as _types
    return _types.CallToolResult(content=[_types.TextContent(type="text", text=text)], isError=True)


# --- degraded mode ------------------------------------------------------------

def stub_tool(err: ConnectorError, connector: str):
    """The single tool a degraded bridge exposes: its name and description ARE
    the failure report, so an agent listing or searching tools cannot miss it."""
    import mcp_types as _types
    msg = human_message(err, connector)
    return _types.Tool(
        name=STUB_TOOL_NAMES.get(err.kind, STUB_TOOL_NAMES[KIND_UPSTREAM]),
        description=(f"UNAVAILABLE -- {msg} Calling this tool re-checks the connector and reports "
                     f"its current status. Tell the user the exact fix above; the {connector} "
                     f"capability is configured, it is just not working right now."),
        inputSchema={"type": "object", "properties": {}},
        _meta={DEGRADED_META_KEY: {"kind": err.kind, "connector": connector, "message": msg,
                                   "relogin": relogin_command(err.service) if err.kind == KIND_REAUTH else None}},
    )


async def run_degraded(err: ConnectorError, connector: str, recheck) -> None:
    """Serve stdio with one stub tool instead of exiting.

    Exiting made Hermes log "Connection closed", register zero tools, and
    respawn the bridge over and over; a stable degraded server is visible
    (tool list, tool_search, banner) and costs nothing while idle."""
    msg = human_message(err, connector)
    log.error("DEGRADED: %s", msg)
    tool = stub_tool(err, connector)

    async def _status_text() -> str:
        now = await recheck()
        if now is None:
            return (f"{connector} connector is healthy again. Run /reload-mcp in Hermes (or restart "
                    f"Hermes) to load its real tools.")
        return "STILL UNAVAILABLE -- " + human_message(now, connector)

    if _MCP_LEGACY_HTTP:
        local = Server("cc-proxy-degraded")

        @local.list_tools()
        async def _list_tools():  # type: ignore[no-redef]
            return [tool]

        @local.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any]):  # type: ignore[no-redef]
            return _error_result(await _status_text())
    else:
        import mcp_types as _types

        async def _on_list_tools(_ctx, _params):
            return _types.ListToolsResult(tools=[tool])

        async def _on_call_tool(_ctx, params):
            return _error_result(await _status_text())

        local = Server("cc-proxy-degraded", instructions=msg,
                       on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    async with stdio_server() as (in_stream, out_stream):
        await local.run(in_stream, out_stream, local.create_initialization_options())


# --- health check -------------------------------------------------------------

async def check_connector(creds: Optional[CredStore], connector: str,
                          server_id: Optional[str] = None) -> Optional[ConnectorError]:
    """Read-only probe: token (normal refresh only if expired), resolve, one
    ``initialize`` POST. Returns None when healthy, else the classified error."""
    try:
        if creds is None:
            creds = CredStore(CREDS_PATH)
        token = await creds.get_access_token()
        sid = server_id or await resolve_server_id(creds, connector)
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "cc-proxy-health", "version": "1"}}}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                PROXY_URL_TMPL.format(server_id=sid), json=body,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json, text/event-stream",
                         "X-Mcp-Client-Session-Id": str(uuid.uuid4())})
        if resp.status_code == 200:
            return None
        if resp.status_code == 401:
            code, message = _parse_401(resp.content)
            if code in CONNECTOR_TOKEN_INVALID_CODES:
                return ConnectorError(KIND_CONNECTOR_UNAUTHORIZED, f"mcp-proxy 401 {code}: {message}".strip(),
                                      service=creds.service)
            return ConnectorError(KIND_REAUTH, f"access token rejected (HTTP 401: {message or 'no detail'})",
                                  service=creds.service)
        return ConnectorError(KIND_UPSTREAM, f"initialize returned HTTP {resp.status_code}: "
                                             f"{_redact(resp.text)[:200]}", service=creds.service)
    except Exception as exc:
        return classify_exception(exc, service=getattr(creds, "service", None))


def _bridge_entries_from_config(config_path: Path) -> list[dict]:
    """Every mcp_servers entry that runs this bridge: name, connector, pinned service."""
    try:
        import yaml
        with config_path.open("r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        raise ConnectorError(KIND_CONFIG, f"cannot read {config_path}: {e}")
    out = []
    for name, entry in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(entry, dict):
            continue
        args = [str(a) for a in (entry.get("args") or [])]
        if not any("cc_proxy_mcp" in a for a in args) or "--connector" not in args:
            continue
        i = args.index("--connector")
        if i + 1 >= len(args):
            continue
        out.append({"name": name, "connector": args[i + 1],
                    "service": (entry.get("env") or {}).get("CC_PROXY_KEYCHAIN_SERVICE"),
                    "enabled": entry.get("enabled", True) not in (False, "false", "no", "off", 0)})
    return out


async def run_health(args: argparse.Namespace) -> int:
    if args.connector:
        entries = [{"name": args.connector, "connector": args.connector,
                    "service": os.environ.get("CC_PROXY_KEYCHAIN_SERVICE"), "enabled": True}]
    else:
        cfg_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"
        entries = _bridge_entries_from_config(cfg_path)
    results = []
    stores: dict[Optional[str], Any] = {}
    for e in entries:
        if not e["enabled"]:
            results.append({**e, "status": "disabled"})
            continue
        key = e["service"]
        if key not in stores:
            prev = os.environ.get("CC_PROXY_KEYCHAIN_SERVICE")
            try:
                if key:
                    os.environ["CC_PROXY_KEYCHAIN_SERVICE"] = key
                stores[key] = CredStore(CREDS_PATH)
            except ConnectorError as ce:
                stores[key] = ce
            finally:
                if key and prev is None:
                    os.environ.pop("CC_PROXY_KEYCHAIN_SERVICE", None)
                elif key:
                    os.environ["CC_PROXY_KEYCHAIN_SERVICE"] = prev
        store = stores[key]
        err = store if isinstance(store, ConnectorError) else await check_connector(store, e["connector"])
        results.append({**e, "status": "ok" if err is None else err.kind,
                        "message": None if err is None else human_message(err, e["connector"])})
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = {"ok": "OK  ", "disabled": "OFF "}.get(r["status"], "FAIL")
            line = f"{mark} {r['name']:<22} ({r['connector']}) -- {r['status']}"
            print(line)
            if r.get("message"):
                print(f"     {r['message']}")
    return 0 if all(r["status"] in ("ok", "disabled") for r in results) else 1


# --- entrypoint -------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    global _CONNECTOR_LABEL
    if args.health:
        return await run_health(args)

    connector = args.connector or args.server_id or "connector"
    _CONNECTOR_LABEL = connector
    pinned = os.environ.get("CC_PROXY_KEYCHAIN_SERVICE")
    creds: Optional[CredStore] = None
    verdict = _AuthVerdict()
    try:
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
        await run_proxy(server_id, creds, verdict)
        return 0
    except Exception as exc:
        if args.list:
            raise
        err = classify_exception(exc, auth_hint=verdict.error,
                                 service=creds.service if creds is not None else pinned)
        if err.service is None:
            err.service = creds.service if creds is not None else pinned
        if args.no_degraded:
            raise err

        async def _recheck():
            try:
                store = creds or CredStore(CREDS_PATH)
            except ConnectorError as ce:
                return ce
            return await check_connector(store, connector, args.server_id)

        await run_degraded(err, connector, _recheck)
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--connector", help="Connector display name (substring match), e.g. 'slack'")
    p.add_argument("--server-id", help="Explicit server UUID; skips resolution")
    p.add_argument("--list", action="store_true", help="Print all available connectors and exit")
    p.add_argument("--health", action="store_true",
                   help="Report per-connector status and exit (all bridge entries in "
                        "$HERMES_HOME/config.yaml, or just --connector). Exit 1 if any is unhealthy.")
    p.add_argument("--json", action="store_true", help="With --health: JSON output")
    p.add_argument("--no-degraded", action="store_true",
                   help="Exit on startup failure instead of serving a status stub tool")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    except ConnectorError as e:
        log.error("fatal: %s", human_message(e, args.connector or "connector"))
        return 1
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
