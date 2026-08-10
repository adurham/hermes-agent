"""Persona discovery + curated per-role model policy.

Ported from the standalone ``hermes-swarm`` package's
``swarm.persona_library`` module (2026-08-09), which was retired along with
the rest of hermes-swarm's multi-agent coordination code. This module is
the sole surviving piece: persona markdown discovery and the curated
``SUGGESTED_ROLE_MODELS`` table used by ``delegate_task``'s ``agent_type``
parameter. Nothing else from hermes-swarm (state.db, messaging, tasks,
votes, lifecycle) is used by hermes-agent and none of it was ported.

Personas live as markdown files with YAML frontmatter under
``~/.hermes/personas/<category>/<name>.md``.

Public surface:

  * :class:`Persona` — discovered persona record.
  * :func:`discover_personas`, :func:`lookup_persona`, :func:`group_by_category`.
  * :data:`SUGGESTED_ROLE_MODELS` — curated persona -> model mapping.
  * :func:`get_personas_path` — env+default resolver (no config read; the
    wrapper in :mod:`hermes_cli.personas` additionally reads
    ``delegation.personas_path`` from config.yaml).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

# ---------------------------------------------------------------------------
# Personas directory resolution
# ---------------------------------------------------------------------------

DEFAULT_PERSONAS_PATH = "~/.hermes/personas"

# README-style files that may sit alongside personas; filter so they don't
# show up in the picker.
_NON_AGENT_BASENAMES = frozenset({
    "MIGRATION_SUMMARY",
    "README",
    "INDEX",
})


def get_personas_path(config_path: Optional[str] = None) -> Path:
    """Resolve the personas directory.

    Precedence (no hermes config read here — that's the wrapper in
    ``hermes_cli.personas.get_personas_path``):
        explicit ``config_path`` arg > ``HERMES_PERSONAS_PATH`` env >
        :data:`DEFAULT_PERSONAS_PATH`.
    """
    if config_path:
        return Path(os.path.expanduser(config_path)).resolve()
    env = os.environ.get("HERMES_PERSONAS_PATH")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return Path(os.path.expanduser(DEFAULT_PERSONAS_PATH)).resolve()


# ---------------------------------------------------------------------------
# Frontmatter parsing — kept dependency-free (no PyYAML).
# ---------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    """Strip leading YAML frontmatter (``---\\n...\\n---\\n``) if present."""
    if not text.startswith("---"):
        return text
    rest = text[3:]
    closer = rest.find("\n---")
    if closer < 0:
        return text
    after = rest[closer + 4:]
    return after.lstrip("\n")


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Extract simple flat key/value pairs from YAML frontmatter.

    Multi-line values (continuation lines indented under the previous key)
    are joined into a single string. Returns an empty dict if no
    frontmatter is found.
    """
    if not text.startswith("---"):
        return {}
    rest = text[3:]
    closer = rest.find("\n---")
    if closer < 0:
        return {}
    block = rest[:closer].strip()
    out: Dict[str, str] = {}
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


# ---------------------------------------------------------------------------
# Persona record + discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    """A discovered persona (system prompt + metadata).

    Attributes:
        name: Stable identifier (basename without .md). Use this as the
            ``agent_type`` when calling delegation/spawn tools.
        description: One-line description from the file's YAML
            frontmatter. Empty string if the file has no parseable
            description.
        category: Subdirectory under the personas root (e.g. ``"core"``,
            ``"github"``). ``"general"`` for files at the root.
        path: Absolute path to the .md file. Use :meth:`load_prompt` to
            read the markdown body (frontmatter stripped).
    """

    name: str
    description: str
    category: str
    path: str

    def load_prompt(self) -> str:
        """Return the markdown body of the persona file (everything after
        the closing ``---`` of the YAML frontmatter). Returns the whole
        file if there's no frontmatter, or an empty string on read error.
        """
        try:
            text = Path(self.path).read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return ""
        return _strip_frontmatter(text)


def discover_personas(
    personas_path: Optional[Path] = None,
) -> list[Persona]:
    """Scan the personas directory for .md files.

    Args:
        personas_path: Personas root. Defaults to :func:`get_personas_path`.

    Returns:
        Sorted list of :class:`Persona` objects, ordered by (category, name).
        Returns an empty list if the directory is missing or empty.

    Layout convention:
        ``<root>/<category>/<name>.md`` — top-level files use ``"general"``
        as their category.

    README-style files (see :data:`_NON_AGENT_BASENAMES`) are filtered out
    so users can drop documentation alongside personas without cluttering
    the picker.
    """
    base = personas_path or get_personas_path()
    if not base.is_dir():
        return []

    seen: Dict[str, Persona] = {}
    for md in base.rglob("*.md"):
        if not md.is_file():
            continue
        name = md.stem
        if name in _NON_AGENT_BASENAMES:
            continue
        try:
            rel = md.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) > 1:
            category = rel.parts[0]
        else:
            category = "general"
        if name in seen:
            continue  # dedupe — first encounter wins
        try:
            with md.open("r", encoding="utf-8", errors="replace") as f:
                head = f.read(2048)
        except OSError:
            continue
        meta = _parse_frontmatter(head)
        description = meta.get("description", "")
        seen[name] = Persona(
            name=name,
            description=description,
            category=category,
            path=str(md),
        )
    return sorted(seen.values(), key=lambda a: (a.category, a.name))


def lookup_persona(
    name: str,
    personas_path: Optional[Path] = None,
) -> Optional[Persona]:
    """Find a discovered persona by name. Returns None if not found."""
    if not name:
        return None
    needle = name.strip()
    for p in discover_personas(personas_path):
        if p.name == needle:
            return p
    return None


def group_by_category(
    personas: Iterable[Persona],
) -> Dict[str, list[Persona]]:
    """Group personas by category, preserving sort order within each bucket."""
    out: Dict[str, list[Persona]] = {}
    for p in personas:
        out.setdefault(p.category, []).append(p)
    return out


# ---------------------------------------------------------------------------
# Curated per-role model defaults
# ---------------------------------------------------------------------------
#
# Mapping rules:
#   Haiku 4.5  — cheap retrieval / triage / monitors / scanners / glue.
#                Anything that mostly reads state, routes work, emits status.
#   Sonnet 4.6 — balanced default for code work: coders, testers, reviewers,
#                research roles that fan out across multiple sources (the
#                1M-context tier prevents mid-task compaction).
#   Opus 4.7   — deep reasoning: architecture, security, novel algorithm
#                design, complex consensus, multi-step planning under
#                uncertainty.

_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-6"
_OPUS = "claude-opus-4-7"

SUGGESTED_ROLE_MODELS: Dict[str, str] = {
    # ── Haiku — pure retrieval / triage / monitors / scanners / glue ──────
    "pii-detector": _HAIKU,
    "project-board-sync": _HAIKU,
    "sync-coordinator": _HAIKU,
    "performance-monitor": _HAIKU,
    "resource-allocator": _HAIKU,
    "base-template-generator": _HAIKU,
    "release-manager": _HAIKU,
    "workflow-automation": _HAIKU,
    "load-balancer": _HAIKU,
    "test-long-runner": _HAIKU,
    "aidefence-guardian": _HAIKU,
    "claims-authorizer": _HAIKU,

    # ── Sonnet — balanced default for code work + research roles ─────────
    "researcher": _SONNET,
    "scout-explorer": _SONNET,
    "code-analyzer": _SONNET,
    "analyze-code-quality": _SONNET,
    "issue-tracker": _SONNET,
    "pr-manager": _SONNET,
    "coder": _SONNET,
    "tester": _SONNET,
    "reviewer": _SONNET,
    "planner": _SONNET,
    "github-modes": _SONNET,
    "dev-backend-api": _SONNET,
    "data-ml-model": _SONNET,
    "ops-cicd-github": _SONNET,
    "docs-api-openapi": _SONNET,
    "spec-mobile-react-native": _SONNET,
    "production-validator": _SONNET,
    "test-architect": _SONNET,
    "python-specialist": _SONNET,
    "typescript-specialist": _SONNET,
    "database-specialist": _SONNET,
    "project-coordinator": _SONNET,
    "topology-optimizer": _SONNET,
    "benchmark-suite": _SONNET,
    "performance-benchmarker": _SONNET,
    # SPARC stages — mostly tactical (architecture stage is in Opus below).
    "specification": _SONNET,
    "pseudocode": _SONNET,
    "refinement": _SONNET,
    # Memory subsystem (storage/index work; not novel design)
    "memory-specialist": _SONNET,
    # Goal planning (tactical)
    "agent": _SONNET,
    "goal-planner": _SONNET,
    "code-goal-planner": _SONNET,
    "performance-optimizer": _SONNET,

    # ── Opus — deep reasoning, architecture, security, novel design ───────
    "arch-system-design": _OPUS,
    "architecture": _OPUS,  # SPARC architecture stage
    "adr-architect": _OPUS,
    "security-architect": _OPUS,
    "security-architect-aidefence": _OPUS,
    "security-auditor": _OPUS,
    "ddd-domain-expert": _OPUS,
    "performance-engineer": _OPUS,
    "sparc-orchestrator": _OPUS,
    "injection-analyst": _OPUS,
    "repo-architect": _OPUS,
    "reasoningbank-learner": _OPUS,
}


__all__ = [
    "DEFAULT_PERSONAS_PATH",
    "Persona",
    "SUGGESTED_ROLE_MODELS",
    "discover_personas",
    "get_personas_path",
    "group_by_category",
    "lookup_persona",
]
