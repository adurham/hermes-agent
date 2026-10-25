"""Promise-without-action gate.

Some models — most visibly deepseek-v4.1-flash on ollama-cloud — end a turn by
announcing work instead of performing it: "I'll build the diagnostic mode now",
"building the console page next", "let me add the gate". The operator then sees a
commitment rather than a result, and nothing was dispatched. This is endemic enough
to be a model behaviour, not a one-off lapse, so it is gated in the turn loop rather
than left to per-turn discipline.

Two shapes are gated:

* ``turn`` — the turn made no tool call and the reply is a future-tense work
  commitment. Whole-body scan: nothing ran, so any promise in it is dangling.
* ``tail`` — the turn DID use tools, but the reply's last prose paragraph ends on a
  commitment ("I'll build that offline rather than keep firing"). The earlier calls
  were earlier steps; nothing ran after the promise, so by position it is unfulfilled.
  Tail-only, because a long analysis legitimately says "I'll" mid-reply, and never
  when that final paragraph is followed by a delivered fenced block.

A third shape rides on the tail path: the present-progressive announce
("I'm dispatching it now: take the table, rank the rest…") followed by a closing
paragraph of commentary. Future tense is absent there, and the commitment is one
paragraph above the last one, so both the sentence and the paragraph tests miss it.
A first-person present-progressive work verb anchored on "now" is an action the model
claims is already underway; when the turn ends there, by construction nothing was
dispatched. That shape is read across the closing TWO paragraphs.

Narrow by construction: one retry per turn (shared by both shapes), the commitment
must sit in the final prose paragraph, and an explicit external blocker ("blocked",
"waiting on", "needs you to") is a stated blocker, not a promise.
"""

from __future__ import annotations

import re

#: Appended as an ephemeral user row when the gate fires; never persisted.
PROMISE_GATE_NUDGE = (
    "Your previous message promised work — \"{excerpt}\" — but this turn made no tool call, "
    "so nothing was actually done. Do it now in this turn with a real tool call. If it cannot "
    "be done now, say plainly what blocks it. Do not restate the intention."
)

#: Same re-prompt for a turn that DID act and then ended by promising the next piece.
PROMISE_GATE_TAIL_NUDGE = (
    "Your previous message ended by promising work — \"{excerpt}\" — and then stopped: "
    "nothing in this turn carried it out. Do it now, in THIS turn, with a real tool call; "
    "a later turn or \"offline\" is not an option. If it cannot be done now, say in one line "
    "what blocks it. Do not restate the intention."
)

#: Gate kinds, also used as the log/telemetry discriminator.
GATE_TURN = "turn"
GATE_TAIL = "tail"

#: Cap on re-prompts per turn, shared by both shapes. The loop also stops nudging once a
#: tool call appears.
MAX_PROMISE_GATE_RETRIES = 1

#: First-person future constructions — the shape of the promise, with NO verb whitelist.
#: A verb list cannot be complete: the recorded misses walked straight past it ("I'll work that
#: next while you play", "That's what I'll go after next", "I'll pin that down from the
#: disassembly"). What is bounded is the *grammar* of a promise, not its vocabulary.
_FUTURE = r"(?:i['’]ll|i will|i['’]m going to|i am going to|we['’]ll|we will|let me)"

_PROMISE_RE = re.compile(r"\b" + _FUTURE + r"\b", re.IGNORECASE)

#: Present-progressive announcement: "I'm dispatching it now", "I am launching it now". The verb
#: is deliberately unconstrained; the sentence-bounded "now" is the anchor.
_ACT_NOW_RE = re.compile(
    r"\b(?:i['’]m|i am)\s+(?:now\s+)?[a-z]+ing\b[^.!?\n]{0,50}?\bnow\b",
    re.IGNORECASE,
)

#: Bare announcements that carry no future tense but still promise work.
_BARE_RE = re.compile(
    r"^\W*(?:building now|working on it now|doing it now|on it now|starting now)\W*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Commitments that promise nothing to do: commentary about the message itself.
_META_RE = re.compile(
    r"\b(?:i['’]ll|i will|let me)\s+(?:be|say|note|keep|admit|repeat|clarify|emphasi[sz]e|mention|"
    r"flag that|add that|call it|know)\b",
    re.IGNORECASE,
)

#: A clause introduced by a condition is not a promise — but only when the condition precedes
#: it. "I'll build the chain once you confirm" stays gated on purpose (a soft deferral is the
#: exact dodge this gate rejects); "once the run lands, I'll report" is a stated dependency.
_CONDITIONAL_RE = re.compile(
    r"\b(?:if|unless|once|when|whenever|until|after|before|whether|in case|as soon as)\b",
    re.IGNORECASE,
)

#: Waiting on the world, not deferring work: the promised action cannot happen until something
#: outside the agent finishes ("I'll report when the run lands", "I'll fix it after this sweep").
#: Measured over three days of transcripts, this is the single largest family of false positives
#: — a nudge there just makes the model restate that it is waiting.
_BACKGROUND_CONDITION_RE = re.compile(
    r"\b(?:when|once|as soon as|the moment|after|until)\b[^.!?\n]{0,60}?"
    r"\b(?:lands?|finished|finishes|completes?|reports?|returns?|arrives?|fires?|asks?|"
    r"is done|are done|comes back|sweep|build|run|batch|job|worker|subagent|analyst)\b",
    re.IGNORECASE,
)

#: A trailing "if" is a conditional statement about the future, not a deferral — unless it is
#: the operator being asked ("if you want", "if you'd like"), which stays gated.
_USER_CONDITION_RE = re.compile(r"\b(?:if|once|when|unless)\s+(?:you|you['’]d|you['’]ll|your)\b", re.I)

#: Something else is already running, so there is nothing for this turn to start.
_STILL_RUNNING_RE = re.compile(r"\bstill (?:working|running|grinding|going|building)\b", re.I)

#: A promise conjoined to a clause that tells the USER to do something first ("Restart the app
#: and I'll run exactly that", "Pair it and I'll report the fetch") is contingent on the operator,
#: not dangling work. Suppressed only when the clause BEFORE the conjunction is not itself a
#: first-person commitment ("I'll build it, and I'll test it" stays gated).
_IMPERATIVE_CONJUNCTION_RE = re.compile(r"\band\s*$", re.IGNORECASE)
_FIRST_PERSON_HEAD_RE = re.compile(r"^(?:i\b|i['’]|we\b|let me\b)", re.IGNORECASE)

#: A sentence that OPENS with an instruction is addressed to the operator ("Restart and the
#: oracle run goes first, then I'll add …"). Its tail is contingent on that instruction, so the
#: whole sentence is skipped rather than parsed for a commitment.
_USER_IMPERATIVE_HEAD_RE = re.compile(
    r"^\W*(?:restart|relaunch|reload|tap|click|pair|plug|insert|open|close|create|send|tell|"
    r"give|delete|remove|press|connect|unplug|power|reboot|launch|try|check|look|set|put)\b",
    re.IGNORECASE,
)

#: A tail commitment naming a dependency outside the agent's control is a stated blocker,
#: not a promise: nudging it only costs a turn. Deliberately does NOT include soft dodges
#: ("if you want", "once you've", "your call", "let me know") — those are exactly the
#: deferral this gate exists to reject.
_BLOCKER_RE = re.compile(
    r"\b(?:blocked|can't|cannot|unable to|waiting (?:on|for)|needs you|needs the|"
    r"need the|you'll need|requires your|until you)\b",
    re.IGNORECASE,
)

_FENCE = ("```", "~~~")


def _prose_after_last_fence(text: str) -> str:
    """The prose that follows the last *closed* fenced block.

    Anything above a closing fence was followed by delivered output, so a commitment
    there is backed: "I'll write the patch:" + a fenced patch must never be gated. A
    message that ends inside a fence has no prose tail at all.
    """
    lines = (text or "").splitlines()
    last_close = -1
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_FENCE):
            if in_fence:
                last_close = i
            in_fence = not in_fence
    if in_fence:
        # The reply ends INSIDE a fenced block (truncated output, a half-written snippet):
        # the last thing said is code, not prose. A comment there ("# I'll build that later")
        # is code, not a commitment.
        return ""
    if last_close < 0:
        return text or ""
    return "\n".join(lines[last_close + 1:])


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


#: How many closing prose paragraphs the commitment scan reads. One is not enough: the
#: announcement is routinely followed by a closing paragraph of commentary ("The next launch
#: then tests one named hypothesis…"), which is what the recorded 2026-10-25 miss looked like.
#: Still bounded, so an intention stated earlier in a long analysis stays untouched.
_CLOSING_PARAGRAPHS = 2


def _commitment_in_sentence(sentence: str) -> bool:
    """True when *sentence* commits the speaker to work, judged by grammar not vocabulary.

    Excluded, each for a measured reason:

    * meta commentary ("I'll be honest", "I'll say this plainly") — no work in it;
    * a condition BEFORE the commitment ("once the run lands, I'll report") — a stated
      dependency. A condition AFTER it ("I'll build it once you confirm") stays gated: that
      is the soft deferral this gate exists to reject;
    * a commitment conjoined to a user instruction ("Restart the app and I'll run exactly
      that") — contingent on the operator, not dangling work;
    * a promise waiting on something outside the agent ("I'll report when the run lands",
      "I'll fix it after this sweep") — the action cannot happen yet.
    """
    if _USER_IMPERATIVE_HEAD_RE.match(sentence):
        return False
    match = _PROMISE_RE.search(sentence) or _ACT_NOW_RE.search(sentence)
    if not match:
        return False
    # A present-progressive announce only counts at the head of a sentence: "What I'm reading:
    # … as of now" is a list heading, not a commitment.
    if not _PROMISE_RE.search(sentence) and match.start() > 0:
        return False
    if _META_RE.search(sentence):
        return False
    prefix = sentence[: match.start()]
    tail = sentence[match.end() :]
    if _CONDITIONAL_RE.search(prefix):
        return False
    if _IMPERATIVE_CONJUNCTION_RE.search(prefix.strip()) and not _FIRST_PERSON_HEAD_RE.match(
        prefix.strip()
    ):
        return False
    if _USER_CONDITION_RE.search(tail):
        return True
    if _BACKGROUND_CONDITION_RE.search(tail):
        return False
    return True


def tail_commitment(text: str | None) -> str | None:
    """The commitment sentence a reply ENDS on, or None.

    Only the closing prose paragraphs decide, so a long reply that mentions an intention
    mid-analysis (or before a delivered code block) is untouched. The turn must not end on a
    question, and a tail naming an external blocker is a stated blocker, not a promise.
    """
    if not text:
        return None
    paragraphs = _paragraphs(_prose_after_last_fence(text))
    if not paragraphs:
        return None
    if _sentences(paragraphs[-1]) and _sentences(paragraphs[-1])[-1].rstrip().endswith("?"):
        return None
    for paragraph in reversed(paragraphs[-_CLOSING_PARAGRAPHS:]):
        if _BLOCKER_RE.search(paragraph) or _STILL_RUNNING_RE.search(paragraph):
            continue
        for sentence in _sentences(paragraph):
            if sentence.rstrip().endswith("?"):
                continue
            if _commitment_in_sentence(sentence):
                return sentence
    return None


def _trim(sentence: str, limit: int) -> str:
    if len(sentence) > limit:
        return sentence[: limit - 1].rstrip() + "…"
    return sentence


def looks_like_promise(text: str | None) -> bool:
    """True when *text* commits the speaker to work it has not done.

    Scanned sentence by sentence through the same grammar test the tail path uses, so the
    suppressors (meta commentary, preceding condition, user-instruction conjunction) apply
    here too. Completed-work summaries ("I built X", "the gate is in") never match.
    """
    if not text:
        return False
    body = text.strip()
    if not body:
        return False
    if _BARE_RE.search(body):
        return True
    return any(_commitment_in_sentence(sentence) for sentence in _sentences(body))


def _sentence_containing(text: str, offset: int) -> str:
    """The sentence of *text* that spans character *offset* (used for the nudge excerpt)."""
    start = 0
    for m in re.finditer(r"[.!?…]\s+|\n", text[:offset]):
        start = m.end()
    end = len(text)
    for m in re.finditer(r"[.!?…](?=\s|$)", text[offset:]):
        end = offset + m.end()
        break
    return text[start:end].strip()


def promise_excerpt(text: str | None, limit: int = 120) -> str:
    """The matched promise, trimmed for the nudge text (empty when no match)."""
    if not text:
        return ""
    tail = tail_commitment(text)
    if tail:
        return _trim(tail, limit)
    body = text.strip()
    match = _PROMISE_RE.search(body) or _BARE_RE.search(body)
    if not match:
        return ""
    return _trim(_sentence_containing(body, match.start()), limit)


def gate_kind(
    *,
    final_text: str | None,
    turn_tool_call_count: int,
    retries: int,
    finish_reason: str | None = None,
) -> str | None:
    """Which promise shape (if any) should re-prompt instead of ending the turn.

    ``turn_tool_call_count`` must be scoped to THIS turn (rows after the last user row).
    A session-wide count makes the zero-tool-call branch unreachable in any session that
    ever ran a tool — the bug that let 5-probe-then-promise turns through untouched.
    """
    if retries >= MAX_PROMISE_GATE_RETRIES:
        return None
    if finish_reason not in (None, "stop", "length"):
        return None
    if turn_tool_call_count > 0:
        return GATE_TAIL if tail_commitment(final_text) else None
    return GATE_TURN if looks_like_promise(final_text) else None


def should_gate(
    *,
    final_text: str | None,
    turn_tool_call_count: int,
    retries: int,
    finish_reason: str | None = None,
) -> bool:
    """Whether to re-prompt instead of ending the turn (see :func:`gate_kind`)."""
    return gate_kind(
        final_text=final_text,
        turn_tool_call_count=turn_tool_call_count,
        retries=retries,
        finish_reason=finish_reason,
    ) is not None
