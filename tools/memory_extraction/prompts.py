"""Extraction prompts for Phase 2 auto-memory.

Prompt design philosophy:
  - Output is STRICT JSON. Parse failures are non-fatal — we drop the
    proposal silently rather than confusing the user.
  - Each prompt has a tight system message that primes the model on
    what counts as "memorable" for THIS user (Tanium support engineer,
    project context, single-user setup).
  - Few-shot examples are minimal — Sonnet/Haiku follow JSON schema
    instructions reliably without heavy priming.
  - Categories are free-form but suggested values are listed to keep
    them stable across extractions (preventing tag-soup explosion).
  - "Memorable" is bias-down: the right default is to extract NOTHING.
    Cost of a missed fact is low (it'll come up again); cost of noisy
    extractions is engineer fatigue at session-end confirm UI.

Inspired by mem0's prompt structure (system role + JSON output schema
+ minimal examples) but rewritten for our single-user / support-domain
context — we own this code.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Domain hints — tuned for Adam (TSE/PrEE) but generic enough for any user
# ---------------------------------------------------------------------------

DOMAIN_PRIMER = """\
You are extracting durable memory entries for an AI assistant that helps a
support engineer at a tech company. The user is working on:

- Tanium support cases (Salesforce, modules like TDS / Reporting / Connect / Comply)
- Hermes Agent — a long-running CLI / chat agent (their personal fork)
- Various MCPs, scripts, internal tooling

What COUNTS as memorable:
- New tooling discovered, new commands, new MCP/script paths
- API quirks, undocumented behavior, version-specific gotchas
- Project conventions ("for X, route through Y; never via Z")
- User preferences and corrections — STRONG signal: anything the user said
  to fix the assistant's behavior is HIGH priority
- Architecture facts about Tanium internals, Hermes internals, etc.
- "Solved problem" patterns the user might hit again

What DOES NOT count:
- Task progress / "we did X then Y"
- Summary of files read / commands run / outputs seen
- Outcomes of one-off investigations (those go to session_search)
- Restating what's already in well-known docs
- Anything the user is just thinking out loud about

DEFAULT to extracting NOTHING. Only propose entries when you have HIGH
confidence the fact will matter again. Empty list is a valid (and common)
answer.
"""


# Suggested category values. Free-form is allowed but consistency helps recall.
SUGGESTED_CATEGORIES = [
    "tanium",       # TDS, Reporting, Connect, Comply, etc.
    "hermes",       # Hermes Agent internals, fork drift, plugins
    "mcp",          # MCP servers, debugging, auth
    "salesforce",   # SF case workflow, time logging, Jira refs
    "preferences",  # user preferences / corrections
    "tooling",      # CLI tools, scripts, shell quirks
    "review",       # PR review patterns, git workflows
    "general",      # default fallback
]


# ---------------------------------------------------------------------------
# JSON output schema (shared across all extraction calls)
# ---------------------------------------------------------------------------

OUTPUT_SCHEMA_DOCS = """\
Output ONLY a JSON object with this exact shape:

{
  "entries": [
    {
      "content": "<the fact, stated declaratively, as a self-contained sentence>",
      "category": "<one of: tanium, hermes, mcp, salesforce, preferences, tooling, review, general>",
      "tags": "<comma-separated keywords, can be empty>",
      "rationale": "<1-line explanation of why this is memorable>"
    }
  ]
}

Rules:
- "entries" can be an empty list. EMPTY IS THE RIGHT ANSWER MOST OF THE TIME.
- Do NOT include any prose before or after the JSON.
- Do NOT use markdown code fences.
- Each entry's "content" must be a complete declarative fact (not a question,
  not a bullet point, not a fragment). Aim for 1-3 sentences.
- Maximum 5 entries per response. If you have more, pick the highest-value 5.
"""


# ---------------------------------------------------------------------------
# Per-turn extraction (smallest context: one user/assistant exchange)
# ---------------------------------------------------------------------------

PER_TURN_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: read a single user/assistant exchange and propose 0-5
memory entries that are worth storing for future sessions.

{OUTPUT_SCHEMA_DOCS}

Be conservative. A typical exchange yields 0 entries. Only extract when the
user said something durable (a preference, a correction, a new fact) OR the
assistant discovered something durable (a tool path, an API quirk, a fix).
"""


def per_turn_user(user_msg: str, assistant_msg: str) -> str:
    """Build the user message for a per-turn extraction call."""
    return f"""User said:
{_truncate_for_extraction(user_msg, 4000)}

Assistant replied:
{_truncate_for_extraction(assistant_msg, 8000)}

Propose 0-5 memory entries. Output JSON only."""


# ---------------------------------------------------------------------------
# Pre-compression extraction (piggybacks on compression call)
# ---------------------------------------------------------------------------

PRE_COMPRESS_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: review a slice of conversation messages that are about to
be compressed and discarded. Identify any durable memory entries worth
preserving BEFORE they're lost. Be more aggressive than per-turn extraction —
this is the last chance to capture facts from this slice.

{OUTPUT_SCHEMA_DOCS}

Aim for 0-5 entries. Empty list is still valid if the slice was just
back-and-forth with no durable facts.
"""


def pre_compress_user(messages: List[Dict[str, Any]]) -> str:
    """Build the user message for pre-compression extraction."""
    body = _format_messages_for_review(messages, max_chars=20000)
    return f"""Conversation slice about to be compressed:

{body}

Propose 0-5 memory entries from this slice. Output JSON only."""


# ---------------------------------------------------------------------------
# Session-end extraction (final pass over post-compression remainder)
# ---------------------------------------------------------------------------

SESSION_END_SYSTEM = f"""{DOMAIN_PRIMER}

Your job RIGHT NOW: review the FINAL state of a conversation that just
ended, plus a buffer of memory entries already proposed during the session
(from per-turn and pre-compress hooks). Produce the FINAL deduplicated list
of entries to commit.

You MUST:
1. Drop entries from the buffer that turned out to be wrong/superseded by
   later turns in the conversation.
2. Add any new entries from the final conversation state that weren't
   captured by earlier hooks.
3. Merge near-duplicates from the buffer into single coherent entries.

{OUTPUT_SCHEMA_DOCS}

Final list should typically be 0-5 entries. Quality over quantity.
"""


def session_end_user(
    final_messages: List[Dict[str, Any]],
    buffered_entries: List[Dict[str, Any]],
) -> str:
    """Build the user message for session-end extraction."""
    body = _format_messages_for_review(final_messages, max_chars=30000)
    if buffered_entries:
        buffer_str = json.dumps(buffered_entries, indent=2, default=str)
    else:
        buffer_str = "[]"
    return f"""Final conversation state (post-compression):

{body}

Buffered proposals from earlier in the session:
{buffer_str}

Produce the final deduplicated list of memory entries to commit. Output JSON only."""


# ---------------------------------------------------------------------------
# Conflict classification — given a new entry + existing similar entries,
# classify the relationship
# ---------------------------------------------------------------------------

CONFLICT_SYSTEM = """\
You are a memory conflict resolver. Given a NEW proposed memory entry and a
list of EXISTING similar entries (retrieved by keyword search from the
user's memory store), classify the relationship.

Output ONLY a JSON object with this exact shape:

{
  "verdict": "DUPLICATE" | "REFINEMENT" | "CONTRADICTION" | "NEW",
  "matched_id": <fact_id or null>,
  "rationale": "<1-line explanation>",
  "merged_content": "<merged text, only when verdict='REFINEMENT'>"
}

Definitions:
- DUPLICATE: the new entry says nothing materially different from an
  existing one. Pick the closest match; we'll just bump its retrieval count.
- REFINEMENT: the new entry adds detail to an existing one (more specific
  paths, version info, edge cases). Provide ``merged_content`` that
  preserves all detail from both.
- CONTRADICTION: the new entry directly conflicts with an existing one
  (e.g. "TDS uses Badger" vs "TDS uses cdsdb column files"). The user
  needs to resolve.
- NEW: the new entry is genuinely new — no existing entry overlaps.

Be strict: prefer NEW unless the overlap is clear. False REFINEMENT/DUPLICATE
verdicts cause data loss.
"""


def conflict_user(
    new_content: str,
    existing: List[Dict[str, Any]],
) -> str:
    """Build the user message for conflict classification."""
    if not existing:
        return f"New entry:\n{new_content}\n\nNo existing matches. Verdict should be NEW."
    existing_str = "\n".join(
        f"  [id={e['fact_id']}] {e['content']}"
        for e in existing
    )
    return f"""New proposed entry:
{new_content}

Existing similar entries:
{existing_str}

Classify the relationship. Output JSON only."""


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

# Match a top-level JSON object even if the model wraps it in code fences
# or chats around it. Greedy match the outer braces.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_extraction_response(text: str) -> List[Dict[str, Any]]:
    """Parse an extraction response into a list of entry dicts.

    Returns an empty list on any parse failure — extraction is best-effort.
    """
    if not text or not text.strip():
        return []
    obj = _extract_json_object(text)
    if obj is None:
        return []
    raw_entries = obj.get("entries", [])
    if not isinstance(raw_entries, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        content = (raw.get("content") or "").strip()
        if not content or len(content) < 10:
            continue
        cleaned.append({
            "content": content,
            "category": (raw.get("category") or "general").strip().lower(),
            "tags": (raw.get("tags") or "").strip(),
            "rationale": (raw.get("rationale") or "").strip(),
        })
    return cleaned[:5]  # hard cap


def parse_conflict_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse a conflict-classification response. Returns None on failure."""
    if not text or not text.strip():
        return None
    obj = _extract_json_object(text)
    if obj is None:
        return None
    verdict = (obj.get("verdict") or "").strip().upper()
    if verdict not in ("DUPLICATE", "REFINEMENT", "CONTRADICTION", "NEW"):
        return None
    return {
        "verdict": verdict,
        "matched_id": obj.get("matched_id"),
        "rationale": (obj.get("rationale") or "").strip(),
        "merged_content": (obj.get("merged_content") or "").strip() or None,
    }


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Pull a top-level JSON object out of free-form text, tolerating fences."""
    # Strip code fences first
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence line
        cleaned = "\n".join(cleaned.splitlines()[1:])
        # Drop the closing fence
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    # Try direct parse
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Fall back to regex extraction
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


# ---------------------------------------------------------------------------
# Message-formatting helpers
# ---------------------------------------------------------------------------

def _truncate_for_extraction(text: Any, max_chars: int) -> str:
    """Coerce text-ish content to a string and truncate if too long."""
    if text is None:
        return ""
    if isinstance(text, str):
        s = text
    elif isinstance(text, list):
        # OpenAI message content can be a list of {type, text} blocks
        parts = []
        for block in text:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif "content" in block:
                    parts.append(str(block.get("content") or ""))
            else:
                parts.append(str(block))
        s = "\n".join(parts)
    else:
        s = str(text)
    if len(s) <= max_chars:
        return s
    head = s[: max_chars // 2]
    tail = s[-max_chars // 2 :]
    return f"{head}\n[... {len(s) - max_chars} chars elided ...]\n{tail}"


def _format_messages_for_review(
    messages: List[Dict[str, Any]],
    max_chars: int = 20000,
) -> str:
    """Format a message list for inclusion in an extraction prompt.

    Trims to last messages that fit within max_chars. Drops tool messages
    (they're noisy and rarely contain durable facts; tool RESULTS that the
    assistant cites are already in the assistant's own text).
    """
    out: List[str] = []
    total = 0
    # Iterate in reverse so we keep the LATEST messages within budget
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "tool":
            continue
        content = _truncate_for_extraction(msg.get("content"), 2000)
        if not content.strip():
            continue
        block = f"--- {role} ---\n{content}"
        if total + len(block) > max_chars:
            break
        out.insert(0, block)
        total += len(block) + 4
    return "\n\n".join(out)
