# Fork Notes — adurham/hermes-agent

Tracks the divergences between `adurham/hermes-agent` `main` and
`NousResearch/hermes-agent` `main`. Update this file whenever the fork
gains or drops a patch against upstream.

## Per-model reasoning effort isolation

**Commit:** `47c4903af`
**Files:** `cli.py`, `gateway/run.py`, `gateway/slash_commands.py`
**Status:** fork-only (not upstreamed)

### What it does

Adds a new config key `agent.reasoning_effort_by_model` — a dict mapping
model names to effort levels. When you set `/reasoning xhigh` while on
DSv4, it saves both the global `agent.reasoning_effort` AND a per-model
entry. When you `/model` switch to an Anthropic model and set
`/reasoning high`, that model gets its own entry. Switching back to DSv4
auto-applies `xhigh`.

### Why it exists

The user switches between models frequently (DSv4-Flash on exo, Claude
on Anthropic) and each model has a different optimal reasoning level.
Previously, `/reasoning` was a single global value that stayed the same
across model switches, requiring a manual `/reasoning <level>` after
every `/model`.

### How it works

- **`_resolve_reasoning_for_model()`** — module-level helper in `cli.py`
  that checks the per-model map (case-insensitive match) before falling
  back to the global `agent.reasoning_effort`.
- **`_apply_reasoning_arg()`** — on `/reasoning <level>`, saves to both
  `agent.reasoning_effort` (global) and
  `agent.reasoning_effort_by_model[current_model]` (per-model).
- **`_apply_reasoning_for_new_model()`** — called on every `/model`
  switch to look up the saved per-model effort and apply it.
- **Gateway** — `_load_reasoning_config(model)` accepts an optional
  model parameter; `_resolve_session_reasoning_config()` passes the
  resolved model through. Both `_run_agent` paths resolve reasoning
  with the current model.

### Config shape

```yaml
agent:
  reasoning_effort: "medium"           # global fallback
  reasoning_effort_by_model:
    deepseek-v4-flash: "xhigh"
    claude-sonnet-4-6: "high"
```

### Merge notes

The changes are additive — they add a new config key and a new helper
function, but never remove or change the behavior of the existing
`agent.reasoning_effort` path. When `reasoning_effort_by_model` is
empty or absent, everything works exactly as before.

Potential merge conflicts:
- `cli.py` — the `load_cli_config()` defaults dict and `__init__`
  reasoning-config loading block. Both are near the existing
  `reasoning_effort` line.
- `gateway/run.py` — `_load_reasoning_config()` and
  `_resolve_session_reasoning_config()` signatures changed.
- `gateway/slash_commands.py` — `_handle_reasoning_command()` body
  changed.
