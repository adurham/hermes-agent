"""Live failover identity for an agent — what it is ACTUALLY running on.

``agent/chat_completion_helpers.py::try_activate_fallback()`` swaps a live
agent onto its fallback provider by MUTATING the agent in place::

    agent.model = fb_model
    agent.provider = fb_provider
    ...
    agent._fallback_activated = True

and ``agent/agent_runtime_helpers.py::restore_primary_runtime()`` swaps it
back at the top of the next turn.  So ``agent.model`` / ``agent.provider``
are authoritative but *volatile*: any display surface that snapshots the
model STRING once at dispatch/registration time reports a model the child
may not be running on any more.

``cli.py::_get_status_bar_snapshot()`` already reads the live attribute for
the main session for exactly this reason.  This module is the shared,
reusable form of that pattern so the subagent-facing surfaces (delegate_task
``action='list'``, the progress-event payload, the swarm board, the async
completion notification) all resolve the same way instead of each inventing
their own.

Two functions, both deliberately total:

* :func:`resolve_effective_model` — live read, never raises.
* :func:`format_model_label` — display string, never raises.

Why "never raises" is a hard requirement, not defensive habit: both run
inside render paths (a prompt_toolkit widget getter, a status-bar builder)
and inside a lock-adjacent registry read in ``delegate_tool``.  An exception
in either place takes down the display or strands the registry lock, and the
input is an arbitrary caller-supplied object that may be ``None``, a dead
``weakref``, a ``MagicMock`` from a test, or a real agent mid-teardown.
"""
from __future__ import annotations

import weakref
from typing import Any, Dict, Optional

# House glyphs.  ``⚠`` is this codebase's established degraded/caution
# marker (``cli.py`` "⚠ YOLO" status-bar badge, the shallow-clone warning,
# the rate-limit buffer notices in ``agent/conversation_loop.py``); ``→``
# marks the swap direction.  Deliberately NOT a new symbol family.
FALLBACK_GLYPH = "⚠"
FALLBACK_ARROW = "→"

# Rendered when a model slug is missing entirely — matches
# ``swarm_board.format_row``'s existing ``row.model or "?"`` convention.
_UNKNOWN_MODEL = "?"

# The keys :func:`resolve_effective_model` always returns.  Named so callers
# (and the TypeScript renderers built against this wire shape) can assert the
# contract instead of hard-coding the list.
EFFECTIVE_MODEL_KEYS = (
    "model",
    "provider",
    "fallback_active",
    "primary_model",
    "primary_provider",
)


def _empty_state() -> Dict[str, Any]:
    return {
        "model": None,
        "provider": None,
        "fallback_active": False,
        "primary_model": None,
        "primary_provider": None,
    }


def _deref(agent: Any) -> Any:
    """Resolve *agent* through a weakref/proxy, returning None when dead.

    Callers hold subagent references in three different shapes across this
    codebase: a strong ref (``_active_subagents[sid]["agent"]``), a
    ``weakref.ref`` (``_delegate_parent_ref``), and a proxy.  Accept all
    three so no call site has to remember which one it has.
    """
    if agent is None:
        return None
    try:
        if isinstance(agent, weakref.ReferenceType):
            return agent()
        if isinstance(agent, weakref.ProxyTypes):
            # Touch the proxy so a dead referent surfaces here (as
            # ReferenceError) rather than at the first attribute read.
            repr(agent)
            return agent
        if callable(agent) and not hasattr(agent, "model"):
            # A plain ``lambda: child`` style accessor.  Only treated as a
            # deref when it clearly isn't an agent itself.
            return agent()
    except Exception:
        return None
    return agent


def _clean_str(value: Any) -> Optional[str]:
    """Return a non-empty ``str`` or None.

    isinstance-guarded rather than ``str(value)``: a ``MagicMock`` test
    double auto-vivifies unset attributes into a Mock, and stringifying one
    would put ``<MagicMock id=...>`` into a user-visible label and onto the
    JSON wire.  Mirrors the existing guard style at
    ``delegate_tool.py``'s registry-record and result-entry builds.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _clean_bool(value: Any) -> bool:
    """Coerce to a real bool, treating non-numeric objects as False.

    ``bool(MagicMock())`` is True, which would make every mocked agent look
    like it had failed over.  Only genuine bools/ints answer this question.
    """
    if isinstance(value, (bool, int)) and not isinstance(value, complex):
        return bool(value)
    return False


def resolve_effective_model(agent: Any) -> Dict[str, Any]:
    """Live-read an agent's actually-active model/provider + fallback state.

    Returns a dict with exactly :data:`EFFECTIVE_MODEL_KEYS`::

        {"model", "provider", "fallback_active",
         "primary_model", "primary_provider"}

    ``model``/``provider`` are read LIVE off the agent every call — never
    cached — because failover is reversible (``restore_primary_runtime``)
    and a cached post-swap value would be just as wrong as the pre-swap
    snapshot this function exists to replace.

    ``primary_model``/``primary_provider`` come from ``agent._primary_runtime``,
    the pre-swap snapshot written by ``init_agent`` and re-written by
    ``switch_model`` (which also clears ``_fallback_activated``), so it always
    describes the identity the agent would return to.  They are reported only
    while a fallback is actually active — when the agent is on its primary,
    "primary" and "effective" are the same thing and duplicating it would
    invite renderers to draw a pointless ``x→x`` swap arrow.

    Never raises.  ``agent=None``, a dead weakref, or an object missing every
    attribute all yield the all-None/False dict.
    """
    state = _empty_state()

    try:
        target = _deref(agent)
    except Exception:
        return state
    if target is None:
        return state

    try:
        state["model"] = _clean_str(getattr(target, "model", None))
    except Exception:
        pass
    try:
        state["provider"] = _clean_str(getattr(target, "provider", None))
    except Exception:
        pass
    try:
        state["fallback_active"] = _clean_bool(
            getattr(target, "_fallback_activated", False)
        )
    except Exception:
        state["fallback_active"] = False

    if not state["fallback_active"]:
        return state

    # Pre-swap identity.  ``_primary_runtime`` is a plain dict on a real
    # agent, but guard the type: a partially-initialised agent can carry
    # None here, and a test double can carry anything at all.
    try:
        primary = getattr(target, "_primary_runtime", None)
    except Exception:
        primary = None
    if isinstance(primary, dict):
        state["primary_model"] = _clean_str(primary.get("model"))
        state["primary_provider"] = _clean_str(primary.get("provider"))

    return state


def format_model_label(
    model: Any,
    *,
    fallback_active: Any = False,
    primary_model: Any = None,
    compact: bool = False,
) -> str:
    """Render the model identity for display, marking an active fallback.

    Not in fallback (the overwhelmingly common case) the output is exactly
    the model slug — byte-identical to what every one of these surfaces
    printed before, so nothing shifts for a healthy run::

        format_model_label("claude-opus-5") == "claude-opus-5"

    In fallback, two forms:

    * ``compact=True`` — the swarm board's narrow row, which already shares
      its line with status, tool name, note and elapsed time::

          "⚠ glm-5.3→claude-opus-5"

    * ``compact=False`` — list output and completion notifications, where
      there is room to say it in words::

          "⚠ claude-opus-5 (fallback from glm-5.3)"

    A missing ``primary_model`` degrades instead of rendering a dangling
    arrow or an empty parenthetical: ``"⚠ claude-opus-5 (fallback)"``.

    Never raises; non-string inputs are treated as absent.
    """
    effective = _clean_str(model) or _UNKNOWN_MODEL

    if not _clean_bool(fallback_active):
        return effective

    primary = _clean_str(primary_model)

    if compact:
        if primary and primary != effective:
            return f"{FALLBACK_GLYPH} {primary}{FALLBACK_ARROW}{effective}"
        return f"{FALLBACK_GLYPH} {effective}"

    if primary and primary != effective:
        return f"{FALLBACK_GLYPH} {effective} (fallback from {primary})"
    return f"{FALLBACK_GLYPH} {effective} (fallback)"


def effective_model_fields(
    agent: Any,
    *,
    snapshot_model: Any = None,
    snapshot_provider: Any = None,
    compact: bool = False,
) -> Dict[str, Any]:
    """The full six-key wire payload for one subagent surface.

    :func:`resolve_effective_model` plus the rendered ``model_label``, with a
    documented degradation path: when the live agent object is gone (the
    child finished and its ref was dropped, or the record predates the ref)
    the caller's dispatch-time *snapshot* is used for ``model``/``provider``
    rather than reporting None.  A stale-but-plausible model beats a blank
    one; a dead agent can no longer fail over, so the snapshot is the last
    thing that was true.

    ``compact`` selects the label form — the swarm board passes True, the
    JSON/notification surfaces leave it False.
    """
    state = resolve_effective_model(agent)

    if state["model"] is None:
        state["model"] = _clean_str(snapshot_model)
    if state["provider"] is None:
        state["provider"] = _clean_str(snapshot_provider)

    state["model_label"] = format_model_label(
        state["model"],
        fallback_active=state["fallback_active"],
        primary_model=state["primary_model"],
        compact=compact,
    )
    return state


__all__ = [
    "EFFECTIVE_MODEL_KEYS",
    "FALLBACK_ARROW",
    "FALLBACK_GLYPH",
    "effective_model_fields",
    "format_model_label",
    "resolve_effective_model",
]
