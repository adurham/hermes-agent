#!/usr/bin/env python3
"""
Consult Tool Module - Second Opinion From a Reference Model

Lets the agent (main or delegated subagent) ask a smarter/more capable
reference model for a second opinion on a specific, bounded question before
committing to something risky or uncertain. Routes through the shared
auxiliary LLM client (``auxiliary.consult`` in config.yaml) so the reference
model is configurable independently of the main chat model — the canonical
use case is pointing it at an expensive frontier model (e.g. Claude Fable 5)
that would be a bad MAIN model (slow, prone to over-cautious refusals) but is
a great second opinion for a narrow, well-scoped question.

Design notes:
  * This is a plain registry tool (no agent-loop state), same shape as
    ``vision_analyze`` — it just wraps ``agent.auxiliary_client.call_llm``.
  * Refusals / empty responses from the reference model are NOT exceptions.
    They're reported back as ``unavailable: true`` with a short reason so the
    calling agent can proceed on its own judgment instead of stalling or
    retrying in a loop. Fable-class models refuse often enough that this has
    to be a first-class, expected outcome, not an error path.
  * No automatic retry against a fallback model — a refusal here just means
    "no second opinion this time," and the calling agent already has its own
    judgment to fall back on.
"""

import json
from typing import Optional


# Hard caps so a runaway question/context can't blow the aux call's context
# window or turn one "consult" call into a de-facto full-transcript replay.
MAX_QUESTION_CHARS = 4000
MAX_CONTEXT_CHARS = 40000

_CONSULT_SYSTEM_PROMPT = (
    "You are acting as an independent second opinion for another AI agent that "
    "is mid-task. You are NOT the one doing the task -- you are being asked to "
    "sanity-check one specific decision, plan, piece of reasoning, or claim. "
    "Be direct and concise: call out concrete risks, errors, or blind spots if "
    "you see them, or say plainly that the approach looks sound if it does. Do "
    "not pad the response with disclaimers, caveats about not having full "
    "context, or restating the question back. If you genuinely don't have "
    "enough information to judge, say exactly what is missing rather than "
    "refusing outright or hedging everything."
)


def consult_tool(question: str, context: Optional[str] = None) -> str:
    """
    Ask a configured reference model (``auxiliary.consult``) for a second
    opinion on a specific question, optionally with supporting context.

    Args:
        question: The specific judgment call to get a second opinion on.
        context:  Optional supporting material (code, plan, diff, reasoning
                  trace) the reviewer needs to judge the question.

    Returns:
        JSON string. On success: ``{"unavailable": false, "answer": "..."}``.
        When the reference model refuses, returns empty, or the call fails:
        ``{"unavailable": true, "answer": null, "reason": "..."}`` -- this is
        an expected outcome, not an error; the caller should proceed on its
        own judgment rather than retry.
    """
    if not question or not question.strip():
        return tool_error("question is required.")

    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        question = question[:MAX_QUESTION_CHARS] + "...(truncated)"

    context = (context or "").strip()
    if context:
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS] + "\n...(truncated)"
        user_content = f"{question}\n\n---\nContext:\n{context}"
    else:
        user_content = question

    messages = [
        {"role": "system", "content": _CONSULT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    try:
        response = call_llm(
            task="consult",
            messages=messages,
            max_tokens=2000,
        )
    except Exception as exc:
        return json.dumps(
            {
                "unavailable": True,
                "answer": None,
                "reason": (
                    f"Consult call failed ({type(exc).__name__}: {exc}) -- "
                    "no second opinion available this time. Proceed using "
                    "your own judgment."
                ),
            },
            ensure_ascii=False,
        )

    finish_reason = None
    try:
        finish_reason = response.choices[0].finish_reason
    except Exception:
        pass

    try:
        answer = extract_content_or_reasoning(response)
    except Exception:
        answer = ""

    if not answer or not answer.strip() or finish_reason == "content_filter":
        return json.dumps(
            {
                "unavailable": True,
                "answer": None,
                "reason": (
                    "The consult model declined to answer or returned nothing "
                    "(safety refusal, filtered response, or empty output). "
                    "No second opinion available this time -- proceed using "
                    "your own judgment rather than retrying."
                ),
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {"unavailable": False, "answer": answer.strip()},
        ensure_ascii=False,
    )


def check_consult_requirements() -> bool:
    """Consult has no hard external requirement -- auxiliary 'auto' routing
    falls back through the same provider chain every other auxiliary task
    uses, so the tool is always usable even before auxiliary.consult is
    explicitly configured."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CONSULT_SCHEMA = {
    "name": "consult",
    "description": (
        "Get a second opinion from a smarter/more capable reference model "
        "before committing to a risky or uncertain decision. Use it to "
        "sanity-check your own reasoning, review a plan or diff for "
        "structural issues you might be tunnel-visioned past, or ask a "
        "pointed question you're not fully confident about. NOT for routine "
        "work -- this costs one full call to a (usually more expensive, "
        "sometimes slower) model, so use it sparingly and only for genuinely "
        "uncertain judgment calls.\n\n"
        "The reference model is configured via `auxiliary.consult` in "
        "config.yaml (e.g. Claude Fable 5) and may occasionally decline to "
        "answer -- safety refusal, empty response, or timeout are all "
        "expected outcomes, not errors. When that happens this tool returns "
        "unavailable=true with a reason; proceed using your own judgment "
        "instead of retrying.\n\n"
        "Available to both the main agent and delegated subagents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The specific question or judgment call you want a "
                    "second opinion on. Be precise -- a vague question gets "
                    "a vague answer."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Relevant background the reviewer needs to judge the "
                    "question: code, a plan, a diff, a reasoning trace, or "
                    "key facts. Keep it focused -- trim to what's actually "
                    "relevant rather than pasting the whole conversation."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="consult",
    toolset="consult",
    schema=CONSULT_SCHEMA,
    handler=lambda args, **kw: consult_tool(
        question=args.get("question", ""),
        context=args.get("context"),
    ),
    check_fn=check_consult_requirements,
    emoji="🧭",
)
