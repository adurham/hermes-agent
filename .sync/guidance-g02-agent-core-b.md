# Group g02-agent-core-b — FORK.md guidance

Files: agent/context_compressor.py, agent/conversation_compression.py,
agent/conversation_loop.py, agent/credential_pool.py (see note), agent/credential_sources.py,
agent/curator.py, agent/curator_backup.py, agent/display.py

NOTE: `agent/credential_pool.py` full guidance is in guidance-g01-agent-core-a.md (it was
grouped there in a prior sync's guidance) — if it's in YOUR file list instead, use this:
`_seed_from_singletons` auth seeding — keep the fork's keychain-longlived precedence; nest
upstream's api-key-path pruning INSIDE the fork's `if not longlived_token:` block. The
pruning predicate uses `is_borrowed_credential_source()` — verify `keychain_longlived`
stays kept-while-active.

## agent/conversation_loop.py
**Partially defused by a prior refactor (T2.3).** Refusal detection is now
`agent._is_anthropic_refusal()` (forwarder → `agent/fork/anthropic_recovery.
is_anthropic_refusal`); cold-start stale-timeout is `agent/fork/stream_recovery.
effective_stale_timeout`. Forwarders are one-liners — take ours if conflicted.

Residual accepted fork inline code: the refusal-recovery LADDER (fallback → sanitize →
giveup with `continue`/`return`/loop-var resets) and stale-kill counters stay inline —
moving this control flow out of the retry loop is riskier than the conflict it saves. On
conflict: take "ours", then verify loop vars (`retry_count`, `compression_attempts`,
`primary_recovery_attempted`) still reset correctly; keep BOTH recovery blocks
(cache-strip-on-overload vs multimodal-tool-content) if upstream added parallel logic —
they are independent, not either/or.

## No specific prior guidance found for
agent/context_compressor.py, agent/conversation_compression.py,
agent/credential_sources.py, agent/curator.py, agent/curator_backup.py, agent/display.py
— use the common-guidance default (adopt upstream's structure, re-home fork additions on
top). Flag any design-collision picks explicitly; these will need new FORK.md entries.

Extra context: `agent/curator.py` / `agent/curator_backup.py` are a cost/curation
subsystem the fork instruments for cost auditing in some syncs — if you see fork-added
cost/pricing fields or auxiliary-model cost tracking hooks in either file, keep them
(don't let upstream's refactor silently drop a cost field the fork relies on elsewhere —
check for callers of any function/field you're about to drop).
