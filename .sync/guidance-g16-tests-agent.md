# Group g16-tests-agent — FORK.md guidance

Files: tests/agent/test_auxiliary_client.py, tests/agent/test_auxiliary_main_first.py,
tests/agent/test_context_compressor.py, tests/agent/test_empty_tool_name_loop_dampening.py,
tests/agent/test_proactive_prune_loop_wiring.py, tests/agent/test_streaming.py,
tests/agent/test_system_prompt.py, tests/agent/test_tool_guardrails.py

## General
NOTE: tests dir was reorganized upstream this sync (`tests/run_agent` -> `tests/agent`,
`tests/cli` -> `tests/hermes_cli`, `tests/acp` -> `tests/acp_adapter`, `tests/relay` ->
`tests/gateway/relay`, `tests/state` -> `tests/hermes_state`). These are the NEW
(post-reorg) paths. Resolve conflicts in place at these paths — do not try to move them
back.

Upstream test files that assert its OWN branding/wire-shape will fail on the fork (e.g.
literal "Hermes Agent v" strings assumed elsewhere, `thinking.display` shape, "the 4th
cache breakpoint the fork reserves for tools[]"). Reconcile a test assertion mismatch by
matching on SHAPE or inverting the assertion with an explanatory comment — never by
deleting or skipping the test outright.

## tests/agent/test_auxiliary_main_first.py
Fork addition: `TestExoScopedAuxDelegation` (2 tests, exo-scoped aux delegation guard,
added 2026-06-18). If this class exists in the fork's version and upstream's conflicting
diff doesn't have it, keep the fork's class — it's an intentional fork-only test class in
an otherwise-shared upstream test file.

## tests/agent/test_empty_tool_name_loop_dampening.py
Ties to `agent/tool_guardrails.py` (in group g04) — if the guardrail source function this
test exercises was refactored upstream, update ONLY the test's setup/mocking to match the
new call shape; do not change what behavior it's asserting.

## Known pre-existing flakes — NOT merge regressions if you see these fail
`auxiliary_client` provider/vision tests (`test_vision_routing_31179.py`,
`test_provider_parity.py::...openrouter_always_wins`, `test_auxiliary_main_first.py`) are
documented to fail ONLY under full-suite ordering (global-state pollution) and pass in
isolation — don't "fix" them if you're not asked to run tests (you're not running tests
in this group anyway — the orchestrator does). Just don't be alarmed if their content
looks like it "should" work; leave the test logic as-is unless the merge itself corrupted
it.

## No specific prior guidance found for
tests/agent/test_context_compressor.py, tests/agent/test_proactive_prune_loop_wiring.py,
tests/agent/test_streaming.py, tests/agent/test_system_prompt.py,
tests/agent/test_tool_guardrails.py — use the common-guidance default (resolve to match
whatever the corresponding source file's group decided; if the source file isn't in your
context, resolve conservatively by keeping both sides' test cases where they test
different things, and flag any genuine assertion collision for the orchestrator).
