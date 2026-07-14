# Hot-Tier Audit (curator-driven trim + flush)

> **Status:** SCOPED, not started. Addendum to
> `2026-05-19-memory-recall-reminder-and-session-pin.md`, whose Feature A
> (recall reminder) and Feature B (session-pin) both landed. This picks up
> the explicit non-goal from that doc: "Hot-tier rotation (auto-swap stale
> hot for high-retrieval warm). Too magical for v1."
>
> Trigger: Adam noticed his hot-tier `MEMORY.md` had a stale repo path
> (`~/repos/claude-code` — the actual clone is `~/repos/hermes-agent`) sitting
> unnoticed at 92% of the char cap, plus a dead `[BLOCKED: ...]` placeholder
> line from a rejected write that can never be cleared by string-match
> removal. Manual trims only happen when the cap rejects a write and forces
> the issue.

## 1. Problem statement

Hot tier (`MEMORY.md` / `USER.md`, ~4500/5200 char caps) only gets edited in
two situations today:
1. Adam explicitly asks for something to be added/removed.
2. A write is rejected because the cap is full, forcing a consolidation pass.

Nothing periodically re-reads hot tier for staleness (dead paths, superseded
facts, or content that's more naturally a warm fact + skill), so entries can
sit wrong or overweight for months. The existing warm-tier curator
(`agent/curator.py`) already does an analogous job for skills — periodic,
snapshot-before-mutate, LLM-reviewed — but has no path into hot-tier memory
at all today (`Grep` over `agent/curator.py` for `memory|hot_tier` returns
only unrelated variable names).

Separately: the recall-reminder feature (2026-05-19 doc, Feature A) already
nudges the agent to query warm tier periodically. It fired repeatedly during
the session that prompted this doc. The gap there isn't the mechanism — it's
that the agent (any agent, not just this session) can silently ignore the
nudge with no consequence. Two options, not mutually exclusive:
  - (a) Tune the nudge to be harder to ignore (e.g. escalate wording/frequency
    if N reminders fire with zero voluntary recalls in between).
  - (b) Track "reminders fired vs. recalls issued" as a session metric,
    surfaced at session end, so ignoring it is visible rather than silent.

This doc scopes (a): the hot-tier audit is the bigger unbuilt piece.
(b) is a small follow-up noted in Non-goals as future work, not blocking.

## 2. Design

### 2.1 Hot-tier audit pass, piggybacked on the existing curator cycle

Reuse `curator.should_run_now()` / `interval_hours` / `min_idle_hours` gating
— no new scheduler. When a real (non-dry) curator pass runs, after the
existing skill review, run a second review over hot-tier contents:

1. Read `MEMORY.md` and `USER.md` entries (same `ENTRY_DELIMITER` split used
   by `scripts/migrate_memory_to_warm.py` and `tools/memory_tool.py`).
2. For each entry, an LLM review pass (same aux-model binding the curator
   already uses — see `_resolve_review_model` in `agent/curator.py`) classifies:
   - `keep` — still hot-tier appropriate (recurring correction / preference
     / must-influence-every-turn).
   - `demote` — durable fact, but doesn't need to be in every prompt; move
     to warm.
   - `stale` — refers to a path/fact that verifiably no longer holds (e.g.
     grep-checkable file paths that don't exist, or explicit supersession
     language already present in the entry).
   - `dead` — un-removable placeholder / error text (like the `[BLOCKED:...]`
     line) that provides zero value and isn't a real memory.
3. `stale` and `dead` entries: remove outright (mirrors `prune_builtins`
   semantics for skills — opt-in via `curator.prune_builtins`, reuse that
   flag rather than adding a new one).
4. `demote` entries: write to warm tier via `tools.memory_warm.get_warm_store`
   (same insertion path `migrate_memory_to_warm.py` already uses), then
   remove from the hot-tier file.
5. Emit a run-report entry alongside the existing skill-curator report
   (`_write_run_report` / `_render_report_markdown` in `agent/curator.py`
   already produce `~/.hermes/skills/.curator_backups/<ts>/report.md`; add a
   `## Hot-tier audit` section to the same report rather than a second file).
6. Respect `curator.consolidate` (currently `false`/opt-in) as the gate for
   whether the LLM review runs at all vs. a dry list-only pass — mirrors how
   skill consolidation is opt-in today.

### 2.2 Stale-path detection heuristic (cheap, no LLM needed for this part)

Before the LLM classification, run a fast local check: extract path-shaped
tokens from each entry (regex for `~/[\w./-]+` and `/Users/[\w./-]+`) and
`Path.exists()` each one. Any entry where an extracted path definitively
doesn't exist gets pre-flagged `stale` for the LLM step to confirm/reject
(confirm needed because a path can be *intentionally* historical, e.g. "this
used to live at X, now at Y" prose that mentions a dead path on purpose).

### 2.3 Config

```yaml
curator:
  hot_tier_audit: false   # opt-in; off by default, same posture as `consolidate`
  hot_tier_audit_dry_run: true  # first runs report-only, no mutation, until
                                 # the user has seen a few reports and trusts it
```

Gate hard behind `hot_tier_audit: false` default — this touches memory files
the user reads every session; must not surprise anyone the way `prune_builtins`
already has documented caution around.

### 2.4 Safety

- Same pre-run snapshot mechanism `curator_backup.py` already uses for
  skills — extend `snapshot_skills` (or add a sibling
  `snapshot_memory(reason=...)`) to also tar.gz `~/.hermes/memories/` before
  a mutating hot-tier audit pass.
- `hermes curator rollback` should restore both skills and memory snapshots
  from the same run.
- Never touch warm tier's own content, only hot → warm migration and
  hot-tier deletion.

## 3. Non-goals (this pass)

- Auto-tuning the recall-reminder interval/mode based on ignore-rate. Track
  as a metric first (see idea (b) above); revisit once there's data.
- Any change to warm-tier trust scoring — hot-tier audit's `demote`
  insertions should NOT get a trust boost just for being ex-hot; they enter
  warm at the normal default trust like any other `add`.
- Cross-repo hot tier (this is single-profile; no multi-profile hot-tier
  audit in v1).

## 4. Rollout

1. Land opt-in, dry-run-only. Adam turns it on manually via config, reviews
   a few reports.
2. If reports are accurate (no false-positive `stale`/`dead` classifications
   over a few real runs), flip `hot_tier_audit_dry_run: false` for live
   mutation, still opt-in.
3. Consider default-on only after that's proven out over weeks, same posture
   the skill curator itself took before `enabled: true` became the default.
