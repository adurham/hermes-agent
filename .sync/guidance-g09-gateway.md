# Group g09-gateway — FORK.md guidance

Files: gateway/authz_mixin.py, gateway/config.py, gateway/platforms/api_server.py,
gateway/platforms/api_server_runs.py, gateway/run.py, gateway/session_state.py,
gateway/slash_commands.py

## General
`gateway/run.py` is one of the god-files this codebase periodically splits (per this
repo's AGENTS.md: "Refactor god-files into clean modules" is wanted upstream work, and
this fork's own Tier-2 refactors moved worst offenders into `agent/fork/*`). If upstream
did a large extraction out of `gateway/run.py` in this range, treat it like the
`run_agent.py` extraction precedent: adopt upstream's new module layout wholesale, then
re-home any fork-specific behavior from the old monolithic function into the new
location. Do NOT try to preserve the old monolithic shape by force-merging fork code back
into a function upstream has already split into pieces — check whether upstream's split
already has a natural insertion point for the fork's addition before choosing where to
put it.

## gateway/session_state.py
No specific prior guidance, but per this repo's own AGENTS.md core principle: "Strict
message role alternation (never two same-role messages in a row; never a synthetic user
message injected mid-loop)" and "a system prompt that is byte-stable for the life of a
conversation" are invariants that session-state code must not violate. If you see fork
logic enforcing either invariant, keep it — do not let an upstream refactor of session
state silently drop an alternation-safety check.

## No specific prior guidance found for
gateway/authz_mixin.py, gateway/config.py, gateway/platforms/api_server.py,
gateway/platforms/api_server_runs.py, gateway/slash_commands.py — use the
common-guidance default (adopt upstream's structure, re-home fork additions on top of
it). Flag design-collision picks explicitly; these will need new FORK.md entries.
