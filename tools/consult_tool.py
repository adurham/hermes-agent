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

import difflib
import json
from typing import Optional


# Hard caps so a runaway question/context can't blow the aux call's context
# window or turn one "consult" call into a de-facto full-transcript replay.
MAX_QUESTION_CHARS = 4000
MAX_CONTEXT_CHARS = 40000

# Chat-template sentinel (U+FF5C fullwidth bar) as used in DSML tool-call
# markup (``<｜DSML｜invoke ...>``). A consult answer carrying this is leaked
# template/control text from the aux model, not an opinion.
_DSML_SENTINEL = "｜DSML｜"

# Markers the calling MAIN model sometimes writes verbatim into `question`/
# `context` instead of the real content -- e.g. "(1) foo... (2) onward and
# the actual baseline numbers were cut off[truncated]" or "see above for
# details". This is elision/laziness in the tool-call generation itself, not
# a length-cap truncation: both fields are well under MAX_QUESTION_CHARS /
# MAX_CONTEXT_CHARS when this happens (observed 2026-08-22 on claude-sonnet-5
# -- a >2000-char context was generated with real early options followed by
# a literal "...[truncated]" placeholder for options 2-5 and the numbers,
# then the reference model correctly reported it couldn't see the missing
# material, forcing an immediate wasted resend). Catching this BEFORE the
# aux call fires saves a real (often 25-60s, non-trivial-cost) reference
# model round trip on input the model already knows is incomplete.
_ELISION_MARKERS = (
    "[truncated]",
    "...[truncated]",
    "…[truncated]",
    "(truncated)",
    "[cut off]",
    "(cut off for length)",
    "[rest omitted]",
    "(rest omitted)",
    "[continued]",
    "(see above)",
    "(see full context above)",
    "...(omitted)",
    "[omitted for brevity]",
)


def _elided_request_reason(question: str, context: str) -> Optional[str]:
    """Return a reason string when *question*/*context* contain a literal
    elision placeholder the calling model wrote instead of real content.

    This is a pre-flight guard on the OUTGOING request (contrast with
    ``_degenerate_answer_reason``, which checks the incoming answer). Both
    fields are already known-short at this point (post length-cap
    truncation above), so a marker found here is the calling model's own
    laziness, not a genuine size overflow.
    """
    combined_lower = f"{question}\n{context}".lower()
    for marker in _ELISION_MARKERS:
        if marker in combined_lower:
            return (
                f"the request text itself contains a literal placeholder "
                f"({marker!r}) instead of real content"
            )
    return None


def _degenerate_answer_reason(
    answer: str, question: str, context: str
) -> Optional[str]:
    """Return a reason string when *answer* is template garbage or an echo
    of the request, else None.

    Observed failure (2026-07-09): a local aux model returned the consult
    REQUEST itself, wrapped in raw DSML tool-call markup, as its "answer";
    the calling agent then paraphrased its own words as the reference
    model's opinion. Both failure shapes are cheap to detect:

      * leaked tool-call/template markup — the DSML sentinel with tool-call
        structure around it, or the answer *opening* with a control tag;
      * echo — the bulk of the answer is one contiguous block of the
        request text.
    """
    stripped = answer.strip()
    if _DSML_SENTINEL in answer and (
        "tool_calls" in answer or "invoke name=" in answer
        or "parameter name=" in answer
    ):
        return "leaked tool-call markup (DSML block) instead of an answer"
    if stripped.startswith(("<｜", "<|im_", "<|start")):
        return "leaked chat-template control tokens instead of an answer"

    request = " ".join(f"{question} {context}".split())
    normalized = " ".join(stripped.split())
    if len(normalized) >= 120 and request:
        match = difflib.SequenceMatcher(
            None, normalized, request, autojunk=False
        ).find_longest_match(0, len(normalized), 0, len(request))
        if match.size / len(normalized) > 0.7:
            return "echoed the consult request back instead of answering it"
    return None

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


def consult_tool(
    question: str,
    context: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
) -> str:
    """
    Ask a configured reference model (``auxiliary.consult``) for a second
    opinion on a specific question, optionally with supporting context.

    Args:
        question: The specific judgment call to get a second opinion on.
        context:  Optional supporting material (code, plan, diff, reasoning
                  trace) the reviewer needs to judge the question.
        model:    Optional override for the reference model, e.g. when the
                  configured ``auxiliary.consult`` model/provider is
                  rate-limited or out of credits and a different model
                  should be asked instead just for this call. Passed
                  straight through to ``call_llm``, which always prefers an
                  explicit ``model``/``provider`` argument over the
                  ``auxiliary.consult`` config (see
                  ``_resolve_task_provider_model``'s priority order) --
                  config is untouched, this is a one-call override only.
        provider: Optional provider override paired with ``model`` (e.g.
                  ``"anthropic"``, ``"ollama-cloud"``). Only meaningful when
                  ``model`` is also given; a bare provider override with no
                  model still uses the configured ``auxiliary.consult.model``
                  for that provider (or that provider's default consult model
                  when no consult model is configured).
        effort:   Optional one-call reasoning-effort override (e.g. "low",
                  "high", "max", or "none"/"off" to disable thinking for
                  this call). Parsed with the same ``parse_reasoning_effort``
                  helper used for ``auxiliary.<provider>.consult.
                  reasoning_effort`` in config.yaml, and takes priority over
                  that config value for this call only -- config is
                  untouched. An unparseable value is ignored (logged) and
                  the configured/default effort is used instead, same as an
                  invalid config value would be.

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
    context = (context or "").strip()

    # Guard the OUTGOING request before the length-cap truncation below
    # (which appends its own "...(truncated)" marker and would otherwise
    # self-trigger this check). See _elided_request_reason's docstring:
    # this catches the calling model writing a literal placeholder like
    # "...[truncated]" or "(see above)" into question/context in place of
    # the real content it was supposed to include -- fails fast instead of
    # spending a real reference-model call on input known to be incomplete.
    _elision = _elided_request_reason(question, context)
    if _elision:
        return json.dumps(
            {
                "unavailable": True,
                "answer": None,
                "reason": (
                    f"Consult request rejected before calling the reference "
                    f"model: {_elision}. Write the FULL question/context "
                    f"text out -- do not summarize with a placeholder like "
                    f"\"[truncated]\" or \"(see above)\"; the reviewer has no "
                    f"access to anything outside these two arguments. Retry "
                    f"with the complete text (trim genuinely unnecessary "
                    f"detail instead of eliding it)."
                ),
            },
            ensure_ascii=False,
        )

    if len(question) > MAX_QUESTION_CHARS:
        question = question[:MAX_QUESTION_CHARS] + "...(truncated)"

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

    reasoning_config = None
    if effort is not None and str(effort).strip():
        from hermes_constants import parse_reasoning_effort

        reasoning_config = parse_reasoning_effort(effort)
        if reasoning_config is None:
            import logging

            logging.getLogger(__name__).warning(
                "consult_tool: effort=%r is not a valid level (none, "
                "minimal, low, medium, high, xhigh, max, ultra) -- "
                "ignoring, falling back to configured/default effort.",
                effort,
            )

    try:
        response = call_llm(
            task="consult",
            provider=provider,
            model=model,
            messages=messages,
            max_tokens=2000,
            reasoning_config=reasoning_config,
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

    degenerate = _degenerate_answer_reason(answer, question, context)
    if degenerate:
        return json.dumps(
            {
                "unavailable": True,
                "answer": None,
                "reason": (
                    f"The consult model's response was unusable: {degenerate}. "
                    "Treat this as NO second opinion -- do not paraphrase, "
                    "reconstruct, or attribute an answer to the reference "
                    "model. Proceed on your own judgment, and tell the user "
                    "the consult failed if they asked for it."
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
        "The reference model defaults to `auxiliary.consult` in "
        "config.yaml (e.g. Claude Fable 5) and may occasionally decline to "
        "answer -- safety refusal, empty response, or timeout are all "
        "expected outcomes, not errors. When that happens this tool returns "
        "unavailable=true with a reason; proceed using your own judgment "
        "instead of retrying immediately with the same model -- if the "
        "reason indicates a rate limit or exhausted credits (not a genuine "
        "refusal), retry ONCE with an explicit `model`/`provider` override "
        "naming a different model before giving up on getting a second "
        "opinion.\n\n"
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
                    "a vague answer. Write it out in full: the reviewer sees "
                    "ONLY this string and `context`, nothing else from this "
                    "conversation. Never write a placeholder like "
                    "\"[truncated]\", \"(see above)\", or \"...\" in place of "
                    "real content -- there is no \"above\" for the reviewer "
                    "to see, so that produces a broken, wasted call."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Relevant background the reviewer needs to judge the "
                    "question: code, a plan, a diff, a reasoning trace, or "
                    "key facts. Keep it focused -- trim to what's actually "
                    "relevant rather than pasting the whole conversation. "
                    "'Trim' means leave material OUT entirely, never means "
                    "replace it with a placeholder like \"[truncated]\" or "
                    "\"(cut off for length)\" -- the reviewer has no other "
                    "source to fill the gap from, so a placeholder produces "
                    "an unusable request that fails before the model is even "
                    "called."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional: override the configured auxiliary.consult "
                    "reference model for this ONE call, e.g. "
                    "\"claude-opus-4-7\", \"deepseek-v4-pro\", "
                    "\"gemini-3-pro\". Use this when the default consult "
                    "model (usually Claude Fable 5) is rate-limited or its "
                    "credits/quota are exhausted -- the reason string will "
                    "say so explicitly. Config is untouched; this only "
                    "affects the current call. Pair with `provider` when "
                    "the model lives on a specific backend (e.g. "
                    "provider=\"ollama-cloud\" for a locally-hosted frontier "
                    "model)."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional: provider to use with `model` (e.g. "
                    "\"anthropic\", \"ollama-cloud\", \"openrouter\"). Only "
                    "meaningful together with `model` -- a bare provider "
                    "override with no model still uses the configured "
                    "auxiliary.consult model for that provider (or that "
                    "provider's default consult model when no consult model "
                    "is configured)."
                ),
            },
            "effort": {
                "type": "string",
                "description": (
                    "Optional: reasoning-effort override for this ONE call "
                    "(e.g. \"low\", \"medium\", \"high\", \"max\", or "
                    "\"none\"/\"off\" to disable thinking). Takes priority "
                    "over the configured auxiliary.<provider>.consult."
                    "reasoning_effort for this call only -- config is "
                    "untouched. Use this to save latency/cost with a lower "
                    "effort on a simple sanity check, or force max effort "
                    "on a genuinely hard judgment call regardless of the "
                    "configured default."
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
        model=args.get("model"),
        provider=args.get("provider"),
        effort=args.get("effort"),
    ),
    check_fn=check_consult_requirements,
    emoji="🧭",
)
