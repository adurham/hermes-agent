#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands."""

from __future__ import annotations

import json
from typing import List, Optional

from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


def _verdict_tag(record: dict) -> str:
    """``[! CONFLICT]``-style tag for a staged extraction proposal, '' for anything else.

    Only extraction proposals carry conflict metadata; the agent's own gated hot-tier
    writes (add/replace/remove/batch) have no verdict and render exactly as before.
    """
    payload = record.get("payload") or {}
    if payload.get("action") != "extraction_proposal":
        return ""
    verdict = ((payload.get("conflict") or {}).get("verdict") or "NEW").upper()
    return {"NEW": "[+ NEW]", "DUPLICATE": "[= DUPE]",
            "REFINEMENT": "[~ REFINE]", "CONTRADICTION": "[! CONFLICT]"}.get(verdict, f"[{verdict}]")


def _tier_label(record: dict) -> str:
    """``warm:<category>`` / ``hot:<target>`` for a proposal, '' for anything else."""
    payload = record.get("payload") or {}
    if payload.get("action") != "extraction_proposal":
        return ""
    if (payload.get("tier") or "warm").lower() == "hot":
        return f"[hot:{payload.get('target') or 'memory'}]"
    return f"[warm:{payload.get('category') or 'general'}]"


def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    has_proposal = False
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        # Verdict + tier are prepended (not appended) so a scan down the list reads
        # the risky ones — CONFLICT, and hot-tier entries that cost prompt budget
        # every turn — without having to read each summary to the end.
        marks = " ".join(m for m in (_verdict_tag(r), _tier_label(r)) if m)
        if marks:
            has_proposal = True
            marks += " "
        lines.append(f"  {r['id']}{tag}  {marks}{r.get('summary', '')}")
    lines.append("")
    lines.append(f"Apply: /{subsystem} approve <id>   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    if has_proposal:
        lines.append("Inspect a proposal: /memory show <id>   Revise before approving: "
                     "/memory edit <id> <new text>")
    return "\n".join(lines)


def handle_pending_subcommand(
    subsystem: str, args: List[str], *, memory_store=None, set_mode_fn=None) -> Optional[str]:
    """Dispatch a /memory or /skills write-approval subcommand.

    ``memory_store`` applies approved memory writes (CLI passes its live store; gateway a freshly
    loaded one); ``set_mode_fn`` persists the write_approval boolean. Returns text for the user,
    or None when the args are not a write-approval subcommand so the caller falls through to its
    other handling (e.g. /skills search).
    """
    if not args:
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)
    sub, rest = args[0].lower(), args[1:]
    if sub == "pending":
        return _fmt_pending_list(subsystem)
    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)
    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)
    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)
    if sub == "show" and subsystem == wa.MEMORY:
        return _show_memory(rest)
    if sub == "edit" and subsystem == wa.MEMORY:
        return _edit_memory(rest)
    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)
    return None  # not ours — caller handles


def _usage(subsystem: str) -> str:
    return f"Usage: /{subsystem} approve|reject <id>  (or 'all')"


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed, overwritten = 0, [], []
    for rec in targets:
        ok, msg, result = _apply_one(subsystem, rec, memory_store)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
            overwritten.extend(f"  {rec['id']}: {text}" for text in _replaced_entries(result))
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if overwritten:
        # A memory 'replace' overwrites the WHOLE matched entry (#117952); the approver
        # is the last person who can notice a clause went missing, so show what was lost.
        out.append("Overwrote entire entry (re-add anything you still need):")
        out.extend(overwritten)
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _replaced_entries(result: dict) -> List[str]:
    """Full text of every entry a memory replace overwrote, single-op or batch shape."""
    single = result.get("replaced_entry")
    batch = result.get("replaced_entries") or {}
    return ([single] if single else []) + [batch[k] for k in sorted(batch, key=int)]


def _apply_one(subsystem: str, rec, memory_store):
    """``(ok, error, result)`` — *result* is the applier's full payload (empty on exceptions)."""
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable", {}
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
        return bool(result.get("success")), result.get("error", ""), result
    except Exception as e:
        return False, str(e), {}


def _reject(subsystem: str, rest: List[str]) -> str:
    if not rest:
        return _usage(subsystem)
    target = rest[0]
    if target.lower() == "all":
        n = sum(1 for rec in wa.list_pending(subsystem) if wa.discard_pending(subsystem, rec["id"]))
        return f"Rejected {n} pending {subsystem} write(s)."
    if wa.discard_pending(subsystem, target):
        return f"Rejected pending {subsystem} write '{target}'."
    return f"No pending {subsystem} write with id '{target}'."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    return f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n\n" + wa.skill_pending_diff(rec)


def _shorten(text: str, width: int = 100) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def _show_memory(rest: List[str]) -> str:
    """``/memory show <id>`` — the full conflict view for a staged proposal.

    This is the review affordance the session-exit UI used to provide inline: the
    proposed text AND the existing fact it collides with, rendered together so a
    REFINEMENT/CONTRADICTION can be judged without going and looking the fact up.
    """
    if not rest:
        return "Usage: /memory show <id>"
    rec = wa.get_pending(wa.MEMORY, rest[0])
    if not rec:
        return f"No pending memory write with id '{rest[0]}'."
    payload = rec.get("payload") or {}
    if payload.get("action") != "extraction_proposal":
        return (f"# Pending memory write {rec['id']}\n\n"
                f"action:  {payload.get('action')}\n"
                f"target:  {payload.get('target', 'memory')}\n"
                f"summary: {rec.get('summary', '')}")

    conflict = payload.get("conflict") or {}
    verdict = (conflict.get("verdict") or "NEW").upper()
    out = [f"# Pending memory proposal {rec['id']}  {_verdict_tag(rec)} {_tier_label(rec)}", "",
           "proposed:", f"  {payload.get('content') or ''}"]
    if conflict.get("matched_content"):
        label = {"DUPLICATE": "duplicate of", "REFINEMENT": "refines",
                 "CONTRADICTION": "conflicts with"}.get(verdict, "matched")
        out += ["", f"{label} (fact {conflict.get('matched_id')}):",
                f"  {conflict['matched_content']}"]
    if conflict.get("merged_content"):
        out += ["", "merged result if approved:", f"  {conflict['merged_content']}"]
    # NEW verdicts still carry FTS5 near-misses: surfacing the closest one lets a
    # user catch near-duplicate accretion the classifier decided against.
    if verdict == "NEW" and conflict.get("candidates"):
        top = conflict["candidates"][0]
        if top.get("content"):
            out += ["", "similar existing fact:", f"  {_shorten(top['content'])}"]
    if conflict.get("rationale"):
        out += ["", f"classifier rationale: {conflict['rationale']}"]
    if payload.get("rationale"):
        out += [f"extraction rationale: {payload['rationale']}"]
    out += ["", f"Apply: /memory approve {rec['id']}   Reject: /memory reject {rec['id']}"]
    return "\n".join(out)


def _edit_memory(rest: List[str]) -> str:
    """``/memory edit <id> <new text>`` — revise a staged proposal before approving.

    The replacement text is re-classified against the warm store, because the old
    verdict described the OLD text: approving edited content under a stale
    REFINEMENT would merge it into a fact it may no longer refine. Re-classifying
    here (at explicit user request) does not violate the
    "never re-classify at approve time" invariant — that one protects a verdict the
    user already reviewed, and this text has never been reviewed under any verdict.
    """
    if len(rest) < 2:
        return "Usage: /memory edit <id> <new text>"
    pending_id, new_text = rest[0], " ".join(rest[1:]).strip()
    if not new_text:
        return "Usage: /memory edit <id> <new text>"
    rec = wa.get_pending(wa.MEMORY, pending_id)
    if not rec:
        return f"No pending memory write with id '{pending_id}'."
    payload = rec.get("payload") or {}
    if payload.get("action") != "extraction_proposal":
        return (f"'{pending_id}' is a staged {payload.get('action')} write, not an extraction "
                "proposal — approve or reject it as-is.")

    payload["content"] = new_text
    note = ""
    try:
        from tools.memory_extraction import conflict as _conflict
        verdict = _conflict.classify(new_text)
        payload["conflict"] = _conflict.verdict_to_dict(verdict)
    except Exception as e:
        # Keep the edit, drop the stale verdict: a verdict describing the old text is
        # worse than none, and verdict_from_dict degrades a missing one to NEW.
        payload["conflict"] = None
        note = f"\n(could not re-classify: {e} — will be applied as a new fact)"
        verdict = None

    rec["payload"] = payload
    rec["summary"] = f"[{verdict.verdict if verdict else 'NEW'}] {_shorten(new_text, 120)}"
    try:
        from utils import atomic_json_write
        atomic_json_write(wa._pending_path(wa.MEMORY, pending_id), rec)
    except Exception as e:
        return f"Failed to save edit to '{pending_id}': {e}"
    return (f"Updated pending memory proposal {pending_id}"
            f"{f' — new verdict: {verdict.verdict}' if verdict else ''}.{note}\n"
            f"  {_shorten(new_text)}\n"
            f"Inspect: /memory show {pending_id}   Apply: /memory approve {pending_id}")


_APPROVAL_VALUES = {
    **dict.fromkeys(("on", "true", "yes", "1", "enable", "enabled"), True),
    **dict.fromkeys(("off", "false", "no", "0", "disable", "disabled"), False)}


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem."""
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    enabled = _APPROVAL_VALUES.get(arg)
    if enabled is None:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."
