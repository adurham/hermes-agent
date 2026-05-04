"""Persona discovery + per-role model config (hermes-runtime side).

The canonical implementation lives in :mod:`swarm.persona_library` (shipped
in the hermes-swarm package).  This module is a thin wrapper that:

  * Re-exports the library's persona discovery + curated-policy surface
    (:class:`Persona`, :func:`discover_personas`, :data:`SUGGESTED_ROLE_MODELS`,
    etc.) so the existing public API in hermes-agent keeps working without
    churn for ``tools/delegate_tool.py``, ``cli.py``, slash commands, etc.
  * Adds the hermes-runtime config bits — reading/writing
    ``delegation.model_by_role`` in ``~/.hermes/config.yaml`` and the
    one-shot :func:`sync_from_ruflo` bootstrap.  These belong here because
    they're tied to hermes-agent's config plumbing, not to the library.

When hermes-swarm isn't installed (it's an optional dependency), the
fallbacks below kick in: persona discovery still works (it's pure
filesystem), but :data:`SUGGESTED_ROLE_MODELS` is empty so
:func:`apply_suggested_defaults` becomes a no-op.  Install hermes-swarm to
get the curated table.

Public surface (callers shouldn't need to know whether the library is
available — same names either way):

  * :class:`Persona` (alias :class:`RufloAgent` for back-compat) — discovered
    persona record.
  * :func:`discover_personas` (alias :func:`discover_ruflo_agents`) — scan
    the personas directory.
  * :func:`lookup_agent` — find one by name.
  * :func:`group_by_category` — bucket by subdir.
  * :data:`SUGGESTED_ROLE_MODELS` and :func:`apply_suggested_defaults` —
    curated per-role model defaults.
  * :func:`get_role_model_map`, :func:`set_role_model`,
    :func:`lookup_model_for_role` — read/write ``delegation.model_by_role``
    in ~/.hermes/config.yaml.
  * :func:`sync_from_ruflo` — one-shot rsync from a ruflo checkout.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Library import + fallback
# ---------------------------------------------------------------------------
#
# We prefer ``swarm.persona_library`` (canonical).  If the hermes-swarm
# package isn't installed, fall back to a minimal local implementation so
# hermes-agent still runs; the curated SUGGESTED_ROLE_MODELS table is just
# empty in that mode (apply_suggested_defaults becomes a no-op).

try:
    from swarm import persona_library as _plib
    _HAVE_LIBRARY = True
except ImportError:
    _plib = None  # type: ignore[assignment]
    _HAVE_LIBRARY = False


if _HAVE_LIBRARY:
    # Re-export the library's surface verbatim so callers see the same
    # types / functions either way.
    Persona = _plib.Persona
    DEFAULT_PERSONAS_PATH = _plib.DEFAULT_PERSONAS_PATH
    SUGGESTED_ROLE_MODELS = _plib.SUGGESTED_ROLE_MODELS
    _strip_frontmatter = _plib._strip_frontmatter
    _parse_frontmatter = _plib._parse_frontmatter
    discover_personas = _plib.discover_personas
    group_by_category = _plib.group_by_category
    _lookup_persona_lib = _plib.lookup_persona
    _get_personas_path_lib = _plib.get_personas_path
else:
    # ── Minimal local fallback ────────────────────────────────────────────
    from dataclasses import dataclass

    DEFAULT_PERSONAS_PATH = "~/.hermes/personas"
    SUGGESTED_ROLE_MODELS: dict[str, str] = {}  # empty without the library

    def _strip_frontmatter(text: str) -> str:
        if not text.startswith("---"):
            return text
        rest = text[3:]
        closer = rest.find("\n---")
        if closer < 0:
            return text
        return rest[closer + 4:].lstrip("\n")

    def _parse_frontmatter(text: str) -> dict[str, str]:
        if not text.startswith("---"):
            return {}
        rest = text[3:]
        closer = rest.find("\n---")
        if closer < 0:
            return {}
        block = rest[:closer].strip()
        out: dict[str, str] = {}
        current_key: Optional[str] = None
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            if not raw_line.startswith((" ", "\t")) and ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                out[key] = value
                current_key = key
            elif current_key and raw_line.startswith((" ", "\t")):
                extra = raw_line.strip()
                if extra:
                    out[current_key] = (out.get(current_key, "") + " " + extra).strip()
        return out

    @dataclass(frozen=True)
    class Persona:  # type: ignore[no-redef]
        name: str
        description: str
        category: str
        path: str

        def load_prompt(self) -> str:
            try:
                text = Path(self.path).read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                return ""
            return _strip_frontmatter(text)

    _NON_AGENT_BASENAMES_FALLBACK = frozenset({"MIGRATION_SUMMARY", "README", "INDEX"})

    def _get_personas_path_lib(config_path: Optional[str] = None) -> Path:
        if config_path:
            return Path(os.path.expanduser(config_path)).resolve()
        env = os.environ.get("HERMES_PERSONAS_PATH")
        if env:
            return Path(os.path.expanduser(env)).resolve()
        return Path(os.path.expanduser(DEFAULT_PERSONAS_PATH)).resolve()

    def discover_personas(personas_path: Optional[Path] = None) -> list[Persona]:
        base = personas_path or _get_personas_path_lib()
        if not base.is_dir():
            return []
        seen: dict[str, Persona] = {}
        for md in base.rglob("*.md"):
            if not md.is_file():
                continue
            name = md.stem
            if name in _NON_AGENT_BASENAMES_FALLBACK:
                continue
            try:
                rel = md.relative_to(base)
            except ValueError:
                continue
            category = rel.parts[0] if len(rel.parts) > 1 else "general"
            if name in seen:
                continue
            try:
                with md.open("r", encoding="utf-8", errors="replace") as f:
                    head = f.read(2048)
            except OSError:
                continue
            meta = _parse_frontmatter(head)
            seen[name] = Persona(
                name=name,
                description=meta.get("description", ""),
                category=category,
                path=str(md),
            )
        return sorted(seen.values(), key=lambda a: (a.category, a.name))

    def group_by_category(personas: Iterable[Persona]) -> dict[str, list[Persona]]:
        out: dict[str, list[Persona]] = {}
        for p in personas:
            out.setdefault(p.category, []).append(p)
        return out

    def _lookup_persona_lib(
        name: str, personas_path: Optional[Path] = None
    ) -> Optional[Persona]:
        if not name:
            return None
        needle = name.strip()
        for p in discover_personas(personas_path):
            if p.name == needle:
                return p
        return None


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

    No-op when hermes-swarm isn't installed (the curated table is empty).
    """
    current = get_role_model_map()
    merged = dict(current)
    applied = 0
    skipped = 0
    for role, model in SUGGESTED_ROLE_MODELS.items():
        if not overwrite and role in current:
            skipped += 1
            continue
        if current.get(role) == model:
            skipped += 1
            continue
        merged[role] = model
        applied += 1
    if applied == 0:
        return (0, skipped)
    if not _save_to_config_yaml("delegation.model_by_role", merged):
        return (0, skipped)
    return (applied, skipped)


def get_role_model_map() -> dict[str, str]:
    """Read ``delegation.model_by_role`` from ~/.hermes/config.yaml.

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
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def set_role_model(role: str, model: Optional[str]) -> bool:
    """Persist a per-role model assignment to ~/.hermes/config.yaml.

    Pass ``model=None`` or empty string to remove the assignment.
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


__all__ = [
    "DEFAULT_PERSONAS_PATH",
    "Persona",
    "RufloAgent",
    "SUGGESTED_ROLE_MODELS",
    "apply_suggested_defaults",
    "discover_personas",
    "discover_ruflo_agents",
    "get_personas_path",
    "get_role_model_map",
    "get_ruflo_path",
    "group_by_category",
    "lookup_agent",
    "lookup_model_for_role",
    "set_role_model",
    "sync_from_ruflo",
]
