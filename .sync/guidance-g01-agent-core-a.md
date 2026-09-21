# Group g01-agent-core-a — FORK.md guidance

Files: agent/account_usage.py, agent/agent_init.py, agent/agent_runtime_helpers.py,
agent/anthropic_adapter.py, agent/anthropic_credentials.py, agent/auxiliary_client.py,
agent/background_review.py, agent/chat_completion_helpers.py

## agent/anthropic_adapter.py
**Converter defused by a prior refactor (T2.2).** The ~540-line
`convert_messages_to_anthropic` (vs upstream's much shorter version) now lives in
`agent/fork/anthropic_messages.py`; the adapter has a 2-line forwarder. Upstream's own
extract-method refactors of its converter can no longer tangle with it — **on conflict,
take "ours" for the forwarder.** The block/tool/content helpers stay in the adapter (some
upstream-shared); the fork converter binds them via a lazy `from agent import
anthropic_adapter` import (also breaks a circular dep) — do not "fix" that lazy import.

Still take "ours" for CC (Claude Code) wire-shape edits: alias translation, metadata
blob, billing header, SSE observer. These are deliberate fork additions on the Anthropic
wire path — do not let upstream's version silently drop them.

**Tool naming — a DELIBERATE, repeated fork divergence:** the fork does NOT prepend
`mcp_` to bare tool names (it registers MCP tools as `mcp__server__tool`). Upstream
re-adds single-underscore prefixing every few syncs. **Take ours, drop upstream's prefix
loop entirely** (and drop any of upstream's outgoing-prefix tests that assert the
prefixed behavior — do not adapt them, they test behavior we deliberately don't have).

## agent/credential_pool.py
`_seed_from_singletons` auth seeding. Keep the fork's keychain-longlived precedence;
nest upstream's api-key-path pruning INSIDE the fork's `if not longlived_token:` block
(don't let it run unconditionally). The pruning predicate uses
`is_borrowed_credential_source()` — verify `keychain_longlived` credentials stay
kept-while-active after your merge (i.e. the pruning logic must not evict a still-active
longlived keychain token).

## agent/agent_runtime_helpers.py
`switch_model` — keep the fork's 1M-beta latch + `drop_context_1m_beta=` parameter;
integrate it into upstream's try/except-rollback + MiniMax-OAuth structure (upstream may
have restructured error handling around model switching — thread the fork's param/latch
through whatever new structure upstream introduces, don't just paste the old function
back verbatim over upstream's new error handling).

## agent/chat_completion_helpers.py
**Partially defused by a prior refactor (T2.3).** Refusal detection is now
`agent._is_anthropic_refusal()` (a forwarder to
`agent/fork/anthropic_recovery.is_anthropic_refusal`); the cold-start stale-timeout is
`agent/fork/stream_recovery.effective_stale_timeout`. These forwarders should not
conflict — if they do, take ours (they're one-liners).

Residual, accepted fork inline code (control-flow-coupled, deliberately NOT extracted):
the refusal-recovery LADDER (fallback → sanitize → giveup, with `continue`/`return`/
loop-var resets) and the stale-kill counters. On conflict here: **take "ours"**, then
verify the loop vars (`retry_count`, `compression_attempts`,
`primary_recovery_attempted`) still get reset correctly against upstream's surrounding
structure. Keep BOTH recovery blocks if upstream added a new one — cache-strip-on-overload
vs multimodal-tool-content handling are two independent recovery paths, not alternatives.

## No specific prior guidance found for
agent/account_usage.py, agent/agent_init.py, agent/anthropic_credentials.py,
agent/auxiliary_client.py, agent/background_review.py — use the common-guidance default
(adopt upstream's structure, re-home fork additions on top; flag as new decisions in your
report so these get FORK.md entries).
