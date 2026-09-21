# Group g03-agent-core-c — FORK.md guidance

Files: agent/error_classifier.py, agent/image_routing.py, agent/insights.py,
agent/message_sanitization.py, agent/model_metadata.py, agent/prompt_builder.py,
agent/prompt_caching.py, agent/rate_limit_tracker.py

## agent/image_routing.py
Flagged in a prior sync's de-fork audit as "FOR MONITORING" (no decision made at the
time — later resolved in a subsequent audit pass, see FORK.md "De-fork audit — 2026-09-01"
if you need the full history, but functionally the fork's image-routing behavior should
still be treated as an intentional divergence unless you find NO fork-specific logic left
in it at all, in which case flag it as "possibly converged" for the orchestrator to verify
against upstream via a real image-routing test).

## No specific prior guidance found for
agent/error_classifier.py, agent/insights.py, agent/message_sanitization.py,
agent/model_metadata.py, agent/prompt_builder.py, agent/prompt_caching.py,
agent/rate_limit_tracker.py — use the common-guidance default (adopt upstream's
structure, re-home fork additions on top of it). Watch especially for:
- `agent/prompt_caching.py`: the fork reserves a cache breakpoint arrangement (mentioned
  elsewhere in FORK.md as "the 4th cache breakpoint the fork reserves for tools[]") — if
  you see cache-breakpoint-count logic, don't let upstream silently change the count/order
  fork tests may depend on.
- `agent/rate_limit_tracker.py`: there's a fork-only test file
  `tests/agent/test_rate_limit_observability.py` (6 tests) exercising rate-limit
  observability hooks (INFO log on first header capture, WARN at 80% utilization,
  hysteresis on bucket state) — if this file has hooks/logging fork added, keep them;
  don't let an upstream refactor of the rate-limit tracker silently drop the observability
  hooks those tests assert on.

Flag any design-collision picks explicitly; these will need new FORK.md entries.
