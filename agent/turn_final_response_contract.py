"""The turn-close contract's verdict on a text-only ending: the loop-side half of
``agent/end_turn.py``, split out of ``finish_text_response``.

A turn that used a tool may not end on prose: the model must call ``end_turn`` and put the
reply in its summary. This replaces detection with a structural rule — the failure
("announced work, ran nothing") is impossible to express without lying in a structured,
parseable field. See the ``end_turn`` module docstring for the measurements that led here,
for why the promise gate is disabled under it, and for why a close the harness failed to run
is its own outcome and never a rejection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from agent.end_turn import (
    CLOSE,
    CONTINUE,
    CONTINUE_EXHAUSTED,
    ESCAPE,
    HARNESS_ERROR,
    IDLE,
    MAX_TURN_CONTRACT_REJECTIONS,
    REJECT,
    TURN_CONTRACT_NUDGE,
    attempted_end_turn_close,
    carry_open_items,
    clear_end_turn,
    close_verdict,
    continuation_nudge,
    contract_decision,
    end_turn_payload,
    record_turn_contract_event,
    recover_close,
    render_summary,
    split_items,
    turn_continuation_budget,
    turn_contract_enabled,
)
from agent.message_metadata import append_message

# The loop's logger: the ``turn-contract:`` lines are read by name (FORK.md, 2026-10-25).
logger = logging.getLogger("agent.conversation_loop")


@dataclass
class ContractVerdict:
    """``enabled``: the contract is live for this agent (the promise gate stays off under it).
    ``reprompt``: REJECT — the nudge rows are already appended; the loop continues with no
    final response. ``final_response``: the reply to deliver in place of the prose (a CLOSE,
    or a HARNESS_ERROR whose call carried a summary); None keeps the prose."""

    enabled: bool
    decision: Optional[str] = None
    reprompt: bool = False
    final_response: Optional[str] = None


def apply_turn_contract(
    agent: Any, *, messages: list, final_msg: dict, tool_calls_this_turn: int,
) -> ContractVerdict:
    """Judge a text-only ending under the contract and do what the verdict requires:
    deliver a close, deliver and record a harness fault, append the rejection nudge, or
    deliver and record an escape. Bookkeeping (counters, rejection budget, payload reset)
    happens here so the loop only has to act on the verdict."""
    enabled = turn_contract_enabled(agent)
    if not enabled:
        return ContractVerdict(enabled=False)
    payload = end_turn_payload(agent)
    # A close the model attempted that produced no payload: the harness failed to run the
    # tool it demanded. Judged before REJECT so the counter only ever blames the model for
    # ending on prose, never for being blocked (2026-10-25: 31 "rejections" were this).
    attempt = attempted_end_turn_close(messages) if payload is None else None
    rejections = getattr(agent, "_turn_contract_rejections", 0)
    continuations = getattr(agent, "_turn_continuations", 0)
    decision = contract_decision(
        payload=payload, tool_calls_this_turn=tool_calls_this_turn, rejections=rejections,
        close_attempted=attempt is not None, continuations=continuations,
        max_continuations=turn_continuation_budget(agent),
    )
    if decision == CLOSE:
        # Closed properly: the payload IS the reply. Trailing prose, if any, is ignored —
        # the summary is what the model committed to and what the operator reads.
        reply = render_summary(payload or {})
        final_msg["content"] = reply
        logger.info(
            "turn-contract: closed[%s] open_items=%d tool_calls=%d (model=%s provider=%s)",
            (payload or {}).get("status"), len((payload or {}).get("open_items") or []),
            tool_calls_this_turn, agent.model, agent.provider,
        )
        clear_end_turn(agent)
        agent._turn_contract_rejections = 0
        agent._turn_continuations = 0
        record_turn_contract_event("closed")
        return ContractVerdict(True, CLOSE, final_response=reply)
    if decision in (CONTINUE, CONTINUE_EXHAUSTED):
        # The OWNER field is load-bearing: an agent-owned item is work the model can still do,
        # and `deferred` is the same claim in words. Either way the turn is handed back with
        # those items as the assignment, not delivered as a plan. Measured 2026-10-25: 28 of 28
        # such closes in three days were followed by a user row, none by work.
        _verdict, reason = close_verdict(payload or {})
        agent_items, user_items = split_items((payload or {}).get("open_items") or [])
        if decision == CONTINUE:
            agent._turn_continuations = continuations + 1
            record_turn_contract_event("continued")
            logger.warning(
                "turn-contract: CONTINUE[%s] — %d agent item(s), %d user item(s) after %d tool call(s); "
                "handing the turn back (continuation %d/%d, model=%s provider=%s)",
                reason, len(agent_items), len(user_items), tool_calls_this_turn, continuations + 1,
                turn_continuation_budget(agent), agent.model, agent.provider,
            )
            agent._emit_diagnostic_status(
                f"↻ continuing — {len(agent_items)} open, working item 1"
            )
            clear_end_turn(agent)
            final_msg["content"] = render_summary(payload or {})
            append_message(messages, final_msg)
            append_message(messages, {
                "role": "user",
                "content": continuation_nudge(payload or {}, reason),
                "_turn_contract_nudge": True,
            })
            return ContractVerdict(True, CONTINUE, reprompt=True)
        # Ceiling reached: deliver what there is, say so in the reply instead of letting an
        # undone list read like a plan, and carry the agent-owned items into the todo list so
        # the next turn can resume them instead of the operator having to retype them.
        carried = carry_open_items(agent, payload or {})
        reply = render_summary(
            payload or {}, open_label="Open (turn budget exhausted — still undone):",
        )
        final_msg["content"] = reply
        record_turn_contract_event("deferred_delivered")
        logger.error(
            "turn-contract: CONTINUE_EXHAUSTED[%s] — %d agent item(s) survived %d continuation(s); "
            "delivering, %d carried into the todo list (model=%s provider=%s)",
            reason, len(agent_items), continuations, carried, agent.model, agent.provider,
        )
        agent._emit_diagnostic_status(
            "⚠ Turn ended with work still open after the continuation ceiling — answer delivered"
        )
        clear_end_turn(agent)
        agent._turn_continuations = 0
        return ContractVerdict(True, CONTINUE_EXHAUSTED, final_response=reply)
    if decision == IDLE:
        # Handed the turn back to act, closed again with zero tool calls: the bounce asked for a
        # tool call, so this is a refusal to continue, not another continuation. It spends the
        # existing rejection budget; the next idle close escapes and is delivered.
        _verdict, reason = close_verdict(payload or {})
        agent._turn_contract_rejections = rejections + 1
        record_turn_contract_event("idle_continuation")
        logger.warning(
            "turn-contract: IDLE[%s] — closed with agent-owned work and no tool call since the "
            "hand-back (rejection %d/%d, model=%s provider=%s)",
            reason, rejections + 1, MAX_TURN_CONTRACT_REJECTIONS, agent.model, agent.provider,
        )
        agent._emit_diagnostic_status("↻ Closed without acting — requiring a tool call")
        clear_end_turn(agent)
        final_msg["content"] = render_summary(payload or {})
        append_message(messages, final_msg)
        append_message(messages, {
            "role": "user",
            "content": continuation_nudge(payload or {}, reason, idle=True),
            "_turn_contract_nudge": True,
        })
        return ContractVerdict(True, IDLE, reprompt=True)
    if decision == HARNESS_ERROR:
        # The model did what it was told and the harness could not run end_turn (dispatch
        # gate without the name, executor exception, bridge rejection). Deliver — the summary
        # it wrote into the call when there is one, else the prose — and record the fault
        # against the harness. Another round-trip would only re-run the failure.
        recovered = recover_close(attempt)
        reply = render_summary(recovered) if recovered is not None else None
        if reply is not None:
            final_msg["content"] = reply
        result_preview = str((attempt or {}).get("result") or "")[:200].replace("\n", " ")
        record_turn_contract_event("harness_error")
        logger.error(
            "turn-contract: HARNESS_ERROR — the model called end_turn and the harness failed "
            "to run it (result=%r); delivering the %s (tool_calls=%d model=%s provider=%s)",
            result_preview, "summary from the call" if reply is not None else "prose",
            tool_calls_this_turn, agent.model, agent.provider,
        )
        agent._emit_diagnostic_status(
            "⚠ end_turn was called but the harness could not run it — answer delivered"
        )
        clear_end_turn(agent)
        agent._turn_contract_rejections = 0
        return ContractVerdict(True, HARNESS_ERROR, final_response=reply)
    if decision == REJECT:
        agent._turn_contract_rejections = rejections + 1
        record_turn_contract_event("rejected")
        logger.warning(
            "turn-contract: rejected text-only ending after %d tool call(s) "
            "(rejection %d/%d, model=%s provider=%s)",
            tool_calls_this_turn, rejections + 1, MAX_TURN_CONTRACT_REJECTIONS,
            agent.model, agent.provider,
        )
        agent._emit_diagnostic_status(
            "↻ Turn used tools but ended on prose — requiring end_turn"
        )
        final_msg["_turn_contract_nudge"] = True
        append_message(messages, final_msg)
        append_message(messages, {
            "role": "user",
            "content": TURN_CONTRACT_NUDGE,
            "_turn_contract_nudge": True,
        })
        return ContractVerdict(True, REJECT, reprompt=True)
    if decision == ESCAPE:
        # The model refused the contract as often as allowed — with no close attempted, so
        # this really is the model. Deliver the prose (the operator must still get an answer)
        # and record it: an escape is the empirical trigger for changing models, not for
        # another detector.
        record_turn_contract_event("escaped")
        logger.error(
            "turn-contract: ESCAPE — text-only ending after %d tool call(s) survived %d "
            "rejections (model=%s provider=%s) — the model refused the contract; "
            "this is the signal to change models, not to add a detector",
            tool_calls_this_turn, rejections, agent.model, agent.provider,
        )
        agent._emit_diagnostic_status(
            "⚠ Model refused the turn contract — answer delivered unbacked"
        )
        agent._turn_contract_rejections = 0
    return ContractVerdict(True, decision)
