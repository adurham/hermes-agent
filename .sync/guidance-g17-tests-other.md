# Group g17-tests-other — FORK.md guidance

Files: tests/gateway/test_completion_delivery.py, tests/hermes_cli/test_setup_blank_slate.py,
tests/hermes_state/test_hermes_state.py, tests/plugins/test_a2a_plugin.py,
tests/plugins/web/test_web_search_provider_plugins.py,
tests/tools/test_delegate_summary_budget.py, tests/tools/test_mcp_circuit_breaker.py,
tests/tools/test_mcp_tool.py, tests/tools/test_process_wait_clarity.py,
tests/tools/test_web_tools_config.py, tests/tui_gateway/test_tui_gateway_server.py

## General
Same tests/ reorg note as sibling group g16: these are POST-reorg paths
(`tests/state` -> `tests/hermes_state`, etc.) — resolve in place at these paths.

## tests/hermes_state/test_hermes_state.py
Directly tests `hermes_state.py` (group g15, resolved by the orchestrator directly).
This test file may assert exact column counts / INSERT placeholder counts for the
INSERT/VALUES sites the g15 guidance flags as a critical known-defect class (column vs
placeholder mismatch). If a test here fails to even COLLECT (import error) or asserts a
specific schema shape, do not weaken the assertion to make it pass — that's exactly the
kind of test that should catch a g15 column-count regression. Resolve the test's own
merge conflict faithfully (keep both sides' test cases where they test different things)
and let the orchestrator's full test run be the actual verdict on `hermes_state.py`'s
correctness.

## tests/plugins/web/test_web_search_provider_plugins.py
Ties to the Tavily keyed-only decision (groups g04/g14). If this test asserts Tavily is
NOT in the keyless ring / IS keyed-only, keep that assertion — do not adapt it toward
upstream's shape if upstream's shape would make Tavily keyless-by-default (that would be
adapting a test to hide a real regression against the fork's deliberate policy).

## tests/tools/test_mcp_tool.py / tests/tools/test_mcp_circuit_breaker.py
Ties to the MCP tool-naming divergence (fork does NOT prepend `mcp_` to bare tool names,
registers as `mcp__server__tool` — see groups g01/g11). Keep fork assertions matching
that convention; drop any upstream test asserting single-underscore `mcp_` prefixing
behavior (per the standing rule: "take ours, drop upstream's prefix loop + its outgoing-
prefix tests").

## No specific prior guidance found for
tests/gateway/test_completion_delivery.py, tests/hermes_cli/test_setup_blank_slate.py,
tests/plugins/test_a2a_plugin.py, tests/tools/test_delegate_summary_budget.py,
tests/tools/test_process_wait_clarity.py, tests/tools/test_web_tools_config.py,
tests/tui_gateway/test_tui_gateway_server.py — use the common-guidance default (keep
both sides' test cases where they test different things; flag genuine assertion
collisions for the orchestrator).
