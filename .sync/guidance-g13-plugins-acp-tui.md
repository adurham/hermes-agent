# Group g13-plugins-acp-tui — FORK.md guidance

Files: acp_adapter/server.py, acp_adapter/session.py, tui_gateway/server.py,
ui-tui/src/app/turnController.ts, ui-tui/src/gatewayTypes.ts,
plugins/google_meet/__init__.py, plugins/google_meet/cli.py,
plugins/image_gen/openrouter/__init__.py, plugins/memory/holographic/retrieval.py,
plugins/memory/holographic/store.py, plugins/model-providers/anthropic/__init__.py

## General plugin policy (from this repo's own AGENTS.md, plugins/AGENTS.md)
Plugins live in their own directory and work within the ABCs/hooks the framework
provides. A plugin file MUST NOT need to reach into core files — if a conflict here
seems to require deeper core changes, that's a sign something is architecturally off;
flag it rather than improvising a deeper core hook.

`plugins/memory/` is a CLOSED set of in-tree providers (honcho, mem0, supermemory,
byterover, hindsight, holographic, openviking, retaindb) — bug fixes are welcome, no new
providers should appear. If `plugins/memory/holographic/retrieval.py` or `store.py` shows
upstream adding significant new functionality (not just a bug fix), that's expected — take
it. If the fork has any holographic-specific bug fixes, keep them layered on top of
upstream's structure.

`plugins/model-providers/anthropic/__init__.py` — this registers the Anthropic model
provider profile via `providers.register_provider(ProviderProfile(...))`. This is
different from `agent/anthropic_adapter.py` (message wire-shape conversion, a separate
concern) — do not confuse the two guidance sets. If this file conflicts, it's almost
always a provider profile/pricing metadata update from upstream; take upstream's new
model/pricing entries, keep any fork-specific provider profile field if one is set.

## No specific prior guidance found for
acp_adapter/server.py, acp_adapter/session.py, tui_gateway/server.py,
ui-tui/src/app/turnController.ts, ui-tui/src/gatewayTypes.ts,
plugins/google_meet/__init__.py, plugins/google_meet/cli.py,
plugins/image_gen/openrouter/__init__.py — use the common-guidance default (adopt
upstream's structure, re-home fork additions on top of it). Flag design-collision picks
explicitly; these will need new FORK.md entries.
