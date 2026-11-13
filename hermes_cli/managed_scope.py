"""Managed scope — IT-pushed, user-immutable config & env layer.

DISTINCT from ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED`` (a coarse package-manager
write-lock that blocks all mutation); this layer injects specific immutable values. The two are
independent and may coexist. v1 enforcement is filesystem permissions only (see
``docs/design/managed-scope.md`` §7); ``_resolve_managed_dirs()`` is the single seam for adding
macOS / Windows native locations later.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional


# Stale-module bridge: this module binds ``utils`` names at import time, so a fresh
# import in a post-pull updater process (pre-handoff purge keeps root modules cached) dies
# unless the stale ``utils`` is dropped first. See hermes_cli.stale_modules.
from hermes_cli.stale_modules import drop_stale_root_modules

drop_stale_root_modules()

from utils import env_var_enabled, fast_safe_load, file_cache_version, file_signature  # file_signature: bound for the bridge above

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")

# Opt-in fail-closed switch (see ``strict_mode``). Like HERMES_MANAGED_DIR it is a deployment knob,
# set by whoever owns the managed files; it is never persisted to any .env.
STRICT_ENV = "HERMES_MANAGED_STRICT"

# What this tree's managed scope enforces, declared as a LITERAL tuple of strings so a deployment
# launcher can read it with ``ast.literal_eval`` straight from this file's source, without importing
# hermes. A tree that predates a behaviour lacks its name, so a launcher whose policy depends on it
# can refuse to start instead of silently enforcing less than it claims. Add a name when a managed
# behaviour is added; never rename or reuse one.
MANAGED_SCOPE_CAPABILITIES = (
    "strict-mode",  # HERMES_MANAGED_STRICT=1 fails closed on a broken managed scope
    "layered-managed-dirs",  # HERMES_MANAGED_DIR layers under /etc/hermes, never replaces it
    "save-config-keeps-user-values",  # save_config does not strip the user's own values for managed keys
    "raw-config-readers-honor-overlay",  # gates that read the raw user config apply the overlay
    "blocked-features-bare-and-platform-names",  # 'telegram' and 'platform.telegram' match each other
    "plugins-disabled-globs",  # plugins.disabled entries may be fnmatch patterns
    "plugins-user-dir-allowlist",  # plugins.allow_user_dir / user_dir_allowlist
    "plugins-toolsets-opt-in",  # plugins.toolsets_opt_in
)

_CACHE_LOCK = threading.Lock()
# path_key -> (*file_cache_version, parsed)
_CONFIG_CACHE: dict[str, tuple] = {}
_ENV_CACHE: dict[str, tuple] = {}
# Strict mode: the managed dirs this process last resolved in full, and the breakages already reported.
_STRICT_LAST_DIRS: list[Path] = []
_STRICT_REPORTED: set[str] = set()


class ManagedScopeError(SystemExit):
    """``HERMES_MANAGED_STRICT=1`` and the managed scope cannot be applied in full.

    A ``SystemExit`` on purpose: the managed loaders' callers are fail-open (``except Exception``)
    so a malformed managed file can never brick a NON-strict start, and none of them may swallow
    a strict refusal. Uncaught, it prints its message to stderr and exits with status 1.
    """


def strict_mode() -> bool:
    """``HERMES_MANAGED_STRICT`` is truthy: never run on a partially applied managed scope.

    Startup refuses (``ManagedScopeError``) when ``HERMES_MANAGED_DIR`` is set but is not a
    directory, when a managed directory's ``config.yaml`` is missing, unreadable, unparseable or not
    a mapping (an empty file included; write ``{}`` for an intentionally empty policy), or when a
    managed ``.env`` that exists cannot be read. With no managed scope configured at all (variable
    unset, ``/etc/hermes`` absent) there is nothing to enforce and nothing refuses. A breakage
    that appears after this process loaded the scope keeps the last good policy in force (logged at
    CRITICAL) rather than dropping it or killing whichever thread read it next.
    """
    return env_var_enabled(STRICT_ENV)


def _strict_refusal(reason: str) -> ManagedScopeError:
    message = (f"hermes: refusing to run: {reason}. {STRICT_ENV}=1 requires the administrator's "
               "managed scope to load in full.")
    if threading.current_thread() is not threading.main_thread():
        # Uncaught, the main thread prints a SystemExit's message; any other thread drops it silently.
        logger.critical("%s", message)
    return ManagedScopeError(message)


def _strict_report_once(reason: str) -> None:
    if reason not in _STRICT_REPORTED:
        _STRICT_REPORTED.add(reason)
        logger.critical("managed scope: %s; keeping the managed policy this process already loaded "
                        "in force (%s=1)", reason, STRICT_ENV)


def _under_pytest() -> bool:
    """True inside the test suite: ignore the system ``/etc/hermes`` so a real managed scope on a
    dev/CI box can't leak policy into the suite. An explicit ``HERMES_MANAGED_DIR`` still applies."""
    return "PYTEST_CURRENT_TEST" in os.environ


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _resolve_managed_dirs() -> tuple[tuple[Path, ...], str]:
    """``(active dirs, lowest precedence first; problem)`` — *problem* is "" when the scope is complete."""
    dirs: list[Path] = []
    problem = ""
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
        if p.is_dir():
            dirs.append(p)
        else:
            problem = f"HERMES_MANAGED_DIR={override!r} is not a directory"
    if not _under_pytest() and _DEFAULT_MANAGED_DIR.is_dir():
        if dirs and _same_dir(dirs[0], _DEFAULT_MANAGED_DIR):
            dirs.clear()  # one directory named twice: read it once, at system precedence
        dirs.append(_DEFAULT_MANAGED_DIR)
    return tuple(dirs), problem


def get_managed_dirs() -> tuple[Path, ...]:
    """The active managed-scope directories, lowest precedence first; ``()`` when none.

    ``$HERMES_MANAGED_DIR`` (IT-only bootstrap override; never persisted to any .env; honored only
    when non-empty AND the directory exists) layers UNDER ``/etc/hermes``: the system directory,
    when it exists, is always read and wins per leaf, so pointing the variable at a missing or
    different directory can neither disable nor shadow it. A missing directory contributes nothing
    — the common case, so this must be cheap + side-effect-free. Under ``HERMES_MANAGED_STRICT`` an
    incomplete resolution raises ``ManagedScopeError`` instead (see ``strict_mode``).
    """
    dirs, problem = _resolve_managed_dirs()
    if not strict_mode():
        return dirs
    if not problem and (dirs or not _STRICT_LAST_DIRS):
        _STRICT_LAST_DIRS[:] = dirs
        return dirs
    if _STRICT_LAST_DIRS:  # broke or vanished after this process loaded the scope: its files fall back below
        _strict_report_once(problem or "the managed scope this process loaded no longer resolves")
        return tuple(_STRICT_LAST_DIRS)
    raise _strict_refusal(problem)


def get_managed_dir() -> Optional[Path]:
    """The highest-precedence active managed directory (``/etc/hermes`` when it exists), or None.
    For "is a scope active" checks and labels; readers of the managed files use ``get_managed_dirs``."""
    dirs = get_managed_dirs()
    return dirs[-1] if dirs else None


def describe_source(filename: str) -> str:
    """The managed ``<dir>/<filename>`` paths that exist, highest precedence first, for messages."""
    found = [str(d / filename) for d in reversed(get_managed_dirs()) if (d / filename).exists()]
    return ", ".join(found) or "the managed scope"


def config_file_signature() -> tuple:
    """Change-detection key over every managed ``config.yaml`` that exists (``()`` when none), so a
    cache of the merged config invalidates when any layer is edited, added or removed."""
    sig: list = []
    for managed_dir in get_managed_dirs():
        path = managed_dir / "config.yaml"
        try:
            sig.append((str(path), *file_cache_version(path)))
        except OSError:
            continue  # absent: contributes nothing, exactly like a missing scope
    return tuple(sig)


def invalidate_managed_cache() -> None:
    """Drop cached managed config/env. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()
        _STRICT_LAST_DIRS.clear()
        _STRICT_REPORTED.clear()


def _strict_fallback(path: Path, cache: dict[str, tuple], reason: str):
    """Strict mode: the last value this process parsed from *path*, or raise ``ManagedScopeError``."""
    with _CACHE_LOCK:
        hit = cache.get(str(path))
    if hit is None:
        raise _strict_refusal(reason)
    _strict_report_once(reason)
    return copy.deepcopy(hit[-1])


def _cached_read(path: Path, cache: dict[str, tuple], parse, *, required: bool = False, mapping: bool = False):
    """Shared stat-signature-keyed read; returns a deepcopy of the parsed value.

    ``None`` when the file is absent or fails to parse (fail-open). A parse failure is logged
    LOUDLY — the admin needs to know their policy isn't applied — but never raises, so a malformed
    managed file can't brick startup. Under ``HERMES_MANAGED_STRICT`` (``strict_mode``) a missing
    *required* file, an unreadable or unparseable one, or a non-mapping root when *mapping* is set
    raises ``ManagedScopeError`` (or keeps the last value parsed in this process).
    """
    strict = strict_mode()
    try:
        st = path.stat()
    except FileNotFoundError:
        return _strict_fallback(path, cache, f"managed file {path} is missing") if strict and required else None
    except OSError as exc:
        return _strict_fallback(path, cache, f"managed file {path} cannot be accessed: {exc}") if strict else None
    key = file_cache_version(path, st)
    path_key = str(path)
    with _CACHE_LOCK:
        hit = cache.get(path_key)
        if hit is not None and hit[:len(key)] == key:
            return copy.deepcopy(hit[len(key)])
    try:
        parsed = parse(path)
    except Exception as exc:
        if strict:
            return _strict_fallback(path, cache, f"managed file {path} failed to parse: {exc}")
        logger.warning(
            "managed scope: failed to parse %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path, exc)
        return None
    if mapping and not isinstance(parsed, dict):
        # Never cached, so the cache only ever holds a usable policy (the strict fallback's source).
        if strict:
            found = "an empty document" if parsed is None else f"a {type(parsed).__name__}"
            return _strict_fallback(path, cache, f"managed file {path} is {found}, not a mapping")
        return parsed
    with _CACHE_LOCK:
        cache[path_key] = (*key, copy.deepcopy(parsed))
    return parsed


def _parse_managed_config(path: Path):
    return fast_safe_load(path.read_text(encoding="utf-8-sig"))


def _read_managed_config_file(managed_dir: Path):
    return _cached_read(managed_dir / "config.yaml", _CONFIG_CACHE, _parse_managed_config, required=True, mapping=True)


def _read_managed_env_file(managed_dir: Path):
    return _cached_read(managed_dir / ".env", _ENV_CACHE, _parse_managed_env)


def _merge_layer(base: dict, top: dict) -> dict:
    """*top* over *base* per leaf (``hermes_cli.config._deep_merge`` rules: dicts recurse, anything
    else replaces wholesale, a ``None`` over a dict section is ignored)."""
    out = dict(base)
    for key, value in top.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge_layer(out[key], value)
        elif not (isinstance(out.get(key), dict) and value is None):
            out[key] = value
    return out


def _load_managed_file(read, merge) -> dict:
    merged: dict = {}
    for managed_dir in get_managed_dirs():
        parsed = read(managed_dir)
        if isinstance(parsed, dict):
            merged = merge(merged, parsed)
    return merged


def check_strict() -> None:
    """Under ``HERMES_MANAGED_STRICT``, load the whole managed scope now so a broken one stops the
    process at startup (``ManagedScopeError``) rather than at its first policy read. No-op
    otherwise. Called by the dotenv loader, which every entry point runs before anything else."""
    if not strict_mode():
        return
    for managed_dir in get_managed_dirs():  # strict: an unresolvable HERMES_MANAGED_DIR raises here
        _read_managed_config_file(managed_dir)
        _read_managed_env_file(managed_dir)


def load_managed_config() -> dict:
    """Parsed managed config.yaml — every managed dir's, merged per leaf with ``/etc/hermes`` on top
    — or {} when absent/malformed (fail-open). Always {} under a config backend that takes config
    and locks from elsewhere (remote mode, D18; boot warns about the file)."""
    from hermes_cli.config_backend import get_config_backend

    if not get_config_backend().honors_managed_config():
        return {}
    return _load_managed_file(_read_managed_config_file, _merge_layer)


def load_managed_env() -> dict[str, str]:
    """Parsed managed .env (KEY=VALUE) of every managed dir, ``/etc/hermes`` winning per key, or {}
    when absent (fail-open)."""
    return _load_managed_file(_read_managed_env_file, lambda base, top: {**base, **top})


def _parse_managed_env(path: Path) -> dict[str, str]:
    from agent.secret_scope import load_env_file

    path.read_text(encoding="utf-8-sig")  # load_env_file swallows decode errors; an admin file must fail LOUD
    return load_env_file(path)


def apply_managed_overlay(config: dict) -> dict:
    """Overlay administrator-pinned config values on top of an already-built dict.

    ``${VAR}`` refs in the managed config expand against the PROCESS env only, so a user cannot
    shadow a managed literal via a ref they control; a bare root ``model: x/y`` string is promoted
    to ``model.default`` so it can't clobber the dict shape callers expect; managed values
    deep-merge ON TOP per leaf while sibling keys stay user-controlled. Fail-open: returns
    ``config`` unchanged when no scope is present or on any error. Mutates and returns ``config``.
    """
    try:
        managed = load_managed_config()
        if not managed:
            return config
        # Imported lazily to avoid an import cycle (config imports managed_scope).
        from hermes_cli.config import _deep_merge, _expand_env_vars, _normalize_root_model_keys
        managed_expanded = _normalize_root_model_keys(_expand_env_vars(managed))
        # _normalize_root_model_keys only promotes the string when root provider/base_url
        # keys exist to migrate; handle the bare case here (matches cli.py) so _deep_merge
        # never replaces the caller's ``model`` dict with a string.
        if isinstance(managed_expanded.get("model"), str):
            managed_expanded = dict(managed_expanded)
            managed_expanded["model"] = {"default": managed_expanded["model"]}
        return _deep_merge(config, managed_expanded)
    except Exception:
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """Dotted leaf keys pinned by the managed config (e.g. {'model.default'})."""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """True if the exact dotted config key is pinned by the managed layer."""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """True if the env var name is pinned by the managed .env layer."""
    return name in load_managed_env()


def pinned_terminal_backend() -> Optional[str]:
    """The non-empty ``terminal.backend`` the managed config pins, or None. An empty or null value
    names no backend to run, so it pins nothing."""
    terminal = apply_managed_overlay({}).get("terminal")
    backend = terminal.get("backend") if isinstance(terminal, dict) else None
    return (backend.strip() or None) if isinstance(backend, str) else None
