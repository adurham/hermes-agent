"""Silence guard (fork-only): text-starvation / convergence pressure for long silent turns.

Why: as a turn runs, the agent loop may keep issuing tool-call rounds that emit NO
user-visible assistant text. On a slow local model a single turn once ran 75 tool rounds
over 36 minutes with zero user-visible output and never converged; the user killed it by
hand. This guard notices the silence and applies two bounded, task-agnostic nudges, so the
loop reports progress and eventually delivers a final answer instead of spinning.

Knobs (``agent.silence_guard`` in config.yaml; see ``hermes_cli.config_defaults``):

* ``enabled`` (bool, default true) — false disables the whole feature.
* ``status_after_rounds`` (int, default 20) — silent assistant rounds before a
  one-per-streak progress nudge.
* ``converge_after_rounds`` (int, default 50) — assistant rounds this turn before a
  one-per-turn wrap-up nudge.

Both nudges append a ``[SYSTEM NOTICE — …]`` line to the NEWEST tool result — the same
cache-safe one-shot channel the iteration-budget warning and the run-budget wrap-up use
(``agent.turn_iteration_prep`` / ``agent.conversation_loop``). Only the current,
not-yet-persisted tool-result tail is mutable; an older/durable row is left alone and the
latch stays open so the notice retries on the next iteration.

None of this imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.message_content import flatten_message_text

logger = logging.getLogger("agent.conversation_loop")

DEFAULT_ENABLED = True
DEFAULT_STATUS_AFTER_ROUNDS = 20
DEFAULT_CONVERGE_AFTER_ROUNDS = 50

#: A turn this long that never emitted user-visible assistant text is worth a loud log line.
SILENT_LONG_TURN_MIN_ROUNDS = 25

# Stable markers: tests assert on these exact substrings, so treat them as an interface.
STATUS_NUDGE_MARKER = "[SYSTEM NOTICE — no user-visible output]"
CONVERGE_NUDGE_MARKER = "[SYSTEM NOTICE — turn round limit]"

STATUS_NUDGE_TEMPLATE = (
    STATUS_NUDGE_MARKER
    + " {rounds} tool rounds have run with no user-visible output this turn. Post a brief "
    "progress update now (what you have done, what is left), then continue."
)
CONVERGE_NUDGE_TEMPLATE = (
    CONVERGE_NUDGE_MARKER
    + " This turn has run {rounds} rounds without reaching a final response. Wrap up: deliver "
    "your best current findings as the final answer now; list anything genuinely unfinished as "
    "follow-ups instead of continuing."
)


def _coerce_positive_int(value: Any, default: int) -> int:
    """A positive int, else ``default``. ``bool`` rejected (YAML ``true`` must not mean 1)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced >= 1 else default


def resolve_silence_guard_settings(raw: Any) -> tuple[bool, int, int]:
    """``(enabled, status_after_rounds, converge_after_rounds)`` from a partial/invalid block.

    A missing/non-dict section, a missing key, or an invalid value each fall back to the
    default for that key, so ``silence_guard: {converge_after_rounds: 30}`` keeps ``enabled``
    true and ``status_after_rounds`` at 20. Only a real ``enabled: false`` disables the mode.
    """
    if not isinstance(raw, dict):
        return DEFAULT_ENABLED, DEFAULT_STATUS_AFTER_ROUNDS, DEFAULT_CONVERGE_AFTER_ROUNDS
    enabled_raw = raw.get("enabled", DEFAULT_ENABLED)
    enabled = enabled_raw if isinstance(enabled_raw, bool) else DEFAULT_ENABLED
    status_after = _coerce_positive_int(raw.get("status_after_rounds"), DEFAULT_STATUS_AFTER_ROUNDS)
    converge_after = _coerce_positive_int(raw.get("converge_after_rounds"), DEFAULT_CONVERGE_AFTER_ROUNDS)
    return enabled, status_after, converge_after


def _enabled(agent: Any) -> bool:
    return bool(getattr(agent, "_silence_guard_enabled", DEFAULT_ENABLED))


def _has_visible_text(assistant_message: Any) -> bool:
    """True when the round's assistant message carried non-empty user-visible text."""
    if assistant_message is None:
        return False
    content = getattr(assistant_message, "content", None)
    if isinstance(assistant_message, dict):
        content = assistant_message.get("content")
    return bool(flatten_message_text(content).strip())


def note_assistant_round(agent: Any, assistant_message: Any) -> None:
    """Count one assistant API round for the turn and update the silent streak.

    A round whose assistant message carried non-empty visible text resets the streak to 0
    and re-arms the progress nudge for the next streak; a silent round (tool-call-only or
    empty content) extends the streak. No-op while the guard is disabled.
    """
    if not _enabled(agent):
        return
    agent._silence_guard_rounds = int(getattr(agent, "_silence_guard_rounds", 0) or 0) + 1
    if _has_visible_text(assistant_message):
        agent._silence_guard_silent_streak = 0
        agent._silence_guard_status_injected = False
    else:
        agent._silence_guard_silent_streak = int(getattr(agent, "_silence_guard_silent_streak", 0) or 0) + 1


def _append_to_newest_tool_result(messages: Any, notice: str) -> bool:
    """Append ``notice`` to the newest mutable tool result; False when none is mutable.

    Same contract as ``_maybe_inject_iteration_budget_warning``: only the current tool-result
    tail is mutable (an older turn may already be cached), and a persisted row is append-only.
    """
    if not messages:
        return False
    from agent.context_compressor import _DB_PERSISTED_MARKER

    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "tool":
            if msg.get(_DB_PERSISTED_MARKER):
                return False
            existing = msg.get("content", "")
            if isinstance(existing, str):
                msg["content"] = existing + f"\n\n{notice}"
            else:  # multimodal content blocks — append a text block
                try:
                    msg["content"] = [*(existing or []), {"type": "text", "text": notice}]
                except TypeError:
                    return False
            return True
    return False


def _pending_notice(agent: Any) -> Optional[tuple[str, str]]:
    """``(kind, text)`` for the nudge due this iteration, or None.

    The progress nudge (Nudge 1) is checked first; the convergence nudge (Nudge 2) second.
    At most one is emitted per iteration — each independent latch means a nudge suppressed
    here simply fires on the next iteration. With the default thresholds (20 < 50) the two
    never collide.
    """
    rounds = int(getattr(agent, "_silence_guard_rounds", 0) or 0)
    streak = int(getattr(agent, "_silence_guard_silent_streak", 0) or 0)
    status_after = int(getattr(agent, "_silence_guard_status_after_rounds", DEFAULT_STATUS_AFTER_ROUNDS) or 0)
    converge_after = int(getattr(agent, "_silence_guard_converge_after_rounds", DEFAULT_CONVERGE_AFTER_ROUNDS) or 0)
    if (
        streak >= max(1, status_after)
        and not getattr(agent, "_silence_guard_status_injected", False)
    ):
        return "status", STATUS_NUDGE_TEMPLATE.format(rounds=rounds)
    if (
        rounds >= max(1, converge_after)
        and not getattr(agent, "_silence_guard_converge_injected", False)
    ):
        return "converge", CONVERGE_NUDGE_TEMPLATE.format(rounds=rounds)
    return None


def maybe_inject_silence_guard(agent: Any, messages: Any) -> bool:
    """Append at most one silence-guard notice to the newest tool result; True when injected.

    The progress nudge fires once per silent streak; the convergence nudge fires once per
    turn. Both latch only on a successful append.
    """
    if not _enabled(agent):
        return False
    pending = _pending_notice(agent)
    if pending is None:
        return False
    kind, notice = pending
    if not _append_to_newest_tool_result(messages, notice):
        return False
    if kind == "converge":
        agent._silence_guard_converge_injected = True
    else:
        agent._silence_guard_status_injected = True
    logger.info(
        "Silence guard %s nudge injected (rounds=%s, silent_streak=%s, session=%s)",
        kind, getattr(agent, "_silence_guard_rounds", 0),
        getattr(agent, "_silence_guard_silent_streak", 0),
        getattr(agent, "session_id", None) or "-",
    )
    return True


def maybe_log_silent_long_turn(agent: Any) -> None:
    """WARN once at turn end when a long turn emitted no user-visible assistant text.

    ``rounds >= SILENT_LONG_TURN_MIN_ROUNDS`` and the silent streak still spans every round
    (a single visible-text round would have reset it) means the whole turn stayed silent.
    """
    if not _enabled(agent):
        return
    rounds = int(getattr(agent, "_silence_guard_rounds", 0) or 0)
    streak = int(getattr(agent, "_silence_guard_silent_streak", 0) or 0)
    if rounds < SILENT_LONG_TURN_MIN_ROUNDS or streak < rounds:
        return
    logger.warning(
        "silent long turn: rounds=%d no user-visible text (session=%s)",
        rounds, getattr(agent, "session_id", None) or "-",
    )
