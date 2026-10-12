"""Turn events a local viewer (the classic CLI, TUI, Desktop) reads from the canonical stream.

The classic CLI rendered every agent callback in process: the tool-generating line, the
kawaii thinking text, reasoning, MoA references, hearts, and every status line the agent printed
through ``_print_fn``. On the session authority the agent runs in the gateway, so each of those
callbacks is projected as an execution event (generation-fenced through
``TurnRunner._publish_execution``) and only for a local viewer's turn: a messaging route never
installs them, so its agent behaves exactly as before.

The cached agent is shared across turns. ``wire`` installs the sinks for one turn and returns the
previous values; ``restore`` puts them back when the turn's ``run_conversation`` returns.
"""
from __future__ import annotations

import time

# Attributes ``wire`` may overwrite on the cached agent for one local-viewer turn.
_TURN_ATTRS = ("tool_gen_callback", "_print_fn")


def wire(turn, agent) -> dict:
    """Install this turn's local-viewer sinks on ``agent``; returns what ``restore`` needs."""
    previous = {name: getattr(agent, name, None) for name in _TURN_ATTRS}
    publish = turn._publish_execution
    agent.tool_gen_callback = lambda name: publish("tool.generating", {"name": str(name or "")})

    def print_fn(*args, **kwargs):
        publish("agent.print", {"text": str(kwargs.get("sep", " ")).join(str(a) for a in args)})
    agent._print_fn = print_fn
    return previous


def restore(agent, previous: dict | None) -> None:
    if not previous:
        return
    for name, value in previous.items():
        setattr(agent, name, value)


def note_tool_start(turn, call_id) -> None:
    """Monotonic start time of one call, for ``tool.complete.duration_s``."""
    turn.__dict__.setdefault("_local_tool_starts", {})[str(call_id or "")] = time.monotonic()


def complete_fields(turn, call_id) -> dict:
    started = turn.__dict__.setdefault("_local_tool_starts", {}).pop(str(call_id or ""), None)
    return {"duration_s": (time.monotonic() - started) if started is not None else None}
