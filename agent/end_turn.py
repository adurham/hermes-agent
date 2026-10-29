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

That reading of ESCAPE is only honest when the harness itself cannot be the cause. The tool
is appended after toolset assembly, so it has to be registered in ONE place that owns both
the wire list and the dispatch gate (``register_end_turn``): on 2026-10-25 the two were
split — the request builder put ``end_turn`` on the wire while a tool-snapshot rebuild had
dropped it from ``agent.valid_tool_names`` — and every direct call came back "Tool 'end_turn'
does not exist". The model did exactly what it was told, the payload stayed empty, and the
loop counted 31 rejections and 8 "refusals" against 4 closes. So the decision table has a
fourth outcome: a close the model attempted and the harness failed to run is delivered and
recorded as ``harness_error``, never as a rejection. ``rejected`` means "ended on prose with
no close attempted" and nothing else; the go/no-go metric is meaningless otherwise.

Residual risk, accepted: the model can lie in structured form (``status=done`` when it
deferred). That is strictly better than lying in prose — it is parseable, it forces an
explicit ``open_items``/``blocker`` statement, and ``open_items`` can be diffed across turns
to catch ghost tasks.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: How many times a text-only ending is rejected before the answer is delivered anyway.
#: Two is enough for a model that has read the contract; more just burns calls on one that
#: refuses it, and a refusal is the signal to change models (see the module docstring).
MAX_TURN_CONTRACT_REJECTIONS = 2

#: Statuses a turn can close with.
TURN_STATUSES = ("done", "deferred", "blocked", "need_input")

END_TURN_TOOL_NAME = "end_turn"

#: Hard ceiling on re-entries into a turn that closed with agent-owned work. This is a runaway
#: stop, not a per-item budget: a continuation that acts is cheap and legitimate, so the budget
#: is charged by the RE-ENTRY count and an idle re-entry (zero tool calls) is charged against
#: the rejection budget instead (see ``contract_decision``). Measured 2026-10-25: 28 of 28
#: deferred closes in three days were followed by a user row — 0 continued on their own — so the
#: declared items were a report, never a queue. The bounce is the queue.
MAX_TURN_CONTINUATIONS = 20

END_TURN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": END_TURN_TOOL_NAME,
        "description": (
            "Close the turn and speak to the user. REQUIRED: a turn that used any other tool "
            "may not end with prose — the harness rejects a text-only ending. Call this tool "
            "directly (it is top-level; `tool_call`/`tool_search` reach deferred tools only), "
            "with what the user should read. `open_items` is a work queue, not an ending: an "
            "item you can still act on hands the turn straight back to you, so close only "
            "when the list is empty or what remains is not yours to do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(TURN_STATUSES),
                    "description": (
                        "done = the requested work is finished and verified (open_items empty); "
                        "blocked = an external dependency stops progress — name it in blocker; "
                        "need_input = the user must answer first — say what in blocker. "
                        "deferred is NOT a stopping state: it tells the harness work remains "
                        "that you can still do, and the turn continues with your open_items."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "The reply the user sees, in full. Markdown allowed.",
                },
                "open_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item": {"type": "string", "description": "The work, concretely."},
                            "owner": {
                                "type": "string",
                                "enum": ["agent", "user"],
                                "description": (
                                    "agent (default) = you can do it now; the turn keeps running. "
                                    "user = only the owner can do it."
                                ),
                            },
                            "needs": {
                                "type": "string",
                                "description": "owner=user only: exactly what you need from the owner.",
                            },
                        },
                        "required": ["item"],
                    },
                    "description": (
                        "Unfinished work, one concrete item per entry — a task, not a promise. An item "
                        "owned by the agent hands the turn straight back to you; a done close may only "
                        "carry owner=user items, each naming what it needs. Empty when the work is done."
                    ),
                },
                "blocker": {
                    "type": "string",
                    "description": (
                        "Legacy terminal-close reason, still honoured for blocked/need_input. Prefer an "
                        "owner=user item with `needs`."
                    ),
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
    "note in `summary` plus the real content in prose leaves the user with the note. "
    "`open_items` is a work queue, not a report — and every entry names its OWNER. An entry "
    "owned by you (`owner=\"agent\"`, the default) is work you can still do, so the harness hands "
    "the turn straight back to you with it: listing it means the turn is not over. Only an "
    "`owner=\"user\"` entry — one only the owner can do, with `needs` naming exactly what you need "
    "from them — lets a turn end; those render as 'Waiting on you'. `done` may carry user-owned "
    "items only, `blocked`/`need_input` need one (or a legacy `blocker`)."
)


def turn_contract_enabled(agent: Any = None) -> bool:
    """Whether the end_turn contract is live for *agent* (``agent.turn_contract``).

    Default OFF: on the day it shipped (2026-10-25) the harness-side split described in the
    module docstring produced rejected:closed = 31:4, and enforcement that over-fires is
    worse than none. Opt in once a session reads closed > 0, rejected = 0, escaped = 0. An
    unreadable config is also OFF — enforcement must never switch itself on by accident.
    """
    if agent is not None:
        flag = getattr(agent, "_turn_contract_enabled", None)
        if flag is not None:
            return bool(flag)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        value = (cfg.get("agent") or {}).get("turn_contract", False)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    except Exception:
        logger.debug("turn-contract: config unreadable; the contract stays off", exc_info=True)
        return False


def _carries_end_turn(tools: Iterable[dict[str, Any]]) -> bool:
    return any(((t or {}).get("function") or {}).get("name") == END_TURN_TOOL_NAME for t in tools)


def turn_continuation_budget(agent: Any = None) -> int:
    """Hard ceiling on re-entries into a turn that closed with agent-owned work.

    ``agent.turn_continuations`` (default :data:`MAX_TURN_CONTINUATIONS`); 0 turns the gate
    off and restores the old deliver-anything behaviour. Cached on the agent by
    :func:`register_end_turn`, so the hot path does not read config per close; an unreadable
    config keeps the default rather than silently disabling the gate.
    """
    if agent is not None:
        cached = getattr(agent, "_turn_continuations_budget", None)
        if cached is not None:
            return int(cached)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        value = (cfg.get("agent") or {}).get("turn_continuations", MAX_TURN_CONTINUATIONS)
        return max(0, int(value))
    except Exception:
        logger.debug("turn-contract: continuation budget unreadable; using the default", exc_info=True)
        return MAX_TURN_CONTINUATIONS


def carry_open_items(agent: Any, payload: dict[str, Any]) -> int:
    """Persist the agent-owned items in the agent's own todo list when the ceiling is reached.

    A turn that ends with its work list intact must not lose it: the ceiling is a runaway stop,
    not permission to drop the queue. The items land as pending todos (ids ``open-<session
    tag>-<n>`` so a later close cannot collide), which the model sees through ``todo_list`` and
    which survive context compression via the store's own injection. Fail-soft: bookkeeping must
    never break the close.
    """
    agent_items, _user = split_items(payload.get("open_items") or [])
    store = getattr(agent, "_todo_store", None)
    if not agent_items or store is None:
        return 0
    try:
        existing = store.read()
        tag = str(getattr(agent, "session_id", "") or "")[-6:] or "turn"
        carried = [
            {"id": f"open-{tag}-{i}", "content": f"[left open by the turn contract] {item['item']}",
             "status": "pending"}
            for i, item in enumerate(agent_items, 1)
        ]
        store.write(list(existing) + carried)
        return len(carried)
    except Exception:
        logger.warning("turn-contract: could not carry %d open item(s) into the todo list", len(agent_items),
                       exc_info=True)
        return 0


def register_end_turn(agent: Any, tools: list[dict[str, Any]], names: Optional[set] = None) -> list[dict[str, Any]]:
    """The ONE registration of ``end_turn``: the wire list and the dispatch gate together.

    Appends the schema to *tools* and the name to *names* — the set the tool-call validator
    resolves every direct call against (``agent.valid_tool_names`` when *names* is omitted)
    — when the contract is enabled, and latches the flag the prompt builder reads. Callers
    that stage a ``(tools, names)`` pair before publishing it atomically pass their staged
    set; the request builder, which only ever sees the wire copy, lets this fall through to
    the live set so wire-present always implies dispatchable. Idempotent, cheap, and
    fail-soft on an agent without a name set (the executor table, ``INLINE_TOOL_EXECUTORS``,
    is static and already knows the name).
    """
    enabled = turn_contract_enabled(agent)
    agent._turn_contract_enabled = enabled
    if not enabled:
        return tools
    if getattr(agent, "_turn_continuations_budget", None) is None:
        agent._turn_continuations_budget = turn_continuation_budget(agent)
    if names is None:
        names = getattr(agent, "valid_tool_names", None)
    if isinstance(names, set):
        names.add(END_TURN_TOOL_NAME)
    tools = list(tools or [])
    if _carries_end_turn(tools):
        return tools
    tools.append(json.loads(json.dumps(END_TURN_SCHEMA)))
    logger.debug("turn-contract: appended end_turn to a %d-tool list that lacked it", len(tools) - 1)
    return tools


#: Older name for :func:`register_end_turn`, kept for the call sites that only hold a list.
ensure_end_turn_schema = register_end_turn


def normalize_payload(
    status: Any = "done", summary: Any = "", open_items: Any = None, blocker: Any = None,
) -> dict[str, Any]:
    """The close as the harness stores it: one normalization for a dispatched call and for
    the arguments of a call the harness failed to dispatch (``recover_close``).

    An item is ``{"item", "owner", "needs"}``. A bare string is accepted as ``owner="agent"``
    (the pre-owner-field shape, and what a model that ignores the schema still emits) — which
    keeps the gate closed on it: a string item claims work the model can do.
    """
    status = str(status or "done").strip().lower()
    if status not in TURN_STATUSES:
        status = "done"
    raw = open_items if isinstance(open_items, (list, tuple)) else ([open_items] if open_items else [])
    items = [_normalize_item(i) for i in raw]
    return {
        "status": status,
        "summary": str(summary or "").strip(),
        "open_items": [i for i in items if i["item"]],
        "blocker": (str(blocker).strip() if blocker else "") or None,
    }


def _normalize_item(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        text = str(raw.get("item") or raw.get("task") or raw.get("content") or "").strip()
        owner = str(raw.get("owner") or "agent").strip().lower()
        needs = str(raw.get("needs") or raw.get("need") or "").strip() or None
        return {"item": text, "owner": owner if owner in ("agent", "user") else "agent", "needs": needs}
    return {"item": str(raw or "").strip(), "owner": "agent", "needs": None}


def split_items(items: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(agent_owned, user_owned)`` — the field the whole gate turns on.

    Tolerant on purpose: a raw payload (strings, or dicts that never went through
    ``normalize_payload``) is normalized here, so a caller cannot bypass the owner split by
    handing over the shape it was written with.
    """
    agent_items, user_items = [], []
    for raw in items or ():
        item = _normalize_item(raw)
        if not item["item"]:
            continue
        (user_items if item["owner"] == "user" else agent_items).append(item)
    return agent_items, user_items


def end_turn_tool(
    status: str = "done",
    summary: str = "",
    open_items: Optional[list[Any]] = None,
    blocker: Optional[str] = None,
    *,
    agent: Any = None,
) -> str:
    """Record the model's turn close. The harness delivers ``summary`` as the reply."""
    payload = normalize_payload(status, summary, open_items, blocker)
    if not payload["summary"]:
        # A close with no summary would deliver an empty reply: refuse it and record nothing,
        # so a prose ending after it is judged as what it is (no close) instead of delivering "".
        return json.dumps({"accepted": False, "error": "summary is required and must not be empty"})
    if agent is not None:
        agent._end_turn_payload = payload
    return json.dumps({"accepted": True, "status": payload["status"]})


def end_turn_payload(agent: Any) -> Optional[dict[str, Any]]:
    return getattr(agent, "_end_turn_payload", None)


def note_final_content_replaced(agent: Any) -> None:
    """Record that the delivered reply REPLACED the final assistant message's streamed content.

    The CLOSE branch (and a harness fault whose call carried a summary) writes the close's
    summary over the text the model had already streamed for that final message, and only the
    summary is persisted. A client that paints streamed text as its own bubble cannot tell that
    draft apart from a second reply by comparing words: without the fact, the Ink TUI kept the
    draft AND appended the summary — the operator read the draft text twice while the delivered
    answer rendered as a dim trailing line, or not at all (desktop parity: its
    ``responseTransformed`` settlement, #124128). Read by ``turn_finalizer`` into
    ``result["final_content_replaced"]``, forwarded to local viewers only; other surfaces ignore it.
    """
    if agent is not None:
        agent._final_content_replaced = True


def clear_end_turn(agent: Any) -> None:
    agent._end_turn_payload = None


def render_summary(payload: dict[str, Any], *, open_label: str = "Open:") -> str:
    """The user-visible reply for a closed turn.

    The summary is the answer. Remaining work is rendered by OWNER — agent-owned items under
    ``open_label``, owner-owned items under ``Waiting on you`` with what each one needs. That
    split is what makes the model's ownership claim visible: if it files its own work under
    "user", the operator reads "Waiting on you: disassemble the next handler" and sees the lie.
    Items are rendered for EVERY status that carries them; ``done`` used to drop them.
    """
    summary = (payload.get("summary") or "").strip()
    status = payload.get("status") or "done"
    agent_items, user_items = split_items(payload.get("open_items") or [])
    blocker = payload.get("blocker")
    if status == "done" and not agent_items and not user_items:
        return summary
    lines = [summary]
    if blocker:
        lines.append(f"Blocked: {blocker}")
    if agent_items:
        lines.append(open_label)
        lines.extend(f"- {item['item']}" for item in agent_items)
    if user_items:
        lines.append("Waiting on you:")
        lines.extend(
            f"- {item['item']}" + (f" (needs: {item['needs']})" if item.get("needs") else "")
            for item in user_items
        )
    return "\n\n".join(line for line in lines if line)


def _parse_args(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _executor_answered(result: Any) -> bool:
    """True when *result* is what ``end_turn_tool`` itself returns (``{"accepted": ...}``):
    the call reached the executor, so whatever it said is the contract's own verdict, not a
    harness fault."""
    parsed = _parse_args(result)
    return isinstance(parsed, dict) and "accepted" in parsed


def attempted_end_turn_close(messages: Optional[list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """The last ``end_turn`` call the model emitted THIS turn that never reached the executor.

    Returns ``{"arguments": dict | None, "result": str | None}`` — the call's arguments and
    the tool-result row the harness answered it with ("Tool 'end_turn' does not exist…", a
    dispatch exception, a bridge rejection) — or None when no such attempt exists. A call the
    executor itself answered (``accepted: false`` for an empty summary) is NOT an attempt
    here: that is the model's fault, judged by the ordinary REJECT branch. Window rule as
    ``tool_calls_this_turn``: the most recent user row (contract nudges included) closes it,
    so an attempt before a rejection nudge does not excuse a prose ending after it.
    """
    results: dict[str, Any] = {}
    for msg in reversed(messages or ()):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            break
        if role == "tool":
            results[str(msg.get("tool_call_id") or "")] = msg.get("content")
            continue
        if role != "assistant":
            continue
        for tc in reversed(msg.get("tool_calls") or ()):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if fn.get("name") != END_TURN_TOOL_NAME:
                continue
            result = results.get(str(tc.get("id") or ""))
            if _executor_answered(result):
                continue
            return {"arguments": _parse_args(fn.get("arguments")), "result": result}
    return None


def recover_close(attempt: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The payload the model wrote into a close the harness failed to run, or None when the
    arguments carry no summary. What it wrote for the user is still the best reply we have."""
    args = (attempt or {}).get("arguments")
    if not isinstance(args, dict):
        return None
    payload = normalize_payload(
        args.get("status"), args.get("summary"), args.get("open_items"), args.get("blocker"),
    )
    return payload if payload["summary"] else None


#: Decisions from the turn-close contract, used by the turn loop.
CLOSE = "close"                  # the model called end_turn: deliver its summary
REJECT = "reject"                # text-only ending after tool activity, no close attempted: re-prompt
ESCAPE = "escape"                # rejected as often as allowed: deliver, and log the escape
HARNESS_ERROR = "harness_error"  # the model called end_turn and the harness failed to run it: deliver
PASS = "pass"                    # nothing to enforce (no tool activity, or the contract is off)
CONTINUE = "continue"            # closed with work it can still do: hand the turn back
CONTINUE_EXHAUSTED = "continue_exhausted"  # same, ceiling spent: deliver, and count it
IDLE = "idle"                    # handed the turn back, then closed again without one tool call

#: A close that claims work remains, or claims done while carrying work, is not a stop.
STOP = "stop"
WORK_OPEN = "work_open"


def close_verdict(payload: dict[str, Any]) -> tuple[str, str]:
    """``(STOP|WORK_OPEN, reason)`` for a close payload.

    The discriminator is the OWNER field the model commits to, not a reading of its prose:

    - any ``owner="agent"`` item is work the model can do → never a stop, whatever the status;
    - ``deferred`` asserts agent work remains → never a stop (with no items it names nothing);
    - an ``owner="user"`` item must say what it needs, or the claim is unusable;
    - ``blocked``/``need_input`` are terminal only with a user-owned item (or a legacy
      ``blocker``) — a terminal status with nothing the owner can act on is a bare stop;
    - ``done`` carrying only well-formed user-owned items is a legitimate status report.
    """
    status = str(payload.get("status") or "done")
    agent_items, user_items = split_items(payload.get("open_items") or [])
    blocker = str(payload.get("blocker") or "").strip()
    if agent_items:
        return WORK_OPEN, "agent_work_open"
    if status == "deferred":
        return WORK_OPEN, "deferred"
    if any(not item.get("needs") for item in user_items):
        return WORK_OPEN, "user_item_without_needs"
    if status in ("blocked", "need_input") and not (user_items or blocker):
        return WORK_OPEN, "terminal_without_blocker"
    return STOP, ""


def continuation_nudge(payload: dict[str, Any], reason: str, *, idle: bool = False) -> str:
    """The row that turns a declared work list into the turn's actual assignment.

    The items are echoed back numbered, because the failure being fixed is not that the model
    forgot them — it wrote them — but that nothing ever asked it to do them.
    """
    agent_items, user_items = split_items(payload.get("open_items") or [])
    status = str(payload.get("status") or "deferred")
    if idle:
        head = ("This close was refused: you were handed the turn back to act, and you closed again "
                "without running a single tool. Restating the plan is not the work.")
    elif reason == "deferred" and not agent_items:
        head = ("This close was refused: `deferred` says work remains but names nothing you can do, "
                "so there is nothing to continue and nothing to report.")
    elif reason == "user_item_without_needs":
        head = ("This close was refused: an `owner=\"user\"` item must say what you need from the owner "
                "in `needs`. A user-owned item with no need named is a stop with no ask in it.")
    elif reason == "terminal_without_blocker":
        head = (f"This close was refused: status `{status}` ends the turn, so it must carry either an "
                "`owner=\"user\"` item with `needs`, or a `blocker` naming what stops you. You named nothing.")
    elif not agent_items:
        head = (f"This close was refused: status `{status}` still carries work you own, and a turn does "
                "not end with work you can do.")
    else:
        head = (f"This close was refused: it carries {len(agent_items)} item(s) owned by you, and a turn "
                "does not end with work you can do.")
    lines = [head]
    if agent_items:
        lines += ["", "The work, in order:"] + [f"{i}. {item['item']}" for i, item in enumerate(agent_items, 1)]
        action = "Act on item 1 now, in this turn, with a real tool call — do not restate the plan."
    elif user_items:
        lines += ["", "Waiting on the owner:"] + [
            f"- {item['item']} (needs: {item.get('needs') or '—'})" for item in user_items
        ]
        action = ("If one of these is really yours to do, take it with a tool call. If it is the owner's, "
                  "re-close with `blocked` or `need_input` and say what you need.")
    else:
        action = "Name the next concrete step and take it with a tool call in this turn."
    lines += [
        "",
        action,
        "Close again as `done` only when no item is owned by you — user-owned items must each name what "
        "they need; as `blocked` / `need_input` when the rest is not yours to do.",
    ]
    return "\n".join(lines)


def contract_decision(
    *, payload, tool_calls_this_turn: int, rejections: int, close_attempted: bool = False,
    continuations: int = 0, max_continuations: int = MAX_TURN_CONTINUATIONS,
) -> str:
    """What the turn loop should do about this ending, given the contract is enabled.

    ``close_attempted`` is :func:`attempted_end_turn_close` as a bool: the model invoked the
    tool and got no payload out of it. That is a harness fault by definition — the tool is
    the harness's to run — so it is delivered and counted on its own, never as a rejection.

    Work-open closes are judged before the stop branch. A close reached with ZERO tool calls
    since the last hand-back is ``IDLE`` and spends the REJECTION budget (then ESCAPE): the
    bounce asks for a tool call, so re-closing without one is a refusal, not a continuation.
    A close that did act spends the continuation ceiling instead, so honest incremental work
    on a long list is never punished for taking several re-entries.
    """
    if payload is not None:
        verdict, _reason = close_verdict(payload)
        if verdict == STOP:
            return CLOSE
        if continuations > 0 and tool_calls_this_turn == 0:
            return IDLE if rejections < MAX_TURN_CONTRACT_REJECTIONS else ESCAPE
        if continuations < max(0, max_continuations):
            return CONTINUE
        return CONTINUE_EXHAUSTED
    if close_attempted:
        return HARNESS_ERROR
    if tool_calls_this_turn > 0:
        return REJECT if rejections < MAX_TURN_CONTRACT_REJECTIONS else ESCAPE
    return PASS


#: Where the contract's own counters live. ``escaped`` is the go/no-go metric — an escape
#: means the model refused the contract and the enforcement is being defeated, so the fix is
#: a model change, not another detector — and it is only meaningful because ``harness_error``
#: is counted separately: a close the harness could not run is the harness's failure, and
#: counting it as a rejection (as the first day did) makes the metric measure the harness.
#: Counted mechanically, never by impression.
STATS_PATH = Path.home() / ".hermes" / "state" / "turn_contract.json"


def record_turn_contract_event(kind: str) -> None:
    """Bump today's ``closed`` / ``rejected`` / ``escaped`` / ``harness_error`` counter and
    persist it.

    Never raises: a turn must not fail because bookkeeping did.
    """
    try:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            data = json.loads(STATS_PATH.read_text(encoding="utf-8-sig"))
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
        data = json.loads(STATS_PATH.read_text(encoding="utf-8-sig"))
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        return dict(data.get(today) or {})
    except Exception:
        return {}
