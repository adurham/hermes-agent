"""Persona discovery + per-role model config (hermes-runtime side).

The canonical implementation lives in :mod:`hermes_cli.persona_library`
(ported from the standalone hermes-swarm package on 2026-08-09 when that
package's actual multi-agent coordination code was retired as unused —
persona discovery and the curated model table were the only pieces
hermes-agent depended on). This module is a thin wrapper that:

  * Re-exports the library's persona discovery + curated-policy surface
    (:class:`Persona`, :func:`discover_personas`, :data:`SUGGESTED_ROLE_MODELS`,
    etc.) so the existing public API in hermes-agent keeps working without
    churn for ``tools/delegate_tool.py``, ``cli.py``, slash commands, etc.
  * Adds the hermes-runtime config bits — reading/writing
    ``delegation.model_by_role`` in ``~/.hermes/config.yaml`` and the
    one-shot :func:`sync_from_ruflo` bootstrap. These belong here because
    they're tied to hermes-agent's config plumbing, not to the library.

``delegation.model_by_role`` accepts two entry forms.  A bare string is
just a model name (the historical shape).  A dict entry additionally
carries the provider (and optional endpoint credentials) that model must
run on, mirroring the ``delegation.by_provider`` block shape::

    delegation:
      model_by_role:
        coder: claude-opus-5             # bare string — no provider override
        jr-coder:
          model: qwen3-coder:480b-cloud
          provider: ollama-cloud

Public surface:

  * :class:`Persona` (alias :class:`RufloAgent` for back-compat) — discovered
    persona record.
  * :func:`discover_personas` (alias :func:`discover_ruflo_agents`) — scan
    the personas directory.
  * :func:`lookup_agent` — find one by name.
  * :func:`group_by_category` — bucket by subdir.
  * :data:`SUGGESTED_ROLE_MODELS` and :func:`apply_suggested_defaults` —
    curated per-role model defaults.
  * :func:`get_role_model_map`, :func:`get_role_entry_map`,
    :func:`get_role_provider_map`, :func:`set_role_model`,
    :func:`lookup_model_for_role`, :func:`lookup_provider_for_role` —
    read/write ``delegation.model_by_role`` in ~/.hermes/config.yaml.
  * :func:`sync_from_ruflo` — one-shot rsync from a ruflo checkout.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from hermes_cli.persona_library import (
    DEFAULT_PERSONAS_PATH,
    Persona,
    SUGGESTED_ROLE_MODELS,
    discover_personas,
    group_by_category,
)
from hermes_cli.persona_library import lookup_persona as _lookup_persona_lib
from hermes_cli.persona_library import get_personas_path as _get_personas_path_lib
# Re-exported for legacy callers (ruflo_agents.py shim, older tests) that
# imported the frontmatter helpers directly off this module.
from hermes_cli.persona_library import _parse_frontmatter, _strip_frontmatter


# Back-compat alias — older code (tools/delegate_tool.py before the rename,
# tests imported as RufloAgent) keeps working without churn.
RufloAgent = Persona


# ---------------------------------------------------------------------------
# Personas-path resolution
#
# The library's resolver checks env + default; the hermes wrapper additionally
# reads ``delegation.personas_path`` from ~/.hermes/config.yaml so existing
# users' configs continue to take effect.
# ---------------------------------------------------------------------------


def get_personas_path(config_path: Optional[str] = None) -> Path:
    """Resolve the personas directory.

    Precedence: explicit ``config_path`` arg > ``delegation.personas_path``
    in config.yaml > ``HERMES_PERSONAS_PATH`` env > :data:`DEFAULT_PERSONAS_PATH`.
    """
    if config_path:
        return Path(os.path.expanduser(config_path)).resolve()
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
        if isinstance(delegation, dict):
            cfg_path = delegation.get("personas_path")
            if isinstance(cfg_path, str) and cfg_path.strip():
                return Path(os.path.expanduser(cfg_path.strip())).resolve()
    except Exception:
        pass
    return _get_personas_path_lib()


# Back-compat alias — older code called this ``get_ruflo_path``.  Keep the
# old name working so callers in tools/, tests/, and skills don't break.
def get_ruflo_path(config_path: Optional[str] = None) -> Path:
    """Deprecated alias for :func:`get_personas_path`."""
    return get_personas_path(config_path)


# Back-compat alias — older imports used ``discover_ruflo_agents``.
def discover_ruflo_agents(
    ruflo_path: Optional[Path] = None,
) -> list[Persona]:
    """Deprecated alias for :func:`discover_personas`."""
    return discover_personas(ruflo_path)


def lookup_agent(name: str) -> Optional[Persona]:
    """Find a discovered persona by name (using the configured personas dir).

    Returns None if not found.  Used by ``tools/delegate_tool.py`` to load
    the persona prompt for a given ``agent_type=...`` argument on
    ``delegate_task``.
    """
    return _lookup_persona_lib(name, personas_path=get_personas_path())


# ---------------------------------------------------------------------------
# Config persistence helper — duplicated from cli.save_config_value to avoid
# importing cli (which would pull prompt_toolkit and the agent loop).
# ---------------------------------------------------------------------------


def _save_to_config_yaml(key_path: str, value: object) -> bool:
    """Persist ``value`` at ``key_path`` (dot-separated) in active config.yaml."""
    try:
        import yaml  # type: ignore
    except Exception:
        return False

    home_env = os.environ.get("HERMES_HOME")
    home = home_env or os.path.expanduser("~/.hermes")
    user_path = Path(home) / "config.yaml"
    project_path = Path(__file__).resolve().parent.parent / "cli-config.yaml"
    if home_env:
        cfg_path = user_path
    elif user_path.exists():
        cfg_path = user_path
    elif project_path.exists():
        cfg_path = project_path
    else:
        cfg_path = user_path
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        keys = key_path.split(".")
        cur = cfg
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value
        with cfg_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# One-shot sync helper — pulls a ruflo checkout's .claude/agents tree into
# the personas directory.  Idempotent.  Use to refresh after upstream ruflo
# updates, or as a one-time bootstrap.
# ---------------------------------------------------------------------------

# Filtering matches the rules used by the original ruflo discovery code.
# Kept here (not in the library) because the library is read-only and
# never reaches into a ruflo checkout.
_NON_AGENT_BASENAMES_SYNC = frozenset({"MIGRATION_SUMMARY", "README", "INDEX"})
_SKIP_CATEGORIES_FROM_RUFLO = frozenset({
    "flow-nexus",  # cloud sandbox/auth/payments
    "payments",    # agentic-payments — cloud
    "templates",   # base templates, not personas
})


def sync_from_ruflo(
    ruflo_root: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    dest: Optional[Path] = None,
) -> tuple[int, int]:
    """Copy persona .md files from a ruflo checkout to the personas dir.

    Args:
        ruflo_root: Path to a ruflo repo checkout (e.g. ``~/repos/ruflo``).
        overwrite: When True, replace files that already exist.  Default
            False — first sync wins, subsequent syncs only add new files.
        dest: Override the destination personas directory.  Defaults to
            :func:`get_personas_path`.

    Returns:
        ``(copied, skipped)`` — counts of files copied vs. skipped.

    Filters: skip ``v2/``, ``node_modules/``, ``__tests__/``,
    ``_NON_AGENT_BASENAMES_SYNC``, and the cloud-only category set
    ``_SKIP_CATEGORIES_FROM_RUFLO``.  First-encounter-wins dedup across
    the ruflo monorepo.
    """
    src_root = Path(os.path.expanduser(str(ruflo_root))).resolve()
    if not src_root.is_dir():
        raise FileNotFoundError(f"ruflo checkout not found: {src_root}")
    dst_root = (dest or get_personas_path()).resolve()
    dst_root.mkdir(parents=True, exist_ok=True)

    seen: dict[str, tuple[Path, str]] = {}
    for md in src_root.rglob("*.md"):
        parts = md.parts
        try:
            i = parts.index(".claude")
        except ValueError:
            continue
        if i + 1 >= len(parts) or parts[i + 1] != "agents":
            continue
        if "v2" in parts or "node_modules" in parts or "__tests__" in parts:
            continue
        name = md.stem
        if name in _NON_AGENT_BASENAMES_SYNC:
            continue
        rel_after = parts[i + 2 : -1]
        category = rel_after[0] if rel_after else "general"
        if category in _SKIP_CATEGORIES_FROM_RUFLO:
            continue
        if name in seen:
            continue
        seen[name] = (md, category)

    copied = 0
    skipped = 0
    for name, (src, category) in seen.items():
        dst_dir = dst_root / category
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{name}.md"
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        shutil.copy2(src, dst)
        copied += 1
    return (copied, skipped)


# ---------------------------------------------------------------------------
# Per-role model config (hermes ~/.hermes/config.yaml)
#
# Reading/writing user pins is a hermes-runtime concern — the library
# stays config-free.  These helpers persist ``delegation.model_by_role``.
# ---------------------------------------------------------------------------


def apply_suggested_defaults(*, overwrite: bool = False) -> tuple[int, int]:
    """Bulk-apply :data:`SUGGESTED_ROLE_MODELS` to ``delegation.model_by_role``.

    Args:
        overwrite: When True, replace existing assignments.  When False
            (default), only fill in roles that have no current assignment —
            user-customised pins are preserved.

    Returns:
        ``(applied, skipped)`` — counts of roles updated and roles whose
        existing assignment was kept (or that weren't in the suggested map).
    """
    current = get_role_entry_map()
    merged: dict[str, object] = {}
    for role, entry in current.items():
        # Preserve the raw shape: a provider-bearing entry round-trips as a
        # dict, a plain one collapses back to the bare-string form it came in
        # as.  Flattening everything to a string here would silently drop the
        # user's provider pins on save.
        if set(entry) == {"model"}:
            merged[role] = entry["model"]
        else:
            merged[role] = dict(entry)
    applied = 0
    skipped = 0
    for role, model in SUGGESTED_ROLE_MODELS.items():
        if not overwrite and role in current:
            skipped += 1
            continue
        if current.get(role, {}).get("model") == model:
            skipped += 1
            continue
        merged[role] = model
        applied += 1
    if applied == 0:
        return (0, skipped)
    if not _save_to_config_yaml("delegation.model_by_role", merged):
        return (0, skipped)
    return (applied, skipped)


# Keys a dict-form ``model_by_role`` entry may carry.  Mirrors the shape of a
# ``delegation.by_provider`` block (see ``_resolve_delegation_credentials`` in
# tools/delegate_tool.py) so a per-role entry and a per-provider block are
# consumed by the same downstream credential resolution.
_ENTRY_KEYS = ("model", "provider", "base_url", "api_key", "api_mode")


def get_role_entry_map() -> dict[str, dict[str, str]]:
    """Read ``delegation.model_by_role`` as normalized per-role entries.

    Every entry is normalized to a dict.  A bare-string value ``"foo"``
    (the historical shape) becomes ``{"model": "foo"}``; a dict value
    carries through whichever of ``model``, ``provider``, ``base_url``,
    ``api_key``, ``api_mode`` are present as non-empty strings.  Unknown
    keys are ignored and all values are whitespace-stripped.

    An entry with no usable ``model`` is dropped entirely — including a
    dict that declares only a ``provider``.  A provider without its own
    model would redirect the batch-resolved model to an endpoint that
    doesn't serve it, so provider is never applied on its own.

    Returns an empty dict when the section is missing or unparseable.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config()
    except Exception:
        return {}
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        return {}
    raw = delegation.get("model_by_role")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        entry: dict[str, str] = {}
        if isinstance(v, str):
            entry["model"] = v.strip()
        elif isinstance(v, dict):
            for field in _ENTRY_KEYS:
                val = v.get(field)
                if isinstance(val, str) and val.strip():
                    entry[field] = val.strip()
        if entry.get("model"):
            out[k] = entry
    return out


def get_role_model_map() -> dict[str, str]:
    """Read ``delegation.model_by_role`` from ~/.hermes/config.yaml.

    Returns role -> model string.  A dict-form entry is flattened to its
    ``model``, so callers that only care about the model keep working
    unchanged against either config shape.  Returns an empty dict when the
    section is missing or unparseable.
    """
    return {role: entry["model"] for role, entry in get_role_entry_map().items()}


def get_role_provider_map() -> dict[str, str]:
    """Return role -> provider for the roles that pin one.

    Only roles whose entry declares a non-empty ``provider`` appear; roles
    configured with a bare model string are absent.
    """
    return {
        role: entry["provider"]
        for role, entry in get_role_entry_map().items()
        if entry.get("provider")
    }


def set_role_model(
    role: str,
    model: Optional[str],
    provider: Optional[str] = None,
) -> bool:
    """Persist a per-role model assignment to ~/.hermes/config.yaml.

    With no ``provider`` the entry is written as a bare model string (the
    historical shape).  When ``provider`` is given the dict form
    ``{"model": ..., "provider": ...}`` is written instead, pinning the
    role to that model on that provider.

    Pass ``model=None`` or empty string to remove the assignment entirely,
    with or without a provider.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return False
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        delegation = {}
    by_role = delegation.get("model_by_role")
    if not isinstance(by_role, dict):
        by_role = {}
    role = role.strip()
    if not role:
        return False
    if model and model.strip():
        if provider and provider.strip():
            by_role[role] = {
                "model": model.strip(),
                "provider": provider.strip(),
            }
        else:
            by_role[role] = model.strip()
    else:
        by_role.pop(role, None)
    return _save_to_config_yaml("delegation.model_by_role", by_role)


def lookup_model_for_role(role: Optional[str]) -> Optional[str]:
    """Return the configured model for ``role``, or ``None`` if unset.

    Used by ``tools/delegate_tool.py`` to resolve the per-role model when a
    delegate_task() call passes ``agent_type=...`` but doesn't set ``model=``
    explicitly.  Falls through to the existing precedence chain (top-level
    ``model`` arg → ``delegation.model`` config → parent's model) when
    None is returned.
    """
    if not role:
        return None
    return get_role_model_map().get(role.strip())


def lookup_provider_for_role(role: Optional[str]) -> Optional[str]:
    """Return the configured provider for ``role``, or ``None`` if unset.

    Mirrors :func:`lookup_model_for_role`.  A non-None result always comes
    with a model from :func:`lookup_model_for_role` for the same role — the
    entry map drops provider-only entries — so the caller can switch the
    child's provider knowing the matching model travels with it.
    """
    if not role:
        return None
    return get_role_provider_map().get(role.strip())


# ---------------------------------------------------------------------------
# Per-role reasoning-effort config (hermes ~/.hermes/config.yaml)
#
# Mirrors the model_by_role helpers above, but for delegation.
# reasoning_effort_by_role — lets an orchestrator role (e.g. a PM persona
# dispatched with role="orchestrator") run at a higher thinking budget than
# its leaf workers (e.g. agent_type="coder"/"reviewer"), instead of the
# single global delegation.reasoning_effort knob applying uniformly to
# every delegated child regardless of its job.
# ---------------------------------------------------------------------------


def get_role_reasoning_map() -> dict[str, str]:
    """Read ``delegation.reasoning_effort_by_role`` from ~/.hermes/config.yaml.

    Returns an empty dict when the section is missing or unparseable.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config()
    except Exception:
        return {}
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        return {}
    raw = delegation.get("reasoning_effort_by_role")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def set_role_reasoning(role: str, effort: Optional[str]) -> bool:
    """Persist a per-role reasoning-effort assignment to ~/.hermes/config.yaml.

    Pass ``effort=None`` or empty string to remove the assignment.
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return False
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    delegation = cfg.get("delegation") if isinstance(cfg, dict) else None
    if not isinstance(delegation, dict):
        delegation = {}
    by_role = delegation.get("reasoning_effort_by_role")
    if not isinstance(by_role, dict):
        by_role = {}
    role = role.strip()
    if not role:
        return False
    if effort and effort.strip():
        by_role[role] = effort.strip()
    else:
        by_role.pop(role, None)
    return _save_to_config_yaml("delegation.reasoning_effort_by_role", by_role)


def lookup_reasoning_for_role(role: Optional[str]) -> Optional[str]:
    """Return the configured reasoning effort for ``role``, or ``None`` if unset.

    Used by ``tools/delegate_tool.py`` to resolve per-role thinking depth.
    Callers should try the ``agent_type`` (persona, e.g. "coder"/"reviewer")
    first, then fall back to the spawn ``role`` ("orchestrator"/"leaf") when
    no persona-specific entry exists, before falling through to the existing
    precedence chain (``delegation.reasoning_effort`` global → parent's
    reasoning config) when both return None.
    """
    if not role:
        return None
    return get_role_reasoning_map().get(role.strip())


__all__ = [
    "DEFAULT_PERSONAS_PATH",
    "Persona",
    "RufloAgent",
    "SUGGESTED_ROLE_MODELS",
    "apply_suggested_defaults",
    "discover_personas",
    "discover_ruflo_agents",
    "get_personas_path",
    "get_role_entry_map",
    "get_role_model_map",
    "get_role_provider_map",
    "get_role_reasoning_map",
    "get_ruflo_path",
    "group_by_category",
    "lookup_agent",
    "lookup_model_for_role",
    "lookup_provider_for_role",
    "lookup_reasoning_for_role",
    "set_role_model",
    "set_role_reasoning",
    "sync_from_ruflo",
]
