"""Skill-recall reminder feature (fork-only).

Tracks skills loaded via ``skill_view`` and emits a one-line reminder
on every Nth risky tool result, nudging the agent to re-check
``skill_pitfalls()`` for those skills before destructive operations.
The full skill content scrolls out of immediate context long before the
session ends, but its gotchas stay operationally relevant.

See ``tools/skills_tool.py::skill_pitfalls`` for the cheap recall path
the reminder points the agent toward.
"""

from __future__ import annotations

import json
from typing import Optional


# Tool names that count as a "risky operation" for the skill-recall
# reminder. Tick the counter when one of these runs; when it hits the
# configured interval, the NEXT tool result gets a one-line nudge
# asking the agent to re-check skill_pitfalls for the loaded skills.
_RISKY_TOOL_NAMES = frozenset({
    "terminal",
    "Bash",
    "write_file",
    "Write",
    "patch",
    "Edit",
    "execute_code",
    "process",
    "ha_call_service",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "send_message",
})


def record_loaded_skill(agent, name: str, tool_result: str) -> None:
    """Record that a skill was loaded successfully via ``skill_view``.

    Parses the tool result JSON; on success adds the resolved skill
    name to ``agent._loaded_skills_this_session``. Resets the risky-op
    counter so the agent doesn't get a reminder on the very next tool
    — the agent just SAW the full skill content, the reminder is
    useful later when that context has scrolled off.

    Best-effort: any parse failure is silently ignored.
    """
    if not name or not tool_result:
        return
    try:
        parsed = json.loads(tool_result) if isinstance(tool_result, str) else tool_result
        if isinstance(parsed, dict) and parsed.get("success"):
            resolved = parsed.get("name") or name
            if isinstance(resolved, str) and resolved:
                agent._loaded_skills_this_session.add(resolved)
                agent._risky_ops_since_skill_recall = 0
    except Exception:
        pass  # Defensive: never break tool execution on tracker error.


def maybe_skill_recall_hint(agent, function_name: str) -> Optional[str]:
    """Return a one-line reminder string when it's time to nudge the
    agent to re-check loaded-skill pitfalls, else ``None``.

    Fires only when ALL of these hold:
      * recall_reminder_interval > 0 (feature enabled)
      * at least one skill has been loaded this session
      * the current tool is in ``_RISKY_TOOL_NAMES``
      * the risky-op counter has reached the interval

    Increments the counter when the current tool is risky. Resets the
    counter to 0 when the reminder fires.

    Why this exists: a loaded skill's pitfalls section can scroll out
    of immediate attention 30+ turns after the skill was loaded, but
    its warnings remain operationally relevant. Without an active
    nudge the agent will re-discover the pitfalls by stepping on them.
    See ``skill_pitfalls`` in ``tools/skills_tool.py`` for the cheap
    recall path the reminder points the agent toward.
    """
    interval = getattr(agent, "_skill_recall_reminder_interval", 0)
    if interval <= 0:
        return None
    loaded = getattr(agent, "_loaded_skills_this_session", None)
    if not loaded:
        return None
    if function_name not in _RISKY_TOOL_NAMES:
        return None

    agent._risky_ops_since_skill_recall += 1
    if agent._risky_ops_since_skill_recall < interval:
        return None

    # Fire and reset.
    agent._risky_ops_since_skill_recall = 0
    skills_list = ", ".join(sorted(loaded))
    return (
        "\n\n[skill-recall reminder] You loaded "
        f"{len(loaded)} skill(s) earlier this session ({skills_list}). "
        "Before the next destructive command, consider calling "
        f"skill_pitfalls('{sorted(loaded)[0]}') "
        "to re-check its gotchas — the full skill content has "
        "scrolled out of immediate context. This reminder fires "
        f"every {interval} risky tool calls and is cheap to act on."
    )


def init_state(agent) -> None:
    """Initialize fork instance state for skill-recall feature.

    Called once from ``agent.agent_init.init_agent``.  Sets:

    * ``agent._loaded_skills_this_session``     — names of skills loaded via
      ``skill_view`` so far this session.  Adds to it in
      :func:`record_loaded_skill`.
    * ``agent._risky_ops_since_skill_recall``   — counter ticked by
      :func:`maybe_skill_recall_hint`.  Resets to 0 when the reminder fires
      or when a new skill is loaded.
    * ``agent._skill_recall_reminder_interval`` — default 6.  Overridden later
      by ``init_agent`` from ``agent.skills.recall_reminder_interval`` config.
      Set to 0 to disable the reminder feature entirely.
    """
    agent._loaded_skills_this_session = set()
    agent._risky_ops_since_skill_recall = 0
    agent._skill_recall_reminder_interval = 6
