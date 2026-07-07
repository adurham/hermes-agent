"""Consult nudge feature (fork-only).

Periodic reminder pointing the agent at the ``consult`` tool (second opinion
from a configurable reference model) — mirrors ``agent.fork.skill_recall``'s
shape: tick a counter on risky tool calls, fire a one-line nudge into the
tool result every Nth risky op.

Why keyed on risky tool calls rather than a flat turn/iteration count: the
whole point of ``consult`` is catching mistakes before they land, so the
nudge should show up around destructive/consequential actions (terminal,
file writes, code execution, sending messages) — not during routine reads
and searches where a second opinion adds nothing.
"""

from __future__ import annotations

from typing import Optional

# Reuse the same "risky" tool set the skill-recall reminder uses — a
# destructive/consequential action is exactly the moment a second opinion
# is most useful, and duplicating the same intuition under two frozensets
# would just be a place for the two to drift apart.
from agent.fork.skill_recall import _RISKY_TOOL_NAMES


def maybe_consult_nudge(agent, function_name: str) -> Optional[str]:
    """Return a one-line consult-tool reminder, or ``None``.

    Fires when ALL of these hold:
      * ``_consult_nudge_interval`` > 0 (feature enabled)
      * ``consult`` is actually in the agent's tool list (no point nudging
        toward a tool that isn't available this session)
      * the current tool is in ``_RISKY_TOOL_NAMES``
      * the risky-op counter has reached the interval

    Increments the counter when the current tool is risky. Resets to 0 when
    the reminder fires OR when the agent voluntarily calls ``consult`` (see
    :func:`record_voluntary_consult`) — a spontaneous consult call means the
    agent already has the tool front-of-mind, no need to nag right after.
    """
    interval = getattr(agent, "_consult_nudge_interval", 0)
    if interval <= 0:
        return None
    if "consult" not in (getattr(agent, "valid_tool_names", None) or ()):
        return None
    if function_name not in _RISKY_TOOL_NAMES:
        return None

    agent._risky_ops_since_consult = getattr(agent, "_risky_ops_since_consult", 0) + 1
    if agent._risky_ops_since_consult < interval:
        return None

    agent._risky_ops_since_consult = 0
    return (
        "\n\n[consult reminder] You've made "
        f"{interval} risky tool call(s) since the last second opinion. "
        "If you're not fully confident in the plan or result so far, "
        "consider calling consult(question='...', context='...') to get a "
        "second opinion from the configured reference model before "
        "continuing. Skip it if you're already confident — this is a "
        "nudge, not a requirement."
    )


def record_voluntary_consult(agent) -> None:
    """Reset the nudge counter when the agent calls ``consult`` on its own.

    Best-effort: no-op if the agent wasn't built with the feature attribute
    (subagent w/o consult, test harness, gateway side-call, etc.).
    """
    try:
        agent._risky_ops_since_consult = 0
    except Exception:
        pass  # Defensive: never break a consult call on counter-reset errors.


def init_state(agent) -> None:
    """Initialize per-agent state for the consult nudge.

    Called once from ``agent.agent_init.init_agent``. The interval is
    overridden later by ``init_agent`` from ``consult.nudge_interval``
    config. Set to 0 to disable the nudge entirely (the ``consult`` tool
    itself stays available either way).
    """
    agent._consult_nudge_interval = 8
    agent._risky_ops_since_consult = 0
