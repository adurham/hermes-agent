# Group g21-docs-misc — FORK.md guidance

Files: .github/workflows/ci.yaml, .github/workflows/lint.yml, AGENTS.md,
package-lock.json, website/docs/developer-guide/gateway-internals.md,
website/docs/developer-guide/session-storage.md, website/docs/reference/model-catalog.md,
website/docs/reference/optional-skills-catalog.md, website/docs/reference/slash-commands.md,
website/docs/user-guide/features/kanban.md, website/docs/user-guide/features/web-search.md,
website/docs/user-guide/skills/bundled/research/research-grounded-citations.md,
website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/kanban.md,
website/sidebars.ts

## General
- `.gitignore` / docstrings / comments-style incidental collisions: keep both, or take
  either side — these are typically just line-adjacency collisions from unrelated edits
  near each other. Don't spend excess time here.
- `package-lock.json` — this is the ROOT package-lock (not apps/desktop's). Regenerate
  mentally the same way you'd resolve any lockfile: prefer taking upstream's lockfile
  wholesale if the root package.json didn't conflict (matching deps), otherwise flag to
  orchestrator to regenerate via `npm install` after the merge lands.
- `.github/workflows/ci.yaml` / `lint.yml` — CI workflow YAML. Merge additively: keep
  BOTH sides' job/step additions unless they're genuinely the same job configured two
  different ways (e.g. both sides changed the same matrix/python-version entry) — in that
  case take upstream's newer version since CI infra tracks upstream's release cadence.
- `AGENTS.md` — this is the root development guide (the file the orchestrator quoted
  extensively when starting this task). If fork-specific guidance sections exist (e.g.
  "Never give up on the right solution", the Contribution Rubric, Plugin policy sections),
  KEEP them — they are fork-maintained policy text, not upstream content that should be
  overwritten. Merge additively: if upstream added a new section on a topic the fork
  doesn't cover, take it; if both sides edited the SAME section with different wording,
  keep the fork's wording (this file encodes fork-specific policy decisions) unless
  upstream's edit is a pure factual/technical correction unrelated to policy.

## website/docs/reference/model-catalog.md
This is generated/curated docs content about available models — likely just needs
upstream's newer model entries taken, with any fork-specific note (if one exists, e.g.
about the 1M-beta latch mentioned in `agent/agent_runtime_helpers.py` guidance) kept.

## No specific prior guidance found for the remaining doc files
website/docs/developer-guide/gateway-internals.md, website/docs/developer-guide/
session-storage.md, website/docs/reference/optional-skills-catalog.md,
website/docs/reference/slash-commands.md, website/docs/user-guide/features/kanban.md,
website/docs/user-guide/features/web-search.md, website/docs/user-guide/skills/bundled/
research/research-grounded-citations.md, the zh-Hans i18n kanban.md mirror,
website/sidebars.ts — these are mostly prose; merge additively, prefer upstream's newer
content unless it contradicts a fork-specific behavior documented elsewhere in this
sync's guidance files (e.g. the Tavily keyed-only policy, the MCP tool-naming
divergence, the config.yaml-only policy for behavioral settings). Flag anything
surprising for the orchestrator.
