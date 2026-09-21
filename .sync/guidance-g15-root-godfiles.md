# Group g15-root-godfiles — FORK.md guidance (ORCHESTRATOR HANDLES THIS GROUP DIRECTLY)

Files: cli.py, run_agent.py, model_tools.py, toolsets.py, hermes_constants.py,
hermes_state.py

This group is NOT delegated to a subagent — the orchestrator resolves it directly given
the documented history of critical defects in exactly these files. Guidance retained here
for reference during resolution:

## run_agent.py
Import unions — keep fork's `Set`/`Tuple`/`ForkForwardersMixin` imports alongside
upstream's. `_sync_external_memory_for_turn`: upstream threads a `messages=` kwarg into
`sync_all` — keep that threading (a PRIOR sync's regression was exactly a dropped
`messages=` thread). The fork's separate `memory_extraction.on_turn_end` Phase-2 hook is
independent — keep it too, don't conflate the two.

## cli.py
Additions near `kb = KeyBindings()` historically collide (fork's cancel-ladder vs
upstream's keybindings) — keep BOTH blocks. Tool-count/status logic: keep fork's
`disabled_toolsets` arg AND upstream's defer logic. In a later sync, the fork's
`/reasoning` picker and upstream's Ctrl+P command palette turned out to be two parallel
modal widgets on the SAME dict-state pattern — if both still exist, they collide in many
places (state init, layout-children signature/list, keybindings, the `_normal_input`
guard, both display functions, the construction call site) — keep BOTH everywhere; the
`_normal_input` condition must negate BOTH picker states or one swallows history
navigation.

## hermes_state.py — mostly defused by prior Tier-2 refactor, watch the residuals
- `SCHEMA_VERSION`: both sides may bump it — pick `max(both) + 1`.
  `_reconcile_columns()` runs unconditionally on boot and ALTER-ADDs any column in
  `SCHEMA_SQL` or `FORK_TABLE_COLUMNS` missing live; tables use `CREATE TABLE IF NOT
  EXISTS` — the version bump only gates DESTRUCTIVE migrations.
- Fork-only tables (e.g. `api_calls`) live in `FORK_SCHEMA_SQL` (executed after
  `SCHEMA_SQL`), NOT inline in `SCHEMA_SQL` — no positional collision expected.
- Fork columns (e.g. `anthropic_content_blocks`) live in `FORK_TABLE_COLUMNS`
  (ALTER-ADDed), NOT in the base CREATE TABLE — `SCHEMA_SQL`'s table defs should stay
  pure-upstream shape.
- **CRITICAL KNOWN DEFECT CLASS: column/placeholder count mismatch.** A prior sync's
  merge combined upstream's new column with the fork's own extra column and gave an
  INSERT/VALUES site N columns but N-1 `?` placeholders —
  `sqlite3.OperationalError: N-1 values for N columns` at RUNTIME, invisible to markers
  and syntax checks. After resolving ANY INSERT/VALUES statement in this file, COUNT the
  columns in the column-list and COUNT the `?` placeholders in VALUES — they must match
  exactly. Do this for every INSERT touched, not just the obviously-conflicted ones.
- Two fork-only pure readers (pattern example from a prior sync:
  `get_compression_attempts_total`, `get_lineage_cost_usd`) must NOT acquire the WRITER
  lock — a pure reader under the writer lock violates an upstream invariant test
  (`tests/state/test_no_locked_readers_gate.py` or similar path after the tests/ reorg —
  check `tests/hermes_state/`). Route pure readers to the READ context.
- Consumers read columns BY NAME (`row["col"]`), so column ORDER in a SELECT/CREATE is
  safe even when interleaved — don't over-engineer column ordering, just get the
  count right.

## model_tools.py / toolsets.py / hermes_constants.py
No specific prior per-file guidance recorded — apply the general policy: adopt
upstream's structure, re-home fork additions (tool registration order, fork-specific
toolset gating, any fork-added constants) on top of it. These files are read by
`discover_plugins()` and toolset gating logic — verify no fork-specific toolset entry
(e.g. any `disabled_toolsets`-related constant) got silently dropped.
