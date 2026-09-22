"""``hermes peer`` — bot-to-bot DMs across machines/gateways.

A *peer* is another Hermes gateway running the ``api_server`` platform; its stock
API is the transport (no new server surface). ``dm`` resolves the remote canonical
"Bot Chat" session (creating it when missing) and runs ONE synchronous turn — the
cross-machine twin of ``hermes -p <bot> chat --in ~ -c "Bot Chat"``. ``run``/``status``
/``stop``/``tail`` do the same turn through the async Runs API. Peer labels/URLs live
in config.yaml (``bot_peers``); the key lives in ``~/.hermes/.env`` as
``HERMES_PEER_<NAME>_KEY``. ``<peer>/<profile>`` targets the ``/p/<profile>/`` mirror.

Fork-only additions (carried over from the retired ``hermes submit``, which this
command replaces — see FORK.md):

* ``tail`` / ``run --tail`` stream the run's SSE event feed until it ends.
* Credential resolution is *widened*, never narrowed. A registered ``bot_peers``
  entry still resolves exactly as upstream intends (that path owns the
  ``Idempotency-Key`` bookkeeping and the registry's key guarantees). On top of it:
  an explicit ``--url``/``--api-key`` pair addresses an unregistered gateway
  ad hoc, and the reserved target name ``default`` resolves through
  ``HERMES_GATEWAY_URL`` → ``~/.hermes/.env`` → ``DEFAULT_GATEWAY_URL`` with the
  key chain ``HERMES_PEER_<NAME>_KEY`` → ``HERMES_GATEWAY_API_KEY`` →
  ``API_SERVER_KEY``. An *unrecognized* name still hard-fails, so a mistyped peer
  can never silently route to the default gateway.
* ``run --no-session`` skips the remote canonical "Bot Chat" so the run lands in
  its own peer-assigned session. The forced-session behavior remains the default:
  the Bot Chat is what gives a bot-to-bot exchange cross-turn continuity and a
  human-readable transcript on the remote machine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BOT_CHAT_TITLE = "Bot Chat"
_PEER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# One synchronous agent turn can legitimately take minutes.
DM_TIMEOUT_S = 600
LIST_TIMEOUT_S = 30
# Fork: the SSE feed emits a `: keepalive` comment every 30s, so anything past
# that is a dead connection rather than a quiet run.
TAIL_TIMEOUT_S = 120

# Fork: the reserved target name that resolves through the env/.env/default
# gateway chain instead of the ``bot_peers`` registry (the ``hermes submit``
# replacement path). Only honored when NO peer is registered under it.
DEFAULT_TARGET_NAME = "default"
# TLS via `tailscale serve` with a real Let's Encrypt cert.
DEFAULT_GATEWAY_URL = "https://hermes-gw-01.tail19c543.ts.net"

_TERMINAL_RUN_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
_FAILED_RUN_EVENTS = frozenset({"run.failed", "error"})


def _peer_key_env(name: str) -> str:
    return f"HERMES_PEER_{name.upper().replace('-', '_')}_KEY"


def _env_or_dotenv(*names: str) -> str:
    """First non-empty value across ``os.environ`` then ``~/.hermes/.env``.

    Fork: the credential chain the retired ``hermes submit`` used. Env beats
    the dotenv file for every name before the next name is tried, matching the
    old precedence (``HERMES_GATEWAY_API_KEY`` anywhere beats ``API_SERVER_KEY``).
    """
    import os

    from hermes_cli.config import get_env_value

    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
        try:
            value = (get_env_value(name) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _fork_gateway_url() -> tuple[str, str]:
    """Fork: ``(url, source)`` for the reserved ``default`` target."""
    import os

    if (url := (os.environ.get("HERMES_GATEWAY_URL") or "").strip()):
        return url.rstrip("/"), "env HERMES_GATEWAY_URL"
    try:
        from hermes_cli.config import get_env_value

        if (url := (get_env_value("HERMES_GATEWAY_URL") or "").strip()):
            return url.rstrip("/"), "~/.hermes/.env HERMES_GATEWAY_URL"
    except Exception:
        pass
    return DEFAULT_GATEWAY_URL, f"default ({DEFAULT_GATEWAY_URL})"


def _load_peers() -> dict:
    from hermes_cli.config import load_config

    cfg = load_config() or {}
    peers = cfg.get("bot_peers")
    return peers if isinstance(peers, dict) else {}


def _save_peers(peers: dict) -> None:
    from hermes_cli.config import load_config, save_config

    cfg = load_config() or {}
    cfg["bot_peers"] = peers
    save_config(cfg)


def _peer_secret(name: str) -> str:
    """The peer's API key: profile-scoped secret store first, raw env fallback."""
    env_name = _peer_key_env(name)
    try:
        from agent.secret_scope import get_secret

        return (get_secret(env_name, "") or "").strip()
    except Exception:
        import os

        return (os.environ.get(env_name) or "").strip()


def _request(
    url: str, key: str, *, method: str = "GET", body: dict | None = None,
    timeout: int = LIST_TIMEOUT_S, headers: dict[str, str] | None = None) -> dict:
    from hermes_cli.urllib_security import open_credentialed_url
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "hermes-peer-dm"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    # The peer URL is user-registered (``hermes peer add``); a redirect to a
    # different origin must not carry the Authorization: Bearer key with it —
    # a compromised/MITM'd peer could otherwise harvest it. open_credentialed_url
    # strips non-safelisted headers across a cross-origin redirect.
    with open_credentialed_url(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"Peer returned non-JSON response: {payload[:200]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Peer returned a non-object JSON response")
    return parsed


def _base_url(peer: dict, profile: str | None) -> str:
    url = str(peer.get("url") or "").rstrip("/")
    if profile:
        # Multiplex mirror: same handlers, scoped to the named profile.
        return f"{url}/p/{urllib.parse.quote(profile, safe='')}"
    return url


def _find_bot_chat(base: str, key: str) -> str | None:
    """The remote canonical Bot Chat's session id, or None.

    Bot Mode always HIDES canonical chats, so the plain listing (which
    excludes hidden sessions) misses an existing Bot Chat and the caller
    would try to create a duplicate that the peer's UNIQUE(title) guard
    rejects (issue #91583). Newer peers support an exact-title lookup with
    ``include_hidden=1``; older peers ignore the unknown query params and
    return the ordinary visible listing, so this single request degrades
    to exactly the previous behavior against them.
    """
    query = urllib.parse.urlencode({"limit": 200, "title": BOT_CHAT_TITLE, "include_hidden": 1})
    listing = _request(f"{base}/api/sessions?{query}", key)
    for session in listing.get("data") or []:
        if isinstance(session, dict) and (session.get("title") or "").strip() == BOT_CHAT_TITLE:
            return str(session.get("id") or "") or None
    return None


def _ensure_bot_chat(base: str, key: str) -> str:
    existing = _find_bot_chat(base, key)
    if existing:
        return existing
    try:
        created = _request(
            f"{base}/api/sessions", key, method="POST",
            body={"title": BOT_CHAT_TITLE, "source": "bot_peer_dm"})
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code == 400 and "title" in detail.lower():
            # Older peer (no title/include_hidden lookup support): its
            # canonical Bot Chat exists but is hidden, so we couldn't see it
            # and the create collided with the UNIQUE(title) guard.
            raise RuntimeError(
                f"Peer already has a '{BOT_CHAT_TITLE}' session but it is hidden and the "
                f"peer's gateway is too old to expose hidden sessions to this lookup "
                f"(HTTP 400: {detail}). Update the peer's hermes-agent, or unhide the "
                f"session there: PATCH /api/sessions/<id> {{\"hidden\": false}}.") from exc
        raise
    # Real api_server wraps the row: {"object": "hermes.session", "session": {...}}.
    session = created.get("session") if isinstance(created.get("session"), dict) else created
    session_id = str(session.get("id") or session.get("session_id") or "")
    if not session_id:
        raise RuntimeError("Peer did not return a session id for the new Bot Chat")
    return session_id


def _parse_target(target: str) -> tuple[str, str | None]:
    """``<peer>`` or ``<peer>/<profile>`` → (peer, profile|None)."""
    raw = (target or "").strip()
    peer, _, profile = raw.partition("/")
    peer = peer.strip()
    profile = profile.strip() or None
    if not peer:
        raise ValueError("Peer name required (hermes peer dm <peer>[/<agent>] ...)")
    if profile and not _PROFILE_RE.match(profile):
        raise ValueError(f"Invalid agent/profile name: {profile!r}")
    return peer, profile


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
        parsed = json.loads(body)
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        return message or body[:200]
    except Exception:
        return str(exc)


def _resolve_peer_target(target: str, args=None) -> tuple[str, str | None, dict, str]:
    """Resolve a target to ``(name, profile, config, key)``.

    Upstream's registered path is unchanged and still comes first for any
    registered name. The fork WIDENS it with two extra ways to reach a gateway
    that has no ``bot_peers`` entry, so retiring ``hermes submit`` costs nothing:

    1. ``--url`` given explicitly → that URL, ad hoc (any target label).
    2. registered ``bot_peers[name]`` + ``HERMES_PEER_<NAME>_KEY`` (upstream).
    3. the reserved name ``default`` → ``HERMES_GATEWAY_URL`` / ``~/.hermes/.env``
       / ``DEFAULT_GATEWAY_URL``.
    4. anything else → upstream's LookupError, verbatim. An unregistered name is
       NEVER silently redirected to the default gateway: a typo'd peer must fail
       loudly rather than deliver a prompt to the wrong machine.
    """
    peer_name, profile = _parse_target(target)
    peers = _load_peers()
    peer = peers.get(peer_name)
    registered = isinstance(peer, dict) and bool(peer.get("url"))
    explicit_url = ((getattr(args, "url", None) or "") if args is not None else "").strip()

    if not registered and not explicit_url and peer_name != DEFAULT_TARGET_NAME:
        raise LookupError(f"No peer named '{peer_name}'. Run: hermes peer list")

    if explicit_url:
        if not explicit_url.lower().startswith(("http://", "https://")):
            raise ValueError(f"--url must be an http(s) gateway base URL: {explicit_url!r}")
        peer, source = {"url": explicit_url.rstrip("/")}, "--url"
    elif registered:
        source = f"bot_peers.{peer_name}"
    else:
        url, source = _fork_gateway_url()
        peer = {"url": url}

    key = (
        (((getattr(args, "api_key", None) or "") if args is not None else "").strip())
        or _peer_secret(peer_name)
    )
    if not key and not registered:
        # Fork: only the widened (unregistered) paths consult the gateway-wide
        # chain. A registered peer keeps its own dedicated key, so borrowing an
        # unrelated gateway key for it would weaken the registry's guarantee.
        key = _env_or_dotenv("HERMES_GATEWAY_API_KEY", "API_SERVER_KEY")
    if not key:
        if registered:
            raise PermissionError(
                f"No API key for peer '{peer_name}'. Set it: hermes peer add {peer_name} "
                f"--url <url> --key <key> (or add {_peer_key_env(peer_name)}=<key> to ~/.hermes/.env)")
        raise PermissionError(
            f"No API key for '{peer_name}' ({source}). Pass --api-key, or set any of "
            f"{_peer_key_env(peer_name)} / HERMES_GATEWAY_API_KEY / API_SERVER_KEY "
            f"in the environment or ~/.hermes/.env.")
    peer = dict(peer or {}, hermes_source=source)
    return peer_name, profile, peer, key


def _message_from_args(args) -> str:
    message = (getattr(args, "message", None) or "").strip()
    # Fork: --file, carried over from the retired `hermes submit -f task.md`.
    if not message and (path := getattr(args, "file", None)):
        with open(path, "r", encoding="utf-8") as handle:
            message = handle.read().strip()
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    return message


def _peer_run_durability(base: str, key: str) -> bool | None:
    """Return durable support, or None when an older peer cannot advertise it."""
    try:
        capabilities = _request(f"{base}/v1/capabilities", key)
    except Exception:
        return None
    features = capabilities.get("features")
    if not isinstance(features, dict):
        return None
    contract = features.get("runs_idempotency")
    if not isinstance(contract, dict) or not contract.get("supported"):
        return None
    return bool(contract.get("durable"))


def _peer_failure(peer_name: str, exc: Exception) -> int:
    """Print a peer HTTP rejection or transport failure to stderr; always exit 1."""
    if isinstance(exc, urllib.error.HTTPError):
        detail = _http_error_detail(exc)
        print(f"Peer '{peer_name}' rejected the request (HTTP {exc.code}): {detail}",
              file=sys.stderr)
    else:
        print(f"Could not reach peer '{peer_name}': {exc}", file=sys.stderr)
    return 1


def _emit(args, payload: dict, text_lines: list[str]) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        for line in text_lines:
            print(line)
    return 0


def _peer_add(args) -> int:
    name = (args.name or "").strip().lower()
    if not _PEER_NAME_RE.match(name):
        print(f"Invalid peer name: {name!r} (lowercase, digits, -, _; max 64)", file=sys.stderr)
        return 2
    url = (args.url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        print("Peer --url must be an http(s) gateway base URL, e.g. http://spark.lan:8377", file=sys.stderr)
        return 2
    peers = _load_peers()
    peers[name] = {"url": url.rstrip("/"), **({"note": args.note.strip()} if getattr(args, "note", "") else {})}
    _save_peers(peers)
    key = (getattr(args, "key", "") or "").strip()
    if key:
        from hermes_cli.config import save_env_value

        save_env_value(_peer_key_env(name), key)
        print(f"Peer '{name}' saved ({url}) — key stored as {_peer_key_env(name)} in ~/.hermes/.env")
    else:
        print(
            f"Peer '{name}' saved ({url}). No key given — set the peer's API_SERVER_KEY with:\n"
            f"  hermes peer add {name} --url {url} --key <key>\n"
            f"  (or add {_peer_key_env(name)}=<key> to ~/.hermes/.env)")
    return 0


def _peer_remove(args) -> int:
    name = (args.name or "").strip().lower()
    peers = _load_peers()
    if name not in peers:
        print(f"No peer named '{name}'.", file=sys.stderr)
        return 1
    peers.pop(name)
    _save_peers(peers)
    print(f"Peer '{name}' removed (its {_peer_key_env(name)} entry in .env is kept; delete it manually if unused).")
    return 0


def _peer_list(args) -> int:
    peers = _load_peers()
    if not peers:
        print("No peers registered. Add one: hermes peer add <name> --url http://host:port --key <API_SERVER_KEY>")
        return 0
    for name in sorted(peers):
        entry = peers[name] if isinstance(peers[name], dict) else {}
        has_key = "key set" if _peer_secret(name) else f"NO KEY ({_peer_key_env(name)} unset)"
        note = f" — {entry.get('note')}" if entry.get("note") else ""
        print(f"{name}\t{entry.get('url', '?')}\t[{has_key}]{note}")
    return 0


def _peer_run_ctl(args, action: str, peer_name: str, profile: str | None, base: str,
                  key: str) -> int:
    """``status`` / ``stop`` on an asynchronous run."""
    run_id = (getattr(args, "run_id", None) or "").strip()
    if not run_id:
        print("Run ID required.", file=sys.stderr)
        return 2
    stop = action == "stop"
    try:
        result = _request(
            f"{base}/v1/runs/{urllib.parse.quote(run_id, safe='')}" + ("/stop" if stop else ""),
            key, method="POST" if stop else "GET", body={} if stop else None)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    if getattr(args, "json", False):
        print(json.dumps({"peer": peer_name, "profile": profile, **result}))
        return 0
    print(f"{run_id}: {result.get('status', 'unknown')}")
    if not stop and result.get("output"):
        print(result["output"])
    elif not stop and result.get("error"):
        print(result["error"], file=sys.stderr)
    return 0


def _tail_run_events(peer_name: str, base: str, key: str, run_id: str, *, as_json: bool = False) -> int:
    """Fork: stream ``GET /v1/runs/<id>/events`` (SSE) until the run ends.

    Carried over from the retired ``hermes submit --tail``, re-based on the
    ``run_id`` model ``status``/``stop`` already use and routed through the same
    ``open_credentialed_url`` policy as every other peer request, so the peer's
    Bearer key still cannot ride a cross-origin redirect.

    Returns 0 on a clean finish, 1 on a failed run or a dropped stream.
    Ctrl-C detaches without stopping the run (it keeps going on the peer).
    """
    from hermes_cli.urllib_security import open_credentialed_url

    url = f"{base}/v1/runs/{urllib.parse.quote(run_id, safe='')}/events"
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {key}", "Accept": "text/event-stream",
        "User-Agent": "hermes-peer-dm"})
    rc = 0
    try:
        with open_credentialed_url(request, timeout=TAIL_TIMEOUT_S) as response:
            if getattr(response, "status", 200) >= 400:
                print(f"Peer '{peer_name}' refused the event stream "
                      f"(HTTP {response.status}).", file=sys.stderr)
                return 1
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line or line.startswith(":"):
                    # Blank separator or a `: keepalive` / `: stream closed` comment.
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if as_json:
                    print(payload, flush=True)
                try:
                    event = json.loads(payload)
                except ValueError:
                    if not as_json:
                        print(payload, flush=True)
                    continue
                if not isinstance(event, dict):
                    continue
                name = str(event.get("event") or event.get("type") or "")
                if not as_json:
                    _print_tail_event(name, event)
                if name in _FAILED_RUN_EVENTS:
                    rc = 1
                if name in _TERMINAL_RUN_EVENTS:
                    break
    except KeyboardInterrupt:
        print(f"\nDetached from run {run_id} (it continues on peer '{peer_name}').",
              file=sys.stderr)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        return _peer_failure(peer_name, exc)
    return rc


def _print_tail_event(name: str, event: dict) -> None:
    """Render one SSE event for a human: deltas inline, everything else labelled."""
    if name == "message.delta":
        sys.stdout.write(str(event.get("delta") or ""))
        sys.stdout.flush()
        return
    detail = event.get("output") or event.get("error") or event.get("preview") or ""
    suffix = f": {detail}" if detail else ""
    print(f"\n[{name or 'event'}]{suffix}" if name in _TERMINAL_RUN_EVENTS
          else f"[{name or 'event'}]{suffix}", flush=True)


def _peer_tail(args, peer_name: str, profile: str | None, base: str, key: str) -> int:
    """``hermes peer tail <target> <run_id>`` — attach to an in-flight run."""
    run_id = (getattr(args, "run_id", None) or "").strip()
    if not run_id:
        print("Run ID required.", file=sys.stderr)
        return 2
    return _tail_run_events(peer_name, base, key, run_id,
                            as_json=bool(getattr(args, "json", False)))


def _turn_body(message: str, *, message_key: str, **extra) -> dict:
    """Request body for one turn. ``author`` is added only when a dispatcher set HERMES_TURN_AUTHOR."""
    from agent.turn_author import turn_author_from_env

    body = {message_key: message, **extra}
    author = turn_author_from_env()
    if author is not None:
        body["author"] = author
    return body


def _peer_run(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    idempotency_key = (getattr(args, "idempotency_key", None) or f"peer-{uuid.uuid4().hex}").strip()
    if (not idempotency_key or len(idempotency_key) > 255
            or re.search(r"[\r\n\x00]", idempotency_key)):
        print("Idempotency key must be 1-255 characters without control newlines.", file=sys.stderr)
        return 2
    # Fork: --no-session runs outside the remote canonical "Bot Chat". Default
    # stays upstream's forced session — that transcript is what makes a
    # bot-to-bot exchange continuous and auditable on the remote machine — but
    # a one-shot submission (the retired `hermes submit`) neither needs the
    # continuity nor the two session round trips, and works against a peer
    # whose key has no session-management rights. Omitting session_id makes the
    # peer assign the run its own session (api_server_runs: `session_id or run_id`).
    session_less = bool(getattr(args, "no_session", False))
    session_id = None
    # Fork: --instructions, the retired `hermes submit --instructions` —
    # an ephemeral system-prompt override the Runs API accepts per run.
    extra_body = {}
    if (instructions := (getattr(args, "instructions", None) or "").strip()):
        extra_body["instructions"] = instructions
    try:
        if _peer_run_durability(base, key) is not True:
            print(
                "Warning: this peer does not advertise restart-durable "
                "run replay; keep the run ID and avoid blind retries "
                "after a gateway restart.", file=sys.stderr)
        if not session_less:
            session_id = _ensure_bot_chat(base, key)
            extra_body["session_id"] = session_id
        result = _request(
            f"{base}/v1/runs", key, method="POST",
            body=_turn_body(message, message_key="input", **extra_body),
            headers={"Idempotency-Key": idempotency_key})
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return _peer_failure(peer_name, exc)
    run_id = str(result.get("run_id") or "")
    if not run_id:
        print(f"Peer '{peer_name}' did not return a run ID.", file=sys.stderr)
        return 1
    # A session-less run still reports whichever session the peer assigned it.
    reported_session = session_id or result.get("session_id") or run_id
    payload = {
        "peer": peer_name, "profile": profile, "session_id": reported_session, "run_id": run_id,
        "status": result.get("status") or "started", "idempotency_key": idempotency_key,
        "replayed": bool(result.get("replayed", False))}
    replay = " (replayed)" if payload["replayed"] else ""
    if getattr(args, "quiet", False) and not getattr(args, "json", False):
        # Fork: the retired `hermes submit -q` contract — only the run id, so
        # RUN=$(hermes peer run default -q "...") works.
        print(run_id)
        rc = 0
    else:
        rc = _emit(args, payload, [
            f"{run_id}: {payload['status']}{replay}", f"session_id: {reported_session}",
            f"idempotency_key: {idempotency_key}",
            f"tail:    hermes peer tail {args.target} {run_id}",
            f"status:  hermes peer status {args.target} {run_id}"])
    if rc == 0 and getattr(args, "tail", False):
        return _tail_run_events(peer_name, base, key, run_id,
                                as_json=bool(getattr(args, "json", False)))
    return rc


def _peer_dm(args, message: str, peer_name: str, profile: str | None, base: str, key: str) -> int:
    try:
        session_id = _ensure_bot_chat(base, key)
        result = _request(
            f"{base}/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat", key,
            method="POST", body=_turn_body(message, message_key="message"), timeout=DM_TIMEOUT_S)
    except RuntimeError as exc:
        print(f"Peer '{peer_name}': {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _peer_failure(peer_name, exc)
    msg = result.get("message")
    reply = str(msg.get("content") or "") if isinstance(msg, dict) else ""
    payload = {"peer": peer_name, "profile": profile,
               "session_id": result.get("session_id") or session_id, "reply": reply}
    return _emit(args, payload, [reply or "(no reply)"])


_REGISTRY_ACTIONS = {
    "add": _peer_add, "set": _peer_add, "remove": _peer_remove, "rm": _peer_remove,
    "list": _peer_list, "ls": _peer_list, None: _peer_list}


def cmd_peer(args) -> int:
    action = getattr(args, "peer_action", None)
    if action in _REGISTRY_ACTIONS:
        return _REGISTRY_ACTIONS[action](args)
    if action not in {"dm", "run", "status", "stop", "tail"}:
        print("Unknown peer action. See: hermes peer --help", file=sys.stderr)
        return 2
    try:
        peer_name, profile, peer, key = _resolve_peer_target(args.target, args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LookupError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    base = _base_url(peer, profile)
    if action in {"status", "stop"}:
        return _peer_run_ctl(args, action, peer_name, profile, base, key)
    if action == "tail":
        return _peer_tail(args, peer_name, profile, base, key)
    try:
        message = _message_from_args(args)
    except OSError as exc:
        print(f"Could not read the message file: {exc}", file=sys.stderr)
        return 2
    if not message:
        print("Message required (argument, --file, or stdin).", file=sys.stderr)
        return 2
    handler = _peer_run if action == "run" else _peer_dm
    return handler(args, message, peer_name, profile, base, key)


def build_peer_parser(subparsers) -> None:
    """Attach the ``peer`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "peer", help="Bot-to-bot DMs across machines (peer Hermes gateways)",
        description="Register other Hermes gateways as peers and message their agents. "
            "'hermes peer dm <peer>[/<agent>] \"...\"' delivers into the remote "
            "agent's canonical Bot Chat over the peer's API server and prints "
            "the reply — the cross-machine twin of 'hermes -p <bot> chat'. "
            "The peer must run the api_server platform; its API_SERVER_KEY is "
            "stored locally as a credential in ~/.hermes/.env. 'peer run' also "
            "accepts the fork-only --tail / --no-session / --url / --api-key / "
            "--file / -q flags carried over from the retired 'hermes submit'.",
        epilog=(
            "Examples:\n"
            "  hermes peer add spark --url http://spark.lan:8377 --key <API_SERVER_KEY>\n"
            "  hermes peer list\n"
            '  hermes peer dm spark "Message from 🤖 dixie (@dixie): disk status?"\n'
            '  hermes peer dm spark/researcher "..."   # named profile on a multiplexed peer\n'
            "  hermes peer run spark --idempotency-key ticket-123 < long-task.txt\n"
            "  hermes peer status spark run_abc123\n"
            "  hermes peer stop spark run_abc123\n"
            "  hermes peer remove spark\n"
            "\n"
            "Fork-only (replaces the retired 'hermes submit'):\n"
            '  hermes peer run default "do the thing" --no-session --tail\n'
            "      # 'default' = HERMES_GATEWAY_URL / ~/.hermes/.env / the built-in\n"
            "      #   default gateway, no bot_peers entry needed; --no-session keeps\n"
            "      #   the run out of the remote canonical Bot Chat; --tail streams\n"
            "      #   the SSE event feed until the run ends (ctrl-C detaches).\n"
            "  hermes peer run ad-hoc --url https://gw.example:8642 -f task.md -q\n"
            "  hermes peer tail default run_abc123      # attach to an existing run\n"
            "      # NOTE: a message positional must come BEFORE any flag\n"
            "      #   (argparse cannot fill an optional positional after one) —\n"
            "      #   or use -f/--file or stdin instead.\n"
            "\n"
            "Exit codes: 0 ok, 1 delivery/peer error, 2 usage error."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    peer_sub = parser.add_subparsers(dest="peer_action")

    add_p = peer_sub.add_parser("add", aliases=["set"], help="Register (or update) a peer gateway")
    add_p.add_argument("name", help="Peer name (lowercase slug, e.g. spark, homelab)")
    add_p.add_argument("--url", required=True, help="Peer gateway base URL, e.g. http://spark.lan:8377")
    add_p.add_argument("--key", default="", help="The peer's API_SERVER_KEY (stored in ~/.hermes/.env)")
    add_p.add_argument("--note", default="", help="Optional description")

    peer_sub.add_parser("list", aliases=["ls"], help="List registered peers")

    rm_p = peer_sub.add_parser("remove", aliases=["rm"], help="Remove a peer")
    rm_p.add_argument("name", help="Peer name")

    def _remote(name: str, help: str, *, run_id: bool):
        sp = peer_sub.add_parser(name, help=help)
        sp.add_argument(
            "target",
            help=f"<peer> or <peer>/<agent>; '{DEFAULT_TARGET_NAME}' resolves through "
                 f"HERMES_GATEWAY_URL / ~/.hermes/.env / the built-in default gateway (fork)")
        if run_id:
            sp.add_argument("run_id", help="Run ID returned by 'hermes peer run'")
        else:
            sp.add_argument("message", nargs="?", default=None, help="Message text (or --file, or stdin)")
            # Fork: carried over from the retired `hermes submit`.
            sp.add_argument("--file", "-f", default=None,
                            help="Read the message from this file instead of the argument/stdin (fork)")
            if name == "run":
                sp.add_argument(
                    "--idempotency-key", default=None, help="Stable retry key (generated when omitted)")
                sp.add_argument(
                    "--instructions", default=None,
                    help="Ephemeral system-prompt override sent as the run's `instructions` (fork)")
                sp.add_argument(
                    "--no-session", action="store_true", default=False,
                    help="Do not route the run through the peer's canonical 'Bot Chat'; let the "
                         "peer give it its own session (fork)")
                sp.add_argument(
                    "--tail", action="store_true", default=False,
                    help="After starting the run, stream its SSE event feed until it ends; "
                         "ctrl-C detaches without stopping it (fork)")
                sp.add_argument(
                    "--quiet", "-q", action="store_true", default=False,
                    help="Print only the run ID (machine-friendly, fork)")
        # Fork: address a gateway with no bot_peers entry at all.
        sp.add_argument("--url", default=None,
                        help="Ad-hoc gateway base URL; skips the bot_peers registry (fork)")
        sp.add_argument("--api-key", default=None,
                        help="Bearer token for this call; else HERMES_PEER_<NAME>_KEY / "
                             "HERMES_GATEWAY_API_KEY / API_SERVER_KEY (fork)")
        sp.add_argument("--json", action="store_true", default=False, help="Emit a JSON result")

    _remote("dm", "Message an agent on a peer gateway and print its reply", run_id=False)
    _remote("run", "Start a long peer turn asynchronously and return its run ID", run_id=False)
    _remote("status", "Read the status and final output of an asynchronous peer run", run_id=True)
    _remote("stop", "Stop one asynchronous peer run without affecting another turn", run_id=True)
    _remote("tail", "Stream an asynchronous peer run's live event feed until it ends (fork)",
            run_id=True)

    parser.set_defaults(func=cmd_peer)
