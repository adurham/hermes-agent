"""Use the Browser Use CLI 3.0 (https://browser-use.com) for browser automation

When browser.backend is "browser-use", the model gets ``browser_exec`` tool
instead of default browser tools
"""

import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from hermes_constants import (get_hermes_home, hermes_home_key, reset_hermes_home_override,
                              set_hermes_home_override)
from tools.browser_harness_dirs import daemon_env, daemon_identity, namespace_harness_dirs
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"

# Cloud daemon names become the BU_NAME env var
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Set on the env dict by the CDP resolvers when the resolved browser is EXCLUSIVE to this named session
# (per-name provider / named BU cloud / Lightpanda). Popped before the subprocess launches — never exported.
_PRIVATE_BROWSER_SENTINEL = "_HERMES_BU_PRIVATE_BROWSER"
# Internal route provenance: this exec resolved to a browser on the Bot Desktop display and must use
# the same human-control lease fence as the built-in browser tools. Popped before launching the CLI.
_BOT_DESKTOP_BROWSER_SENTINEL = "_HERMES_BU_BOT_DESKTOP_BROWSER"
# The note when the consented real profile could not load (the route fell back). Popped before launch.
_REAL_PROFILE_FALLBACK_SENTINEL = "_HERMES_BU_REAL_PROFILE_FALLBACK"
# local=True whose real profile could not load: run on the local sidecar browser. Popped before launch.
_LOCAL_FALLBACK_SENTINEL = "_HERMES_BU_LOCAL_FALLBACK"

# Prepended to the model's code for named sessions on SHARED browsers (a /browser connect CDP override): the
# harness daemon attaches to the first existing page at startup, so two fresh named daemons can land on the
# SAME tab. Steering each onto a tab it created prevents clobbering. Runs once per daemon (marker keyed by
# BU_NAME + daemon pid).
_OWN_TAB_PREAMBLE = """\
# hermes: pin this named session to its own tab (once per daemon process)
def _hermes_ensure_own_tab():
    import os as _os, tempfile as _tf
    _name = _os.environ.get("BU_NAME", "default")
    try:
        # Key the marker by the daemon's pid so a daemon restart (which
        # re-attaches to the first shared page) re-pins automatically,
        # while agent-driven tab switches mid-session are left alone.
        from browser_harness import _ipc as _bipc
        _dpid = _bipc.pid_path(_name).read_text().strip() or "0"
    except Exception:
        _dpid = "0"
    _uid = _os.getuid() if hasattr(_os, "getuid") else 0
    _marker = _os.path.join(
        _tf.gettempdir(), "hermes-bu-owntab-%s-%s-%s" % (_uid, _name, _dpid)
    )
    if _os.path.exists(_marker):
        return
    try:
        # Force a fresh target: new_tab() would REUSE a blank current tab,
        # which is exactly the tab a sibling daemon may also hold.
        _tid = cdp("Target.createTarget", url="about:blank").get("targetId")
        if _tid:
            switch_tab(_tid)
    except Exception:
        pass  # best-effort: worst case is pre-fix behavior
    try:
        open(_marker, "w").close()
    except OSError:
        pass
_hermes_ensure_own_tab()
del _hermes_ensure_own_tab
"""

_DEFAULT_TIMEOUT_S = 300
_MIN_TIMEOUT_S = 5
_MAX_TIMEOUT_S = 1800
_STDERR_CAP_CHARS = 4000

_TASK_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")  # filesystem-safe task ids
# Screenshot paths printed by capture_screenshot(): POSIX or Windows drive-letter absolute.
_IMAGE_PATH_RE = re.compile(r"((?:[A-Za-z]:[\\/]|/)[^\s\"']+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
# http(s) URL literals in exec code checked against browser_navigate's policy
_URL_RE = re.compile(r"https?://[^\s'\"\\)]+", re.IGNORECASE)
_FHS_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def _quiet(fn: Callable[[], Any], default: Any, log_prefix: str = "") -> Any:
    """``fn()``, or ``default`` on any exception (debug-logged when ``log_prefix`` is set)."""
    try:
        return fn()
    except Exception as e:
        if log_prefix:
            logger.debug("%s: %s", log_prefix, e)
    return default


def _lazy_call(module: str, name: str, default: Any, log_prefix: str) -> Any:
    """Call ``module.name()`` resolved at call time (tests stub the module / patch the
    attribute); on any failure log ``log_prefix`` and return ``default``."""
    return _quiet(lambda: getattr(importlib.import_module(module), name)(), default, log_prefix)


def _camofox_active(context: str = "") -> bool:
    return _lazy_call("tools.browser_camofox", "is_camofox_mode", False, f"Camofox activity check failed{context}")


def _real_profile_consented() -> bool:
    return _lazy_call("tools.browser_tool_cloud", "_use_real_profile", False, "real-profile consent lookup failed")


def _set_cdp_env(env: dict, cdp: str) -> None:
    """Export a CDP endpoint under the BU_CDP_* contract (http(s) → URL, else WS)."""
    env["BU_CDP_URL" if cdp.startswith(("http://", "https://")) else "BU_CDP_WS"] = cdp


def _has_cdp_env(env: dict) -> bool:
    return bool(env.get("BU_CDP_WS") or env.get("BU_CDP_URL"))


def _export_session_cdp(env: dict, get_session_info: Callable[[str], Any], cache_key: str,
                        fail_msg: Callable[[Exception], str], no_cdp_msg: str) -> Optional[str]:
    """Export the CDP endpoint from ``get_session_info(cache_key)``; error string on failure / no CDP."""
    try:
        cdp = str((get_session_info(cache_key) or {}).get("cdp_url") or "")
    except Exception as e:
        return fail_msg(e)
    if not cdp:
        return no_cdp_msg
    _set_cdp_env(env, cdp)
    return None


class _CodeUrlVerdict(NamedTuple):
    """What one pass over a code blob's URL literals decided (see ``_code_url_verdict``)."""

    blocked: Optional[str]
    local_sidecar: bool


def _code_url_verdict(code: str) -> _CodeUrlVerdict:
    """One pass over the code's URL literals: the blocking error, if any, and whether the call must run on
    the hybrid LOCAL sidecar. Each URL's routing predicate is evaluated ONCE and feeds both answers: it
    resolves DNS unmemoised, so a rebinding host evaluated twice could relax the private-address floor and
    still route to the cloud provider."""
    from tools.browser_tool import _hybrid_routes_locally, evaluate_url_safety
    local_sidecar = False
    for url in _URL_RE.findall(code or ""):
        routes_local = _hybrid_routes_locally(url)
        local_sidecar = local_sidecar or routes_local
        err = evaluate_url_safety(url, auto_local=routes_local)
        if err:
            return _CodeUrlVerdict(err.get("error", "Blocked: unsafe URL"), local_sidecar)
    return _CodeUrlVerdict(None, local_sidecar)


def _base_subprocess_env() -> dict:
    from tools.browser_tool import _build_browser_env
    env = _build_browser_env()
    # The harness runs on Hermes's own interpreter, but a bundled Desktop install boots that
    # interpreter with its site dir on PYTHONPATH (no venv to activate), and the harness's daemon
    # re-runs sys.executable. Point PYTHONPATH at the harness's site dir, replacing whatever the
    # agent process inherited, so both the CLI and its daemon import the same packages.
    env.pop("PYTHONHOME", None)
    site_dir = _harness_site_dir()
    if site_dir:
        env["PYTHONPATH"] = site_dir
    else:
        env.pop("PYTHONPATH", None)
    env["PATH"] = _floor_subprocess_path(env.get("PATH", ""))
    env.setdefault("ANONYMIZED_TELEMETRY", "false")
    namespace_harness_dirs(env, _served_profile_tag())  # a served profile's own harness runtime/tmp dirs
    return env


def _floor_subprocess_path(path: str) -> str:
    """Keep system commands reachable from profile workers with a minimal PATH.

    Browser subprocesses may launch shell helpers; version-manager-only paths
    omit /usr/bin. Reuse the shared browser PATH floor on POSIX.
    """
    if os.name == "nt":
        return path
    with contextlib.suppress(Exception):
        from tools.browser_tool_install import _merge_browser_path
        return _merge_browser_path(path or "")
    parts = [p for p in (path or "").split(os.pathsep) if p]
    return os.pathsep.join(parts + [d for d in _FHS_BIN_DIRS if d not in set(parts) and os.path.isdir(d)])


def _read_browser_cfg() -> dict:
    """Return the ``browser:`` config section, or {} on any failure."""
    try:
        from hermes_cli.config import cfg_get, read_raw_config
        cfg = cfg_get(read_raw_config(), "browser", default={})
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug("Could not read browser config section: %s", e)
        return {}


def _use_gateway(browser_cfg: dict) -> bool:
    """True when the browser section selects the Nous Tool Gateway — by the current ``hermes tools``
    picker row (``cloud_provider: nous``) or the pre-picker ``use_gateway: true`` flag. Reading only
    the legacy flag missed every picker-configured gateway, and the direct-API branch it fell into
    holds no credentials in managed mode (#108310)."""
    if is_truthy_value(browser_cfg.get("use_gateway"), default=False):
        return True
    try:
        from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER
    except Exception:  # pragma: no cover — helper ships with the package
        return False
    return str(browser_cfg.get("cloud_provider") or "").strip().lower() == NOUS_MANAGED_PROVIDER


def get_browser_backend() -> str:
    """Configured browser backend key ("" = unset → default). YAML 1.1 parses an
    unquoted ``off`` as False — that must mean BACKEND_DISABLED, not "unset"."""
    raw = _read_browser_cfg().get("backend")
    return (BACKEND_DISABLED if raw is False else "") if isinstance(raw, bool) else str(raw or "").strip().lower()


def set_browser_use_mode(enabled: bool) -> None:
    """``/browser use [off]`` on every surface: persist ``browser.backend`` for the current profile and drop
    cached tool availability. A live agent keeps its tools (prompt cache); the next one built gets the swap."""
    from hermes_cli.config import load_config, save_config
    from tools.registry import invalidate_check_fn_cache
    config = load_config()
    config.setdefault("browser", {})["backend"] = _BACKEND_KEY if enabled else BACKEND_DISABLED
    save_config(config)
    invalidate_check_fn_cache()


def is_legacy_browser_use_cloud_config(browser_cfg: dict) -> bool:
    """True for pre-CLI direct-API Browser Use cloud configs. An explicit backend or
    a non-Browser-Use cloud_provider wins; Camofox is selected via env var, not
    cloud_provider, so a Camofox user with a stray BROWSER_USE_API_KEY keeps it."""
    if not isinstance(browser_cfg, dict) or browser_cfg.get("backend"):
        return False
    provider = str(browser_cfg.get("cloud_provider") or "").strip().lower()
    if provider not in {"browser-use", ""} or _use_gateway(browser_cfg) or _camofox_active(" during migration"):
        return False
    return _browser_use_api_key_set()


def _browser_use_api_key_set() -> bool:
    """BROWSER_USE_API_KEY for the served profile: the only credential Hermes autospawns BU cloud with.
    Profile-scoped so a multiplexed secondary never inherits the default's cloud mode."""
    from agent.secret_scope import get_secret
    return bool(get_secret("BROWSER_USE_API_KEY", ""))


def is_browser_use_cli_mode() -> bool:
    """True when the Browser Use CLI replaces the built-in browser stack. Browser Use mode is the DEFAULT:
    unset ``browser.backend`` ("") enables it whenever browser-harness is importable (a core dependency);
    ``browser.backend: off`` keeps the built-in browser_* tools. Camofox always falls back to the built-in
    tools (Firefox, custom HTTP API, no CDP surface for the harness)."""
    if _camofox_active():
        return False
    backend = get_browser_backend()
    return backend == _BACKEND_KEY if backend else (is_legacy_browser_use_cloud_config(_read_browser_cfg()) or _find_cli() is not None)


def default_downgrade_notice() -> Optional[str]:
    """One-line notice when ``browser.backend`` is unset but the CLI is not runnable, so
    the session fell back to the built-in tools. Rate-limited to once per 24h via a stamp file."""
    try:
        if get_browser_backend() or _camofox_active() or _find_cli() is not None:
            return None  # explicit choice / Camofox / CLI present — nothing downgraded
        stamp = Path(get_hermes_home()) / "cache" / ".browser_use_default_notice"
        now = time.time()
        with contextlib.suppress(OSError):
            if 0 <= now - stamp.stat().st_mtime < 24 * 3600:
                return None
        with contextlib.suppress(OSError):
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.touch()
            os.utime(stamp, (now, now))
        return ("browser-harness is missing from Hermes's Python environment — using the built-in browser tools. "
                "Run `hermes update` to re-sync it, or set `browser.backend: off` in config.yaml to silence this.")
    except Exception as e:  # pragma: no cover — a notice must never break startup
        logger.debug("browser-use downgrade notice failed: %s", e)
        return None


def _harness_site_dir() -> Optional[str]:
    """The site dir Hermes's interpreter imports ``browser_harness`` from, or None."""
    spec = importlib.util.find_spec("browser_harness")
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).resolve().parent.parent)


def _find_cli() -> Optional[list[str]]:
    """The Browser Use CLI's engine (browser-harness) is a core dependency of Hermes's own venv,
    so every install, the Desktop bundle included, runs it on the current interpreter."""
    if _harness_site_dir() is None:
        return None
    return [sys.executable, "-m", "browser_harness.run"]


def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable per-task scratch dir that persists across browser_exec calls"""
    if os.environ.get("BH_AGENT_WORKSPACE"):
        return os.environ["BH_AGENT_WORKSPACE"]
    try:
        safe = _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"
        path = Path(get_hermes_home()) / "cache" / "browser-use" / "workspace" / safe
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except Exception as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None


def _find_screenshot(stdout: str, since: float) -> Optional[str]:
    """Last screenshot path printed during this exec that exists and was written after
    the exec started, or None."""
    for path in reversed(_IMAGE_PATH_RE.findall(stdout or "")):
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= since - 1:
                return path
        except OSError:
            continue
    return None


def _native_screenshot_result(result: dict[str, Any], path: str) -> Optional[dict[str, Any]]:
    """Build a multimodal tool result attaching path for vision models"""
    try:
        from tools.vision_tools import (_EMBED_MAX_DIMENSION,
                                        _resize_image_for_vision, _should_use_native_vision_fast_path)
        from tools.vision_tools_history_budget import resolve_embed_target_bytes
        if not _should_use_native_vision_fast_path():
            return None
        # History-reuse cap: this data URL bakes into the tool result and is re-sent every later turn —
        # same policy as the vision_analyze / browser_vision native embeds.
        data_url = _resize_image_for_vision(Path(path), mime_type="image/png",
                                            max_base64_bytes=resolve_embed_target_bytes(),
                                            max_dimension=_EMBED_MAX_DIMENSION, force_jpeg=True)
        text = json.dumps(result, ensure_ascii=False)
        attached = text + "\n\nThe screenshot from this call is attached — inspect it with your native vision."
        return {"_multimodal": True, "text_summary": text, "meta": {"screenshot_path": path, "native_vision": True},
                "content": [{"type": "text", "text": attached}, {"type": "image_url", "image_url": {"url": data_url}}]}
    except Exception as e:
        logger.debug("Native screenshot attach failed (falling back to text): %s", e)
        return None


def _served_profile_tag() -> str:
    """``""`` outside a served-profile scope (every legacy key stays byte-identical); under a
    multiplexed turn, the routed profile's home key — one profile's browser must never be handed
    to another that happens to use the same session name or task id (#110032)."""
    from hermes_constants import get_hermes_home_override, hermes_home_key
    return "" if get_hermes_home_override() is None else hermes_home_key()


def _backend_cache_key(task_id: Optional[str], session_name: str = "", sidecar: bool = False) -> str:
    """Session-cache key for a backend browser: named sessions get their own; served profiles get their own.
    ``sidecar`` appends the hybrid ``::local`` suffix, which makes ``_get_session_info`` build local Chromium
    even when a cloud provider is configured."""
    key = f"bu-named-{session_name}" if session_name else (task_id or "browser-exec-default")
    tag = _served_profile_tag()
    key = f"{key}@{tag}" if tag else key
    return f"{key}::local" if sidecar else key


def _sidecar_daemon_name(name: str) -> str:
    """BU_NAME of the harness daemon serving the hybrid local sidecar. The daemon reads BU_CDP_* once at
    spawn and outlives the call, so sharing the cloud route's daemon would relay a private URL to the
    cloud browser it is already attached to. User session names cannot start with ``_``."""
    return f"_local_{name or 'default'}"[:64]


def _export_real_profile_note(env: dict, key: str) -> None:
    """The session layer decides real profile vs fresh browser once per key; carry its note (if it fell
    back) so the result reports the browser this call actually drives."""
    from tools.browser_tool_session import real_profile_note
    note = real_profile_note(key)
    if note:
        env.setdefault(_REAL_PROFILE_FALLBACK_SENTINEL, note)


def _resolve_lightpanda_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                            sidecar: bool = False) -> Optional[str]:
    """Point the harness at a Hermes-spawned ``lightpanda serve`` (``browser.engine: lightpanda`` and
    nothing of higher precedence claimed the session). Each cache key gets its own process via the
    legacy ``_get_session_info()`` (cache, reaper, atexit): private browser, own-tab preamble skipped."""
    try:
        from tools.browser_tool_session import _get_session_info
        from tools.browser_tool_lightpanda_fallback import _using_lightpanda_engine
        if not _using_lightpanda_engine():
            return None
    except Exception as e:  # stubbed browser_tool in tests / engine lookup failure
        logger.debug("browser engine lookup failed: %s", e)
        return None
    err = _export_session_cdp(
        env, _get_session_info, _backend_cache_key(task_id, session_name, sidecar),
        lambda e: (f"Lightpanda could not be started: {e} Set browser.engine to auto "
                   "to use local Chrome, or switch backends via `hermes tools` → Browser Automation."),
        "Lightpanda session returned no CDP endpoint. Set browser.engine to auto to use local Chrome.",
    )
    if err is None:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
        _export_real_profile_note(env, _backend_cache_key(task_id, session_name, sidecar))
    return err


def _reach_sandbox_cdp(cdp: str) -> str:
    """A CDP endpoint agent-browser reported from INSIDE the terminal backend's sandbox is that sandbox's
    loopback: unreachable from this host (Docker bridge / ssh remote). The harness, the vault supervisor
    and ``browser_exec`` all connect from here, so forward the port over the sandbox's exec stream and hand
    them the local end. Chromium's DevTools accepts any loopback ``Host`` header, port included."""
    try:
        from tools.browser_tool_session import _browser_in_sandbox
        if not _browser_in_sandbox():
            return cdp
        from urllib.parse import urlsplit, urlunsplit
        from tools.bot_desktop import runtime as _bd_runtime, sandbox_host
        from tools.environments import streams
        parts = urlsplit(cdp)
        if parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
            return cdp
        sandbox = _bd_runtime._sandbox_env(create=True)
        if sandbox is None:
            return cdp
        local = streams.forward_port(sandbox, parts.port, user=sandbox_host._user_for(sandbox))
        return urlunsplit(parts._replace(netloc=f"127.0.0.1:{local}"))
    except Exception as e:  # a failed forward degrades to the unreachable endpoint's own error
        logger.debug("sandbox CDP forward unavailable: %s", e)
        return cdp


def _resolve_managed_chromium_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                                  sidecar: bool = False) -> Optional[str]:
    """Point the harness at Hermes' packaged Chromium, launched through agent-browser for this cache key —
    the same browser the built-in tools drive. Left alone, the harness discovers the user's INSTALLED
    Chrome on its default profile, which needs the chrome://inspect toggle + an Allow popup per run and
    is blocked outright on Chrome >=136; on a headless box it just reports ``chrome-not-running``.
    ``get cdp-url`` runs through ``_run_browser_command`` (legacy cache, inactivity reaper, atexit, Chromium
    preflight/auto-install) on EVERY call: it launches the browser cold, follows a relaunch, and refreshes
    the agent-browser daemon's idle timer, which never sees the harness's direct CDP traffic."""
    try:
        from tools.browser_tool_session import _run_browser_command
        from tools.browser_tool import _get_open_command_timeout
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("managed chromium resolution unavailable: %s", e)
        return None
    res = _run_browser_command(_backend_cache_key(task_id, session_name, sidecar), "get", ["cdp-url"],
                               timeout=_get_open_command_timeout(first_open=True))
    cdp = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp:
        return (f"The local browser could not be started: {(res or {}).get('error') or 'agent-browser returned no CDP endpoint'} "
                "Run `hermes tools` → Browser Automation to (re)install Chromium, or switch backends.")
    cdp = _reach_sandbox_cdp(cdp)
    _set_cdp_env(env, cdp)
    env[_PRIVATE_BROWSER_SENTINEL] = "1"  # one Chromium per cache key: nothing to share a tab with
    env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
    _export_real_profile_note(env, _backend_cache_key(task_id, session_name, sidecar))
    return None


def _resolve_local_engine_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                              sidecar: bool = False) -> Optional[str]:
    """Local engine (no provider / override, or the hybrid sidecar): ``browser.engine: lightpanda`` or the
    packaged Chromium."""
    err = _resolve_lightpanda_cdp(env, task_id, session_name, sidecar)
    if err or _has_cdp_env(env):
        return err
    return _resolve_managed_chromium_cdp(env, task_id, session_name, sidecar)


def _resolve_backend_cdp(env: dict, task_id: Optional[str], session_name: str = "",
                         force_local_sidecar: bool = False) -> Optional[str]:
    """Point the harness at the configured backend's CDP endpoint; error string on failure.

    Precedence: (1) ``BU_CDP_WS``/``BU_CDP_URL`` already in env (operator override); (2) ``BROWSER_CDP_URL``
    env / ``browser.cdp_url`` (``/browser connect``); (3) a cloud provider via the legacy ``_get_session_info()``
    so browser_exec shares the SAME session machinery (per-task cache, expiry, reaper, atexit);
    (4) the local engine — ``browser.engine: lightpanda`` or Hermes' packaged Chromium via agent-browser
    (never the harness's own discovery of the user's installed Chrome); (5) BU direct-API configs WITH
    ``BROWSER_USE_API_KEY`` → None (checked before (3)): the CLI reaches BU cloud natively (BU_AUTOSPAWN); a
    keyless ``browser-use`` provider (managed-gateway autodetect, or a selection missing its key) takes (3).
    ``session_name`` (BU_NAME) keys the session cache so each name gets its OWN browser — what makes named
    sessions concurrent-safe.

    ``force_local_sidecar`` skips step (3): the call runs on the local engine under the ``::local`` cache
    key (hybrid routing; see ``_hybrid_routes_locally``). It sits BELOW the two operator overrides, and the
    sidecar verdict never fires while a CDP override is set."""
    if _has_cdp_env(env):
        return None
    try:
        from tools.browser_tool_cloud import _get_cloud_provider
        from tools.browser_tool_session import _get_session_info
        from tools.browser_tool_cdp import _get_cdp_override
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("browser_tool backend resolution unavailable: %s", e)
        return None
    override = _quiet(_get_cdp_override, "")
    if override:
        _set_cdp_env(env, override)
        return None
    if force_local_sidecar:
        return _resolve_local_engine_cdp(env, task_id, session_name, sidecar=True)
    provider = _quiet(_get_cloud_provider, None, "Cloud provider lookup failed")
    if provider is None:
        return _resolve_local_engine_cdp(env, task_id, session_name)

    # Browser Use direct-API configs: the CLI talks to BU cloud natively (BU_AUTOSPAWN) — the
    # legacy provider would create a second, redundant session. Nous-gateway configs (cloud_provider: nous
    # from the picker, or the pre-picker use_gateway: true) DO resolve through the provider: the gateway
    # provisions the browser server-side and returns its CDP URL. Without BROWSER_USE_API_KEY the CLI cannot
    # autospawn, and the provider is either the managed gateway (autodetect on a never-configured install) or
    # a selection missing its key; both go through the session path (gateway CDP, or the session layer's
    # local-Chromium fallback) instead of leaving the harness to hunt for an installed Chrome.
    provider_key = str(getattr(provider, "name", "") or "").strip().lower()
    if provider_key == _BACKEND_KEY and not _use_gateway(_read_browser_cfg()) and _browser_use_api_key_set():
        env[_PRIVATE_BROWSER_SENTINEL] = "1"  # named BU cloud browsers are exclusive to their daemon
        return None

    provider_name = type(provider).__name__
    session: dict = {}

    def _session_info(key: str) -> dict:
        session.update(_get_session_info(key) or {})
        return session

    err = _export_session_cdp(
        env, _session_info, _backend_cache_key(task_id, session_name),
        lambda e: (f"Cloud browser provider {provider_name} failed to provide a session: {e}. "
                   "Fix the provider configuration or switch backends via `hermes tools` → Browser Automation."),
        f"Cloud browser provider {provider_name} returned no CDP endpoint, so Browser Use mode "
        "cannot drive it. Switch to the built-in browser tools for this provider.",
    )
    if err and session.get("fallback_from_cloud"):
        # The provider failed (rate limit, quota, outage) and the session layer fell back to local
        # Chromium, as it does for the built-in tools; that session has no CDP URL until it launches.
        logger.warning("browser_exec: %s failed (%s); using local Chromium for this session",
                       provider_name, session.get("fallback_reason"))
        return _resolve_managed_chromium_cdp(env, task_id, session_name)
    # A provider browser keyed bu-named-<name> is exclusive to this session — the
    # own-tab preamble would just leak a blank tab into it.
    if err is None and session_name:
        env[_PRIVATE_BROWSER_SENTINEL] = "1"
    return err


def _resolve_real_profile_cdp(env: dict, force_local: bool) -> Optional[str]:
    """Point the harness at the user's real-profile copy-browser (a SNAPSHOT of their default Chromium
    profile, hermes_cli.browser_connect) when consented. Two ways in: the effective backend is already local
    (no provider, CDP override, or legacy BU cloud config) → silent upgrade; or ``force_local`` (consent-gated
    ``local`` arg) → the user's browser even under a cloud backend. Operator overrides (BU_CDP_* env,
    /browser connect, ``browser.cdp_url``) own the session either way. A launch error is returned;
    ``_route_backend`` then falls back to the normal backend and reports it, never silently."""
    if not _real_profile_consented() or _has_cdp_env(env):
        return None
    try:
        from tools.browser_tool_cdp import _get_cdp_override_raw
        from tools.browser_tool_cloud import _get_cloud_provider
        from tools.browser_tool_real_profile import _real_profile_cdp
    except Exception as e:  # pragma: no cover — stubbed browser_tool in tests
        logger.debug("real-profile backend resolution unavailable: %s", e)
        return None
    if _quiet(_get_cdp_override_raw, ""):
        return None
    # Only auto-upgrade genuinely-local attaches; any cloud path (provider, provider lookup failure, or
    # legacy BU cloud config) stays on its backend unless the model passes local=true.
    if not force_local and (_quiet(_get_cloud_provider, object()) is not None
                            or is_legacy_browser_use_cloud_config(_read_browser_cfg())):
        return None
    cdp, err = _real_profile_cdp()
    if cdp and not err:
        _set_cdp_env(env, cdp)
        env[_BOT_DESKTOP_BROWSER_SENTINEL] = "1"
    return err or None


def _attach_vault_supervisor(env: dict, task_id: Optional[str]) -> None:
    """Attach the per-task CDP supervisor to the browser this exec drives so ``browser_vault`` fill has
    a secret-capable WebSocket (never argv) into the SAME browser. Only CDP-routed backends expose an
    endpoint; BU direct-cloud (BU_AUTOSPAWN) does not, and the vault tools report ``supervisor_required``."""
    cdp = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    if not cdp:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(task_id=task_id or "default", cdp_url=_resolve_cdp_override(cdp),
                                         dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("browser_exec: CDP supervisor attach failed (non-fatal): %s", exc)


def _route_backend(env: dict, session: str, task_id: Optional[str], local: bool,
                   force_local_sidecar: bool = False, real_profile: bool = True) -> Optional[str]:
    """Resolve where the harness connects; returns an error string or None. Real-profile consent runs
    BEFORE provider resolution so a hit short-circuits the cloud path via the BU_CDP_* env contract. Named
    sessions compose with the backend: BU_NAME namespaces the harness daemon (IPC socket, log, pid) and on
    provider backends additionally keys its own cloud browser.

    ``force_local_sidecar`` (a call carrying a private URL) declines the cloud provider for the hybrid local
    sidecar; unlike ``local`` (the consent-gated real-profile browser) it never touches the user's profile."""
    # ``real_profile=False``: this route already fell back, so it stays on its fresh browser instead
    # of swapping browsers mid-task once the profile frees up.
    rp_err = _resolve_real_profile_cdp(env, force_local=local) if real_profile else None
    # local=True is only served by the real-profile route; consent off must not pretend.
    if local and not _has_cdp_env(env) and not _real_profile_consented():
        return ("local=true was requested but browser.use_real_profile is off. Enable it in config.yaml "
                "(browser.use_real_profile: true) or the desktop Settings → Browser section, then retry.")
    # local=True promised the user's own machine: a profile that can't load runs on a fresh LOCAL browser
    # (the sidecar, which never loads the profile), never the cloud one. Without ``local`` the session
    # layer makes the one real-profile-or-fresh decision and reports it (_resolve_managed_chromium_cdp).
    if local and not _has_cdp_env(env) and (rp_err or not real_profile):
        if rp_err:
            from tools.browser_tool_real_profile import real_profile_fallback_note
            env[_REAL_PROFILE_FALLBACK_SENTINEL] = real_profile_fallback_note(rp_err)
        env[_LOCAL_FALLBACK_SENTINEL] = "1"
        force_local_sidecar = True
    return _resolve_backend_cdp(env, task_id, session_name=session,
                                force_local_sidecar=force_local_sidecar)


def _group_popen_kwargs() -> dict:
    """Popen kwargs starting the CLI in its own process group (a new session on POSIX) so a
    timeout can take down every process that inherited the capture pipes, not just the CLI
    child. Windows also hides the console the .cmd shim would flash (as browser_tool does)."""
    def _flags() -> dict:
        from hermes_cli._subprocess_compat import windows_hide_flags
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": windows_hide_flags() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                "startupinfo": si}
    return _quiet(_flags, {}, "Windows hide-flags unavailable") if os.name == "nt" else {"start_new_session": True}


def _clamp_timeout(timeout_s: Any) -> int:
    try:
        return max(_MIN_TIMEOUT_S, min(int(timeout_s), _MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


# After a whole-group SIGKILL, every pipe holder is dead, so the drain below is normally
# instant; the deadline only guards against a process outside the group still holding a pipe.
_POST_KILL_DRAIN_S = 10.0


def _kill_cli_process_group(proc) -> None:
    """SIGKILL the CLI's whole process group (POSIX; ``start_new_session`` made pgid == pid) or,
    on Windows, its process tree via ``taskkill /T /F`` — the only group-wide kill it offers."""
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                           check=False, creationflags=windows_hide_flags())
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX only, the nt branch returned above


def _reap_dead_child(proc) -> None:
    """``wait()`` a child we just SIGKILLed so it never lingers as ``<defunct>``.

    The drain after a group kill can still time out when a process OUTSIDE the group holds the capture
    pipes; the child itself is already dead, but its ``Popen`` handle stays referenced by the
    ``TimeoutExpired`` traceback callers may keep, so nothing reaps it. ``wait()`` blocks on the PROCESS
    (dead already), never on the pipes, so it cannot wedge the way the drain can.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.wait(timeout=_POST_KILL_DRAIN_S)


def _run_cli_killing_process_group(cmd, code, env, timeout):
    """Run the CLI in its own process group and kill the whole group on timeout.

    ``subprocess.run`` only kills the direct child on ``TimeoutExpired``; a grandchild that
    inherited the stdout/stderr pipes (browser_harness daemon / Chrome helper) is orphaned
    still holding them, and on Windows ``run()``'s unbounded post-kill ``communicate()`` then
    blocks on pipe EOF forever — so the tool call, plus its activity heartbeat, wedges (#106244).
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, **_group_popen_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input=code, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_cli_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_POST_KILL_DRAIN_S)
        _reap_dead_child(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# Harness daemons this process drove: each latches BU_CDP_* at start and outlives every call, so a backend
# swap (/browser connect|disconnect) leaves later calls in the old browser until these are stopped.
_driven_daemons: dict[tuple[str, ...], str] = {}  # daemon identity -> home
_driven_daemons_lock = threading.Lock()
_live_generation = functools.partial(_lazy_call, "tools.browser_tool_cdp", "live_cdp_generation", 0,
                                     "live CDP generation lookup failed")  # this profile's publish count
# Route cache key -> whether its last browser_exec ran on the hybrid local sidecar.
_exec_route_is_sidecar: dict[str, bool] = {}
# Route cache key -> the note from its real-profile fallback; that route stays on the fallback browser.
_real_profile_fallback: dict[str, str] = {}
_real_profile_note_delivered: set = set()


def stop_harness_daemons(home_key: Optional[str] = None, claim_aware: bool = False) -> None:
    """Stop every harness daemon this process drove (only ``home_key``'s when given), through the
    harness's own identity-checked ``--reload``; the next browser_exec respawns one on the endpoint
    it resolves then.

    ``claim_aware`` (process exit): a daemon another LIVE Hermes process also claims (same dirs +
    BU_NAME, same profile) is left running and only OUR ownership record is dropped — the other owner
    wins, exactly as in ``stop_idle_harness_daemons``; its own idle/exit stop, or the orphan sweep once
    it dies, takes the daemon down. Without ``claim_aware`` (``/browser connect`` backend swap) the swap
    wins: the daemon latched the old endpoint, so it is stopped even under a peer's claim, or our next
    call would silently keep driving the old browser."""
    with _driven_daemons_lock:
        daemons = sorted((key, home) for key, home in _driven_daemons.items() if home_key in (None, home))
        for key, _home in daemons:
            _driven_daemons.pop(key, None)
            _daemon_last_used.pop(key, None)
            _daemon_cdp.pop(key, None)
    if not claim_aware:
        _reload_daemons([key for key, _home in daemons])
        return
    for key, home in daemons:
        token = set_hermes_home_override(home) if home else None
        try:
            if _other_live_owner(key):
                forget_harness_daemon_owner(key)
                continue
            _reload_daemons([key])
        finally:
            if token is not None:
                reset_hermes_home_override(token)


# --- harness daemon lifetime under a LIVE owner: idle stop ------------------------------------
#
# Lifetime contract. ``browser_harness.admin.ensure_daemon()`` (run by the CLI each browser_exec spawns)
# starts the daemon DETACHED (``start_new_session=True``) and the CLI then exits, so every daemon is
# ppid=1 from its first second: being reparented is normal, not the leak. The daemon has NO idle exit — it
# only stops on ``meta:shutdown`` (``--reload``). So Hermes, which caused the spawn, owns the stop:
#   * shutdown / ``/browser connect``: ``stop_harness_daemons`` (in-memory ``_driven_daemons``);
#   * owner died without that: ``reap_orphaned_harness_daemons`` (durable records, next process);
#   * owner ALIVE but done browsing: ``stop_idle_harness_daemons``, below, from the browser janitor.
# The third leg was missing. A long-lived gateway closed each Chromium session after
# ``browser.inactivity_timeout`` but never the harness daemon attached to it, so every BU_NAME the gateway
# ever used left one immortal daemon (holding a dead CDP socket) for the life of the gateway — measured:
# four ``browser_harness.daemon`` at ppid=1, hours idle, their spawning gateway still running.
_daemon_last_used: dict[tuple[str, ...], float] = {}  # identity -> monotonic end of its last call
_daemon_cdp: dict[tuple[str, ...], str] = {}  # identity -> BU_CDP_* endpoint its last call handed it
_daemon_calls_in_flight: dict[tuple[str, ...], int] = {}
_daemons_stopping: set = set()  # identities whose idle stop (--reload) is running right now
_driven_daemons_cond = threading.Condition(_driven_daemons_lock)


def _monotonic() -> float:  # the idle clock (a seam: tests advance it without touching time.monotonic)
    return time.monotonic()


@contextlib.contextmanager
def _track_daemon_call(daemon: tuple[str, ...], home: str, cdp: Optional[str] = None):
    """Mark ``daemon`` driven and busy for one CLI run; its idle clock restarts when the run ends.

    Waits out an idle stop already in flight for the same daemon, so a call never races the
    ``--reload`` that is tearing its daemon down (the call then spawns a fresh one). ``cdp`` is the
    BU_CDP_* endpoint this run hands the daemon: the idle stop probes it (``_cdp_endpoint_alive``)."""
    with _driven_daemons_cond:
        while daemon in _daemons_stopping:
            _driven_daemons_cond.wait(timeout=1.0)
        _driven_daemons[daemon] = home
        if cdp:
            _daemon_cdp[daemon] = cdp
        else:
            _daemon_cdp.pop(daemon, None)
        _daemon_calls_in_flight[daemon] = _daemon_calls_in_flight.get(daemon, 0) + 1
    try:
        yield
    finally:
        with _driven_daemons_cond:
            remaining = _daemon_calls_in_flight.get(daemon, 1) - 1
            if remaining > 0:
                _daemon_calls_in_flight[daemon] = remaining
            else:
                _daemon_calls_in_flight.pop(daemon, None)
            _daemon_last_used[daemon] = _monotonic()


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
_CDP_PROBE_TIMEOUT_S = 0.5


def _loopback_cdp(cdp: Optional[str]) -> Optional[tuple[str, int]]:
    """``(host, port)`` to probe for a LOOPBACK CDP endpoint, else None (none / remote / unparseable)."""
    if not cdp:
        return None
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(cdp)
        host, port = parts.hostname, parts.port or {"http": 80, "ws": 80, "https": 443, "wss": 443}.get(parts.scheme)
    except ValueError:
        return None
    if not host or not port or host.lower() not in _LOOPBACK_HOSTS:
        return None
    return ("127.0.0.1" if host == "0.0.0.0" else host), port


def _cdp_endpoint_alive(cdp: Optional[str]) -> Optional[bool]:
    """Whether something still listens on a LOOPBACK CDP endpoint (``http://``/``ws://`` host:port).

    True only when the connect succeeds; False only when the connect is REFUSED or RESET (nothing
    listens: the Chrome is gone); None when it cannot be told — no endpoint, a remote one (a cloud
    ``wss://``, never probed from the janitor), or the probe timed out / failed any other way. A timeout
    is NOT evidence of a dead Chrome: a loaded machine answers slowly, and treating that as dead would
    stop the daemon mid-browse. Callers may stop a daemon on the endpoint check only for an explicit False."""
    target = _loopback_cdp(cdp)
    if target is None:
        return None
    host, port = target
    import errno
    import socket
    try:
        with socket.create_connection((host, port),
                                      timeout=_CDP_PROBE_TIMEOUT_S):
            return True
    except (ConnectionRefusedError, ConnectionResetError):
        return False
    except OSError as e:  # socket.timeout is TimeoutError, an OSError: unknown, never "dead"
        if e.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
            return False
        return None


# Second, much longer ceiling for a daemon whose Chrome is ALIVE: without it a daemon nobody calls for
# days is immortal (the dead-endpoint gate never fires). Stops only the daemon (the automation bridge,
# via its own --reload); Chrome and its tabs are never touched, and the next call respawns a daemon.
DEFAULT_HARNESS_DAEMON_MAX_IDLE_S = 43200.0


def _harness_daemon_max_idle_s() -> float:
    """``browser.harness_daemon_max_idle_seconds`` (default 12 h; <= 0 disables the ceiling)."""
    def read() -> float:
        from hermes_cli.config import read_raw_config_readonly
        cfg = read_raw_config_readonly().get("browser", {})
        raw = cfg.get("harness_daemon_max_idle_seconds") if isinstance(cfg, dict) else None
        return DEFAULT_HARNESS_DAEMON_MAX_IDLE_S if raw is None else float(raw)
    return _quiet(read, DEFAULT_HARNESS_DAEMON_MAX_IDLE_S, "harness daemon max idle lookup failed")


def stop_idle_harness_daemons(idle_s: float, now: Optional[float] = None,
                              max_idle_s: Optional[float] = None) -> list:
    """Stop daemons this process drove whose Chrome is gone; returns them.

    The gate is the daemon's Chrome endpoint being DEAD (nothing listening on the loopback CDP port its
    last call handed it — the measured leak: daemons holding sockets to ports 62914/52806/... that no
    longer existed). ``idle_s`` (``browser.inactivity_timeout``) only avoids churn around a Chrome
    restart; it is NOT the stop signal: a daemon whose Chrome is still live is left alone however long
    the agent pauses. Only a daemon with no loopback endpoint to probe (a remote/cloud browser, or none
    recorded) falls back to ``idle_s`` alone, since nothing local can say whether it is still in use.
    A probe that cannot tell (timeout / other error: ``None`` for a loopback endpoint) never stops it.

    ``max_idle_s`` (``browser.harness_daemon_max_idle_seconds``, default 12 h; <= 0 off) is the ceiling
    for a daemon whose Chrome IS alive: past it the daemon (the automation bridge) is stopped too. Chrome
    itself is never stopped here.

    A daemon with a call in flight is never stopped (a browser_exec may run up to ``_MAX_TIMEOUT_S``).
    The probe runs outside the lock; the in-flight/idle predicate is then re-checked and the daemon fenced
    (``_daemons_stopping``) under the SAME lock ``_track_daemon_call`` takes, so a call can never slip
    between the check and the stop. Each stop runs the harness's own ``--reload`` under its owning
    profile's home, so that profile's ownership record is the one cleared."""
    now = _monotonic() if now is None else now
    max_idle_s = _harness_daemon_max_idle_s() if max_idle_s is None else max_idle_s

    def idle_for(key: tuple[str, ...], floor: float) -> bool:  # caller holds _driven_daemons_cond
        return (key in _driven_daemons and not _daemon_calls_in_flight.get(key)
                and key not in _daemons_stopping and now - _daemon_last_used.get(key, now) >= floor)

    def stoppable(cdp: Optional[str]) -> Optional[float]:
        """The idle floor past which a daemon on ``cdp`` may be stopped, or None (never, this tick)."""
        if not cdp or _loopback_cdp(cdp) is None:
            return idle_s  # nothing local to probe (remote/cloud or none recorded): time rule only
        alive = _cdp_endpoint_alive(cdp)
        if alive is False:
            return idle_s  # Chrome provably gone
        if alive is True and max_idle_s and max_idle_s > 0:
            return max(idle_s, max_idle_s)  # Chrome alive: only the long ceiling
        return None  # probe inconclusive (timeout/other error), or alive with the ceiling off

    with _driven_daemons_cond:
        candidates = [(key, _daemon_cdp.get(key)) for key in sorted(_driven_daemons) if idle_for(key, idle_s)]
    floors = {key: (cdp, floor) for key, cdp in candidates if (floor := stoppable(cdp)) is not None}
    with _driven_daemons_cond:
        idle = sorted((key, _driven_daemons[key]) for key, (cdp, floor) in floors.items()
                      if idle_for(key, floor) and _daemon_cdp.get(key) == cdp)
        for key, _home in idle:
            _driven_daemons.pop(key, None)
            _daemon_last_used.pop(key, None)
            _daemon_cdp.pop(key, None)
            _daemons_stopping.add(key)
    try:
        for key, home in idle:
            token = set_hermes_home_override(home) if home else None
            try:
                if _other_live_owner(key):
                    # Another live Hermes process drives the same daemon (same dirs + BU_NAME) and may be
                    # mid-call: drop only OUR claim. The last live owner's own idle stop takes it down, and
                    # if that owner dies first the orphan sweep does.
                    forget_harness_daemon_owner(key)
                    continue
                if _reload_daemons([key]):  # failed, not yet escalated: stay driven so the next tick retries
                    with _driven_daemons_cond:
                        if key not in _driven_daemons:
                            _driven_daemons[key] = home
                            _daemon_last_used.setdefault(key, now - idle_s)
            finally:
                if token is not None:
                    reset_hermes_home_override(token)
                with _driven_daemons_cond:  # release a caller waiting on THIS daemon as soon as it is down
                    _daemons_stopping.discard(key)
                    _driven_daemons_cond.notify_all()
    finally:  # an exception mid-loop must not leave later daemons fenced off forever
        with _driven_daemons_cond:
            for key, _home in idle:
                _daemons_stopping.discard(key)
            _driven_daemons_cond.notify_all()
    if idle:
        logger.info("Stopped %d idle browser-harness daemon(s)", len(idle))
    return [key for key, _home in idle]


# --- harness daemon ownership: durable records, orphan reaping --------------------------------
#
# A browser_harness daemon outlives the CLI that spawned it BY DESIGN: ensure_daemon() starts it with
# start_new_session=True (browser_harness._ipc.spawn_kwargs) and it has no idle exit, so the NEXT call
# reuses its warm CDP connection. Nothing in that contract ties the daemon's lifetime to the Hermes
# process that owns the session, and ``_driven_daemons`` above lives only in that process's memory. Any
# exit that skips stop_harness_daemons() — Ctrl-C, SIGKILL, a crashed or OOM-killed gateway, a one-shot
# ``hermes -p`` run, a subagent — leaves the daemon reparented to launchd/init (ppid=1) with no way for
# anyone to find it again: one immortal helper per BU_NAME that process ever used, plus its .sock/.pid.
#
# The record below is the durable half of that contract. Every daemon Hermes drives gets a per-profile
# record naming the Hermes PID that owns it — one file per (daemon identity, owner), written before the
# CLI runs, so two processes sharing one daemon (same dirs + BU_NAME) can never overwrite each other's
# claim. Any later Hermes process reaps a daemon once ALL its claims are provably dead, through the
# harness's own identity-checked ``--reload`` (which sends meta:shutdown, waits, cleans up the socket/pid
# and stops a billable cloud browser). "Provably dead" means the PID is gone, or its start-time
# fingerprint no longer matches (a recycled PID is not a live owner).
_HARNESS_RECORD_VERSION = 1
_HARNESS_RECORD_DIRNAME = "browser-harness"
# The sweep is throttled per process: it runs on the browser path, so it must not cost a subprocess
# (or a directory scan) on every browser_exec call. Each reaped daemon costs one short `--reload` child
# (and up to 15s if that daemon is wedged), hence the per-sweep cap: a big backlog drains over a few
# sweeps instead of stalling the one browser call that happens to run right after a crash.
HARNESS_ORPHAN_SWEEP_INTERVAL_S = 300.0
HARNESS_ORPHAN_SWEEP_MAX = 8
# An ownership record that cannot be parsed (torn by a crash mid-write on a filesystem without atomic
# rename, hand-edited, written by an incompatible version) fails SAFE while it is recent: its owner cannot
# be ruled out, so the exit and idle stops leave the daemon running. Without an end to that, one bad write
# would make its daemon permanently unstoppable. Records are rewritten on every call that drives the
# daemon, so a corrupt one untouched for this long belongs to no live call: it stops counting as an owner
# and the orphan sweep deletes it. A corrupt record is NEVER grounds to signal anything — once ignored,
# the daemon is judged on the records that do parse.
HARNESS_CORRUPT_RECORD_GRACE_S = 24 * 3600.0
_last_harness_orphan_sweep = 0.0
_harness_orphan_sweep_lock = threading.Lock()


def _process_start_time(pid: int) -> Optional[int]:
    """A stable per-process start-time fingerprint (PID-reuse guard), or None when unavailable."""
    def read() -> Optional[int]:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    return _quiet(read, None, "process start-time lookup failed")


def _harness_runtime_dir(env: dict) -> Path:
    """The dir the harness binds ``bu[-<name>].sock`` in for a CLI started with ``env``.

    Mirrors ``browser_harness._ipc`` as it resolves ``BH_RUNTIME_DIR`` at import: the caller's runtime
    dir, else the caller's tmp dir (legacy single-dir callers), else ``paths.runtime_dir()`` under the
    harness home."""
    raw = env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
    if home:
        return Path(home).expanduser().resolve() / "runtime"
    xdg = env.get("XDG_CONFIG_HOME")
    base = (Path(xdg).expanduser() / "browser-harness" if xdg
            else Path(env.get("HOME") or Path.home()) / ".config" / "browser-harness")
    return base.resolve() / "runtime"


def _harness_endpoint_stem(env: dict) -> str:
    """``bu`` when the runtime dir isolates one daemon, else ``bu-<BU_NAME>`` (``_ipc._runtime_stem``)."""
    name = env.get("BU_NAME") or "default"
    return "bu" if env.get("BH_RUNTIME_DIR") and env.get("BH_RUNTIME_DIR_SHARED") != "1" else f"bu-{name}"


def _harness_record_dir() -> Path:
    """``<HERMES_HOME>/run/browser-harness`` — per profile, so a multiplexed gateway's profiles never
    reap each other's daemons."""
    return Path(get_hermes_home()) / "run" / _HARNESS_RECORD_DIRNAME


def _harness_record_digest(identity: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(identity).encode()).hexdigest()[:16]


def _harness_record_path(identity: tuple[str, ...], pid: Optional[int] = None) -> Path:
    """The record ONE owning process writes for ``identity``: one file per (daemon, owner).

    A shared owners map would need read-modify-write, and two processes claiming the same daemon at the
    same time would lose one claim — after which a sweep could kill a daemon that is still in use. A file
    per owner needs no locking: it is written once and unlinked by whoever no longer owns it."""
    return _harness_record_dir() / f"{_harness_record_digest(identity)}-{os.getpid() if pid is None else pid}.json"


def _read_harness_record(path: Path) -> Optional[dict]:
    """The parsed record at ``path``, or None when missing/corrupt/foreign (never raises)."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != _HARNESS_RECORD_VERSION:
        return None
    identity = record.get("identity")
    if not isinstance(identity, list) or len(identity) != 5 or not all(isinstance(p, str) for p in identity):
        return None
    if not isinstance(record.get("owner_pid"), int):
        return None
    return record


def _unparseable_record_state(path: Path, now: Optional[float] = None) -> str:
    """For a record ``_read_harness_record`` could not parse: ``"gone"`` (unlinked mid-scan), ``"stale"``
    (untouched for ``HARNESS_CORRUPT_RECORD_GRACE_S``) or ``"recent"``. A stat that fails for any other
    reason is ``"recent"`` — fail safe, its owner cannot be ruled out."""
    try:
        mtime = Path(path).stat().st_mtime
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "recent"
    age = (time.time() if now is None else now) - mtime
    return "stale" if age >= HARNESS_CORRUPT_RECORD_GRACE_S else "recent"


def _prune_stale_corrupt_records() -> int:
    """Delete ownership records that cannot be parsed and are older than the grace; returns how many.

    Never touches a record that parses, and never signals a daemon: the record is only bookkeeping."""
    try:
        paths = sorted(_harness_record_dir().glob("*.json"))
    except OSError:
        return 0
    pruned = 0
    for path in paths:
        # Re-checked right before the unlink: a record rewritten since the scan parses (or is recent).
        if _read_harness_record(path) is not None or _unparseable_record_state(path) != "stale":
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.debug("could not delete stale corrupt harness record %s: %s", path, e)
            continue
        pruned += 1
        logger.info("Deleted corrupt browser-harness ownership record %s (unparseable, untouched for %.0fh)",
                    path, HARNESS_CORRUPT_RECORD_GRACE_S / 3600)
    return pruned


def _write_harness_record(path: Path, record: dict) -> None:
    """Atomically replace ``path`` with ``record`` (a torn read would look like a dead owner)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _owner_alive(pid: int, started: Optional[int]) -> bool:
    """Whether the recorded owner ``pid`` is still that same process (fail-safe: unreadable = alive)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    from gateway.status import _pid_exists  # cross-platform, and a zombie counts as dead
    if not _pid_exists(pid):
        return False
    if started is None:
        return True
    current = _process_start_time(pid)
    return current is None or int(current) == int(started)


def record_harness_daemon_owner(identity: tuple[str, ...], env: dict) -> None:
    """Record this process as an owner of the daemon ``identity`` names, before the CLI drives it.

    Called before the CLI runs on purpose: the daemon may already exist (a warm one this call reuses),
    and if this process dies mid-call the record is the only way a later process can find the daemon."""
    try:
        _write_harness_record(_harness_record_path(identity), {
            "version": _HARNESS_RECORD_VERSION,
            "identity": list(identity),
            "runtime_dir": str(_harness_runtime_dir(env)),
            "stem": _harness_endpoint_stem(env),
            "owner_pid": os.getpid(),
            "owner_started": _process_start_time(os.getpid()),
            "updated": time.time(),
        })
    except OSError as e:  # a record is cleanup bookkeeping, never a precondition for browsing
        logger.debug("could not record harness daemon ownership for %r: %s", identity, e)


def forget_harness_daemon_owner(identity: tuple[str, ...]) -> None:
    """Drop this process's claim on ``identity``'s daemon (the daemon itself is stopped by the caller)."""
    try:
        _harness_record_path(identity).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("could not clear harness daemon ownership for %r: %s", identity, e)


def _other_live_owner(identity: tuple[str, ...]) -> bool:
    """Whether a live process OTHER than this one holds an ownership record for ``identity``'s daemon.

    Fails SAFE (True = leave the daemon running): the record dir cannot be listed, or a peer's record
    still exists but cannot be read/parsed, so its owner cannot be ruled out. Callers then drop only OUR
    claim; the orphan sweep never signals on a guess. The escape: an unparseable record untouched for
    ``HARNESS_CORRUPT_RECORD_GRACE_S`` is ignored here (and pruned by the sweep), so one bad write cannot
    make a daemon unstoppable forever."""
    me = _harness_record_path(identity).name
    try:
        paths = list(_harness_record_dir().glob(f"{_harness_record_digest(identity)}-*.json"))
    except OSError as e:
        logger.debug("harness ownership records unreadable for %r (%s): assuming another owner", identity, e)
        return True
    for path in paths:
        if path.name == me:
            continue
        record = _read_harness_record(path)
        if record is None:
            state = _unparseable_record_state(path)
            if state == "recent":  # unreadable/corrupt/foreign, not merely unlinked mid-scan
                logger.debug("harness ownership record %s unreadable: assuming another owner", path)
                return True
            if state == "stale":
                logger.debug("harness ownership record %s unreadable and past the grace: ignored", path)
            continue
        if tuple(record["identity"]) == tuple(identity) and record["owner_pid"] != os.getpid() \
                and _owner_alive(record["owner_pid"], record.get("owner_started")):
            return True
    return False


def _harness_endpoint_present(record: dict) -> bool:
    """Whether any of the daemon's endpoint files still exist (unknown record fields = assume present)."""
    runtime, stem = str(record.get("runtime_dir") or ""), str(record.get("stem") or "")
    if not runtime or not stem:
        return True
    return any((Path(runtime) / f"{stem}{suffix}").exists() for suffix in (".sock", ".port", ".pid"))


def orphaned_harness_daemons(exclude=()) -> list[dict]:
    """Daemons whose every owning Hermes process is provably gone (never the ``exclude`` identities).

    One entry per daemon identity: ``{"identity", "records", "paths", "record"}``, where the record list
    holds one claim per owning process."""
    exempt = set(exclude)
    try:
        paths = sorted(_harness_record_dir().glob("*.json"))
    except OSError:
        return []
    claims: dict[tuple[str, ...], list[tuple[Path, dict]]] = {}
    for path in paths:
        record = _read_harness_record(path)
        if record is None:
            continue  # corrupt/foreign: never evidence to reap (a stale one is pruned, never acted on)
        identity = tuple(record["identity"])
        if identity in exempt:
            continue
        claims.setdefault(identity, []).append((path, record))
    orphans = []
    for identity, group in claims.items():
        if any(_owner_alive(record["owner_pid"], record.get("owner_started")) for _, record in group):
            continue  # somebody alive still owns it
        orphans.append({"identity": identity, "record": group[0][1],
                        "records": [record for _, record in group], "paths": [path for path, _ in group]})
    return orphans


def _legacy_harness_daemons(claimed: set[tuple[str, ...]], env: dict) -> list[dict]:
    """Daemons in ``env``'s harness runtime dir that no ownership record claims.

    Daemons leaked BEFORE this fix have no record at all, so the claim sweep can never see them. Adopt
    them the way the agent-browser reaper treats an untracked session: only once nothing has touched their
    endpoint files for ``BROWSER_ORPHAN_GRACE_SECONDS`` (a live pre-fix Hermes process therefore loses at
    most a warm daemon — the browser and its tabs live in Chromium, and the next call respawns one). Only
    this env's own runtime dir is scanned, so a profile never reaches into another profile's daemons.
    """
    runtime = _harness_runtime_dir(env)
    if not (env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR")):
        # Without a dir of our own the harness uses the machine-wide ``~/.config/browser-harness/runtime``,
        # shared with every other Hermes process AND with a user's own plain ``browser-harness`` runs.
        # Nothing there proves which process a daemon belongs to, so a single profile must not adopt it.
        return []
    try:
        from tools.browser_tool import BROWSER_ORPHAN_GRACE_SECONDS as grace
    except Exception:  # pragma: no cover — a stripped-down caller still gets a sane floor
        grace = 3600.0
    now = time.time()
    name = env.get("BU_NAME") or "default"
    private = bool(env.get("BH_RUNTIME_DIR")) and env.get("BH_RUNTIME_DIR_SHARED") != "1"
    found, seen = [], set()
    for path in sorted(runtime.glob("bu*")):
        stem, _, suffix = path.name.partition(".")
        if suffix not in ("sock", "port", "pid") or stem in seen:
            continue
        seen.add(stem)
        if private:  # the runtime dir isolates one daemon: stem is a bare "bu"
            daemon_name = name
        elif stem.startswith("bu-"):
            daemon_name = stem[len("bu-"):]
        else:
            continue
        identity = (env.get("BH_RUNTIME_DIR", ""), env.get("BH_RUNTIME_DIR_SHARED", ""),
                    env.get("BH_TMP_DIR", ""), env.get("BH_TMP_DIR_SHARED", ""), daemon_name)
        if identity in claimed:
            continue
        pid_file = runtime / f"{stem}.pid"
        try:
            touched = max(path.stat().st_mtime, pid_file.stat().st_mtime if pid_file.exists() else 0.0)
        except OSError:
            continue  # vanished mid-scan (or unreadable): next sweep
        if now - touched < grace:
            continue
        found.append({"identity": identity, "record": {"runtime_dir": str(runtime), "stem": stem},
                      "records": [{"runtime_dir": str(runtime), "stem": stem}], "paths": []})
    return found


def reap_orphaned_harness_daemons(exclude=(), force: bool = False) -> int:
    """Stop harness daemons left behind by dead Hermes processes; returns how many were reaped.

    Runs on the browser path (throttled to ``HARNESS_ORPHAN_SWEEP_INTERVAL_S`` per process) so the leak
    is drained by the very code that creates it: the first browser call after a crash reaps that crash's
    daemons. ``exclude`` holds the identity this call is about to drive — reaping it would turn a warm
    daemon into a cold spawn for no reason."""
    global _last_harness_orphan_sweep
    now = time.monotonic()
    with _harness_orphan_sweep_lock:
        if not force and now - _last_harness_orphan_sweep < HARNESS_ORPHAN_SWEEP_INTERVAL_S:
            return 0
        _last_harness_orphan_sweep = now
    _prune_stale_corrupt_records()  # bookkeeping only: a long-corrupt record must not linger as a blocker
    orphans = orphaned_harness_daemons(exclude=exclude)
    try:
        env = _base_subprocess_env()
        claimed = {tuple(record["identity"]) for record in
                   (_read_harness_record(p) for p in _harness_record_dir().glob("*.json")) if record}
        # The daemon this call is about to drive is exempt by stem too: in a private runtime dir every
        # daemon is the bare "bu" stem, so an identity built from a different BU_NAME would not match.
        own_stem = _harness_endpoint_stem(env)
        orphans += [o for o in _legacy_harness_daemons(claimed, env)
                    if o["record"]["stem"] != own_stem
                    and o["identity"] not in set(exclude)
                    and o["identity"] not in {x["identity"] for x in orphans}]
    except Exception as e:  # pragma: no cover — the claim sweep above must survive any legacy-scan failure
        logger.debug("legacy harness daemon scan failed: %s", e)
    if not orphans or _find_cli() is None:
        return 0  # nothing to reap, or nothing to stop them with: leave the records for a run that can
    reaped = 0
    for orphan in orphans[:HARNESS_ORPHAN_SWEEP_MAX]:
        if any(_harness_endpoint_present(record) for record in orphan["records"]):
            _reload_daemons([orphan["identity"]])  # the harness's own identity-checked stop + cleanup
            if any(_harness_endpoint_present(record) for record in orphan["records"]):
                continue  # still there (slow/wedged): keep the records and retry on the next sweep
        cleared = True
        for path in orphan["paths"]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.debug("could not clear stale harness daemon record %s: %s", path, e)
                cleared = False
        reaped += 1 if cleared else 0
    if reaped:
        logger.info("Cleared %d orphaned browser-harness daemon(s) from previous runs", reaped)
    return reaped


def _reap_orphans_before_driving(daemon: tuple[str, ...]) -> None:
    """Best-effort drain of dead owners' daemons; cleanup must never break the browser call itself."""
    try:
        reap_orphaned_harness_daemons(exclude=(daemon,))
    except Exception as e:  # pragma: no cover — defensive: a sweep failure is never fatal
        logger.debug("harness orphan reap failed: %s", e)


# Bounded escalation for a wedged daemon. ``--reload`` asks the daemon to shut itself down; one that never
# answers used to keep its record and be retried on every sweep forever. After this many consecutive
# failed reloads the daemon is signalled directly — but only once its pid is PROVEN to be that daemon.
_RELOAD_FAILURES_BEFORE_KILL = 2
_RELOAD_TIMEOUT_S = 15
_KILL_GRACE_S = 5.0
# identity -> (consecutive failed reloads, (pid, start-time) fingerprint seen at the first failure)
_reload_failures: dict[tuple[str, ...], tuple[int, Optional[tuple[int, Optional[int]]]]] = {}
_reload_failures_lock = threading.Lock()


_POST_KILL_SOCKET_WAIT_S = 1.0


def _wait_socket_released(sock: Path, budget_s: float) -> bool:
    """Poll up to ``budget_s`` until nothing accepts on the AF_UNIX ``sock`` (or it is gone); True once
    released. Bounded: a socket still held past the budget is cleaned up anyway (the pid is proven dead)."""
    import socket
    deadline = time.monotonic() + budget_s
    while True:
        if not sock.exists() or not hasattr(socket, "AF_UNIX"):
            return True
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.05)
        try:
            probe.connect(str(sock))
        except OSError:
            return True  # refused / no listener: the dead process no longer holds it
        finally:
            probe.close()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _signal_pid(pid: int, sig: int) -> None:  # a seam: tests observe which signals were sent
    os.kill(pid, sig)


def _pid_gone(pid: int) -> bool:
    from gateway.status import _pid_exists  # a zombie counts as dead
    return not _pid_exists(pid)


def _daemon_endpoint_paths(env: dict, daemon: tuple[str, ...]) -> tuple[Path, str]:
    denv = daemon_env(env, daemon)
    return _harness_runtime_dir(denv), _harness_endpoint_stem(denv)


def _daemon_fingerprint(env: dict, daemon: tuple[str, ...]) -> Optional[tuple[int, Optional[int]]]:
    """``(pid, start-time)`` of the process the daemon's own pid file names, or None if there is none."""
    runtime, stem = _daemon_endpoint_paths(env, daemon)
    try:
        pid = int((runtime / f"{stem}.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid() or _pid_gone(pid):
        return None
    return pid, _process_start_time(pid)


def _kill_wedged_daemon(env: dict, daemon: tuple[str, ...], seen: Optional[tuple[int, Optional[int]]]) -> None:
    """SIGTERM, then SIGKILL, the daemon whose ``--reload`` keeps failing — only when identity is proven.

    Proof: the daemon's pid file names the same pid it named at the first failed reload, and that pid's
    start-time fingerprint (the PID-reuse guard the ownership records use) is unchanged and known. Any
    mismatch — pid file gone or rewritten, pid recycled, fingerprint unreadable — drops the record and
    signals NOTHING."""
    current = _daemon_fingerprint(env, daemon)
    if seen is None or current is None or current != seen or seen[1] is None:
        logger.warning("browser-harness daemon %r: --reload failed %d times but its pid cannot be proven to "
                       "be that daemon (first seen %r, now %r); dropping its record, signalling nothing",
                       daemon, _RELOAD_FAILURES_BEFORE_KILL, seen, current)
        forget_harness_daemon_owner(daemon)
        return
    pid = current[0]
    logger.warning("browser-harness daemon %r (pid %d) ignored %d --reload attempts; terminating it",
                   daemon, pid, _RELOAD_FAILURES_BEFORE_KILL)
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            _signal_pid(pid, sig)
        except ProcessLookupError:
            break
        except OSError as e:
            logger.warning("could not signal browser-harness daemon pid %d: %s", pid, e)
            return  # keep the record: nothing was proven stopped
        deadline = time.monotonic() + _KILL_GRACE_S
        while time.monotonic() < deadline and not _pid_gone(pid):
            time.sleep(0.05)
        if _pid_gone(pid):
            break
    if not _pid_gone(pid):
        logger.warning("browser-harness daemon pid %d survived SIGKILL; keeping its record", pid)
        return
    runtime, stem = _daemon_endpoint_paths(env, daemon)
    # The pid is gone, but on Darwin the kernel can finish tearing down a just-killed process's sockets a
    # moment later; unlinking while it still holds the listener lets a fresh daemon bind a path whose old
    # socket is mid-close. Poll (bounded) until nothing accepts on it any more, then clean up.
    _wait_socket_released(runtime / f"{stem}.sock", _POST_KILL_SOCKET_WAIT_S)
    for suffix in (".sock", ".port", ".pid"):  # what --reload would have cleaned up
        try:
            (runtime / f"{stem}{suffix}").unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug("could not remove killed daemon endpoint %s%s: %s", stem, suffix, e)
    forget_harness_daemon_owner(daemon)


def _reload_daemons(daemons) -> list:  # each ``daemon_identity`` daemon, with its own dirs
    """``--reload`` each daemon; returns those whose reload failed and that were NOT escalated (retry).

    A successful reload clears the record and resets the daemon's failure count. The
    ``_RELOAD_FAILURES_BEFORE_KILL``-th consecutive failure escalates (``_kill_wedged_daemon``)."""
    cmd = _find_cli() if daemons else None
    if not cmd:
        return []
    env = _base_subprocess_env()
    pending = []
    for daemon in daemons:
        try:
            _run_cli_killing_process_group([*cmd, "--reload"], "", daemon_env(env, daemon), _RELOAD_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:  # TimeoutExpired included: a wedged daemon
            logger.debug("browser-harness --reload failed for %r: %s", daemon, e)
            with _reload_failures_lock:
                count, seen = _reload_failures.get(daemon, (0, None))
                count += 1
                if count == 1:
                    seen = _daemon_fingerprint(env, daemon)
                if count < _RELOAD_FAILURES_BEFORE_KILL:
                    _reload_failures[daemon] = (count, seen)
                else:
                    _reload_failures.pop(daemon, None)
            if count < _RELOAD_FAILURES_BEFORE_KILL:
                pending.append(daemon)  # keeps its record: the next tick/sweep retries it
            else:
                _kill_wedged_daemon(env, daemon, seen)
            continue
        with _reload_failures_lock:
            _reload_failures.pop(daemon, None)
        forget_harness_daemon_owner(daemon)
    return pending



def _route_exec(env: dict, session: str, task_id: Optional[str], local: bool, local_sidecar: bool,
                route_key: str) -> tuple[Optional[str], bool]:
    """``_route_backend`` for one browser_exec call; returns ``(error, runs on the local sidecar)``.
    A real-profile fallback is remembered per route: the route keeps its fresh browser and the note
    waits until a result carries it (``_take_real_profile_note``)."""
    route_err = _route_backend(env, session, task_id, local, force_local_sidecar=local_sidecar,
                               real_profile=route_key not in _real_profile_fallback)
    note = env.pop(_REAL_PROFILE_FALLBACK_SENTINEL, None)
    if note:
        _real_profile_fallback.setdefault(route_key, note)
    return route_err, local_sidecar or bool(env.pop(_LOCAL_FALLBACK_SENTINEL, None))


def _route_exec_current(env: dict, session: str, task_id: Optional[str], local: bool,
                        local_sidecar: bool, route_key: str) -> tuple[dict, Optional[str], bool, int]:
    """``_route_exec`` on a copy of ``env`` -> ``(routed env, error, sidecar, generation)``, re-routed (at
    most 3 tries) when /browser connect published meanwhile; ``_reload_if_republished`` covers dispatch."""
    for _attempt in range(3):
        routed = dict(env)
        generation = _live_generation()
        route_err, sidecar = _route_exec(routed, session, task_id, local, local_sidecar, route_key)
        if route_err or _live_generation() == generation:
            break
    return routed, route_err, sidecar, generation


def _reload_if_republished(env: dict, generation: int) -> None:
    """Stop this call's daemon if the profile published a new endpoint while it spawned on the old one."""
    if _live_generation() == generation:
        return
    daemon = daemon_identity(env)
    with _driven_daemons_lock:
        _driven_daemons.pop(daemon, None)
        _daemon_cdp.pop(daemon, None)
    _reload_daemons([daemon])


def _take_real_profile_note(route_key: str) -> dict:
    """The fallback note for the first result this route returns after falling back, then nothing."""
    if route_key not in _real_profile_fallback or route_key in _real_profile_note_delivered:
        return {}
    _real_profile_note_delivered.add(route_key)
    return {"real_profile_unavailable": _real_profile_fallback[route_key]}


def browser_exec(code: str, session: str = "", timeout_s: int = _DEFAULT_TIMEOUT_S,
                 task_id: Optional[str] = None, local: bool = False):
    """Run Python code through the browser-use CLI, and return its output"""
    from agent.redact import redact_sensitive_text
    from tools.registry import tool_error, tool_result
    if not code or not code.strip():
        return tool_error("No code provided. Pass Python that uses the pre-imported helpers, e.g. new_tab(\"https://example.com\") then print(page_info()).")

    # The verdict is carried into the route, never re-derived: the predicate resolves DNS, so a second
    # evaluation could relax the floor and still pick the cloud for a rebinding host.
    verdict = _code_url_verdict(code)
    if verdict.blocked:
        return tool_error(verdict.blocked)
    # One endpoint per call: any private URL sends the whole call to the local sidecar. A call with no URL
    # literal stays on the route the previous call took (browser_navigate's _last_session_key twin).
    route_key = _backend_cache_key(task_id, session)
    local_sidecar = verdict.local_sidecar if _URL_RE.search(code) else _exec_route_is_sidecar.get(route_key, False)

    cmd = _find_cli()
    if not cmd:
        return tool_error("browser-harness is missing from Hermes's Python environment. "
                          "Run `hermes update` to re-sync it.")

    env = _base_subprocess_env()
    if session:
        if not _SESSION_RE.match(session):
            return tool_error(f"Invalid session name {session!r}: use 1-64 letters, digits, "
                              "dashes, or underscores (e.g. 'r7k2').")
        env["BU_NAME"] = session
    env, route_err, local_sidecar, generation = _route_exec_current(
        env, session, task_id, bool(local), local_sidecar, route_key)
    if route_err:
        return tool_error(route_err)
    if local_sidecar:
        env["BU_NAME"] = _sidecar_daemon_name(session)
    _exec_route_is_sidecar[route_key] = local_sidecar
    bot_desktop_browser = bool(env.pop(_BOT_DESKTOP_BROWSER_SENTINEL, None))

    # SHARED browser (/browser connect CDP override): pin each named session to its own tab (see
    # _OWN_TAB_PREAMBLE). Private per-name browsers skip this — nothing to collide with.
    private_browser = env.pop(_PRIVATE_BROWSER_SENTINEL, None)  # always pop: never exported to the CLI
    if session and not private_browser:
        code = _OWN_TAB_PREAMBLE + code

    workspace = _workspace_dir(task_id)
    if workspace:
        env["BH_AGENT_WORKSPACE"] = workspace

    # BU_AUTOSPAWN makes the CLI start a Browser Use cloud browser when no local
    # Chrome/CDP endpoint is reachable (their API key authenticates it)
    if "BU_AUTOSPAWN" not in env and is_legacy_browser_use_cloud_config(_read_browser_cfg()):
        env["BU_AUTOSPAWN"] = "1"

    timeout = _clamp_timeout(timeout_s)
    started = time.time()
    daemon = daemon_identity(env)
    # Before this call spawns anything: drain daemons whose owning Hermes process died without stopping
    # them (a crash, SIGKILL, a one-shot run). Outside the bot-desktop fence, and never this call's own
    # daemon — the point is to stop the leak, not to hand this call a cold daemon.
    _reap_orphans_before_driving(daemon)

    def dispatch() -> dict[str, Any]:
        _attach_vault_supervisor(env, task_id)
        # The janitor is what stops this call's daemon once it goes idle; only the managed-Chromium route
        # used to start it (via _get_session_info), so a CDP-override / BU-cloud exec had no idle stop.
        _lazy_call("tools.browser_tool_lifecycle", "_start_browser_cleanup_thread", None,
                   "browser janitor start failed")
        # Durable ownership: in-process bookkeeping dies with this process, the record does not.
        record_harness_daemon_owner(daemon, env)
        try:
            # Driven + busy for the run; its idle clock (stop_idle_harness_daemons) restarts at the end.
            with _track_daemon_call(daemon, hermes_home_key(), env.get("BU_CDP_WS") or env.get("BU_CDP_URL")):
                return {"proc": _run_cli_killing_process_group(cmd, code, env, timeout)}
        except subprocess.TimeoutExpired:
            return {"error_result": tool_error(
                f"browser-use exec timed out after {timeout}s. The daemon may still be working; retry "
                f"with a larger timeout_s (max {_MAX_TIMEOUT_S}), or split the work into several calls that "
                "append to workspace files — anything already written to the workspace is preserved."
            )}
        except OSError as e:
            return {"error_result": tool_error(f"Failed to launch browser-use CLI: {e}")}

    if bot_desktop_browser:
        from tools.browser_tool_session import run_fenced
        dispatched = run_fenced({"features": {"local": True}}, dispatch)
    else:
        dispatched = dispatch()
    _reload_if_republished(env, generation)
    if "proc" not in dispatched:
        if "error_result" in dispatched:
            return dispatched["error_result"]
        return tool_result(dispatched)
    proc = dispatched["proc"]

    # browser_vault fill registers injected values with this forced model-egress
    # boundary. Preserve raw stdout only for screenshot-path detection below.
    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": redact_sensitive_text(proc.stdout, force=True),
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    result.update(_take_real_profile_note(route_key))
    stderr = redact_sensitive_text((proc.stderr or "").strip(), force=True)
    if len(stderr) > _STDERR_CAP_CHARS:
        stderr = stderr[:_STDERR_CAP_CHARS] + "\n… (stderr truncated)"
    if stderr:
        result["stderr"] = stderr
    screenshot = _find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = _native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)


_HEADER_BASE = (
    "Drive a real web browser via the Browser Use CLI: `code` runs as full Python (stdlib available) "
    "with pre-imported browser helpers; stdout comes back in the result. Start `code` with a one-line "
    "comment describing the step for the user in plain language, max 60 chars "
    "(e.g. `# Searching Amazon for paper towels`) — the UI shows it as the step label.\n\n"
    "STATE: the browser session and workspace persist across calls; Python variables do NOT (fresh "
    "interpreter each call). The workspace dir is $BH_AGENT_WORKSPACE (also `workspace` in every result); "
    "functions defined in agent_helpers.py there are auto-imported into every call. For multi-item tasks "
    "('all N products / every entry'), append each batch to a JSON/CSV file in the workspace, then read it "
    "back and aggregate in code — dedupe/count/sort with Python, not in your head — and verify the "
    "collected count against what was asked before answering.\n\n"
    "Batch each sub-procedure (navigate, wait, extract, act) into one call — do not spend a call per "
    "action — but for long extractions prefer several medium calls that append to workspace files over "
    "one giant call, so progress survives timeouts."
)

_HEADER_VISION = (
    " Screenshots are attached to your context automatically: when the exec output contains a "
    "capture_screenshot() path, the image arrives with this tool's result and you inspect it directly "
    "with your own vision — never send browser screenshots to a separate vision tool."
)

_HEADER_TEXT_ONLY = (
    " Your model cannot view images, so work text-first: page_info() for state, js() for "
    "reading/extracting DOM text, fill_input(selector, text) for inputs, and "
    "js(\"document.querySelector('…').click()\") for clicks — skip the screenshot-driven workflow described below."
)

# Appended when the local engine is Lightpanda: no graphical renderer, and one CDP
# connection holds one page — a second Target.createTarget fails with
# TargetAlreadyLoaded (drop the new_tab() sentence once lightpanda-io/browser#1962 lands).
_HEADER_LIGHTPANDA = (
    " The local engine is Lightpanda (no graphical renderer, one page per session): capture_screenshot() "
    "is unavailable, so work text-first; navigate with new_tab(url) exactly once, then goto_url(url) for "
    "every later navigation — a second new_tab() fails with TargetAlreadyLoaded."
)

# Pinned quick-reference for the CLI's pre-imported helpers, replacing the live
# ``browser-use skill`` fetch (uncontrolled third-party text in every schema: version
# drift, supply-chain exposure, byte-unstable prompt). A/B benchmarked ~equal.
_HELPERS_DIGEST = (
    "\n\nHELPERS (pre-imported): new_tab(url) opens/navigates (use for the FIRST navigation), goto_url(url) "
    "navigates the current tab, wait_for_load() after navigation, page_info() summarizes the current page "
    "state, js(expr) evaluates a JS expression and returns its value (js('document.title'); wrap function "
    "bodies as js('(() => {...})()') — a bare '() => {...}' returns the function itself, uncalled), "
    "fill_input(selector, text) types into inputs, click_at_xy(x, y) clicks viewport coordinates, "
    "capture_screenshot() saves and prints a screenshot path, cdp('Domain.method', **kwargs) is raw CDP — "
    "cdp('Accessibility.getFullAXTree')['nodes'] lists every element's role/name/backendDOMNodeId (filter "
    "in Python before printing; it is thousands of nodes), then cdp('DOM.getBoxModel', backendNodeId=n) "
    "gives click coordinates. ensure_real_tab() recovers from a stale/internal tab. Login walls: never guess "
    "credentials; see the vault note below if present, otherwise stop and ask the user."
)


def _description_header() -> str:
    """Header tailored to whether the active model can see images natively"""
    if _lazy_call("tools.browser_tool_lightpanda_fallback", "lightpanda_engine_status", (False, ""),
                  "lightpanda engine status unavailable")[0]:  # no screenshots, whatever the model sees
        return _HEADER_BASE + _HEADER_TEXT_ONLY + _HEADER_LIGHTPANDA
    vision = _lazy_call("tools.vision_tools", "_should_use_native_vision_fast_path", False, "")
    return _HEADER_BASE + (_HEADER_VISION if vision else _HEADER_TEXT_ONLY)


def _dynamic_schema_overrides() -> dict:
    overrides: dict = {"description": _description_header() + _HELPERS_DIGEST}
    # ``local`` exists ONLY when the user consented to real-profile browsing — everyone
    # else's schema carries zero extra surface. The caller memoizes on config.yaml mtime,
    # so toggling consent applies next session, not mid-chat.
    if _real_profile_consented():
        props = dict(BROWSER_EXEC_SCHEMA["parameters"]["properties"])
        props["local"] = {
            "type": "boolean", "default": False,
            "description": ("Drive the user's own local browser (a Hermes-managed copy of their real "
                            "default-Chromium profile, logins/cookies included) instead of the configured "
                            "cloud browser backend. Use when the user asks to act as themselves — their "
                            "accounts, their sessions. No-op when the backend is already local. Default false."),
        }
        overrides["parameters"] = {**BROWSER_EXEC_SCHEMA["parameters"], "properties": props}
    return overrides


BROWSER_EXEC_SCHEMA = {
    "name": "browser_exec",
    # Static fallback description, used only when the managed CLI is unavailable
    "description": (_HEADER_BASE + _HELPERS_DIGEST
                    + "\n\n(The browser-use CLI is not installed yet. Install it with `hermes tools` (Browser Automation → Browser Use).)"),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute using the pre-imported browser helpers. Use print(...) for any data you need back."},
            "session": {"type": "string", "description": "Named isolated browser session — its own daemon and (on cloud backends) own browser, so concurrent tasks don't share tabs. Reuse the same name on every related call; omit for the shared default session."},
            "timeout_s": {"type": "integer", "default": _DEFAULT_TIMEOUT_S,
                          "description": f"Max seconds to wait for the code to finish (default {_DEFAULT_TIMEOUT_S}, max {_MAX_TIMEOUT_S})."},
        },
        "required": ["code"],
    },
}


# browser_exec is additionally gated at tool-definition time — sessions whose toolsets
# lack ``terminal`` never see it (model_tools._compute_tool_definitions). check_fn only
# answers "is Browser Use mode configured"; surface policy lives with the session.
from tools.registry import registry

registry.register(
    name="browser_exec",
    toolset="browser-use",
    schema=BROWSER_EXEC_SCHEMA,
    handler=lambda args, **kw: browser_exec(
        code=args.get("code", ""), session=args.get("session", "") or "",
        timeout_s=args.get("timeout_s", _DEFAULT_TIMEOUT_S), task_id=kw.get("task_id"),
        local=bool(args.get("local", False)),
    ),
    check_fn=is_browser_use_cli_mode,
    dynamic_schema_overrides=_dynamic_schema_overrides,
    emoji="🌐",
)
