"""Explicit turn-close contract.

deepseek-v4.1-flash ends turns by *announcing* work instead of doing it — "I'm dispatching
it now: take the compat table, rank the rest…" with no tool call, admitting on the next turn
that nothing was dispatched. Two generations of prose detectors were built for that
(``agent/promise_gate.py``): the first could never fire, the second required a whitelisted
verb, the third matched grammar. Measured over three days of transcripts the third would
judge ~45 replies a day at roughly 50% precision, against 3 real firings in two days under
the previous one. Text classification of a model's own prose is a losing game: the producer
of the text is the thing being caught, so every widening buys a family and loses another.

This module removes the classification instead of improving it. A turn that used a tool may
not end on prose. To speak to the operator the model must call ``end_turn`` with a status, a
summary, the items still open, and (when it is blocked) what blocks it. The harness rejects a
text-only ending and re-prompts, up to ``MAX_TURN_CONTRACT_REJECTIONS``, then delivers with a
loud ``turn-contract: ESCAPE`` log line — an escape means the enforcement is defeated and the
model needs replacing, not another detector.

Residual risk, accepted: the model can lie in structured form (``status=done`` when it
deferred). That is strictly better than lying in prose — it is parseable, it forces an
explicit ``open_items``/``blocker`` statement, and ``open_items`` can be diffed across turns
to catch ghost tasks.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: How many times a text-only ending is rejected before the answer is delivered anyway.
#: Two is enough for a model that has read the contract; more just burns calls on one that
#: refuses it, and a refusal is the signal to change models (see the module docstring).
MAX_TURN_CONTRACT_REJECTIONS = 2

#: Statuses a turn can close with.
TURN_STATUSES = ("done", "deferred", "blocked", "need_input")

END_TURN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "end_turn",
        "description": (
            "Close the turn and speak to the user. REQUIRED: a turn that used any other tool "
            "may not end with prose — the harness rejects a text-only ending. Call this tool "
            "directly (it is top-level; `tool_call`/`tool_search` reach deferred tools only), "
            "with what the user should read. If work remains, put it in open_items as a task, "
            "never as a promise: 'I'll do X next' is not an answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(TURN_STATUSES),
                    "description": (
                        "done = the requested work is finished and verified; deferred = work "
                        "remains (list it in open_items); blocked = an external dependency stops "
                        "progress (name it in blocker); need_input = the user must answer first."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "The reply the user sees, in full. Markdown allowed.",
                },
                "open_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete unfinished work, one item per entry. Empty when done.",
                },
                "blocker": {
                    "type": "string",
                    "description": "What stops progress, when status is blocked or need_input.",
                },
            },
            "required": ["status", "summary", "open_items"],
        },
    },
}

#: Appended as an ephemeral user row when a text-only ending is rejected; never persisted.
TURN_CONTRACT_NUDGE = (
    "This turn used tools, so it cannot end with prose. Either call another tool and keep "
    "working, or call end_turn(status, summary, open_items) and give the user the answer there. "
    "Do not restate the intention — close the turn."
)

#: System-prompt block, shipped only when the contract is live (see system_prompt._guidance_parts).
TURN_CONTRACT_GUIDANCE = (
    "# Turn contract\n"
    "A turn that used a tool cannot end with prose. To speak to the user you MUST call "
    "`end_turn(status, summary, open_items)`; the harness rejects a text-only ending. "
    "`end_turn` is a top-level tool: call it directly, never through `tool_call` or "
    "`tool_search` (those reach deferred tools only). "
    "Put the whole reply in `summary` — text written outside it is dropped, so a short status "
    "note in `summary` plus the real content in prose leaves the user with the note. Anything "
    "unfinished goes in `open_items` as a task — never as a promise ('I'll do X next' is not an "
    "answer, and it will be rejected)."
)


def turn_contract_enabled(agent: Any = None) -> bool:
    """Whether the end_turn contract is live for *agent* (``agent.turn_contract``, default on)."""
    if agent is not None:
        flag = getattr(agent, "_turn_contract_enabled", None)
        if flag is not None:
            return bool(flag)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        value = (cfg.get("agent") or {}).get("turn_contract", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "off", "no")
        return bool(value)
    except Exception:  # config unavailable: the contract is the default
        return True


def ensure_end_turn_schema(agent: Any, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append the ``end_turn`` schema to *tools* when the contract is enabled, and latch the
    flag the prompt builder reads. Idempotent, and cheap enough to run on every request: the
    request builder calls it so no toolset assembly, session pin or surface rebuild can ship a
    contracted prompt without the tool it demands (2026-10-25: every CLI session's persisted
    tools[] carried 37 tools and no ``end_turn`` while the prompt required it)."""
    enabled = turn_contract_enabled(agent)
    agent._turn_contract_enabled = enabled
    if not enabled:
        logger.warning("turn-contract: schema injection enabled=False tools=%d", len(tools or []))
        return tools
    tools = list(tools or [])
    if any(((t or {}).get("function") or {}).get("name") == "end_turn" for t in tools):
        return tools
    tools.append(json.loads(json.dumps(END_TURN_SCHEMA)))
    logger.warning("turn-contract: schema injection enabled=True tools=%d (appended end_turn)", len(tools))
    return tools


def end_turn_tool(
    status: str = "done",
    summary: str = "",
    open_items: Optional[list[str]] = None,
    blocker: Optional[str] = None,
    *,
    agent: Any = None,
) -> str:
    """Record the model's turn close. The harness delivers ``summary`` as the reply."""
    status = (status or "done").strip().lower()
    if status not in TURN_STATUSES:
        status = "done"
    payload = {
        "status": status,
        "summary": (summary or "").strip(),
        "open_items": [str(i).strip() for i in (open_items or []) if str(i).strip()],
        "blocker": (blocker or "").strip() or None,
    }
    if agent is not None:
        agent._end_turn_payload = payload
    if not payload["summary"]:
        # A close with no summary would deliver an empty reply; say so instead of guessing.
        return json.dumps({"accepted": False, "error": "summary is required and must not be empty"})
    return json.dumps({"accepted": True, "status": status})


def end_turn_payload(agent: Any) -> Optional[dict[str, Any]]:
    return getattr(agent, "_end_turn_payload", None)


def clear_end_turn(agent: Any) -> None:
    agent._end_turn_payload = None


def render_summary(payload: dict[str, Any]) -> str:
    """The user-visible reply for a closed turn.

    The summary is the answer. Open items are appended as a short, explicit task list — that
    is the point of the contract: unfinished work is *declared*, so the next turn (or the
    operator) can see it instead of discovering it was only ever promised.
    """
    summary = (payload.get("summary") or "").strip()
    status = payload.get("status") or "done"
    open_items = payload.get("open_items") or []
    blocker = payload.get("blocker")
    if status == "done" and not open_items:
        return summary
    lines = [summary]
    if blocker:
        lines.append(f"Blocked: {blocker}")
    if open_items and status != "done":
        lines.append("Open:")
        lines.extend(f"- {item}" for item in open_items)
    return "\n\n".join(line for line in lines if line)


#: Decisions from the turn-close contract, used by the turn loop.
CLOSE = "close"      # the model called end_turn: deliver its summary
REJECT = "reject"    # text-only ending after tool activity: re-prompt
ESCAPE = "escape"    # rejected as often as allowed: deliver, and log the escape
PASS = "pass"        # nothing to enforce (no tool activity, or the contract is off)


def contract_decision(*, payload, tool_calls_this_turn: int, rejections: int) -> str:
    """What the turn loop should do about this ending, given the contract is enabled."""
    if payload is not None:
        return CLOSE
    if tool_calls_this_turn > 0:
        return REJECT if rejections < MAX_TURN_CONTRACT_REJECTIONS else ESCAPE
    return PASS


#: Where the contract's own counters live. The ESCAPE counter is the go/no-go metric: an
#: escape means the model refused the contract and the enforcement is being defeated, so the
#: fix is a model change, not another detector. Counted mechanically, never by impression.
STATS_PATH = Path.home() / ".hermes" / "state" / "turn_contract.json"


def record_turn_contract_event(kind: str) -> None:
    """Bump today's ``closed`` / ``rejected`` / ``escaped`` counter and persist it.

    Never raises: a turn must not fail because bookkeeping did.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        day = data.setdefault(today, {})
        day[kind] = int(day.get(kind, 0)) + 1
        # Keep a rolling window; the file is a signal, not a ledger.
        for stale in sorted(data)[:-30]:
            data.pop(stale, None)
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATS_PATH)
    except Exception:  # pragma: no cover - bookkeeping must never break a turn
        pass


def read_turn_contract_stats() -> dict[str, Any]:
    """Today's counters (empty when nothing has been recorded)."""
    try:
        data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return dict(data.get(today) or {})
    except Exception:
        return {}
