# Common guidance for ALL conflict-resolution groups (v2026.9.14 sync)

## Ground rules
- Resolve conflict markers (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`) directly in the
  files listed for YOUR group only. Do not touch any file outside your assigned list.
- Do NOT run any git command (no `git add`, `git commit`, `git status`, nothing). The
  orchestrator stages everything after reviewing your work.
- Do NOT run the test suite. The orchestrator runs all tests serialized.
- General merge policy for this fork: the fork's code lives in `agent/fork/*` and is
  hooked into upstream via thin forwarders — NEVER conflicts, leave alone if you see it.
  Elsewhere, the standing rule is: **adopt upstream's refactor as the new base structure,
  then re-home the fork's additive feature/behavior on top of it.** Prefer keeping BOTH
  sides' intent when the hunk is "disjoint" (two unrelated additions in the same
  function/file) — only pick one side when it's a genuine same-question-different-answer
  design collision, and note which you picked and why.
- Never do drive-by reformatting/edits outside the conflict markers.
- `uv.lock` is handled by a merge driver — you should never see it in a file list, but if
  you do, do not touch it manually.

## Gating your own work before reporting back (MANDATORY)
For every `.py` file you touched:
1. `grep -n '^<<<<<<< \|^>>>>>>> '` on the file must return NOTHING (anchored at line
   start — do not use a bare `=======` grep, it false-positives inside markdown/dividers).
2. `python -m py_compile <file>` (or `ast.parse`) must succeed.
3. `ruff check --select F <file>` must be clean (catches referenced-but-undefined names —
   this fork has been bitten before by an auto-merge that silently dropped a function
   definition while keeping its call site — see the "silent auto-merge deletion" class of
   defect in FORK.md's sync notes).

For `.ts`/`.tsx` files: only the marker check (1) is yours to do; the orchestrator runs
`tsc --noEmit` afterward with full project context.

## Reporting back
For each file in your group, report ONE line: `path — one-line rationale, keyed to
whichever specific guidance point applied (or "no prior guidance, new decision" if none
of the guidance applied and you had to make an independent call)`.

Explicitly FLAG (separately, at the end) any file where:
- You found NO relevant guidance in the guidance file provided to you and had to make an
  independent judgment call — these need a new FORK.md entry.
- Upstream deleted/renamed a function or symbol that fork code still calls, and the fix
  needed is bigger than a surface conflict resolution (e.g. a real re-wiring across
  multiple call sites) — flag this back to the orchestrator rather than guessing at a fix.
