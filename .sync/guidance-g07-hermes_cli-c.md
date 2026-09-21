# Group g07-hermes_cli-c — FORK.md guidance

Files: hermes_cli/kanban.py, hermes_cli/main.py, hermes_cli/model_catalog.py,
hermes_cli/model_switch.py, hermes_cli/models.py, hermes_cli/plugins.py,
hermes_cli/portal_cli.py, hermes_cli/profiles.py

## hermes_cli/main.py — HIGH RISK, core CLI entry point
Per this repo's own AGENTS.md plugin policy: a plugin must NEVER hardcode plugin-specific
logic into `main.py` (a past PR removed 95 lines of hardcoded honcho argparse from this
exact file for that reason). If you see the fork has any plugin-specific special-casing
inline here, that's technically against the fork's own stated policy — but for a MERGE,
your job is to preserve existing fork behavior, not redesign it. Keep whatever the fork
currently does; just flag it in your report as "pre-existing policy violation, not
introduced by this merge" so the orchestrator can decide whether to file it separately.
Do not silently drop fork CLI wiring because it looks architecturally wrong.

## hermes_cli/model_switch.py / hermes_cli/model_catalog.py / hermes_cli/models.py
Related to `agent/agent_runtime_helpers.py`'s `switch_model` (a sibling group) — the fork
has a 1M-beta latch + `drop_context_1m_beta=` param and MiniMax-OAuth handling. If any of
these three files reference model-switching config/catalog entries tied to that feature,
keep the fork's entries; do not let upstream's model catalog restructure drop a
fork-specific model metadata field silently.

## No specific prior guidance found for
hermes_cli/kanban.py, hermes_cli/plugins.py, hermes_cli/portal_cli.py,
hermes_cli/profiles.py — use the common-guidance default. Flag design-collision picks
explicitly; these will need new FORK.md entries.
