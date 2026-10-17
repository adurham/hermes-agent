# Code-health ratchet backlog — measured inventory and worklist (2026-10-17)

Series plan for burning down `scripts/code_health` (the ratchet). Session 1 measured the backlog
exactly, landed the first slice (three work units), and left this queue. **Each later session takes
ONE work unit from the queue below, cold, using only this document.**

- **Measured on:** `main` @ `6bb436671e67` (2026-10-17 09:33 CDT) — clean checkout. This document is a **point-in-time snapshot**: line numbers drift with main; re-measure each unit before starting it.
- **Slice 1 branch:** `quality/ratchet-backlog-slice1` (3 refactor commits + this doc).
- **Upstream reference for merge risk:** `NousResearch/hermes-agent` main @ `18c525b669b6`
  (fetched read-only; nothing was pushed or opened there).
- **Companion files** (same directory name as this doc, without `.md`):
  `queue-c-tier{1,2,3,4}.md` (rule-family work units), `queue-d-js-and-out-of-lane.md`,
  `advisory-hits.md`, and the full per-item inventory as TSV: `inventory-metrics.tsv`,
  `inventory-rules-{1-agent-tools-gateway,2-hermes_cli,3-tests,4-other}.tsv` (split to stay under
  the 500 KiB blob guard). The inventory TSVs reflect the tree **after** slice 1 (rows of the five
  touched files re-measured at the branch head); the counts table below gives both.

## TL;DR

| | on main (before) | after slice 1 |
|---|---:|---:|
| Over-cap function units, CC (Python / TypeScript) | 458 open (115 / 343) + 2 waived (TS) | 455 (112 / 343) |
| Over-cap function units, FUNC_LINES (Py / TS) | 57 (8 / 49) | 54 (5 / 49) |
| Over-cap function units, NESTING (Py / TS) | 29 (26 / 3) | 29 |
| Over-cap files, FILE_LINES (Py / TS) | 121 open (92 / 29) + 3 waived (Py) | 120 (91 / 29) |
| Unwaived **blocking** rule findings | **9,413** | 9,412 (HX011 61 → 60) |
| Unwaived advisory rule findings | 277 | 277 |
| Waived (`# health: allow`) rule findings | 74 | 74 |

Blocking rule findings on main, by rule: BLE001 6,723 · HX013 928 · S110 903 · F811 297 · HX006 196 ·
ASYNC240 101 · F821 84 · HX011 61 · HX005 55 · RUF006 23 · ASYNC230 12 · HX007 11 · HX008 8 · HX004 7 ·
HX009 3 · F841 1. (E722, F823, S113 and HX002 have zero hits.) Advisory: HX012 228,
HX001 15, PS-P06 10, HX003 9, HX010 8, PS-P05 7.

Work units after slice 1: **Queue A** 131 Python function units · **Queue B** 91 Python file-size
units · **Queue C** 2,037 Python blocking rule-family units (one family in one file each) ·
**Queue D** 350 TypeScript function units + 29 TS files + 36 out-of-lane Python rule units
(apps/, not for this series) · 277 advisory hits (informational).

**The decision the owner should make first** (details in Owner decisions): 89 of the 131 Python
function units are byte-identical to upstream's copy, and upstream runs the very same ratchet
(`scripts/code_health` is identical in both trees). Refactoring those here turns every later upstream
edit to them into a manual port at the next upstream sync. The pick order below therefore puts
fork-only and already-diverged units first.

## What slice 1 landed

All three are the top-ranked T1 function units (Queue A order at measurement time), each a
mechanical phase extraction with zero intended behavior change; one commit per unit.

| unit | before → after | commit | tests | extra proof |
|---|---|---|---|---|
| `agent/anthropic_adapter.py::build_anthropic_kwargs` | CC 60 → 17, FUNC_LINES 457 → 199, NESTING 6 → 4; file 2,250 → 1,987 lines (now under target) | `573cecd68e` refactor(agent) | 62 files: 1,442 passed, 0 failed, 17 skipped | old vs new over 158,400 input combinations (models × endpoints × OAuth × tools × tool_choice × reasoning × env × fast/cache): 0 mismatches |
| `hermes_cli/backup.py::run_import` | CC 56 → 14, FUNC_LINES 321 → 111, NESTING 6 → 4; file 2,289 → 2,079 lines (cap lowered) | `49625f56b5` refactor(cli) | 21 files: 404 passed, 1 failed*, 3 skipped | old vs new on 48 crafted archives × home states: return value, stdout and resulting file trees identical |
| `tools/memory_tool.py::_handle_warm_action` | CC 49 → 12, FUNC_LINES 314 → 85; its HX011 ladder hit removed | `646ba5bcce` refactor(tools) | 26 files: 798 passed, 8 failed*, 62 skipped | old vs new on 449,280 input combinations with recording fakes: same JSON and same store/agent call log |

\* All 9 failures reproduce identically on clean `main` in this environment (see Pre-existing
failures) and are unrelated to the change.

New siblings: `agent/anthropic_adapter_kwargs.py` (390 lines), `hermes_cli/backup_import.py`
(315 lines); every new function meets all targets. Siblings late-import the facade's names so test
patches on the facade keep working (root AGENTS.md, Facade + siblings). Baselined rule hits inside the
units (the three `except Exception: pass` in the 1M-beta estimate, the gateway-revival broad except,
the recall-reminder and prompt-invalidation excepts) were deliberately left in place and in order.

**Upstream-sync note on the landed units:** `_handle_warm_action` is fork-only (no sync cost).
`build_anthropic_kwargs` was already a fork rewrite (5% similar to upstream's 73-line version), so the
split adds little. `run_import` is 69% similar to upstream's: the next upstream sync that edits
`run_import` will need its change ported into `hermes_cli/backup_import.py`. If you would rather not
carry that, drop `49625f56b5` — the other two commits are independent of it.

### Raw before/after (ratchet's own measurer)

```
$ python units.py . --rev main <file> --only <unit>   # BEFORE (main@6bb43667)
agent/anthropic_adapter.py  FILE_LINES=2250  hits=14
  build_anthropic_kwargs                                  line 1425  CC=60 FUNC_LINES=457 NESTING=6
hermes_cli/backup.py  FILE_LINES=2289  hits=11
  run_import                                              line 731   CC=56 FUNC_LINES=321 NESTING=6
tools/memory_tool.py  FILE_LINES=1177  hits=11
  _handle_warm_action                                     line 130   CC=49 FUNC_LINES=314 NESTING=3

$ python units.py . --rev HEAD <files>   # AFTER (branch head)
agent/anthropic_adapter.py  FILE_LINES=1987  hits=14
  build_anthropic_kwargs                                  line 1425  CC=17 FUNC_LINES=199 NESTING=4
agent/anthropic_adapter_kwargs.py  FILE_LINES=390  hits=0
  _prefix_oauth_system                                    line 15    CC=5 FUNC_LINES=32 NESTING=2
  _rename_oauth_tools                                     line 49    CC=11 FUNC_LINES=57 NESTING=5
  _rename_oauth_tools._to_oauth_wire_name                 line 86    CC=2 FUNC_LINES=5 NESTING=1
  _compact_oauth_system                                   line 113   CC=9 FUNC_LINES=54 NESTING=4
  apply_oauth_identity                                    line 169   CC=1 FUNC_LINES=8 NESTING=0
  apply_tools                                             line 179   CC=7 FUNC_LINES=32 NESTING=1
  _apply_adaptive_thinking                                line 213   CC=4 FUNC_LINES=41 NESTING=1
  apply_thinking                                          line 256   CC=8 FUNC_LINES=71 NESTING=2
  apply_fast_mode                                         line 329   CC=3 FUNC_LINES=36 NESTING=2
  strip_context_1m_beta                                   line 367   CC=4 FUNC_LINES=24 NESTING=2
hermes_cli/backup.py  FILE_LINES=2079  hits=11
  run_import                                              line 731   CC=14 FUNC_LINES=111 NESTING=4
hermes_cli/backup_import.py  FILE_LINES=315  hits=0
  ImportTally                                             line 19    
  ImportTally.report_progress                             line 33    CC=2 FUNC_LINES=3 NESTING=1
  confirm_overwrite                                       line 38    CC=4 FUNC_LINES=20 NESTING=2
  _restore_external_member                                line 60    CC=6 FUNC_LINES=36 NESTING=3
  _home_member_target                                     line 98    CC=8 FUNC_LINES=62 NESTING=1
  _restore_home_member                                    line 162   CC=6 FUNC_LINES=34 NESTING=3
  restore_members                                         line 198   CC=3 FUNC_LINES=25 NESTING=2
  print_import_summary                                    line 225   CC=10 FUNC_LINES=47 NESTING=2
  restore_profile_aliases                                 line 274   CC=12 FUNC_LINES=42 NESTING=4
tools/memory_tool.py  FILE_LINES=1234  hits=10
  _WarmArgs                                               line 133   
  _warm_add                                               line 152   CC=3 FUNC_LINES=13 NESTING=1
  _warm_recall                                            line 167   CC=3 FUNC_LINES=23 NESTING=1
  _warm_recall_related                                    line 192   CC=4 FUNC_LINES=18 NESTING=2
  _warm_read                                              line 212   CC=1 FUNC_LINES=11 NESTING=0
  _warm_remove                                            line 225   CC=2 FUNC_LINES=6 NESTING=1
  _warm_replace                                           line 233   CC=4 FUNC_LINES=18 NESTING=1
  _warm_feedback                                          line 253   CC=3 FUNC_LINES=12 NESTING=1
  _warm_promote                                           line 267   CC=7 FUNC_LINES=43 NESTING=1
  _demote_targets                                         line 312   CC=3 FUNC_LINES=9 NESTING=1
  _warm_demote                                            line 323   CC=7 FUNC_LINES=65 NESTING=2
  _session_pin_action                                     line 403   CC=4 FUNC_LINES=11 NESTING=1
  _handle_warm_action                                     line 416   CC=12 FUNC_LINES=85 NESTING=3
```

## How the inventory was measured

Environment: fresh Ubuntu VM (`nproc` reports **2** CPUs, not 4), Python 3.14 via `uv python install
3.14`, test env built per CONTRIBUTING.md (`python -m pm.build_env --source . --out .venv --group dev
--group test`, 29 s). `python scripts/check --only health` and one focused test file both ran within
5 minutes of starting setup; nothing needed working around.

1. `python -m scripts.code_health --report` on main — the ratchet's own burn-down summary (8 min,
   raw output below). It counts every pattern hit, waived ones included, which is why it says BLE001
   6,787 where the unwaived count is 6,723.
2. Per-item inventory with the ratchet's own engine (script below): `Measurer` over every tracked
   in-scope file, then `compare.compare` against an **empty base** (so every over-target unit and
   every hit surfaces as a finding exactly as a brand-new file would), then `report.apply_allows` to
   apply the real `# health: allow` waivers. Totals reconcile with `--report`: 13,230 files measured,
   10,434 items.
3. Ranking: tier first (T1 core: `agent/ tools/ gateway/ hermes_cli/` and root runtime modules;
   T2 runtime edges: `tui_gateway/ cron/ plugins/ acp_adapter/ pm/ providers/ hermes_platform/`,
   `mcp_serve.py`; T3 tooling: `scripts/ evals/ skills/ optional-skills/`, `batch_runner.py`, …;
   T4 tests), then how far over the cap (value ÷ target, worst metric of the unit). Rule units:
   tier, then family (correctness, async, timeout, profile, structure, swallow, test-sleep), then
   hit count.
4. Merge-risk inputs: commits touching each file on main in the 90 days to 2026-10-17
   (`git log --since=2026-07-17 --no-merges --name-only`, 36,842 commits in that window); string
   patch targets on the module in tests; and the unit compared with upstream main (absent /
   identical / diverged + similarity).

`--report` on main, verbatim:

```
Over target (units that are baselined at their current value):
  CC          target 20    over: 460   worst: 216 apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.resumeSession
  FUNC_LINES  target 300   over: 57    worst: 1223 apps/desktop/src/app/session/hooks/use-session-actions/index.ts::useSessionActions.resumeSession
  NESTING     target 6     over: 29    worst: 9 ui-tui/packages/hermes-ink/src/ink/render-node-to-output.ts::renderNodeToOutput
  FILE_LINES  target 2000  over: 124   worst: 23551 tests/tui_gateway/test_tui_gateway_server.py
Pattern hits (existing ones are baselined; new ones fail):
  BLE001      6787  blind `except Exception`
  HX013        928  wall-clock sleep in a test
  S110         906  try/except/pass
  F811         299  redefinition of unused name
  HX012        228  raw threading.Thread
  HX006        200  subprocess/urlopen without timeout
  ASYNC240     101  blocking path call in async def
  F821          84  undefined name
  HX011         61  if/elif ladder on one name
  HX005         55  home/config/env captured at import time
  RUF006        23  dangling asyncio task
  HX001         15  hardcoded Hermes home
  ASYNC230      12  blocking open() in async def
  HX007         11  sync config I/O inside async def
  PS-P06        10  raw platform credential getenv
  HX003         10  process identity from argv substrings
  HX008          8  asyncio.get_event_loop()
  HX010          8  bool() of a config/env string
  HX004          7  UnscopedSecretError falls back to os.getenv
  PS-P05         7  child env built from os.environ
  HX009          3  isinstance(r, Exception) on gather results
  F841           1  unused local variable
```

Per-item inventory script (run from the repo root with the test venv: `python -I inventory.py .
OUT [--rev REV] [--paths …]`; writes `OUT.tsv`):

```python
"""Full-tree inventory using the ratchet's own engine.

Measures every in-scope tracked file with scripts.code_health.Measurer (same as --report),
then judges the tree against an EMPTY base with scripts.code_health.compare.compare so every
over-target unit and every pattern hit surfaces as a finding, and applies the real
`# health: allow` waivers with report.apply_allows. Output: one TSV row per item.

usage: python inventory.py REPO OUT_PREFIX [--rev REV] [--paths p1 p2 ...]
"""
import pickle
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
out = sys.argv[2]
sys.path.insert(0, str(repo))

from scripts.code_health import gitio  # noqa: E402
from scripts.code_health.compare import compare  # noqa: E402
from scripts.code_health.config import TARGETS, in_scope  # noqa: E402
from scripts.code_health.measure import Measurer  # noqa: E402
from scripts.code_health.report import apply_allows  # noqa: E402
from scripts.code_health.ruff_runner import resolve_ruff  # noqa: E402

args = sys.argv[3:]
rev = None
paths = None
if "--rev" in args:
    rev = args[args.index("--rev") + 1]
if "--paths" in args:
    paths = args[args.index("--paths") + 1:]
head = gitio.resolve_tree(repo, rev) if rev else None
if paths is None:
    paths = [p for p in gitio.tracked_files(repo, head) if in_scope(p)]
measurer = Measurer(repo, resolve_ruff(repo), known_env=gitio.known_env_names(repo, head))
measures = measurer.measure(head, paths)
with open(out + ".pkl", "wb") as fh:
    pickle.dump(measures, fh)

findings = compare({}, measures, [])
apply_allows(findings, measures)
rows = []
for f in findings:
    if f.rule in TARGETS:
        fm = measures[f.path]
        if f.rule == "FILE_LINES":
            value = fm.metrics["FILE_LINES"]
        else:
            value = fm.units[f.scope].metrics[f.rule]
        rows.append(("METRIC", f.path, f.rule, f.scope, f.line, value, TARGETS[f.rule],
                     "allowed" if f.allowed_reason is not None else "open", ""))
    else:
        rows.append(("RULE", f.path, f.rule, f.scope, f.line, 1, 0,
                     "allowed" if f.allowed_reason is not None else "open",
                     "blocking" if f.blocking else "advisory"))
with open(out + ".tsv", "w") as fh:
    fh.write("kind\tpath\trule\tscope\tline\tvalue\ttarget\tstatus\tseverity\n")
    for r in rows:
        fh.write("\t".join(str(x) for x in r) + "\n")
print(f"{len(measures)} files measured, {len(rows)} items -> {out}.tsv")
```

Unit measurer used for every before/after (`python -I units.py . [--rev REV] FILE… [--only q1,q2]`):

```python
"""Print every unit's metrics for given files (working tree or --rev), via the ratchet's Measurer."""
import sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(repo))
from scripts.code_health import gitio
from scripts.code_health.measure import Measurer
from scripts.code_health.ruff_runner import resolve_ruff
args = sys.argv[2:]
rev = None
if args and args[0] == "--rev":
    rev, args = args[1], args[2:]
filt = None
if "--only" in args:
    i = args.index("--only"); filt = set(args[i+1].split(",")); args = args[:i]
head = gitio.resolve_tree(repo, rev) if rev else None
m = Measurer(repo, resolve_ruff(repo), known_env=gitio.known_env_names(repo, head)).measure(head, args)
for path, fm in m.items():
    print(f"{path}  FILE_LINES={fm.metrics.get('FILE_LINES')}  hits={sum(fm.hits.values())}")
    for q, u in sorted(fm.units.items(), key=lambda kv: kv[1].line):
        if filt and q not in filt:
            continue
        print(f"  {q:<55} line {u.line:<5} " + " ".join(f"{k}={v}" for k, v in sorted(u.metrics.items())))
```

## Type checker context (not worked in this series)

Pinned `ty==0.0.82` (`ty check --output-format concise --exit-zero`, under 3 minutes on main): **29,617
diagnostics — 29,417 errors, 200 warnings.** Errors by top-level directory: tests 16,304 ·
tui_gateway 3,290 · gateway 2,507 · hermes_cli 2,069 · plugins 1,198 · tools 957 · agent 770 · evals
680 · optional-skills 245 · skills 232 · scripts 154 · acp_adapter 111. Top rules:
unresolved-attribute 13,669 · invalid-argument-type 6,444 · unresolved-reference 3,125 ·
invalid-assignment 1,879 · not-subscriptable 1,818 · unresolved-import 845.

## How to take a unit (read this before touching code)

1. **Set up** (≈5 min): Python 3.14 (`uv python install 3.14`), then from the checkout
   `python3.14 -m pm.build_env --source . --out .venv --group dev --group test`; export
   `HERMES_PYTHON=$PWD/.venv/bin/python`. Never run the full suite; run files with
   `scripts/run_tests.sh <paths>`. Branch off the latest `main`.
2. **Re-measure the unit on current main** with the units snippet above. If its value moved, use the
   new value; if it is already at or under target, mark it done in your report and take the next one.
   Line numbers here are from main@6bb43667 and drift.
3. **Extract, don't shuffle.** Follow the unit's Remedy. When the file is over 2,000 lines (or would
   go over), extracted code goes to a topical sibling `<stem>_<topic>.py`; helpers there import the
   facade's names **inside the function** (`from agent.anthropic_adapter import _x`), so patches on
   the facade still bite (see `agent/anthropic_adapter_kwargs.py`, `hermes_cli/backup_import.py`).
   Every new function must itself meet CC ≤ 20, ≤ 300 lines, nesting ≤ 6. A branch ladder on one name
   becomes a dict → handler table (`tools/memory_tool.py::_WARM_ACTIONS`).
4. **Never move a baselined rule hit.** The ratchet keys a hit by (rule, enclosing function, code
   line). A hit that ends up in a new function — even in the same file — is a NEW blocking finding;
   so is one reordered past an identical line (e.g. two `except Exception:` / `pass` blocks swapped:
   line matching is order-preserving). Keep excepted blocks inline in the original function and in
   their original order, or fix them first as their own C-unit. Each A-entry lists the hits inside it.
5. **Prove zero behavior change.** Run the unit's test files; where branches are thin on tests, add
   a throwaway differential check outside the repo: load main's copy of the module
   (`git show main:path > old.py`, `importlib.util.spec_from_file_location`) and compare old vs new
   over an input grid with recording fakes. Slice 1's three checks took under 2 minutes each to run.
6. **Gate:** `python scripts/check` (full) must pass; `python scripts/check --only health` for quick
   loops. A sibling that moves a `HERMES_*` env read can show HX002 when you measure the working tree
   with an untracked file — the gate (base-side known names) and the committed tree are fine.
7. **Commit:** one commit per unit, `refactor(<area>): …`, body with before → after numbers and the
   test/differential evidence. Push your own branch; no PRs, issues or comments anywhere; never touch
   `.github/workflows`, CI config, `FORK.md`, `scripts/code_health/config.py`; no new
   `# health: allow`, no new `# noqa`.
8. **Leave the queue current:** append a line to Progress log at the end of this doc.

## Rule-family playbooks (Queue C)

Each C-unit is one family in one file. Fixes are judged hit by hit; when a hit cannot be fixed
without changing behavior someone relies on, leave it and note it — never waive it to make a unit go
away (that needs the owner).

- **correctness** (F821 undefined name, F811 redefinition, F841 unused local): F821s are often real
  latent NameErrors (see Notable findings); F811 is usually a duplicate import or a re-def kept by
  merge — delete the shadowed one after checking which binding callers see.
- **async** (ASYNC230/240 blocking file/path I/O in `async def`, HX007 sync config I/O, HX008
  `get_event_loop`, HX009 gather `isinstance(r, Exception)`, RUF006 dangling task): use the rule's fix
  text from `config.py`; every change here changes timing, so test the coroutine's callers.
- **timeout** (HX006 subprocess/urlopen without timeout): pick a timeout the call can actually need
  (read the call site's history), handle `TimeoutExpired`, reap killed children with `proc.wait()`.
- **profile** (HX004/HX005 blocking): resolve at call time (`get_hermes_home()`, `load_config()`) or
  key by `hermes_home_key()`; read `gateway/AGENTS.md` § Profile scope first.
- **structure** (HX011 if/elif ladder): dict/table → handler, as in `_WARM_ACTIONS`.
- **swallow** (BLE001 blind `except Exception`, S110 `try/except/pass`): narrow to the exceptions the
  body can raise, or keep the catch-all and log the traceback (`logger.exception` / `exc_info=True`;
  a message-only `logger.warning(..., e)` does not count). Adding logging is a visible change — keep
  it at debug level on hot paths. 1,258 of the 2,037 C-units are this family; T1 files first.
- **test-sleep** (HX013): replace the sleep with an Event the code under test sets, or
  `tests.waiting.wait_until(predicate, timeout=...)`; a delay that IS the behavior keeps a reviewed
  allow — that is an owner call.

## Owner decisions (proposed — awaiting owner sign-off)

1. **Upstream-identical units.** 89 of 131 Queue A units, 13 of 91 Queue B files and 1,360 of 2,037
   Queue C units sit in code identical to upstream's, and upstream runs the same ratchet. Options:
   (a) skip them and take the fix from upstream at a sync; (b) fix them here and port upstream edits
   by hand afterwards; (c) fix them here and offer nothing upstream (this repo never contributes
   upstream). The pick order below assumes (a) until you say otherwise.
2. **File-size epics on the busiest facades** (Queue B): `agent/auxiliary_client.py` 9,388 lines
   (406 commits/90 days, 847 test patch targets, 236 test files), `gateway/run.py` 6,093 (765
   commits, 281 patch targets, 535 test files), `hermes_cli/gateway.py` 6,017, `cli.py` 3,866.
   Each needs many sessions and every slice moves names other modules and tests import (no re-export
   shims allowed); during heavy concurrent work and upstream syncs that is real merge cost. Decide
   whether and when; Queue B entries give the approach.
3. **JS lane and out-of-lane Python** (Queue D): 350 TypeScript function units (worst: 
   `useSessionActions.resumeSession` CC 216, 1,223 lines), 29 TS files over 2,000 lines, and 36
   rule units under `apps/` — this series never touches desktop/Electron/JS lanes.
4. **Landed commit `49625f56b5`** (run_import) adds a manual-port point at the next upstream sync;
   keep or drop.

## Notable findings — not fixed here (scope: no "while I'm here")

- `plugins/model-providers/router/__init__.py:174,228` — F821 `_DISK_TTL_SECONDS` is never defined.
  Line 228 is outside any try, so whenever an on-disk efforts mirror parses, that function raises
  `NameError` instead of returning the cached efforts. Looks like a real bug; it is C-unit in `queue-c-tier2.md`.
- `hermes_cli/cli_init_mixin.py:382` — F821 `e`: the deferred `_present_store_warning` closure reads
  the `except … as e` name after Python has unbound it, so the rich store-failure warning always falls
  back to the plain one (the details are lost). Real, low impact.
- `agent/anthropic_adapter_kwargs.py::_rename_oauth_tools` keeps the original's dead
  `elif block.get("type") == "tool_result" …: pass` branch verbatim (zero-change move).
- The `build_anthropic_kwargs` 1M-beta body-size estimate keeps three `except Exception: pass`
  around `json.dumps` (S110+BLE001); `json.dumps` on these shapes raises `TypeError`/`ValueError` —
  an easy narrowing for that file's swallow C-unit.
- `--report` counts waived hits in its per-rule totals (BLE001 6,787 vs 6,723 unwaived).
- `gitio.known_env_names` reads tracked files only, so a measurement of a working tree whose new,
  untracked sibling now holds the only `HERMES_*` literal shows a spurious HX002; the gate is not
  affected.

## Pre-existing test failures in this environment (identical on clean main)

- `tests/tools/test_browser_real_profile.py::TestReviewRound3::test_relaunch_path_does_snapshot` —
  "Chrome exited during startup" (no Chrome on the VM).
- `tests/agent/test_hot_tier_audit.py` (7 tests: `test_live_mode_*`,
  `test_live_llm_classification_*`) and
  `tests/agent/test_memory_tool_warm_dispatch.py::TestMemoryRecallEndToEnd::test_recall_returns_results_from_real_warm_store`
  — `ImportError: memory provider 'holographic' is not installed` (the provider left core for the
  catalog; the test env does not install it).

## Next pick (Queue A, lowest upstream-sync cost first)

Owner policy: prefer fork-only or already-diverged units; leave the ~60–90%-similar band to upstream's own ratchet (refactoring them here creates permanent port cost).

Take the first row whose unit is still over cap on current main. Rows tagged `upstream-identical`
wait on Owner decision 1. Full entries follow in Queue A (same IDs).

| pick | unit | tier | over cap | vs upstream |
|---|---|---|---|---|
| 1 | A-008 `agent/anthropic_adapter.py::_strip_unknown_tool_blocks` | T1 core | CC 29/20, NESTING 7/6 | fork-only |
| 2 | A-011 `tools/personas_sync.py::sync_personas` | T1 core | CC 29/20, NESTING 8/6 | fork-only |
| 3 | A-020 `tools/delegation_router.py::route_task_models` | T1 core | CC 26/20 | fork-only |
| 4 | A-025 `tools/delegate_tool.py::_resolve_task_routes` | T1 core | CC 25/20 | fork-only |
| 5 | A-046 `gateway/run_turn.py::GatewayTurnMixin._resolve_session_agent_runtime` | T1 core | CC 22/20 | fork-only |
| 6 | A-048 `hermes_cli/config.py::convert_auxiliary_to_provider_first` | T1 core | CC 22/20 | fork-only |
| 7 | A-128 `tests/hermes_cli/test_diff_replay_no_stale_cells.py::replay` | T4 tests | CC 26/20 | fork-only |
| 8 | A-032 `agent/anthropic_adapter.py::create_anthropic_message` | T1 core | CC 22/20, NESTING 7/6 | diverged (similarity 0.23) |
| 9 | A-070 `cron/scheduler_delivery.py::_deliver_to_bot_chat` | T2 runtime edge | CC 28/20 | diverged (similarity 0.23) |
| 10 | A-059 `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._emit_stream_text` | T1 core | CC 21/20 | diverged (similarity 0.53) |
| 11 | A-004 `gateway/platforms/api_server_openai_routes.py::OpenAICompatRoutesMixin._handle_responses` | T1 core | CC 33/20 | diverged (similarity 0.55) |
| 12 | A-051 `tools/delegate_tool.py::delegate_task` | T1 core | CC 22/20 | diverged (similarity 0.67) |
| 13 | A-001 `hermes_cli/cli_status_bar_mixin.py::CLIStatusBarMixin._get_status_bar_snapshot` | T1 core | CC 40/20, FUNC_LINES 333/300 | diverged (similarity 0.71) |
| 14 | A-038 `agent/agent_runtime_helpers.py::repair_tool_call` | T1 core | CC 23/20 | diverged (similarity 0.71) |
| 15 | A-017 `agent/transports/chat_completions.py::ChatCompletionsTransport.build_kwargs` | T1 core | CC 26/20 | diverged (similarity 0.78) |
| 16 | A-039 `agent/auxiliary_client.py::_resolve_task_provider_model` | T1 core | CC 23/20 | diverged (similarity 0.8) |
| 17 | A-012 `hermes_cli/cli_status_bar_mixin.py::CLIStatusBarMixin._status_bar_segments` | T1 core | CC 28/20 | diverged (similarity 0.81) |
| 18 | A-043 `agent/chat_completion_helpers.py::_StreamingCall._call_anthropic` | T1 core | CC 22/20 | diverged (similarity 0.82) |
| 19 | A-045 `gateway/platforms/api_server_runs.py::_handle_runs` | T1 core | CC 22/20 | diverged (similarity 0.83) |
| 20 | A-063 `hermes_cli/webhook.py::_cmd_subscribe` | T1 core | CC 21/20 | diverged (similarity 0.9) |
| 21 | A-057 `gateway/platforms/base.py::BasePlatformAdapter._handle_message_while_active` | T1 core | CC 21/20 | diverged (similarity 0.91) |
| 22 | A-062 `hermes_cli/models.py::_credential_fingerprint` | T1 core | CC 21/20 | diverged (similarity 0.91) |
| 23 | A-006 `agent/chat_completion_helpers.py::_StreamingCall._call_chat_completions` | T1 core | CC 31/20 | diverged (similarity 0.92) |
| 24 | A-040 `agent/turn_usage.py::record_response_usage` | T1 core | CC 23/20 | diverged (similarity 0.92) |
| 25 | A-019 `gateway/platforms/api_server_openai_routes.py::OpenAICompatRoutesMixin._handle_chat_completions` | T1 core | CC 26/20 | diverged (similarity 0.93) |
| 26 | A-026 `gateway/run_turn.py::GatewayTurnMixin._run_agent_via_proxy` | T1 core | CC 24/20 | diverged (similarity 0.93) |
| 27 | A-072 `cron/scheduler.py::_run_one_job_body` | T2 runtime edge | CC 27/20 | diverged (similarity 0.93) |
| 28 | A-083 `tui_gateway/ws.py::handle_ws` | T2 runtime edge | CC 21/20 | diverged (similarity 0.94) |
| 29 | A-016 `hermes_cli/gateway.py::_get_service_pids` | T1 core | NESTING 8/6 | diverged (similarity 0.95) |
| 30 | A-102 `hermes_constants_scratch.py::_linked_worktree_repos` | T3 tooling | NESTING 7/6 | diverged (similarity 0.96) |
| 31 | A-125 `tests/agent/lsp/_mock_lsp_server.py::main` | T4 tests | CC 38/20 | diverged (similarity 0.96) |
| 32 | A-003 `hermes_cli/main_desktop.py::cmd_gui` | T1 core | CC 36/20 | diverged (similarity 0.97) |
| 33 | A-027 `hermes_cli/cli_info_mixin.py::CLIInfoMixin.show_help` | T1 core | CC 24/20 | diverged (similarity 0.97) |
| 34 | A-028 `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._stream_delta` | T1 core | CC 24/20 | diverged (similarity 0.97) |
| 35 | A-049 `hermes_cli/profile_cmd.py::_profile_create` | T1 core | CC 22/20 | diverged (similarity 0.97) |
| 36 | A-013 `hermes_cli/web_routers/chat_ws.py::pty_ws` | T1 core | CC 28/20 | diverged (similarity 0.98) |
| 37 | A-065 `hermes_state_messages.py::SessionMessagesMixin._rows_to_conversation` | T1 core | CC 21/20 | diverged (similarity 0.98) |
| 38 | A-067 `hermes_cli/subcommands/sessions.py::build_sessions_parser` | T1 core | FUNC_LINES 304/300 | diverged (similarity 0.98) |
| 39 | A-044 `agent/conversation_loop.py::_run_conversation_turn` | T1 core | CC 22/20 | diverged (similarity 0.99) |
| 40 | A-052 `tools/file_tools.py::read_file_tool` | T1 core | CC 22/20 | diverged (similarity 0.99) |
| 41 | A-055 `agent/turn_finalizer.py::finalize_turn` | T1 core | CC 21/20 | diverged (similarity 0.99) |
| 42 | A-018 `agent/turn_final_response.py::finish_text_response` | T1 core | CC 26/20, FUNC_LINES 310/300 | diverged (similarity 1.0) |
| 43 | A-002 `tools/checkpoint_maintenance.py::_prune_checkpoints` | T1 core | CC 38/20 | upstream-identical |
| 44 | A-005 `tools/checkpoint_manager.py::CheckpointManager._restore` | T1 core | CC 32/20 | upstream-identical |
| 45 | A-007 `hermes_cli/backup.py::_create_quick_snapshot_locked` | T1 core | CC 31/20, NESTING 7/6 | upstream-identical |
| 46 | A-009 `hermes_state_holders.py::foreign_state_db_holders` | T1 core | CC 29/20, NESTING 7/6 | upstream-identical |
| 47 | A-010 `tools/browser_vault_tool.py::browser_vault_fill` | T1 core | CC 29/20 | upstream-identical |
| 48 | A-014 `hermes_cli/web_routers/config_env.py::_write_custom_endpoint` | T1 core | CC 27/20 | upstream-identical |
| 49 | A-015 `agent/deadline.py::_process_tree_snapshot` | T1 core | NESTING 8/6 | upstream-identical |
| 50 | A-021 `agent/model_metadata.py::get_model_context_length` | T1 core | CC 25/20 | upstream-identical |
| 51 | A-022 `hermes_cli/gateway.py::find_windows_gateway_services` | T1 core | CC 25/20 | upstream-identical |
| 52 | A-023 `hermes_cli/kanban.py::_cmd_show` | T1 core | CC 25/20 | upstream-identical |
| 53 | A-024 `hermes_cli/plugins_cmd_catalog.py::_carry_user_files` | T1 core | CC 25/20 | upstream-identical |
| 54 | A-029 `hermes_cli/config.py::set_config_value` | T1 core | CC 24/20 | upstream-identical |
| 55 | A-030 `hermes_cli/sessions_cmd.py::_cmd_repair_prompts` | T1 core | CC 24/20 | upstream-identical |
| 56 | A-031 `hermes_cli/web_routers/chat_ws.py::console_ws` | T1 core | CC 24/20 | upstream-identical |
| 57 | A-033 `gateway/platforms/api_server_openai_routes.py::_cap_history_tool_outputs` | T1 core | NESTING 7/6 | upstream-identical |
| 58 | A-034 `gateway/run_heartbeat_restore.py::restore_heartbeat_watches.scan` | T1 core | NESTING 7/6 | upstream-identical |
| 59 | A-035 `tools/approval_detection.py::_quoted_grep_pattern_spans` | T1 core | NESTING 7/6 | upstream-identical |
| 60 | A-036 `tools/process_registry.py::ProcessRegistry._pty_reader_loop` | T1 core | NESTING 7/6 | upstream-identical |
| 61 | A-037 `tools/vision_tools.py::_download_media` | T1 core | NESTING 7/6 | upstream-identical |
| 62 | A-041 `hermes_cli/kanban_db.py::create_task` | T1 core | CC 23/20 | upstream-identical |
| 63 | A-042 `hermes_cli/model_setup_flows_azure.py::_model_flow_azure_foundry` | T1 core | CC 23/20 | upstream-identical |
| 64 | A-047 `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._on_tool_progress` | T1 core | CC 22/20 | upstream-identical |
| 65 | A-050 `hermes_cli/pty_bridge.py::PtyBridge.close` | T1 core | CC 22/20 | upstream-identical |
| 66 | A-053 `agent/auxiliary_client.py::_resolve_api_key_provider` | T1 core | CC 21/20 | upstream-identical |
| 67 | A-054 `agent/conversation_compression.py::try_shrink_image_parts_in_messages` | T1 core | CC 21/20 | upstream-identical |
| 68 | A-056 `agent/turn_recovery.py::recover_after_classification` | T1 core | CC 21/20 | upstream-identical |
| 69 | A-058 `hermes_cli/cli_model_switch_mixin.py::CLIModelSwitchMixin._restore_session_model` | T1 core | CC 21/20 | upstream-identical |
| 70 | A-060 `hermes_cli/env_loader.py::_apply_external_secret_sources` | T1 core | CC 21/20 | upstream-identical |
| 71 | A-061 `hermes_cli/inventory.py::_apply_pricing` | T1 core | CC 21/20 | upstream-identical |
| 72 | A-064 `hermes_state.py::SessionDB._execute_write` | T1 core | CC 21/20 | upstream-identical |
| 73 | A-066 `tools/skills_hub_clawhub.py::ClawHubSource._download_zip` | T1 core | CC 21/20 | upstream-identical |
| 74 | A-068 `plugins/platforms/discord/adapter.py::DiscordAdapter._handle_message` | T2 runtime edge | CC 36/20 | upstream-identical |
| 75 | A-069 `plugins/platforms/discord/adapter.py::VoiceReceiver._on_packet` | T2 runtime edge | CC 31/20 | upstream-identical |
| 76 | A-071 `plugins/platforms/discord/adapter_media.py::DiscordMediaMixin.send_multiple_images` | T2 runtime edge | CC 28/20 | upstream-identical |
| 77 | A-073 `plugins/platforms/discord/adapter.py::_standalone_send` | T2 runtime edge | CC 27/20, NESTING 8/6 | upstream-identical |
| 78 | A-074 `plugins/platforms/slack/block_kit.py::render_blocks` | T2 runtime edge | CC 26/20 | upstream-identical |
| 79 | A-075 `tui_gateway/methods_prompt.py::_` | T2 runtime edge | CC 25/20 | upstream-identical |
| 80 | A-076 `plugins/platforms/slack/block_kit.py::sanitize_blocks` | T2 runtime edge | CC 24/20 | upstream-identical |
| 81 | A-077 `plugins/platforms/whatsapp/adapter.py::WhatsAppAdapter._poll_messages` | T2 runtime edge | NESTING 7/6 | upstream-identical |
| 82 | A-078 `cron/lifecycle_guard.py::_contains_unsafe_gateway_action` | T2 runtime edge | CC 22/20 | upstream-identical |
| 83 | A-079 `plugins/platforms/telegram/adapter.py::TelegramAdapter.edit_message` | T2 runtime edge | CC 22/20 | upstream-identical |
| 84 | A-080 `plugins/platforms/whatsapp/adapter.py::WhatsAppAdapter.connect` | T2 runtime edge | CC 22/20 | upstream-identical |
| 85 | A-081 `plugins/platforms/discord/adapter.py::DiscordAdapter._fetch_channel_context` | T2 runtime edge | CC 21/20 | upstream-identical |
| 86 | A-082 `tui_gateway/session_history.py::_history_to_messages` | T2 runtime edge | CC 21/20 | upstream-identical |
| 87 | A-084 `optional-skills/security/godmode/scripts/auto_jailbreak.py::auto_jailbreak` | T3 tooling | CC 41/20 | upstream-identical |
| 88 | A-085 `optional-skills/creative/comfyui/scripts/ws_monitor.py::main` | T3 tooling | CC 37/20 | upstream-identical |
| 89 | A-086 `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py::Migrator.migrate_provider_keys` | T3 tooling | CC 37/20 | upstream-identical |
| 90 | A-087 `scripts/run_tests_parallel.py::main` | T3 tooling | CC 34/20, FUNC_LINES 446/300 | upstream-identical |
| 91 | A-088 `optional-skills/creative/ai-presenter-video/scripts/preflight.py::main` | T3 tooling | CC 31/20 | upstream-identical |
| 92 | A-089 `optional-skills/creative/comfyui/scripts/run_workflow.py::main` | T3 tooling | CC 29/20 | upstream-identical |
| 93 | A-090 `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py::Migrator.migrate_agent_config` | T3 tooling | CC 28/20 | upstream-identical |
| 94 | A-091 `optional-skills/security/unbroker/scripts/autopilot.py::next_actions` | T3 tooling | CC 28/20 | upstream-identical |
| 95 | A-092 `optional-skills/web-development/har-derived-api-client/scripts/har_to_client.py::main` | T3 tooling | CC 28/20 | upstream-identical |
| 96 | A-093 `skills/productivity/xlsx/scripts/xlsx_restructure.py::main` | T3 tooling | CC 28/20 | upstream-identical |
| 97 | A-094 `scripts/audit-old-updater-imports.py::main` | T3 tooling | CC 27/20 | upstream-identical |
| 98 | A-095 `scripts/validate_plugin_catalog.py::validate_entry` | T3 tooling | CC 27/20 | upstream-identical |
| 99 | A-096 `evals/postmortem/forensics/tools.py::main` | T3 tooling | NESTING 8/6 | upstream-identical |
| 100 | A-097 `evals/providers/reasoning_shapes.py::run_matrix` | T3 tooling | NESTING 8/6 | upstream-identical |
| 101 | A-098 `evals/session_search_schema/runner.py::main` | T3 tooling | NESTING 8/6 | upstream-identical |
| 102 | A-099 `scripts/write_install_stamp.py::build_stamp` | T3 tooling | CC 26/20 | upstream-identical |
| 103 | A-100 `skills/productivity/powerpoint/scripts/pptx_create.py::build_slide` | T3 tooling | CC 25/20 | upstream-identical |
| 104 | A-101 `optional-skills/creative/comfyui/scripts/run_workflow.py::ComfyRunner.monitor_ws` | T3 tooling | CC 24/20 | upstream-identical |
| 105 | A-103 `optional-skills/creative/comfyui/scripts/_common.py::_http_once` | T3 tooling | NESTING 7/6 | upstream-identical |
| 106 | A-104 `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_terminal_growth_vs_wacc` | T3 tooling | NESTING 7/6 | upstream-identical |
| 107 | A-105 `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_terminal_value_proportion` | T3 tooling | NESTING 7/6 | upstream-identical |
| 108 | A-106 `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_wacc_range` | T3 tooling | NESTING 7/6 | upstream-identical |
| 109 | A-107 `scripts/audit-old-updater-imports.py::_audit_versions` | T3 tooling | NESTING 7/6 | upstream-identical |
| 110 | A-108 `evals/desktop_bug_campaign/persistence_live.py::main` | T3 tooling | CC 23/20 | upstream-identical |
| 111 | A-109 `scripts/audit-old-updater-imports.py::_requirements_in` | T3 tooling | CC 23/20 | upstream-identical |
| 112 | A-110 `scripts/render-builds-table.py::main` | T3 tooling | CC 23/20 | upstream-identical |
| 113 | A-111 `skills/productivity/xlsx/scripts/xlsx_edit.py::main` | T3 tooling | CC 23/20 | upstream-identical |
| 114 | A-112 `scripts/audit-old-updater-imports.py::resolve_in_tree` | T3 tooling | CC 22/20 | upstream-identical |
| 115 | A-113 `scripts/generate_icons.py::target_bytes` | T3 tooling | CC 22/20 | upstream-identical |
| 116 | A-114 `scripts/smoke_nemo_relay_shared_metrics.py::_validate_store` | T3 tooling | CC 22/20 | upstream-identical |
| 117 | A-115 `scripts/verify-bootstrap-version-stamp.py::verify_stamp` | T3 tooling | CC 22/20 | upstream-identical |
| 118 | A-116 `skills/productivity/docx/scripts/docx_validate.py::validate` | T3 tooling | CC 22/20 | upstream-identical |
| 119 | A-117 `skills/research/grounded-citations/scripts/sources.py::main` | T3 tooling | CC 22/20 | upstream-identical |
| 120 | A-118 `evals/completion_backlog_probe.py::probe` | T3 tooling | CC 21/20 | upstream-identical |
| 121 | A-119 `optional-skills/security/godmode/scripts/godmode_race.py::score_response` | T3 tooling | CC 21/20 | upstream-identical |
| 122 | A-120 `optional-skills/security/unbroker/scripts/pdd.py::cmd_doctor` | T3 tooling | CC 21/20 | upstream-identical |
| 123 | A-121 `scripts/build/inputs.py::AgentInputs.validate` | T3 tooling | CC 21/20 | upstream-identical |
| 124 | A-122 `scripts/ci/live_comment.py::run` | T3 tooling | CC 21/20 | upstream-identical |
| 125 | A-123 `scripts/profile-tui.py::format_report` | T3 tooling | CC 21/20 | upstream-identical |
| 126 | A-124 `skills/productivity/xlsx/scripts/xlsx_create.py::build_sheet` | T3 tooling | CC 21/20 | upstream-identical |
| 127 | A-126 `tests/plugins/test_telegram_update_admission.py::test_only_pre_handoff_failure_reopens_admission` | T4 tests | CC 35/20 | upstream-identical |
| 128 | A-127 `tests/gateway/test_platform_reconnect.py::TestVoiceInputCallbackWiring.test_startup_wires_voice_input_callback` | T4 tests | NESTING 8/6 | upstream-identical |
| 129 | A-129 `tests/scripts/test_release_sequencer.py::_sequencer_fixture.run` | T4 tests | CC 24/20 | upstream-identical |
| 130 | A-130 `tests/computer_use/live_cua_0_9_smoke.py::run_smoke` | T4 tests | FUNC_LINES 347/300, NESTING 7/6 | upstream-identical |
| 131 | A-131 `tests/gateway/test_platform_reconnect.py::TestStartupPlatformIsolation.test_start_continues_after_platform_connect_timeout` | T4 tests | NESTING 7/6 | upstream-identical |

## Queue A — Python function units (ranked: tier, then how far over the cap)

`sync:` tag — `fork-only` (absent upstream), `diverged` (shared, already differs; similarity in Merge
risk), `upstream-identical` (see Owner decision 1).

#### A-001 · `hermes_cli/cli_status_bar_mixin.py::CLIStatusBarMixin._get_status_bar_snapshot` · sync: diverged
- **Where:** line 206 on main@6bb43667 · T1 core
- **Over cap:** CC 40 (target 20, cap 40), FUNC_LINES 333 (target 300, cap 333) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; FUNC_LINES: extract phases into named helpers or a topical sibling module. `hermes_cli/cli_status_bar_mixin.py` is 1465 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 241 (BLE001+S110), 297 (BLE001+S110), 308 (BLE001+S110), 319 (BLE001+S110), 328 (BLE001+S110), 333 (BLE001+S110), 343 (BLE001+S110), 355 (BLE001+S110), 372 (BLE001+S110), 413 (BLE001+S110), 509 (BLE001), 535 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_pt_chrome_no_vs16.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 23 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_status_bar_mixin.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.71): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-002 · `tools/checkpoint_maintenance.py::_prune_checkpoints` · sync: upstream-identical
- **Where:** line 197 on main@6bb43667 · T1 core
- **Over cap:** CC 38 (target 20, cap 38) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/checkpoint_maintenance.py` is 614 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/gateway/test_checkpoint_prune_housekeeping.py tests/hermes_cli/test_checkpoints_prune.py tests/hermes_cli/test_console_engine.py tests/hermes_cli/test_profile_rename_checkpoints.py tests/test_utils_rmtree_readonly.py tests/tools/test_checkpoint_footprint_notice.py tests/tools/test_checkpoint_manager.py tests/tools/test_checkpoint_size_cap.py tests/tools/test_checkpoint_store_ownership.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 4 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `tools.checkpoint_maintenance.*` in tests; 9 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-003 · `hermes_cli/main_desktop.py::cmd_gui` · sync: diverged
- **Where:** line 1630 on main@6bb43667 · T1 core
- **Over cap:** CC 36 (target 20, cap 36) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/main_desktop.py` is 1947 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `hermes_cli/main_desktop_tcc.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_bundled_desktop_launch.py tests/hermes_cli/test_desktop_build_git_windows.py tests/hermes_cli/test_desktop_build_lock.py tests/hermes_cli/test_desktop_esbuild_ignore_scripts.py tests/hermes_cli/test_desktop_exe_integrity.py tests/hermes_cli/test_desktop_install_after_update.py tests/hermes_cli/test_desktop_native_deps_stale.py tests/hermes_cli/test_desktop_profile_launch.py tests/hermes_cli/test_desktop_socket_tmpdir.py tests/hermes_cli/test_desktop_source_build.py tests/hermes_cli/test_desktop_startup_cost.py tests/hermes_cli/test_desktop_swap_crash_recovery.py` … +7 more; full list: `grep -rlE "hermes_cli\.main_desktop" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `hermes_cli/main.py`: 246 test files reference `hermes_cli.main` (`grep -rlE "hermes_cli\.main\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 75 commits touched this file on main in the 90 days to 2026-10-17; 52 string patch targets on `hermes_cli.main_desktop.*` in tests; 19 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.97): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-004 · `gateway/platforms/api_server_openai_routes.py::OpenAICompatRoutesMixin._handle_responses` · sync: diverged
- **Where:** line 1034 on main@6bb43667 · T1 core
- **Over cap:** CC 33 (target 20, cap 33) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/platforms/api_server_openai_routes.py` is 1336 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 1050 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server_sse_keepalive.py` Also reached through its facade `gateway/platforms/api_server.py`: 62 test files reference `gateway.platforms.api_server` (`grep -rlE "gateway\.platforms\.api_server\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 39 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.platforms.api_server_openai_routes.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.55): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-005 · `tools/checkpoint_manager.py::CheckpointManager._restore` · sync: upstream-identical
- **Where:** line 1147 on main@6bb43667 · T1 core
- **Over cap:** CC 32 (target 20, cap 32) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/checkpoint_manager.py` is 1677 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tools/checkpoint_manager_profile_rename.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_tool_executor_checkpoint_paths.py tests/gateway/test_diff_command.py tests/gateway/test_profile_isolation_runtime.py tests/hermes_cli/test_checkpoints_clear_legacy.py tests/hermes_cli/test_profile_rename_checkpoints.py tests/tools/test_checkpoint_footprint_notice.py tests/tools/test_checkpoint_gitlinks.py tests/tools/test_checkpoint_manager.py tests/tools/test_checkpoint_path_roundtrip.py tests/tools/test_checkpoint_size_cap.py tests/tools/test_checkpoint_store_ownership.py tests/tools/test_git_consumer_environment.py` … +1 more; full list: `grep -rlE "tools\.checkpoint_manager" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 58 commits touched this file on main in the 90 days to 2026-10-17; 23 string patch targets on `tools.checkpoint_manager.*` in tests; 13 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-006 · `agent/chat_completion_helpers.py::_StreamingCall._call_chat_completions` · sync: diverged
- **Where:** line 3432 on main@6bb43667 · T1 core
- **Over cap:** CC 31 (target 20, cap 31) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/chat_completion_helpers.py` is 4590 lines (over the 2,000 target, capped at 4590): extracted code must go to a topical sibling `chat_completion_helpers_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/chat_completion_helpers_relay.py`, `agent/chat_completion_helpers_tool_calls.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_mid_tool_call_drop.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_stream_beta_routing.py tests/agent/test_anthropic_stream_fallbacks.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_interrupt_post_worker.py tests/agent/test_bedrock_stream_completion.py tests/agent/test_bedrock_transport_replay.py tests/agent/test_cascading_interrupt.py tests/agent/test_chat_completion_helpers_provider_sort.py` … +61 more; full list: `grep -rlE "agent\.chat_completion_helpers" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 345 commits touched this file on main in the 90 days to 2026-10-17; 16 string patch targets on `agent.chat_completion_helpers.*` in tests; 73 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.92): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-007 · `hermes_cli/backup.py::_create_quick_snapshot_locked` · sync: upstream-identical
- **Where:** line 1004 at the slice-1 branch head (this file was touched by slice 1) · T1 core
- **Over cap:** CC 31 (target 20, cap 31), NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `hermes_cli/backup.py` is 2079 lines (over the 2,000 target, capped at 2079): extracted code must go to a topical sibling `backup_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/backup_import.py`, `hermes_cli/backup_restore.py`, `hermes_cli/backup_sqlite.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/cron/test_execution_ledger.py tests/hermes_cli/test_backup.py tests/hermes_cli/test_backup_all_profiles.py tests/hermes_cli/test_backup_path_errors.py tests/hermes_cli/test_backup_stability.py tests/hermes_cli/test_backup_zip_serialization.py tests/hermes_cli/test_old_updater_shims.py tests/hermes_cli/test_plugins_hub_live_catalog.py tests/hermes_cli/test_profile_export_credentials.py tests/hermes_cli/test_profile_pm_portability.py tests/hermes_cli/test_restore_source_integrity.py tests/hermes_cli/test_snapshot_restore_oauth.py` … +7 more; full list: `grep -rlE "hermes_cli\.backup" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 105 commits touched this file on main in the 90 days to 2026-10-17; 5 string patch targets on `hermes_cli.backup.*` in tests; 19 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-008 · `agent/anthropic_adapter.py::_strip_unknown_tool_blocks` · sync: fork-only
- **Where:** line 1089 at the slice-1 branch head (this file was touched by slice 1) · T1 core
- **Over cap:** CC 29 (target 20, cap 29), NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `agent/anthropic_adapter.py` is 1987 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `agent/anthropic_adapter_kwargs.py`.
- **Baselined rule hits inside the unit:** lines 1150 (BLE001), 1198 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_agent_init_fork.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_adapter_extra_headers.py tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_anthropic_client_auth.py tests/agent/test_anthropic_keychain.py tests/agent/test_anthropic_kwargs_sanitize.py tests/agent/test_anthropic_mcp_prefix_strip.py tests/agent/test_anthropic_message_stop.py tests/agent/test_anthropic_oauth_ua_prefix.py tests/agent/test_anthropic_request_client_reuse.py tests/agent/test_anthropic_route_oauth_identity.py` … +43 more; full list: `grep -rlE "agent\.anthropic_adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 119 commits touched this file on main in the 90 days to 2026-10-17; 122 string patch targets on `agent.anthropic_adapter.*` in tests; 55 test files reference it. Upstream (NousResearch main@18c525b6): fork-only function (absent upstream): no upstream-sync cost

#### A-009 · `hermes_state_holders.py::foreign_state_db_holders` · sync: upstream-identical
- **Where:** line 425 on main@6bb43667 · T1 core
- **Over cap:** CC 29 (target 20, cap 29), NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `hermes_state_holders.py` is 658 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 435 (BLE001), 536 (BLE001), 557 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_doctor_journal_modes.py tests/hermes_cli/test_doctor_wal_checkpoint_guard.py tests/hermes_cli/test_observational_sessiondb_modes.py tests/hermes_cli/test_sessions_set_journal_mode.py tests/hermes_state/test_fts_holder_instance_scope.py tests/hermes_state/test_fts_runtime_rebuild.py tests/hermes_state/test_state_db_holders.py tests/hermes_state/test_state_db_holders_windows_live.py tests/hermes_state/test_state_db_repair_live_writer_guard.py tests/hermes_state/test_state_db_second_process_maintenance.py` Also reached through its facade `hermes_state.py`: 536 test files reference `hermes_state` (`grep -rlE "(from hermes_state import|^import hermes_state\b|[\"']hermes_state\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 15 commits touched this file on main in the 90 days to 2026-10-17; 2 string patch targets on `hermes_state_holders.*` in tests; 10 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-010 · `tools/browser_vault_tool.py::browser_vault_fill` · sync: upstream-identical
- **Where:** line 399 on main@6bb43667 · T1 core
- **Over cap:** CC 29 (target 20, cap 29) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/browser_vault_tool.py` is 664 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 531 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_vault_backends.py tests/tools/test_bot_desktop_browser_fence.py tests/tools/test_browser_vault.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 15 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `tools.browser_vault_tool.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-011 · `tools/personas_sync.py::sync_personas` · sync: fork-only
- **Where:** line 208 on main@6bb43667 · T1 core
- **Over cap:** CC 29 (target 20, cap 29), NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `tools/personas_sync.py` is 663 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/tools/test_personas_sync.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 1 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `tools.personas_sync.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): fork-only file (absent upstream): no upstream-sync cost

#### A-012 · `hermes_cli/cli_status_bar_mixin.py::CLIStatusBarMixin._status_bar_segments` · sync: diverged
- **Where:** line 1200 on main@6bb43667 · T1 core
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_status_bar_mixin.py` is 1465 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_pt_chrome_no_vs16.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 23 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_status_bar_mixin.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.81): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-013 · `hermes_cli/web_routers/chat_ws.py::pty_ws` · sync: diverged
- **Where:** line 539 on main@6bb43667 · T1 core
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/web_routers/chat_ws.py` is 772 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `hermes_cli/web_routers/chat_ws_errors.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_serve_mcp_discovery_after_bind.py tests/hermes_cli/test_web_multi_profile_scope.py tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_console_ws.py tests/hermes_cli/web_routers/test_chat_ws_lease_holder.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.web_routers.chat_ws.*` in tests; 5 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.98): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-014 · `hermes_cli/web_routers/config_env.py::_write_custom_endpoint` · sync: upstream-identical
- **Where:** line 573 on main@6bb43667 · T1 core
- **Over cap:** CC 27 (target 20, cap 27) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/web_routers/config_env.py` is 1109 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_gemini_express_key_probes.py tests/hermes_cli/test_shared_metrics_signals.py tests/hermes_cli/test_web_routers_endpoint_probe.py tests/hermes_cli/test_web_server.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.web_routers.config_env.*` in tests; 4 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-015 · `agent/deadline.py::_process_tree_snapshot` · sync: upstream-identical
- **Where:** line 365 on main@6bb43667 · T1 core
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `agent/deadline.py` is 481 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_deadline.py tests/agent/test_deadline_fork_race.py tests/agent/test_treekill_consolidation.py tests/cron/test_cron_script.py tests/gateway/test_telegram_init_deadline.py tests/tools/test_approval_timeout_overflow.py tests/tools/test_browser_process_tree.py tests/tools/test_browser_suspect_recycle.py tests/tools/test_mcp_timeout_resolution.py tests/tools/test_terminal_bounded_execute.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 22 commits touched this file on main in the 90 days to 2026-10-17; 30 string patch targets on `agent.deadline.*` in tests; 10 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-016 · `hermes_cli/gateway.py::_get_service_pids` · sync: diverged
- **Where:** line 119 on main@6bb43667 · T1 core
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `hermes_cli/gateway.py` is 6017 lines (over the 2,000 target, capped at 6017): extracted code must go to a topical sibling `gateway_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/gateway_command_errors.py`, `hermes_cli/gateway_enroll.py`, `hermes_cli/gateway_launchd.py`, `hermes_cli/gateway_migrate.py`, `hermes_cli/gateway_migrate_guards.py`, `hermes_cli/gateway_multiplex_mode.py` ….
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/cron/test_cronjob_gateway_liveness.py tests/cron/test_jobs_file_ownership.py tests/gateway/test_control_socket.py tests/gateway/test_gateway_command_line_matcher.py tests/gateway/test_host_gateway_profile_env.py tests/gateway/test_host_topology.py tests/gateway/test_multiplex_lifecycle.py tests/gateway/test_multiplex_secondary_port_binding_env.py tests/gateway/test_orphan_exit_grace.py tests/gateway/test_restart_after_turn.py tests/gateway/test_status.py tests/gateway/test_status_strict_identity_live.py` … +101 more; full list: `grep -rlE "hermes_cli\.gateway" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 289 commits touched this file on main in the 90 days to 2026-10-17; 89 string patch targets on `hermes_cli.gateway.*` in tests; 113 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.95): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-017 · `agent/transports/chat_completions.py::ChatCompletionsTransport.build_kwargs` · sync: diverged
- **Where:** line 575 on main@6bb43667 · T1 core
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/transports/chat_completions.py` is 848 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_compressed_summary_metadata.py tests/agent/test_declared_conversation_scope.py tests/agent/test_gemini_thinking_config.py tests/agent/test_mistral_tool_ids.py tests/agent/test_model_extra_type_guard.py tests/agent/test_prompt_cache_scope.py tests/agent/test_provider_history_parity.py tests/agent/test_provider_id_mint_normalization.py tests/agent/test_reasoning_carriers.py tests/agent/test_reasoning_effort_wire_translation.py tests/agent/test_reasoning_replay_routes.py tests/agent/test_replay_cleanup.py` … +29 more; full list: `grep -rlE "agent\.transports\.chat_completions" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 71 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `agent.transports.chat_completions.*` in tests; 41 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.78): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-018 · `agent/turn_final_response.py::finish_text_response` · sync: diverged
- **Where:** line 57 on main@6bb43667 · T1 core
- **Over cap:** CC 26 (target 20, cap 26), FUNC_LINES 310 (target 300, cap 310) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; FUNC_LINES: extract phases into named helpers or a topical sibling module. `agent/turn_final_response.py` is 379 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_bare_toolcall_recovery.py` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 26 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `agent.turn_final_response.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 1.0): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-019 · `gateway/platforms/api_server_openai_routes.py::OpenAICompatRoutesMixin._handle_chat_completions` · sync: diverged
- **Where:** line 636 on main@6bb43667 · T1 core
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/platforms/api_server_openai_routes.py` is 1336 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 649 (BLE001), 713 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server_sse_keepalive.py` Also reached through its facade `gateway/platforms/api_server.py`: 62 test files reference `gateway.platforms.api_server` (`grep -rlE "gateway\.platforms\.api_server\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 39 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.platforms.api_server_openai_routes.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.93): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-020 · `tools/delegation_router.py::route_task_models` · sync: fork-only
- **Where:** line 380 on main@6bb43667 · T1 core
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/delegation_router.py` is 717 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/plugins/model_providers/test_exo_reasoning_wire.py tests/test_delegation_router.py tests/test_delegation_router_escalate_only.py tests/tools/test_delegate_auto_route_diagnostics.py tests/tools/test_delegate_auto_route_gate.py tests/tools/test_delegate_auto_route_visibility.py tests/tools/test_delegate_role_fallback.py tests/tools/test_delegate_role_provider.py tests/tools/test_delegate_tier_role_provider.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 6 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tools.delegation_router.*` in tests; 9 test files reference it. Upstream (NousResearch main@18c525b6): fork-only file (absent upstream): no upstream-sync cost

#### A-021 · `agent/model_metadata.py::get_model_context_length` · sync: upstream-identical
- **Where:** line 2238 on main@6bb43667 · T1 core
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/model_metadata.py` is 2691 lines (over the 2,000 target, capped at 2691): extracted code must go to a topical sibling `model_metadata_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/model_metadata_http.py`, `agent/model_metadata_llamacpp.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_server.py tests/agent/metadata_transport.py tests/agent/test_413_compression.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_aux_progress_streaming.py tests/agent/test_auxiliary_client.py tests/agent/test_background_review_input_budget.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_context_cache.py tests/agent/test_budget_reasoning_details_exclusion.py tests/agent/test_capability_probe_credentials.py` … +114 more; full list: `grep -rlE "agent\.model_metadata" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 187 commits touched this file on main in the 90 days to 2026-10-17; 328 string patch targets on `agent.model_metadata.*` in tests; 126 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-022 · `hermes_cli/gateway.py::find_windows_gateway_services` · sync: upstream-identical
- **Where:** line 866 on main@6bb43667 · T1 core
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/gateway.py` is 6017 lines (over the 2,000 target, capped at 6017): extracted code must go to a topical sibling `gateway_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/gateway_command_errors.py`, `hermes_cli/gateway_enroll.py`, `hermes_cli/gateway_launchd.py`, `hermes_cli/gateway_migrate.py`, `hermes_cli/gateway_migrate_guards.py`, `hermes_cli/gateway_multiplex_mode.py` ….
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/cron/test_cronjob_gateway_liveness.py tests/cron/test_jobs_file_ownership.py tests/gateway/test_control_socket.py tests/gateway/test_gateway_command_line_matcher.py tests/gateway/test_host_gateway_profile_env.py tests/gateway/test_host_topology.py tests/gateway/test_multiplex_lifecycle.py tests/gateway/test_multiplex_secondary_port_binding_env.py tests/gateway/test_orphan_exit_grace.py tests/gateway/test_restart_after_turn.py tests/gateway/test_status.py tests/gateway/test_status_strict_identity_live.py` … +101 more; full list: `grep -rlE "hermes_cli\.gateway" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 289 commits touched this file on main in the 90 days to 2026-10-17; 89 string patch targets on `hermes_cli.gateway.*` in tests; 113 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-023 · `hermes_cli/kanban.py::_cmd_show` · sync: upstream-identical
- **Where:** line 474 on main@6bb43667 · T1 core
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/kanban.py` is 1436 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `hermes_cli/kanban_boards.py`, `hermes_cli/kanban_db.py`, `hermes_cli/kanban_db_boards.py`, `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_dispatch.py`, `hermes_cli/kanban_db_graph.py` ….
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_board_path_priority.py tests/hermes_cli/test_kanban_cli.py tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_create_body_file.py tests/hermes_cli/test_kanban_creator_origin.py tests/hermes_cli/test_kanban_db_repair.py tests/hermes_cli/test_kanban_dispatch_claim_allowlist.py tests/hermes_cli/test_kanban_gc_retention.py tests/hermes_cli/test_kanban_goal_judge_affinity.py tests/hermes_cli/test_kanban_goal_mode.py tests/hermes_cli/test_kanban_notify.py` … +6 more; full list: `grep -rlE "hermes_cli\.kanban" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 77 commits touched this file on main in the 90 days to 2026-10-17; 4 string patch targets on `hermes_cli.kanban.*` in tests; 18 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-024 · `hermes_cli/plugins_cmd_catalog.py::_carry_user_files` · sync: upstream-identical
- **Where:** line 369 on main@6bb43667 · T1 core
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/plugins_cmd_catalog.py` is 915 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_plugin_catalog_known_issues.py tests/hermes_cli/test_plugin_update_transaction.py tests/hermes_cli/test_plugin_validate.py tests/hermes_cli/test_plugins_cmd_catalog.py tests/hermes_cli/test_plugins_hub_perf_guard.py tests/tui_gateway/test_plugins_manage_install.py` Also reached through its facade `hermes_cli/plugins_cmd.py`: 44 test files reference `hermes_cli.plugins_cmd` (`grep -rlE "hermes_cli\.plugins_cmd\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 42 commits touched this file on main in the 90 days to 2026-10-17; 4 string patch targets on `hermes_cli.plugins_cmd_catalog.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-025 · `tools/delegate_tool.py::_resolve_task_routes` · sync: fork-only
- **Where:** line 658 on main@6bb43667 · T1 core
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/delegate_tool.py` is 1708 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tools/delegate_tool_child_run.py`, `tools/delegate_tool_config.py`, `tools/delegate_tool_dispatch.py`, `tools/delegate_tool_progress.py`, `tools/delegate_tool_registry.py`, `tools/delegate_tool_results.py` ….
- **Baselined rule hits inside the unit:** lines 681 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_transport_a_registration.py tests/agent/test_agent_guardrails.py tests/agent/test_interrupt_compat.py tests/agent/test_oneshot_footprint.py tests/agent/test_review_engine.py tests/agent/test_sequential_tool_timeout.py tests/agent/test_subagent_lifecycle.py tests/agent/test_subagent_progress.py tests/agent/test_subagent_stop_hook.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_completion_delivery.py tests/gateway/test_review_command.py` … +66 more; full list: `grep -rlE "tools\.delegate_tool" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 180 commits touched this file on main in the 90 days to 2026-10-17; 93 string patch targets on `tools.delegate_tool.*` in tests; 78 test files reference it. Upstream (NousResearch main@18c525b6): fork-only function (absent upstream): no upstream-sync cost

#### A-026 · `gateway/run_turn.py::GatewayTurnMixin._run_agent_via_proxy` · sync: diverged
- **Where:** line 2752 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/run_turn.py` is 4386 lines (over the 2,000 target, capped at 4386): extracted code must go to a topical sibling `run_turn_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `gateway/run_turn_followup_ack.py`, `gateway/run_turn_runner.py`, `gateway/run_turn_runner_approval_settle.py`, `gateway/run_turn_runner_clarify_delivery.py`.
- **Baselined rule hits inside the unit:** lines 2780 (HX004), 2893 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_failed_turn_site_codes.py tests/agent/test_multiplex_base_url_scope.py tests/gateway/test_empty_sentinel_rewrite_copy.py tests/gateway/test_hygiene_no_commit_reason.py tests/gateway/test_interim_only_consumer_warning.py tests/gateway/test_pre_agent_fallback_notice.py tests/gateway/test_session_hygiene.py tests/gateway/test_stream_consumer.py tests/gateway/test_tool_log_mode.py tests/gateway/test_transient_failure_transcript.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 169 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_turn.*` in tests; 10 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.93): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-027 · `hermes_cli/cli_info_mixin.py::CLIInfoMixin.show_help` · sync: diverged
- **Where:** line 245 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_info_mixin.py` is 1136 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 284 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_cli_info_i18n.py tests/hermes_cli/test_skill_collision_visibility.py tests/tools/test_explicit_mcp_schema_reload.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 35 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_info_mixin.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.97): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-028 · `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._stream_delta` · sync: diverged
- **Where:** line 319 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_stream_mixin.py` is 969 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_warning_callback_conservation.py tests/tui_gateway/test_credit_producer_suppression.py tests/tui_gateway/test_foreground_notification_snapshot.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_stream_mixin.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.97): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-029 · `hermes_cli/config.py::set_config_value` · sync: upstream-identical
- **Where:** line 4071 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/config.py` is 4722 lines (over the 2,000 target, capped at 4722): extracted code must go to a topical sibling `config_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/config_backups.py`, `hermes_cli/config_check_diagnostics.py`, `hermes_cli/config_defaults.py`, `hermes_cli/config_effective.py`, `hermes_cli/config_env_routing.py`, `hermes_cli/config_home.py` ….
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_acp_commands.py tests/acp_adapter/test_acp_mcp_discovery.py tests/acp_adapter/test_failed_turn_closure.py tests/acp_adapter/test_named_provider_catalogs.py tests/acp_adapter/test_session.py tests/agent/lsp/test_broken_set.py tests/agent/lsp/test_install_and_lint_fixes.py tests/agent/lsp/test_root_policy.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_adapter_extra_headers.py tests/agent/test_anthropic_prompt_cache_policy.py` … +568 more; full list: `grep -rlE "hermes_cli\.config" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 384 commits touched this file on main in the 90 days to 2026-10-17; 890 string patch targets on `hermes_cli.config.*` in tests; 580 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-030 · `hermes_cli/sessions_cmd.py::_cmd_repair_prompts` · sync: upstream-identical
- **Where:** line 1053 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/sessions_cmd.py` is 1259 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `hermes_cli/sessions_cmd_browse.py`, `hermes_cli/sessions_cmd_journal_mode.py`, `hermes_cli/sessions_cmd_repair_profiles.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_lazy_command_exports.py tests/hermes_cli/test_observational_sessiondb_modes.py tests/hermes_cli/test_sessions_error_exit_codes.py tests/hermes_cli/test_sessions_held_store_gate.py tests/hermes_cli/test_sessions_list_truncation.py tests/hermes_cli/test_sessions_repair_check_only_exit.py tests/hermes_cli/test_sessions_repair_profiles.py tests/hermes_cli/test_sessions_repair_prompts.py tests/hermes_cli/test_sessions_set_journal_mode.py tests/hermes_cli/test_sessions_size_delta_label.py tests/hermes_cli/test_sqlite3_cli_salvage_gate.py tests/hermes_cli/test_ux_messages_chat_sessions.py` … +3 more; full list: `grep -rlE "hermes_cli\.sessions_cmd" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 84 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.sessions_cmd.*` in tests; 15 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-031 · `hermes_cli/web_routers/chat_ws.py::console_ws` · sync: upstream-identical
- **Where:** line 300 on main@6bb43667 · T1 core
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/web_routers/chat_ws.py` is 772 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `hermes_cli/web_routers/chat_ws_errors.py`.
- **Baselined rule hits inside the unit:** lines 320 (BLE001), 351 (BLE001 in `console_ws.run_command`), 448 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_serve_mcp_discovery_after_bind.py tests/hermes_cli/test_web_multi_profile_scope.py tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_console_ws.py tests/hermes_cli/web_routers/test_chat_ws_lease_holder.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.web_routers.chat_ws.*` in tests; 5 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-032 · `agent/anthropic_adapter.py::create_anthropic_message` · sync: diverged
- **Where:** line 1826 at the slice-1 branch head (this file was touched by slice 1) · T1 core
- **Over cap:** CC 22 (target 20, cap 22), NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `agent/anthropic_adapter.py` is 1987 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `agent/anthropic_adapter_kwargs.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_agent_init_fork.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_adapter_extra_headers.py tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_anthropic_client_auth.py tests/agent/test_anthropic_keychain.py tests/agent/test_anthropic_kwargs_sanitize.py tests/agent/test_anthropic_mcp_prefix_strip.py tests/agent/test_anthropic_message_stop.py tests/agent/test_anthropic_oauth_ua_prefix.py tests/agent/test_anthropic_request_client_reuse.py tests/agent/test_anthropic_route_oauth_identity.py` … +43 more; full list: `grep -rlE "agent\.anthropic_adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 119 commits touched this file on main in the 90 days to 2026-10-17; 122 string patch targets on `agent.anthropic_adapter.*` in tests; 55 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.23): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-033 · `gateway/platforms/api_server_openai_routes.py::_cap_history_tool_outputs` · sync: upstream-identical
- **Where:** line 115 on main@6bb43667 · T1 core
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `gateway/platforms/api_server_openai_routes.py` is 1336 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server_sse_keepalive.py` Also reached through its facade `gateway/platforms/api_server.py`: 62 test files reference `gateway.platforms.api_server` (`grep -rlE "gateway\.platforms\.api_server\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 39 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.platforms.api_server_openai_routes.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-034 · `gateway/run_heartbeat_restore.py::restore_heartbeat_watches.scan` · sync: upstream-identical
- **Where:** line 36 on main@6bb43667 · T1 core
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `gateway/run_heartbeat_restore.py` is 79 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/gateway/test_heartbeat_execution_ownership.py tests/gateway/test_heartbeat_watch_restore.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_heartbeat_restore.*` in tests; 2 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-035 · `tools/approval_detection.py::_quoted_grep_pattern_spans` · sync: upstream-identical
- **Where:** line 790 on main@6bb43667 · T1 core
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `tools/approval_detection.py` is 1572 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_approval_transport.py tests/tools/test_approval.py tests/tools/test_approval_deny_rules.py tests/tools/test_approval_global_flag_scan.py tests/tools/test_approval_grep_in_substitution.py tests/tools/test_approval_launchctl_performance.py tests/tools/test_blocked_command_guidance.py tests/tools/test_denial_circuit_breaker.py tests/tools/test_execute_code_approval_cluster.py tests/tools/test_execution_flag_detection.py tests/tools/test_hardline_escaped_quote_state.py` Also reached through its facade `tools/approval.py`: 92 test files reference `tools.approval` (`grep -rlE "tools\.approval\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 34 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tools.approval_detection.*` in tests; 11 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-036 · `tools/process_registry.py::ProcessRegistry._pty_reader_loop` · sync: upstream-identical
- **Where:** line 1539 on main@6bb43667 · T1 core
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `tools/process_registry.py` is 3078 lines (over the 2,000 target, capped at 3078): extracted code must go to a topical sibling `process_registry_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `tools/process_registry_checkpoint.py`, `tools/process_registry_env_log.py`, `tools/process_registry_notifications.py`, `tools/process_registry_results.py`, `tools/process_registry_termination.py`.
- **Baselined rule hits inside the unit:** lines 1580 (BLE001), 1582 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_owned_process_cleanup.py tests/agent/test_review_engine.py tests/cron/test_restart_safe_worker.py tests/cron/test_restart_safe_worker_death_evidence.py tests/gateway/test_abandoned_turn_process_cleanup.py tests/gateway/test_agents_command_delegations.py tests/gateway/test_api_server.py tests/gateway/test_api_server_active_work_drain.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_background_process_notifications.py tests/gateway/test_child_notice_profile_admission.py tests/gateway/test_clean_shutdown_marker.py` … +83 more; full list: `grep -rlE "tools\.process_registry" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 168 commits touched this file on main in the 90 days to 2026-10-17; 107 string patch targets on `tools.process_registry.*` in tests; 95 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-037 · `tools/vision_tools.py::_download_media` · sync: upstream-identical
- **Where:** line 258 on main@6bb43667 · T1 core
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `tools/vision_tools.py` is 1191 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tools/vision_tools_history_budget.py`, `tools/vision_tools_image_prep.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_grid_sweep_cli_tui_stream_diagnostics.py tests/agent/test_image_routing.py tests/agent/test_image_shrink_recovery.py tests/agent/test_models_dev_provider_capabilities.py tests/agent/test_run_agent.py tests/agent/test_vision_routing.py tests/computer_use/test_cua_perf_knobs.py tests/gateway/test_vision_memory_leak.py tests/gateway/test_vision_preprocess.py tests/hermes_cli/test_browser_connect_media_permissions.py tests/hermes_cli/test_cli_custom_provider_vision.py tests/hermes_cli/test_managed_vision_capability.py` … +16 more; full list: `grep -rlE "tools\.vision_tools" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 85 string patch targets on `tools.vision_tools.*` in tests; 28 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-038 · `agent/agent_runtime_helpers.py::repair_tool_call` · sync: diverged
- **Where:** line 2668 on main@6bb43667 · T1 core
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/agent_runtime_helpers.py` is 3936 lines (over the 2,000 target, capped at 3936): extracted code must go to a topical sibling `agent_runtime_helpers_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/agent_runtime_helpers_dead_connections.py`, `agent/agent_runtime_helpers_placeholders.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_prompt_cache_policy.py tests/agent/test_billing_unverified_carrythrough.py tests/agent/test_cache_disabled_on_stubs.py tests/agent/test_codex_model_entitlement_rotation.py tests/agent/test_codex_soft_failure_pool_rotation.py tests/agent/test_codex_token_expired_replay_recovery.py tests/agent/test_context_compressor.py tests/agent/test_context_engine_message_uid.py tests/agent/test_create_openai_client_reuse.py tests/agent/test_credential_pool_routing.py tests/agent/test_cursor_optimizations_parity.py` … +64 more; full list: `grep -rlE "agent\.agent_runtime_helpers" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 274 commits touched this file on main in the 90 days to 2026-10-17; 11 string patch targets on `agent.agent_runtime_helpers.*` in tests; 76 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.71): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-039 · `agent/auxiliary_client.py::_resolve_task_provider_model` · sync: diverged
- **Where:** line 6671 on main@6bb43667 · T1 core
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/auxiliary_client.py` is 9388 lines (over the 2,000 target, capped at 9388): extracted code must go to a topical sibling `auxiliary_client_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/auxiliary_client_registry.py`.
- **Baselined rule hits inside the unit:** lines 6712 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_anthropic_owned_routing.py tests/agent/test_anthropic_route_oauth_identity.py tests/agent/test_anthropic_spent_rotation_verdict.py tests/agent/test_anthropic_structured_output.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_arcee_trinity_overrides.py tests/agent/test_async_httpx_del_neuter.py tests/agent/test_auth_provider_failover.py tests/agent/test_aux_affordable_402_retry.py` … +224 more; full list: `grep -rlE "agent\.auxiliary_client" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 406 commits touched this file on main in the 90 days to 2026-10-17; 847 string patch targets on `agent.auxiliary_client.*` in tests; 236 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.8): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-040 · `agent/turn_usage.py::record_response_usage` · sync: diverged
- **Where:** line 155 on main@6bb43667 · T1 core
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/turn_usage.py` is 410 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 384 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_bedrock_context_cache.py tests/agent/test_generation_stats_capture.py tests/agent/test_nous_wire_auto.py tests/agent/test_status_history_ttft.py tests/agent/test_turn_usage_api_calls_row.py tests/agent/test_turn_usage_log_line.py` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 23 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `agent.turn_usage.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.92): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-041 · `hermes_cli/kanban_db.py::create_task` · sync: upstream-identical
- **Where:** line 1184 on main@6bb43667 · T1 core
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/kanban_db.py` is 4450 lines (over the 2,000 target, capped at 4450): extracted code must go to a topical sibling `kanban_db_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/kanban_db_boards.py`, `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_dispatch.py`, `hermes_cli/kanban_db_graph.py`, `hermes_cli/kanban_db_notify.py`, `hermes_cli/kanban_db_workspace.py`.
- **Baselined rule hits inside the unit:** lines 1232 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/lsp/test_workspace_release.py tests/agent/test_kanban_turn_recovery.py tests/agent/test_title_generator.py tests/conftest.py tests/cron/test_cron_kanban_env_isolation.py tests/gateway/test_busy_wake_admission.py tests/gateway/test_kanban_changes_requested_notifier.py tests/gateway/test_kanban_multiplex_served_profile_parity.py tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_apiserver_wake.py tests/gateway/test_kanban_notifier_served_apiserver_wake.py tests/gateway/test_kanban_notifier_wake_only_ordering.py` … +99 more; full list: `grep -rlE "hermes_cli\.kanban_db" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `hermes_cli/kanban.py`: 18 test files reference `hermes_cli.kanban` (`grep -rlE "hermes_cli\.kanban\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 170 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `hermes_cli.kanban_db.*` in tests; 111 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-042 · `hermes_cli/model_setup_flows_azure.py::_model_flow_azure_foundry` · sync: upstream-identical
- **Where:** line 151 on main@6bb43667 · T1 core
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/model_setup_flows_azure.py` is 272 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "model_setup_flows_azure" tests` Also reached through its facade `hermes_cli/model_setup_flows.py`: 18 test files reference `hermes_cli.model_setup_flows` (`grep -rlE "hermes_cli\.model_setup_flows\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.model_setup_flows_azure.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-043 · `agent/chat_completion_helpers.py::_StreamingCall._call_anthropic` · sync: diverged
- **Where:** line 3834 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/chat_completion_helpers.py` is 4590 lines (over the 2,000 target, capped at 4590): extracted code must go to a topical sibling `chat_completion_helpers_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/chat_completion_helpers_relay.py`, `agent/chat_completion_helpers_tool_calls.py`.
- **Baselined rule hits inside the unit:** lines 3923 (HX011) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_mid_tool_call_drop.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_stream_beta_routing.py tests/agent/test_anthropic_stream_fallbacks.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_interrupt_post_worker.py tests/agent/test_bedrock_stream_completion.py tests/agent/test_bedrock_transport_replay.py tests/agent/test_cascading_interrupt.py tests/agent/test_chat_completion_helpers_provider_sort.py` … +61 more; full list: `grep -rlE "agent\.chat_completion_helpers" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 345 commits touched this file on main in the 90 days to 2026-10-17; 16 string patch targets on `agent.chat_completion_helpers.*` in tests; 73 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.82): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-044 · `agent/conversation_loop.py::_run_conversation_turn` · sync: diverged
- **Where:** line 1797 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/conversation_loop.py` is 2113 lines (over the 2,000 target, capped at 2113): extracted code must go to a topical sibling `conversation_loop_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3).
- **Baselined rule hits inside the unit:** lines 1947 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_failed_turn_closure.py tests/agent/test_413_compression.py tests/agent/test_anthropic_billing_guidance.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_background_review.py tests/agent/test_background_review_input_budget.py tests/agent/test_bare_toolcall_recovery.py tests/agent/test_billing_unverified_carrythrough.py tests/agent/test_bot_chat_toolset_refresh.py tests/agent/test_canon_args_memo_parity.py tests/agent/test_codex_reasoning_only_streak.py tests/agent/test_compression_budget_rearm.py` … +57 more; full list: `grep -rlE "agent\.conversation_loop" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 337 commits touched this file on main in the 90 days to 2026-10-17; 41 string patch targets on `agent.conversation_loop.*` in tests; 69 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.99): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-045 · `gateway/platforms/api_server_runs.py::_handle_runs` · sync: diverged
- **Where:** line 620 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/platforms/api_server_runs.py` is 1320 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 629 (BLE001), 725 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server_active_work_drain.py tests/gateway/test_api_server_runs.py tests/gateway/test_api_server_runs_import_isolation.py tests/gateway/test_api_server_sse_keepalive.py tests/gateway/test_owner_liveness_drift.py tests/gateway/test_peer_dm_into_open_bot_chat.py tests/gateway/test_shutdown_executor_quiesce.py` Also reached through its facade `gateway/platforms/api_server.py`: 62 test files reference `gateway.platforms.api_server` (`grep -rlE "gateway\.platforms\.api_server\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 60 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.platforms.api_server_runs.*` in tests; 7 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.83): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-046 · `gateway/run_turn.py::GatewayTurnMixin._resolve_session_agent_runtime` · sync: fork-only
- **Where:** line 171 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/run_turn.py` is 4386 lines (over the 2,000 target, capped at 4386): extracted code must go to a topical sibling `run_turn_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `gateway/run_turn_followup_ack.py`, `gateway/run_turn_runner.py`, `gateway/run_turn_runner_approval_settle.py`, `gateway/run_turn_runner_clarify_delivery.py`.
- **Baselined rule hits inside the unit:** lines 233 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_failed_turn_site_codes.py tests/agent/test_multiplex_base_url_scope.py tests/gateway/test_empty_sentinel_rewrite_copy.py tests/gateway/test_hygiene_no_commit_reason.py tests/gateway/test_interim_only_consumer_warning.py tests/gateway/test_pre_agent_fallback_notice.py tests/gateway/test_session_hygiene.py tests/gateway/test_stream_consumer.py tests/gateway/test_tool_log_mode.py tests/gateway/test_transient_failure_transcript.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 169 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_turn.*` in tests; 10 test files reference it. Upstream (NousResearch main@18c525b6): fork-only function (absent upstream): no upstream-sync cost

#### A-047 · `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._on_tool_progress` · sync: upstream-identical
- **Where:** line 824 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_stream_mixin.py` is 969 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 842 (BLE001+S110), 847 (BLE001), 875 (BLE001+S110), 895 (BLE001+S110), 911 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_warning_callback_conservation.py tests/tui_gateway/test_credit_producer_suppression.py tests/tui_gateway/test_foreground_notification_snapshot.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_stream_mixin.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-048 · `hermes_cli/config.py::convert_auxiliary_to_provider_first` · sync: fork-only
- **Where:** line 1554 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/config.py` is 4722 lines (over the 2,000 target, capped at 4722): extracted code must go to a topical sibling `config_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/config_backups.py`, `hermes_cli/config_check_diagnostics.py`, `hermes_cli/config_defaults.py`, `hermes_cli/config_effective.py`, `hermes_cli/config_env_routing.py`, `hermes_cli/config_home.py` ….
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_acp_commands.py tests/acp_adapter/test_acp_mcp_discovery.py tests/acp_adapter/test_failed_turn_closure.py tests/acp_adapter/test_named_provider_catalogs.py tests/acp_adapter/test_session.py tests/agent/lsp/test_broken_set.py tests/agent/lsp/test_install_and_lint_fixes.py tests/agent/lsp/test_root_policy.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_adapter_extra_headers.py tests/agent/test_anthropic_prompt_cache_policy.py` … +568 more; full list: `grep -rlE "hermes_cli\.config" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 384 commits touched this file on main in the 90 days to 2026-10-17; 890 string patch targets on `hermes_cli.config.*` in tests; 580 test files reference it. Upstream (NousResearch main@18c525b6): fork-only function (absent upstream): no upstream-sync cost

#### A-049 · `hermes_cli/profile_cmd.py::_profile_create` · sync: diverged
- **Where:** line 188 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/profile_cmd.py` is 640 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 238 (BLE001), 246 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_apply_profile_override.py tests/hermes_cli/test_profile_alias_orphan_removal.py tests/hermes_cli/test_profile_export_default_path.py tests/hermes_cli/test_profile_identity_purge_cmd.py tests/hermes_cli/test_profile_install_env_encoding.py tests/hermes_cli/test_profiles.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.profile_cmd.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.97): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-050 · `hermes_cli/pty_bridge.py::PtyBridge.close` · sync: upstream-identical
- **Where:** line 286 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/pty_bridge.py` is 381 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 312 (BLE001), 326 (BLE001+S110), 365 (BLE001+S110), 374 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_pty_bridge.py tests/hermes_cli/test_pty_session.py tests/hermes_cli/test_web_server.py tests/hermes_cli/test_web_server_pty_import.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 21 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.pty_bridge.*` in tests; 4 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-051 · `tools/delegate_tool.py::delegate_task` · sync: diverged
- **Where:** line 1247 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/delegate_tool.py` is 1708 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tools/delegate_tool_child_run.py`, `tools/delegate_tool_config.py`, `tools/delegate_tool_dispatch.py`, `tools/delegate_tool_progress.py`, `tools/delegate_tool_registry.py`, `tools/delegate_tool_results.py` ….
- **Baselined rule hits inside the unit:** lines 1283 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_transport_a_registration.py tests/agent/test_agent_guardrails.py tests/agent/test_interrupt_compat.py tests/agent/test_oneshot_footprint.py tests/agent/test_review_engine.py tests/agent/test_sequential_tool_timeout.py tests/agent/test_subagent_lifecycle.py tests/agent/test_subagent_progress.py tests/agent/test_subagent_stop_hook.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_completion_delivery.py tests/gateway/test_review_command.py` … +66 more; full list: `grep -rlE "tools\.delegate_tool" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 180 commits touched this file on main in the 90 days to 2026-10-17; 93 string patch targets on `tools.delegate_tool.*` in tests; 78 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.67): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-052 · `tools/file_tools.py::read_file_tool` · sync: diverged
- **Where:** line 606 on main@6bb43667 · T1 core
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/file_tools.py` is 1483 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tools/file_tools_paths.py`, `tools/file_tools_read_tracking.py`, `tools/file_tools_write_guards.py`.
- **Baselined rule hits inside the unit:** lines 755 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_background_review_toolset_restriction.py tests/agent/test_file_safety_credentials.py tests/agent/test_file_safety_session_state.py tests/agent/test_inline_edit_persistence.py tests/agent/test_nt_namespace_guard.py tests/agent/test_terminal_approval_batch.py tests/agent/test_tool_guardrails.py tests/agent/test_verification_evidence.py tests/hermes_cli/test_cli_provider_resolution.py tests/hermes_cli/test_shared_metrics_harness.py tests/tools/test_accretion_caps.py tests/tools/test_binary_document_write_guard.py` … +40 more; full list: `grep -rlE "tools\.file_tools" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 91 commits touched this file on main in the 90 days to 2026-10-17; 67 string patch targets on `tools.file_tools.*` in tests; 52 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.99): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-053 · `agent/auxiliary_client.py::_resolve_api_key_provider` · sync: upstream-identical
- **Where:** line 2321 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/auxiliary_client.py` is 9388 lines (over the 2,000 target, capped at 9388): extracted code must go to a topical sibling `auxiliary_client_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/auxiliary_client_registry.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_anthropic_owned_routing.py tests/agent/test_anthropic_route_oauth_identity.py tests/agent/test_anthropic_spent_rotation_verdict.py tests/agent/test_anthropic_structured_output.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_arcee_trinity_overrides.py tests/agent/test_async_httpx_del_neuter.py tests/agent/test_auth_provider_failover.py tests/agent/test_aux_affordable_402_retry.py` … +224 more; full list: `grep -rlE "agent\.auxiliary_client" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 406 commits touched this file on main in the 90 days to 2026-10-17; 847 string patch targets on `agent.auxiliary_client.*` in tests; 236 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-054 · `agent/conversation_compression.py::try_shrink_image_parts_in_messages` · sync: upstream-identical
- **Where:** line 4473 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/conversation_compression.py` is 4564 lines (over the 2,000 target, capped at 4564): extracted code must go to a topical sibling `conversation_compression_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `agent/conversation_compression_archive.py`, `agent/conversation_compression_codex.py`, `agent/conversation_compression_deadline.py`, `agent/conversation_compression_manual.py`, `agent/conversation_compression_reply_anchor.py`, `agent/conversation_compression_telemetry.py`.
- **Baselined rule hits inside the unit:** lines 4482 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/repro_image_shrink_brick.py tests/agent/test_413_compression.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_aux_progress_streaming.py tests/agent/test_aux_stream_host_deadline.py tests/agent/test_codex_app_server_compaction.py tests/agent/test_compaction_events.py tests/agent/test_compress_context_progress_timeout.py tests/agent/test_compress_signal_leak.py tests/agent/test_compression_abort_state_reset.py tests/agent/test_compression_attempt_effect.py` … +63 more; full list: `grep -rlE "agent\.conversation_compression" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 315 commits touched this file on main in the 90 days to 2026-10-17; 28 string patch targets on `agent.conversation_compression.*` in tests; 75 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-055 · `agent/turn_finalizer.py::finalize_turn` · sync: diverged
- **Where:** line 525 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/turn_finalizer.py` is 816 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 568 (BLE001), 678 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_background_review.py tests/agent/test_context_engine_on_turn_complete_usage.py tests/agent/test_detached_fork_lifecycle_hooks.py tests/agent/test_load_time_durability_stamp.py tests/agent/test_pre_compress_checkpoint_contract.py tests/agent/test_relay_compaction.py tests/agent/test_skip_background_review.py tests/agent/test_surrogate_chokepoints.py tests/agent/test_tool_call_incremental_persistence.py tests/agent/test_transform_llm_output_persistence.py tests/agent/test_turn_end_stall_diagnostics.py tests/agent/test_turn_finalizer_cleanup_guard.py` … +5 more; full list: `grep -rlE "agent\.turn_finalizer" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 78 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `agent.turn_finalizer.*` in tests; 17 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.99): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-056 · `agent/turn_recovery.py::recover_after_classification` · sync: upstream-identical
- **Where:** line 627 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `agent/turn_recovery.py` is 1984 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `agent/turn_recovery_autorecover.py`, `agent/turn_recovery_codex.py`.
- **Baselined rule hits inside the unit:** lines 737 (BLE001+S110), 772 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_codex_token_expired_replay_recovery.py tests/agent/test_error_classifier.py tests/agent/test_failed_turn_chat_copy.py tests/agent/test_fast_mode_auto.py tests/agent/test_free_tier_rate_limit_class.py tests/agent/test_image_rejection_fallback.py tests/agent/test_image_shrink_recovery.py tests/agent/test_nonretryable_result_carries_verdict.py tests/agent/test_nous_welcome_client_contract.py tests/agent/test_rate_limit_observability_call_sites.py tests/agent/test_rate_limit_reset_hint.py` … +7 more; full list: `grep -rlE "agent\.turn_recovery" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 101 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `agent.turn_recovery.*` in tests; 19 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-057 · `gateway/platforms/base.py::BasePlatformAdapter._handle_message_while_active` · sync: diverged
- **Where:** line 4077 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `gateway/platforms/base.py` is 4930 lines (over the 2,000 target, capped at 4930): extracted code must go to a topical sibling `base_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `gateway/platforms/base_exec_approval.py`.
- **Baselined rule hits inside the unit:** lines 4114 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_file_mutation_verifier.py tests/agent/test_generated_media_durable.py tests/agent/test_proxy_bypass_shared.py tests/agent/test_surrogate_chokepoints.py tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py tests/cron/test_cron_live_delivery_confirmation.py tests/cron/test_cron_reconnect_only_rejection.py tests/cron/test_cron_relay_dm_origin_user_id.py tests/cron/test_media_delivery_parity.py tests/cron/test_media_send_timeout.py tests/cron/test_scheduler.py tests/gateway/platforms/test_yuanbao_state_cleanup.py` … +241 more; full list: `grep -rlE "gateway\.platforms\.base" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 221 commits touched this file on main in the 90 days to 2026-10-17; 88 string patch targets on `gateway.platforms.base.*` in tests; 253 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.91): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-058 · `hermes_cli/cli_model_switch_mixin.py::CLIModelSwitchMixin._restore_session_model` · sync: upstream-identical
- **Where:** line 448 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_model_switch_mixin.py` is 957 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 489 (BLE001), 512 (BLE001), 525 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_cli_model_switch_i18n.py tests/hermes_cli/test_codex_credential_host_binding.py tests/hermes_cli/test_model_persist_one_shape.py tests/hermes_cli/test_model_picker_expensive_confirm.py tests/hermes_cli/test_model_switch_reasoning_flag.py tests/hermes_cli/test_model_switch_reasoning_reresolve.py tests/hermes_cli/test_show_model_picker_exhausted_pool.py tests/tui_gateway/test_resume_switched_provider_endpoint.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 37 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `hermes_cli.cli_model_switch_mixin.*` in tests; 8 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-059 · `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._emit_stream_text` · sync: diverged
- **Where:** line 437 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/cli_stream_mixin.py` is 969 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 466 (BLE001), 566 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_warning_callback_conservation.py tests/tui_gateway/test_credit_producer_suppression.py tests/tui_gateway/test_foreground_notification_snapshot.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_stream_mixin.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.53): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-060 · `hermes_cli/env_loader.py::_apply_external_secret_sources` · sync: upstream-identical
- **Where:** line 632 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/env_loader.py` is 823 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 646 (BLE001), 672 (BLE001), 680 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_bitwarden_secrets.py tests/agent/test_command_secret_source.py tests/agent/test_credential_pool.py tests/agent/test_env_loader_applied_homes.py tests/agent/test_env_loader_reload_restore.py tests/agent/test_env_loader_secret_sources.py tests/agent/test_secret_scope.py tests/agent/test_secret_scope_env_cache.py tests/agent/test_serves_routed_profile_pin.py tests/cron/test_cleanup_timeout.py tests/cron/test_cron_incidents.py tests/cron/test_cron_mcp_toolset_empty_block.py` … +60 more; full list: `grep -rlE "hermes_cli\.env_loader" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 74 commits touched this file on main in the 90 days to 2026-10-17; 82 string patch targets on `hermes_cli.env_loader.*` in tests; 72 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-061 · `hermes_cli/inventory.py::_apply_pricing` · sync: upstream-identical
- **Where:** line 865 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/inventory.py` is 1059 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 897 (BLE001), 956 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/conftest.py tests/acp_adapter/test_named_provider_catalogs.py tests/acp_adapter/test_server.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_models_dev_provider_capabilities.py tests/agent/test_openai_ultrafast.py tests/gateway/test_api_server_model_options_include_unconfigured.py tests/hermes_cli/test_aggregator_overlap_twin_upstream.py tests/hermes_cli/test_anon_picker.py tests/hermes_cli/test_aux_picker_inventory.py tests/hermes_cli/test_cli_model_once.py tests/hermes_cli/test_code_skew.py` … +25 more; full list: `grep -rlE "hermes_cli\.inventory" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 74 commits touched this file on main in the 90 days to 2026-10-17; 33 string patch targets on `hermes_cli.inventory.*` in tests; 37 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-062 · `hermes_cli/models.py::_credential_fingerprint` · sync: diverged
- **Where:** line 2025 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/models.py` is 3100 lines (over the 2,000 target, capped at 3100): extracted code must go to a topical sibling `models_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_cli/models_catalog_static.py`, `hermes_cli/models_detect.py`, `hermes_cli/models_local.py`, `hermes_cli/models_pricing.py`, `hermes_cli/models_profile_cache.py`, `hermes_cli/models_reasoning_caps.py` ….
- **Baselined rule hits inside the unit:** lines 2044 (BLE001+S110), 2056 (BLE001+S110), 2064 (BLE001+S110), 2085 (BLE001+S110), 2093 (BLE001+S110), 2120 (BLE001+S110 in `_credential_fingerprint._mtime_part`), 2132 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_named_provider_catalogs.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_reasoning_catalog_floor.py tests/agent/test_bedrock_integration.py tests/agent/test_capability_probe_credentials.py tests/agent/test_copilot_acp_deprecation.py tests/agent/test_copilot_initiator.py tests/agent/test_custom_provider_ca_probes.py tests/agent/test_fast_mode_auto.py tests/agent/test_nous_welcome_client_contract.py tests/agent/test_openai_ultrafast.py` … +136 more; full list: `grep -rlE "hermes_cli\.models" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 259 commits touched this file on main in the 90 days to 2026-10-17; 275 string patch targets on `hermes_cli.models.*` in tests; 148 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.91): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-063 · `hermes_cli/webhook.py::_cmd_subscribe` · sync: diverged
- **Where:** line 178 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_cli/webhook.py` is 361 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_dashboard_admin_endpoints.py tests/hermes_cli/test_webhook_cli.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 22 commits touched this file on main in the 90 days to 2026-10-17; 6 string patch targets on `hermes_cli.webhook.*` in tests; 2 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.9): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-064 · `hermes_state.py::SessionDB._execute_write` · sync: upstream-identical
- **Where:** line 1069 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_state.py` is 2066 lines (over the 2,000 target, capped at 2066): extracted code must go to a topical sibling `hermes_state_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `hermes_state_common.py`, `hermes_state_compression.py`, `hermes_state_coverage.py`, `hermes_state_dbfile.py`, `hermes_state_errors.py`, `hermes_state_fts.py` ….
- **Baselined rule hits inside the unit:** lines 1114 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_empty_session_persistence.py tests/acp_adapter/test_failed_turn_closure.py tests/acp_adapter/test_session.py tests/acp_adapter/test_session_cwd_column.py tests/acp_adapter/test_session_db_private_access.py tests/acp_adapter/test_session_provenance.py tests/agent/test_413_compression.py tests/agent/test_860_dedup.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_thinking_block_order.py tests/agent/test_api_content_row_addressed_backfill.py tests/agent/test_api_content_sidecar.py` … +524 more; full list: `grep -rlE "hermes_state" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 417 commits touched this file on main in the 90 days to 2026-10-17; 35 string patch targets on `hermes_state.*` in tests; 536 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-065 · `hermes_state_messages.py::SessionMessagesMixin._rows_to_conversation` · sync: diverged
- **Where:** line 1565 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `hermes_state_messages.py` is 1997 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "hermes_state_messages" tests` Also reached through its facade `hermes_state.py`: 536 test files reference `hermes_state` (`grep -rlE "(from hermes_state import|^import hermes_state\b|[\"']hermes_state\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 110 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_state_messages.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.98): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-066 · `tools/skills_hub_clawhub.py::ClawHubSource._download_zip` · sync: upstream-identical
- **Where:** line 487 on main@6bb43667 · T1 core
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tools/skills_hub_clawhub.py` is 577 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_skills_hub.py tests/tools/test_clawhub_owner_http.py tests/tools/test_clawhub_zip_stream.py tests/tools/test_skills_hub_clawhub.py` Also reached through its facade `tools/skills_hub.py`: 14 test files reference `tools.skills_hub` (`grep -rlE "tools\.skills_hub\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 16 commits touched this file on main in the 90 days to 2026-10-17; 5 string patch targets on `tools.skills_hub_clawhub.*` in tests; 4 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-067 · `hermes_cli/subcommands/sessions.py::build_sessions_parser` · sync: diverged
- **Where:** line 22 on main@6bb43667 · T1 core
- **Over cap:** FUNC_LINES 304 (target 300, cap 304) — done when every metric is at or under its target
- **Remedy:** FUNC_LINES: extract phases into named helpers or a topical sibling module. `hermes_cli/subcommands/sessions.py` is 332 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_sessions_held_store_gate.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 19 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.subcommands.sessions.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.98): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-068 · `plugins/platforms/discord/adapter.py::DiscordAdapter._handle_message` · sync: upstream-identical
- **Where:** line 6360 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 36 (target 20, cap 36) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/discord/adapter.py` is 7674 lines (over the 2,000 target, capped at 7674): extracted code must go to a topical sibling `adapter_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `plugins/platforms/discord/adapter_component_auth.py`, `plugins/platforms/discord/adapter_media.py`, `plugins/platforms/discord/adapter_slash_auth.py`, `plugins/platforms/discord/adapter_thread_titles.py`, `plugins/platforms/discord/adapter_voice_info.py`.
- **Baselined rule hits inside the unit:** lines 6469 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_completion_admission.py tests/gateway/test_discord_admission_thread_exemption.py tests/gateway/test_discord_allow_bots_yaml_config.py tests/gateway/test_discord_allowed_mentions.py tests/gateway/test_discord_approval_mentions.py tests/gateway/test_discord_attachment_download.py tests/gateway/test_discord_attachment_receipts.py tests/gateway/test_discord_auto_thread_prompt_parity.py tests/gateway/test_discord_auto_thread_rename_pin.py` … +59 more; full list: `grep -rlE "plugins\.platforms\.discord\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 177 commits touched this file on main in the 90 days to 2026-10-17; 12 string patch targets on `plugins.platforms.discord.adapter.*` in tests; 71 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-069 · `plugins/platforms/discord/adapter.py::VoiceReceiver._on_packet` · sync: upstream-identical
- **Where:** line 948 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 31 (target 20, cap 31) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/discord/adapter.py` is 7674 lines (over the 2,000 target, capped at 7674): extracted code must go to a topical sibling `adapter_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `plugins/platforms/discord/adapter_component_auth.py`, `plugins/platforms/discord/adapter_media.py`, `plugins/platforms/discord/adapter_slash_auth.py`, `plugins/platforms/discord/adapter_thread_titles.py`, `plugins/platforms/discord/adapter_voice_info.py`.
- **Baselined rule hits inside the unit:** lines 1007 (BLE001), 1060 (BLE001), 1099 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_completion_admission.py tests/gateway/test_discord_admission_thread_exemption.py tests/gateway/test_discord_allow_bots_yaml_config.py tests/gateway/test_discord_allowed_mentions.py tests/gateway/test_discord_approval_mentions.py tests/gateway/test_discord_attachment_download.py tests/gateway/test_discord_attachment_receipts.py tests/gateway/test_discord_auto_thread_prompt_parity.py tests/gateway/test_discord_auto_thread_rename_pin.py` … +59 more; full list: `grep -rlE "plugins\.platforms\.discord\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 177 commits touched this file on main in the 90 days to 2026-10-17; 12 string patch targets on `plugins.platforms.discord.adapter.*` in tests; 71 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-070 · `cron/scheduler_delivery.py::_deliver_to_bot_chat` · sync: diverged
- **Where:** line 773 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `cron/scheduler_delivery.py` is 2131 lines (over the 2,000 target, capped at 2131): extracted code must go to a topical sibling `scheduler_delivery_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `cron/scheduler_delivery_origin.py`.
- **Baselined rule hits inside the unit:** lines 872 (BLE001), 882 (BLE001), 909 (BLE001), 972 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_exit_cleanup_interrupt.py tests/cron/test_bot_chat_cli_home.py tests/cron/test_bot_chat_delivery_child_env.py tests/cron/test_bot_chat_own_profile_multiplex_home.py tests/cron/test_bot_chat_pending.py tests/cron/test_bot_chat_pending_identity.py tests/cron/test_bot_chat_timeout_marker.py tests/cron/test_bot_chat_warning_replay.py tests/cron/test_cleanup_timeout.py tests/cron/test_cron_bot_chat_delivery.py tests/cron/test_cron_delivery_redaction.py tests/cron/test_cron_failure_deliver.py` … +37 more; full list: `grep -rlE "cron\.scheduler_delivery" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `cron/scheduler.py`: 125 test files reference `cron.scheduler` (`grep -rlE "cron\.scheduler\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 52 string patch targets on `cron.scheduler_delivery.*` in tests; 49 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.23): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-071 · `plugins/platforms/discord/adapter_media.py::DiscordMediaMixin.send_multiple_images` · sync: upstream-identical
- **Where:** line 132 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/discord/adapter_media.py` is 459 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 148 (BLE001), 155 (BLE001), 174 (ASYNC240), 181 (ASYNC240), 226 (BLE001), 273 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/gateway/test_discord_send.py` Also reached through its facade `plugins/platforms/discord/adapter.py`: 71 test files reference `plugins.platforms.discord.adapter` (`grep -rlE "plugins\.platforms\.discord\.adapter\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 9 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `plugins.platforms.discord.adapter_media.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-072 · `cron/scheduler.py::_run_one_job_body` · sync: diverged
- **Where:** line 3304 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 27 (target 20, cap 27) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `cron/scheduler.py` is 4437 lines (over the 2,000 target, capped at 4437): extracted code must go to a topical sibling `scheduler_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `cron/scheduler_delivery.py`, `cron/scheduler_delivery_origin.py`, `cron/scheduler_detached_worker.py`, `cron/scheduler_diagnostics.py`, `cron/scheduler_failure_copy.py`, `cron/scheduler_liveness.py` ….
- **Baselined rule hits inside the unit:** lines 3497 (BLE001), 3503 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_exit_cleanup_interrupt.py tests/agent/test_max_iteration_handoff.py tests/agent/test_prompt_cache_boundary.py tests/cron/test_agent_scheduling_gate.py tests/cron/test_cleanup_timeout.py tests/cron/test_codex_execution_paths.py tests/cron/test_context_from_response_extraction.py tests/cron/test_cron_bot_chat_delivery.py tests/cron/test_cron_context_from.py tests/cron/test_cron_emfile_stall.py tests/cron/test_cron_empty_payload.py tests/cron/test_cron_empty_turn_stays_silent.py` … +113 more; full list: `grep -rlE "cron\.scheduler" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 355 commits touched this file on main in the 90 days to 2026-10-17; 280 string patch targets on `cron.scheduler.*` in tests; 125 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.93): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-073 · `plugins/platforms/discord/adapter.py::_standalone_send` · sync: upstream-identical
- **Where:** line 7379 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 27 (target 20, cap 27), NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; NESTING: return early, or extract the inner block into a helper. `plugins/platforms/discord/adapter.py` is 7674 lines (over the 2,000 target, capped at 7674): extracted code must go to a topical sibling `adapter_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `plugins/platforms/discord/adapter_component_auth.py`, `plugins/platforms/discord/adapter_media.py`, `plugins/platforms/discord/adapter_slash_auth.py`, `plugins/platforms/discord/adapter_thread_titles.py`, `plugins/platforms/discord/adapter_voice_info.py`.
- **Baselined rule hits inside the unit:** lines 7417 (ASYNC240), 7442 (BLE001), 7473 (ASYNC240), 7485 (BLE001), 7497 (ASYNC230), 7507 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_completion_admission.py tests/gateway/test_discord_admission_thread_exemption.py tests/gateway/test_discord_allow_bots_yaml_config.py tests/gateway/test_discord_allowed_mentions.py tests/gateway/test_discord_approval_mentions.py tests/gateway/test_discord_attachment_download.py tests/gateway/test_discord_attachment_receipts.py tests/gateway/test_discord_auto_thread_prompt_parity.py tests/gateway/test_discord_auto_thread_rename_pin.py` … +59 more; full list: `grep -rlE "plugins\.platforms\.discord\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 177 commits touched this file on main in the 90 days to 2026-10-17; 12 string patch targets on `plugins.platforms.discord.adapter.*` in tests; 71 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-074 · `plugins/platforms/slack/block_kit.py::render_blocks` · sync: upstream-identical
- **Where:** line 323 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/slack/block_kit.py` is 621 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 480 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_slack_block_kit.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 16 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `plugins.platforms.slack.block_kit.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-075 · `tui_gateway/methods_prompt.py::_` · sync: upstream-identical
- **Where:** line 736 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tui_gateway/methods_prompt.py` is 1455 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 879 (HX012) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/tui_gateway/test_resume_readonly_mount.py tests/tui_gateway/test_submit_row_session_target.py tests/tui_gateway/test_tui_gateway_server_deep_truncate.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 105 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tui_gateway.methods_prompt.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-076 · `plugins/platforms/slack/block_kit.py::sanitize_blocks` · sync: upstream-identical
- **Where:** line 535 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/slack/block_kit.py` is 621 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 567 (HX011), 619 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_slack_block_kit.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 16 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `plugins.platforms.slack.block_kit.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-077 · `plugins/platforms/whatsapp/adapter.py::WhatsAppAdapter._poll_messages` · sync: upstream-identical
- **Where:** line 909 on main@6bb43667 · T2 runtime edge
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `plugins/platforms/whatsapp/adapter.py` is 1242 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 920 (RUF006), 927 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_config_driven_access_policy.py tests/gateway/test_media_metadata_contract.py tests/gateway/test_multiplex_adapter_registry.py tests/gateway/test_pairing_allowlist_bypass.py tests/gateway/test_stream_consumer.py tests/gateway/test_text_batch_delay_defaults.py tests/gateway/test_whatsapp_allowlist_lid_resolution.py tests/gateway/test_whatsapp_bridge_pidfile.py tests/gateway/test_whatsapp_connect.py tests/gateway/test_whatsapp_formatting.py tests/gateway/test_whatsapp_from_owner.py` … +12 more; full list: `grep -rlE "plugins\.platforms\.whatsapp\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 71 commits touched this file on main in the 90 days to 2026-10-17; 40 string patch targets on `plugins.platforms.whatsapp.adapter.*` in tests; 24 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-078 · `cron/lifecycle_guard.py::_contains_unsafe_gateway_action` · sync: upstream-identical
- **Where:** line 1093 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `cron/lifecycle_guard.py` is 1293 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/cron/test_lifecycle_guard_budget.py tests/cron/test_lifecycle_guard_heredoc_walk.py tests/cron/test_lifecycle_guard_live_db.py tests/cron/test_lifecycle_guard_profile_scan.py tests/hermes_cli/test_gateway_restart_loop.py tests/tools/test_approval.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 60 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `cron.lifecycle_guard.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-079 · `plugins/platforms/telegram/adapter.py::TelegramAdapter.edit_message` · sync: upstream-identical
- **Where:** line 3819 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/telegram/adapter.py` is 7319 lines (over the 2,000 target, capped at 7319): extracted code must go to a topical sibling `adapter_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3).
- **Baselined rule hits inside the unit:** lines 3893 (BLE001), 3927 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/conftest.py tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_allowed_channels_widening.py tests/gateway/test_dm_topics.py tests/gateway/test_gateway_platform_event_hook.py tests/gateway/test_late_plugin_rewire.py tests/gateway/test_multiplex_endpoint_identity_scope.py tests/gateway/test_multiplex_interactive_auth.py tests/gateway/test_native_warning_coverage.py tests/gateway/test_platform_adapter_i18n.py tests/gateway/test_platform_plugin_handlers.py` … +79 more; full list: `grep -rlE "plugins\.platforms\.telegram\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 218 commits touched this file on main in the 90 days to 2026-10-17; 35 string patch targets on `plugins.platforms.telegram.adapter.*` in tests; 91 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-080 · `plugins/platforms/whatsapp/adapter.py::WhatsAppAdapter.connect` · sync: upstream-identical
- **Where:** line 605 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/whatsapp/adapter.py` is 1242 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 636 (BLE001), 667 (BLE001), 684 (ASYNC230) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_config_driven_access_policy.py tests/gateway/test_media_metadata_contract.py tests/gateway/test_multiplex_adapter_registry.py tests/gateway/test_pairing_allowlist_bypass.py tests/gateway/test_stream_consumer.py tests/gateway/test_text_batch_delay_defaults.py tests/gateway/test_whatsapp_allowlist_lid_resolution.py tests/gateway/test_whatsapp_bridge_pidfile.py tests/gateway/test_whatsapp_connect.py tests/gateway/test_whatsapp_formatting.py tests/gateway/test_whatsapp_from_owner.py` … +12 more; full list: `grep -rlE "plugins\.platforms\.whatsapp\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 71 commits touched this file on main in the 90 days to 2026-10-17; 40 string patch targets on `plugins.platforms.whatsapp.adapter.*` in tests; 24 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-081 · `plugins/platforms/discord/adapter.py::DiscordAdapter._fetch_channel_context` · sync: upstream-identical
- **Where:** line 5491 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `plugins/platforms/discord/adapter.py` is 7674 lines (over the 2,000 target, capped at 7674): extracted code must go to a topical sibling `adapter_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3). Existing siblings: `plugins/platforms/discord/adapter_component_auth.py`, `plugins/platforms/discord/adapter_media.py`, `plugins/platforms/discord/adapter_slash_auth.py`, `plugins/platforms/discord/adapter_thread_titles.py`, `plugins/platforms/discord/adapter_voice_info.py`.
- **Baselined rule hits inside the unit:** lines 5630 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_completion_admission.py tests/gateway/test_discord_admission_thread_exemption.py tests/gateway/test_discord_allow_bots_yaml_config.py tests/gateway/test_discord_allowed_mentions.py tests/gateway/test_discord_approval_mentions.py tests/gateway/test_discord_attachment_download.py tests/gateway/test_discord_attachment_receipts.py tests/gateway/test_discord_auto_thread_prompt_parity.py tests/gateway/test_discord_auto_thread_rename_pin.py` … +59 more; full list: `grep -rlE "plugins\.platforms\.discord\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** high — 177 commits touched this file on main in the 90 days to 2026-10-17; 12 string patch targets on `plugins.platforms.discord.adapter.*` in tests; 71 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-082 · `tui_gateway/session_history.py::_history_to_messages` · sync: upstream-identical
- **Where:** line 285 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tui_gateway/session_history.py` is 535 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_sessions_rest_scaffold_notices.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 27 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tui_gateway.session_history.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-083 · `tui_gateway/ws.py::handle_ws` · sync: diverged
- **Where:** line 295 on main@6bb43667 · T2 runtime edge
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tui_gateway/ws.py` is 477 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 330 (BLE001 in `handle_ws._dispatch_loop`), 386 (BLE001), 404 (BLE001), 452 (BLE001), 457 (BLE001), 467 (BLE001), 471 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit. With this many, take this file's swallow C-unit first (narrow or log each except, per its fix text), then extract freely
- **Tests:** `scripts/run_tests.sh tests/gateway/test_browser_control_cloud.py tests/gateway/test_scale_to_zero_dashboard_client.py tests/hermes_cli/test_serve_mcp_discovery_after_bind.py tests/test_hermes_logging.py tests/tui_gateway/test_backend_heartbeat.py tests/tui_gateway/test_cold_start_gil_stall.py tests/tui_gateway/test_multi_client_fanout.py tests/tui_gateway/test_protocol.py tests/tui_gateway/test_tui_gateway_ws.py tests/tui_gateway/test_ws_keepalive.py tests/tui_gateway/test_ws_send_timeout.py tests/tui_gateway/test_ws_surrogate_send.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 49 commits touched this file on main in the 90 days to 2026-10-17; 4 string patch targets on `tui_gateway.ws.*` in tests; 12 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.94): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-084 · `optional-skills/security/godmode/scripts/auto_jailbreak.py::auto_jailbreak` · sync: upstream-identical
- **Where:** line 427 on main@6bb43667 · T3 tooling
- **Over cap:** CC 41 (target 20, cap 41) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/security/godmode/scripts/auto_jailbreak.py` is 776 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 486 (F821), 546 (F821), 556 (F821), 593 (F821), 626 (F821) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "auto_jailbreak" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.security.godmode.scripts.auto_jailbreak.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-085 · `optional-skills/creative/comfyui/scripts/ws_monitor.py::main` · sync: upstream-identical
- **Where:** line 111 on main@6bb43667 · T3 tooling
- **Over cap:** CC 37 (target 20, cap 37) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/creative/comfyui/scripts/ws_monitor.py` is 267 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 185 (BLE001), 194 (HX011), 262 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "ws_monitor" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.creative.comfyui.scripts.ws_monitor.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-086 · `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py::Migrator.migrate_provider_keys` · sync: upstream-identical
- **Where:** line 1666 on main@6bb43667 · T3 tooling
- **Over cap:** CC 37 (target 20, cap 37) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` is 3253 lines (over the 2,000 target, capped at 3253): extracted code must go to a topical sibling `openclaw_to_hermes_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3).
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "openclaw_to_hermes" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 17 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.migration.openclaw-migration.scripts.openclaw_to_hermes.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-087 · `scripts/run_tests_parallel.py::main` · sync: upstream-identical
- **Where:** line 1188 on main@6bb43667 · T3 tooling
- **Over cap:** CC 34 (target 20, cap 34), FUNC_LINES 446 (target 300, cap 446) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler; FUNC_LINES: extract phases into named helpers or a topical sibling module. `scripts/run_tests_parallel.py` is 1699 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 1522 (BLE001 in `main._on_done`) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "run_tests_parallel" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 49 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.run_tests_parallel.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-088 · `optional-skills/creative/ai-presenter-video/scripts/preflight.py::main` · sync: upstream-identical
- **Where:** line 50 on main@6bb43667 · T3 tooling
- **Over cap:** CC 31 (target 20, cap 31) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/creative/ai-presenter-video/scripts/preflight.py` is 177 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "preflight" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.creative.ai-presenter-video.scripts.preflight.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-089 · `optional-skills/creative/comfyui/scripts/run_workflow.py::main` · sync: upstream-identical
- **Where:** line 565 on main@6bb43667 · T3 tooling
- **Over cap:** CC 29 (target 20, cap 29) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/creative/comfyui/scripts/run_workflow.py` is 796 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 666 (BLE001), 671 (BLE001), 745 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "run_workflow" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.creative.comfyui.scripts.run_workflow.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-090 · `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py::Migrator.migrate_agent_config` · sync: upstream-identical
- **Where:** line 2435 on main@6bb43667 · T3 tooling
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` is 3253 lines (over the 2,000 target, capped at 3253): extracted code must go to a topical sibling `openclaw_to_hermes_<topic>.py`, helpers that late-import the facade's names (see How to take a unit, step 3).
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "openclaw_to_hermes" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 17 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.migration.openclaw-migration.scripts.openclaw_to_hermes.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-091 · `optional-skills/security/unbroker/scripts/autopilot.py::next_actions` · sync: upstream-identical
- **Where:** line 204 on main@6bb43667 · T3 tooling
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/security/unbroker/scripts/autopilot.py` is 417 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "autopilot" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.security.unbroker.scripts.autopilot.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-092 · `optional-skills/web-development/har-derived-api-client/scripts/har_to_client.py::main` · sync: upstream-identical
- **Where:** line 57 on main@6bb43667 · T3 tooling
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/web-development/har-derived-api-client/scripts/har_to_client.py` is 147 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "har_to_client" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.web-development.har-derived-api-client.scripts.har_to_client.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-093 · `skills/productivity/xlsx/scripts/xlsx_restructure.py::main` · sync: upstream-identical
- **Where:** line 191 on main@6bb43667 · T3 tooling
- **Over cap:** CC 28 (target 20, cap 28) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/productivity/xlsx/scripts/xlsx_restructure.py` is 337 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "xlsx_restructure" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.productivity.xlsx.scripts.xlsx_restructure.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-094 · `scripts/audit-old-updater-imports.py::main` · sync: upstream-identical
- **Where:** line 1133 on main@6bb43667 · T3 tooling
- **Over cap:** CC 27 (target 20, cap 27) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/audit-old-updater-imports.py` is 1339 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "audit-old-updater-imports" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.audit-old-updater-imports.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-095 · `scripts/validate_plugin_catalog.py::validate_entry` · sync: upstream-identical
- **Where:** line 143 on main@6bb43667 · T3 tooling
- **Over cap:** CC 27 (target 20, cap 27) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/validate_plugin_catalog.py` is 376 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "validate_plugin_catalog" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 15 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.validate_plugin_catalog.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-096 · `evals/postmortem/forensics/tools.py::main` · sync: upstream-identical
- **Where:** line 23 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `evals/postmortem/forensics/tools.py` is 100 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "tools" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `evals.postmortem.forensics.tools.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-097 · `evals/providers/reasoning_shapes.py::run_matrix` · sync: upstream-identical
- **Where:** line 30 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `evals/providers/reasoning_shapes.py` is 169 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 138 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/test_reasoning_shape_boundaries.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `evals.providers.reasoning_shapes.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-098 · `evals/session_search_schema/runner.py::main` · sync: upstream-identical
- **Where:** line 195 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `evals/session_search_schema/runner.py` is 273 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 231 (BLE001+S110), 265 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "runner" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 6 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `evals.session_search_schema.runner.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-099 · `scripts/write_install_stamp.py::build_stamp` · sync: upstream-identical
- **Where:** line 110 on main@6bb43667 · T3 tooling
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/write_install_stamp.py` is 347 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/scripts/test_channel_build_versions.py tests/scripts/test_install_stamp_distance.py tests/scripts/test_write_install_stamp.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 15 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.write_install_stamp.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-100 · `skills/productivity/powerpoint/scripts/pptx_create.py::build_slide` · sync: upstream-identical
- **Where:** line 103 on main@6bb43667 · T3 tooling
- **Over cap:** CC 25 (target 20, cap 25) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/productivity/powerpoint/scripts/pptx_create.py` is 214 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "pptx_create" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.productivity.powerpoint.scripts.pptx_create.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-101 · `optional-skills/creative/comfyui/scripts/run_workflow.py::ComfyRunner.monitor_ws` · sync: upstream-identical
- **Where:** line 246 on main@6bb43667 · T3 tooling
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/creative/comfyui/scripts/run_workflow.py` is 796 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 285 (BLE001), 295 (HX011), 335 (BLE001+S110) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "run_workflow" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.creative.comfyui.scripts.run_workflow.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-102 · `hermes_constants_scratch.py::_linked_worktree_repos` · sync: diverged
- **Where:** line 201 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `hermes_constants_scratch.py` is 296 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/conftest.py tests/test_scratch_dir.py tests/test_scratch_scan_failures.py` Also reached through its facade `hermes_constants.py`: 570 test files reference `hermes_constants` (`grep -rlE "(from hermes_constants import|^import hermes_constants\b|[\"']hermes_constants\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 5 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `hermes_constants_scratch.*` in tests; 3 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.96): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-103 · `optional-skills/creative/comfyui/scripts/_common.py::_http_once` · sync: upstream-identical
- **Where:** line 523 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `optional-skills/creative/comfyui/scripts/_common.py` is 835 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "_common" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 1 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.creative.comfyui.scripts._common.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-104 · `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_terminal_growth_vs_wacc` · sync: upstream-identical
- **Where:** line 114 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `optional-skills/finance/dcf-model/scripts/validate_dcf.py` is 291 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 156 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "validate_dcf" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.finance.dcf-model.scripts.validate_dcf.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-105 · `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_terminal_value_proportion` · sync: upstream-identical
- **Where:** line 188 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `optional-skills/finance/dcf-model/scripts/validate_dcf.py` is 291 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 230 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "validate_dcf" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.finance.dcf-model.scripts.validate_dcf.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-106 · `optional-skills/finance/dcf-model/scripts/validate_dcf.py::DCFModelValidator._check_wacc_range` · sync: upstream-identical
- **Where:** line 159 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `optional-skills/finance/dcf-model/scripts/validate_dcf.py` is 291 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 185 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "validate_dcf" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.finance.dcf-model.scripts.validate_dcf.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-107 · `scripts/audit-old-updater-imports.py::_audit_versions` · sync: upstream-identical
- **Where:** line 901 on main@6bb43667 · T3 tooling
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `scripts/audit-old-updater-imports.py` is 1339 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "audit-old-updater-imports" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.audit-old-updater-imports.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-108 · `evals/desktop_bug_campaign/persistence_live.py::main` · sync: upstream-identical
- **Where:** line 16 on main@6bb43667 · T3 tooling
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `evals/desktop_bug_campaign/persistence_live.py` is 209 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 79 (HX006) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "persistence_live" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 4 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `evals.desktop_bug_campaign.persistence_live.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-109 · `scripts/audit-old-updater-imports.py::_requirements_in` · sync: upstream-identical
- **Where:** line 304 on main@6bb43667 · T3 tooling
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/audit-old-updater-imports.py` is 1339 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 397 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "audit-old-updater-imports" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.audit-old-updater-imports.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-110 · `scripts/render-builds-table.py::main` · sync: upstream-identical
- **Where:** line 593 on main@6bb43667 · T3 tooling
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/render-builds-table.py` is 765 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 727 (HX006), 749 (HX006) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "render-builds-table" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 22 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.render-builds-table.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-111 · `skills/productivity/xlsx/scripts/xlsx_edit.py::main` · sync: upstream-identical
- **Where:** line 105 on main@6bb43667 · T3 tooling
- **Over cap:** CC 23 (target 20, cap 23) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/productivity/xlsx/scripts/xlsx_edit.py` is 263 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "xlsx_edit" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 3 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.productivity.xlsx.scripts.xlsx_edit.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-112 · `scripts/audit-old-updater-imports.py::resolve_in_tree` · sync: upstream-identical
- **Where:** line 1066 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/audit-old-updater-imports.py` is 1339 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "audit-old-updater-imports" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 10 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.audit-old-updater-imports.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-113 · `scripts/generate_icons.py::target_bytes` · sync: upstream-identical
- **Where:** line 792 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/generate_icons.py` is 1012 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 804 (HX011) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "generate_icons" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 28 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.generate_icons.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-114 · `scripts/smoke_nemo_relay_shared_metrics.py::_validate_store` · sync: upstream-identical
- **Where:** line 331 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/smoke_nemo_relay_shared_metrics.py` is 889 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/scripts/test_smoke_nemo_relay_shared_metrics.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 29 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.smoke_nemo_relay_shared_metrics.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-115 · `scripts/verify-bootstrap-version-stamp.py::verify_stamp` · sync: upstream-identical
- **Where:** line 92 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/verify-bootstrap-version-stamp.py` is 202 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "verify-bootstrap-version-stamp" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 6 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.verify-bootstrap-version-stamp.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-116 · `skills/productivity/docx/scripts/docx_validate.py::validate` · sync: upstream-identical
- **Where:** line 54 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/productivity/docx/scripts/docx_validate.py` is 156 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 138 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "docx_validate" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.productivity.docx.scripts.docx_validate.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-117 · `skills/research/grounded-citations/scripts/sources.py::main` · sync: upstream-identical
- **Where:** line 519 on main@6bb43667 · T3 tooling
- **Over cap:** CC 22 (target 20, cap 22) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/research/grounded-citations/scripts/sources.py` is 677 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "sources" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 6 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.research.grounded-citations.scripts.sources.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-118 · `evals/completion_backlog_probe.py::probe` · sync: upstream-identical
- **Where:** line 25 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `evals/completion_backlog_probe.py` is 195 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_completion_backlog.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 2 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `evals.completion_backlog_probe.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-119 · `optional-skills/security/godmode/scripts/godmode_race.py::score_response` · sync: upstream-identical
- **Where:** line 185 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/security/godmode/scripts/godmode_race.py` is 530 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "godmode_race" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 8 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.security.godmode.scripts.godmode_race.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-120 · `optional-skills/security/unbroker/scripts/pdd.py::cmd_doctor` · sync: upstream-identical
- **Where:** line 124 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `optional-skills/security/unbroker/scripts/pdd.py` is 914 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "pdd" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.security.unbroker.scripts.pdd.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-121 · `scripts/build/inputs.py::AgentInputs.validate` · sync: upstream-identical
- **Where:** line 72 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/build/inputs.py` is 122 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/scripts/test_agent_build.py tests/scripts/test_bundle_native.py tests/scripts/test_bundle_payload.py tests/scripts/test_native_build.py tests/scripts/test_termux_agent_assembly.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 3 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.build.inputs.*` in tests; 5 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-122 · `scripts/ci/live_comment.py::run` · sync: upstream-identical
- **Where:** line 546 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/ci/live_comment.py` is 795 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 580 (BLE001) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "live_comment" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 14 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.ci.live_comment.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-123 · `scripts/profile-tui.py::format_report` · sync: upstream-identical
- **Where:** line 148 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `scripts/profile-tui.py` is 627 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "profile-tui" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 15 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.profile-tui.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-124 · `skills/productivity/xlsx/scripts/xlsx_create.py::build_sheet` · sync: upstream-identical
- **Where:** line 172 on main@6bb43667 · T3 tooling
- **Over cap:** CC 21 (target 20, cap 21) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `skills/productivity/xlsx/scripts/xlsx_create.py` is 259 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "xlsx_create" tests`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 3 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `skills.productivity.xlsx.scripts.xlsx_create.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-125 · `tests/agent/lsp/_mock_lsp_server.py::main` · sync: diverged
- **Where:** line 82 on main@6bb43667 · T4 tests
- **Over cap:** CC 38 (target 20, cap 38) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tests/agent/lsp/_mock_lsp_server.py` is 311 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 139 (HX013), 237 (HX013) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/agent/lsp/_mock_lsp_server.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 17 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.lsp._mock_lsp_server.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): shared with upstream but already diverged (similarity 0.96): upstream edits already need manual merging; a refactor adds little extra sync cost when similarity is low, real cost when high

#### A-126 · `tests/plugins/test_telegram_update_admission.py::test_only_pre_handoff_failure_reopens_admission` · sync: upstream-identical
- **Where:** line 501 on main@6bb43667 · T4 tests
- **Over cap:** CC 35 (target 20, cap 35) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tests/plugins/test_telegram_update_admission.py` is 888 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/plugins/test_telegram_update_admission.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 11 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.plugins.test_telegram_update_admission.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-127 · `tests/gateway/test_platform_reconnect.py::TestVoiceInputCallbackWiring.test_startup_wires_voice_input_callback` · sync: upstream-identical
- **Where:** line 863 on main@6bb43667 · T4 tests
- **Over cap:** NESTING 8 (target 6, cap 8) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `tests/gateway/test_platform_reconnect.py` is 1231 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tests/gateway/test_platform_reconnect_fd_leak.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/gateway/test_platform_reconnect.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 39 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_platform_reconnect.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-128 · `tests/hermes_cli/test_diff_replay_no_stale_cells.py::replay` · sync: fork-only
- **Where:** line 85 on main@6bb43667 · T4 tests
- **Over cap:** CC 26 (target 20, cap 26) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tests/hermes_cli/test_diff_replay_no_stale_cells.py` is 276 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 106 (HX011) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_diff_replay_no_stale_cells.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 1 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_diff_replay_no_stale_cells.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): fork-only file (absent upstream): no upstream-sync cost

#### A-129 · `tests/scripts/test_release_sequencer.py::_sequencer_fixture.run` · sync: upstream-identical
- **Where:** line 453 on main@6bb43667 · T4 tests
- **Over cap:** CC 24 (target 20, cap 24) — done when every metric is at or under its target
- **Remedy:** CC: split it: extract phases into a topical sibling (`<stem>_<topic>.py`), or replace a branch ladder with a dict/table -> handler. `tests/scripts/test_release_sequencer.py` is 784 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** lines 497 (HX011) — these baselined hits must stay inside this unit with their lines unchanged and in the same relative order (a hit in a new function, or reordered past an identical hit line, is a NEW finding); fixing them is a separate rule unit
- **Tests:** `scripts/run_tests.sh tests/scripts/test_release_sequencer.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 21 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.scripts.test_release_sequencer.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-130 · `tests/computer_use/live_cua_0_9_smoke.py::run_smoke` · sync: upstream-identical
- **Where:** line 86 on main@6bb43667 · T4 tests
- **Over cap:** FUNC_LINES 347 (target 300, cap 347), NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** FUNC_LINES: extract phases into named helpers or a topical sibling module; NESTING: return early, or extract the inner block into a helper. `tests/computer_use/live_cua_0_9_smoke.py` is 471 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/computer_use/live_cua_0_9_smoke.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** low — 1 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.computer_use.live_cua_0_9_smoke.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

#### A-131 · `tests/gateway/test_platform_reconnect.py::TestStartupPlatformIsolation.test_start_continues_after_platform_connect_timeout` · sync: upstream-identical
- **Where:** line 91 on main@6bb43667 · T4 tests
- **Over cap:** NESTING 7 (target 6, cap 7) — done when every metric is at or under its target
- **Remedy:** NESTING: return early, or extract the inner block into a helper. `tests/gateway/test_platform_reconnect.py` is 1231 lines: same-file helpers are fine while it stays ≤ 2,000; a sibling is fine too. Existing siblings: `tests/gateway/test_platform_reconnect_fd_leak.py`.
- **Baselined rule hits inside the unit:** none
- **Tests:** `scripts/run_tests.sh tests/gateway/test_platform_reconnect.py`
- **Prove:** `python scripts/check` (full) green; unit before/after with the measurer snippet; differential old-vs-new check when the function has branches the tests may not cover
- **Merge risk:** medium — 39 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_platform_reconnect.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): IDENTICAL to upstream's copy: a refactor here makes every later upstream edit to it a manual port at the next upstream sync — prefer waiting for upstream, or take it knowingly

## Queue B — Python file-size units (FILE_LINES)

Multi-session by nature: a session lowers the cap by moving one topic out, then the next session
re-measures. Test files are split into `test_<stem>_<topic>.py`.

#### B-001 · `agent/auxiliary_client.py` FILE_LINES 9388 (target 2000, cap 9388, 7388 over)
- **Where:** T1 core; function units in this file: A-039, A-053; existing siblings: `agent/auxiliary_client_registry.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 370 top-level defs/classes) into a new or existing `auxiliary_client_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_anthropic_owned_routing.py tests/agent/test_anthropic_route_oauth_identity.py tests/agent/test_anthropic_spent_rotation_verdict.py tests/agent/test_anthropic_structured_output.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_arcee_trinity_overrides.py tests/agent/test_async_httpx_del_neuter.py tests/agent/test_auth_provider_failover.py tests/agent/test_aux_affordable_402_retry.py` … +224 more; full list: `grep -rlE "agent\.auxiliary_client" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 406 commits touched this file on main in the 90 days to 2026-10-17; 847 string patch targets on `agent.auxiliary_client.*` in tests; 236 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.94): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-002 · `gateway/run.py` FILE_LINES 6093 (target 2000, cap 6093, 4093 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/run_adapters.py`, `gateway/run_agent_cache.py`, `gateway/run_busy.py`, `gateway/run_common.py`, `gateway/run_config_loaders.py`, `gateway/run_delivery_queue_watch.py`, `gateway/run_goals.py`, `gateway/run_heartbeat_acceptance.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 192 top-level defs/classes) into a new or existing `run_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_compression_terminal_activity_persist.py tests/agent/test_conversation_compression_manual.py tests/agent/test_corruption_recovery_guidance.py tests/agent/test_credential_pool_routing.py tests/agent/test_fast_mode_auto.py tests/agent/test_in_place_preflight_rewind.py tests/agent/test_multiplex_base_url_scope.py tests/agent/test_openai_ultrafast.py tests/agent/test_refine_snapshot_isolation.py tests/agent/test_relay_session_segments.py tests/agent/test_secret_scope.py tests/agent/test_split_turn_compaction.py` … +523 more; full list: `grep -rlE "gateway\.run" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 765 commits touched this file on main in the 90 days to 2026-10-17; 281 string patch targets on `gateway.run.*` in tests; 535 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.91): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-003 · `hermes_cli/gateway.py` FILE_LINES 6017 (target 2000, cap 6017, 4017 over)
- **Where:** T1 core; function units in this file: A-016, A-022; existing siblings: `hermes_cli/gateway_command_errors.py`, `hermes_cli/gateway_enroll.py`, `hermes_cli/gateway_launchd.py`, `hermes_cli/gateway_migrate.py`, `hermes_cli/gateway_migrate_guards.py`, `hermes_cli/gateway_multiplex_mode.py`, `hermes_cli/gateway_multiplex_s6.py`, `hermes_cli/gateway_multiplex_served.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 260 top-level defs/classes) into a new or existing `gateway_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/cron/test_cronjob_gateway_liveness.py tests/cron/test_jobs_file_ownership.py tests/gateway/test_control_socket.py tests/gateway/test_gateway_command_line_matcher.py tests/gateway/test_host_gateway_profile_env.py tests/gateway/test_host_topology.py tests/gateway/test_multiplex_lifecycle.py tests/gateway/test_multiplex_secondary_port_binding_env.py tests/gateway/test_orphan_exit_grace.py tests/gateway/test_restart_after_turn.py tests/gateway/test_status.py tests/gateway/test_status_strict_identity_live.py` … +101 more; full list: `grep -rlE "hermes_cli\.gateway" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 289 commits touched this file on main in the 90 days to 2026-10-17; 89 string patch targets on `hermes_cli.gateway.*` in tests; 113 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-004 · `agent/context_compressor.py` FILE_LINES 5917 (target 2000, cap 5917, 3917 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `agent/context_compressor_prellm.py`, `agent/context_compressor_summary.py`, `agent/context_compressor_telemetry.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 118 top-level defs/classes) into a new or existing `context_compressor_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_413_compression.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_auxiliary_codex_completion_status.py tests/agent/test_background_review_input_budget.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_context_cache.py tests/agent/test_budget_reasoning_details_exclusion.py tests/agent/test_cjk_token_estimation.py` … +218 more; full list: `grep -rlE "agent\.context_compressor" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 397 commits touched this file on main in the 90 days to 2026-10-17; 292 string patch targets on `agent.context_compressor.*` in tests; 230 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-005 · `gateway/platforms/base.py` FILE_LINES 4930 (target 2000, cap 4930, 2930 over)
- **Where:** T1 core; function units in this file: A-057; existing siblings: `gateway/platforms/base_exec_approval.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 114 top-level defs/classes) into a new or existing `base_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_file_mutation_verifier.py tests/agent/test_generated_media_durable.py tests/agent/test_proxy_bypass_shared.py tests/agent/test_surrogate_chokepoints.py tests/conformance/persistence/test_cell5_delivery_outbox_exactly_once.py tests/cron/test_cron_live_delivery_confirmation.py tests/cron/test_cron_reconnect_only_rejection.py tests/cron/test_cron_relay_dm_origin_user_id.py tests/cron/test_media_delivery_parity.py tests/cron/test_media_send_timeout.py tests/cron/test_scheduler.py tests/gateway/platforms/test_yuanbao_state_cleanup.py` … +241 more; full list: `grep -rlE "gateway\.platforms\.base" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 221 commits touched this file on main in the 90 days to 2026-10-17; 88 string patch targets on `gateway.platforms.base.*` in tests; 253 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-006 · `gateway/platforms/api_server.py` FILE_LINES 4756 (target 2000, cap 4756, 2756 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/platforms/api_server_fire_startup.py`, `gateway/platforms/api_server_memory_sessions.py`, `gateway/platforms/api_server_openai_routes.py`, `gateway/platforms/api_server_request_options.py`, `gateway/platforms/api_server_room_dispatch.py`, `gateway/platforms/api_server_room_grants.py`, `gateway/platforms/api_server_run_idempotency.py`, `gateway/platforms/api_server_runs.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 55 top-level defs/classes) into a new or existing `api_server_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_interrupt_issuer_attribution.py tests/gateway/test_adapter_startup_secret_scope.py tests/gateway/test_api_delegation_delivery_contract.py tests/gateway/test_api_notification_outcomes.py tests/gateway/test_api_server.py tests/gateway/test_api_server_active_work_drain.py tests/gateway/test_api_server_artifact_store_async.py tests/gateway/test_api_server_bind_guard.py tests/gateway/test_api_server_compaction_projection.py tests/gateway/test_api_server_declared_conversation.py tests/gateway/test_api_server_jobs.py tests/gateway/test_api_server_key_scope_regression.py` … +50 more; full list: `grep -rlE "gateway\.platforms\.api_server" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 222 commits touched this file on main in the 90 days to 2026-10-17; 26 string patch targets on `gateway.platforms.api_server.*` in tests; 62 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.96): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-007 · `hermes_cli/config.py` FILE_LINES 4722 (target 2000, cap 4722, 2722 over)
- **Where:** T1 core; function units in this file: A-029, A-048; existing siblings: `hermes_cli/config_backups.py`, `hermes_cli/config_check_diagnostics.py`, `hermes_cli/config_defaults.py`, `hermes_cli/config_effective.py`, `hermes_cli/config_env_routing.py`, `hermes_cli/config_home.py`, `hermes_cli/config_language.py`, `hermes_cli/config_migrations.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 201 top-level defs/classes) into a new or existing `config_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_acp_commands.py tests/acp_adapter/test_acp_mcp_discovery.py tests/acp_adapter/test_failed_turn_closure.py tests/acp_adapter/test_named_provider_catalogs.py tests/acp_adapter/test_session.py tests/agent/lsp/test_broken_set.py tests/agent/lsp/test_install_and_lint_fixes.py tests/agent/lsp/test_root_policy.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_adapter_extra_headers.py tests/agent/test_anthropic_prompt_cache_policy.py` … +568 more; full list: `grep -rlE "hermes_cli\.config" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 384 commits touched this file on main in the 90 days to 2026-10-17; 890 string patch targets on `hermes_cli.config.*` in tests; 580 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.91): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-008 · `agent/chat_completion_helpers.py` FILE_LINES 4590 (target 2000, cap 4590, 2590 over)
- **Where:** T1 core; function units in this file: A-006, A-043; existing siblings: `agent/chat_completion_helpers_relay.py`, `agent/chat_completion_helpers_tool_calls.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 113 top-level defs/classes) into a new or existing `chat_completion_helpers_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_mid_tool_call_drop.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_stream_beta_routing.py tests/agent/test_anthropic_stream_fallbacks.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_api_content_sidecar.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_interrupt_post_worker.py tests/agent/test_bedrock_stream_completion.py tests/agent/test_bedrock_transport_replay.py tests/agent/test_cascading_interrupt.py tests/agent/test_chat_completion_helpers_provider_sort.py` … +61 more; full list: `grep -rlE "agent\.chat_completion_helpers" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 345 commits touched this file on main in the 90 days to 2026-10-17; 16 string patch targets on `agent.chat_completion_helpers.*` in tests; 73 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.93): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-009 · `agent/conversation_compression.py` FILE_LINES 4564 (target 2000, cap 4564, 2564 over)
- **Where:** T1 core; function units in this file: A-054; existing siblings: `agent/conversation_compression_archive.py`, `agent/conversation_compression_codex.py`, `agent/conversation_compression_deadline.py`, `agent/conversation_compression_manual.py`, `agent/conversation_compression_reply_anchor.py`, `agent/conversation_compression_telemetry.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 136 top-level defs/classes) into a new or existing `conversation_compression_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/repro_image_shrink_brick.py tests/agent/test_413_compression.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_third_party_oauth_guard.py tests/agent/test_aux_progress_streaming.py tests/agent/test_aux_stream_host_deadline.py tests/agent/test_codex_app_server_compaction.py tests/agent/test_compaction_events.py tests/agent/test_compress_context_progress_timeout.py tests/agent/test_compress_signal_leak.py tests/agent/test_compression_abort_state_reset.py tests/agent/test_compression_attempt_effect.py` … +63 more; full list: `grep -rlE "agent\.conversation_compression" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 315 commits touched this file on main in the 90 days to 2026-10-17; 28 string patch targets on `agent.conversation_compression.*` in tests; 75 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-010 · `hermes_cli/kanban_db.py` FILE_LINES 4450 (target 2000, cap 4450, 2450 over)
- **Where:** T1 core; function units in this file: A-041; existing siblings: `hermes_cli/kanban_db_boards.py`, `hermes_cli/kanban_db_connect.py`, `hermes_cli/kanban_db_dispatch.py`, `hermes_cli/kanban_db_graph.py`, `hermes_cli/kanban_db_notify.py`, `hermes_cli/kanban_db_workspace.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 184 top-level defs/classes) into a new or existing `kanban_db_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/lsp/test_workspace_release.py tests/agent/test_kanban_turn_recovery.py tests/agent/test_title_generator.py tests/conftest.py tests/cron/test_cron_kanban_env_isolation.py tests/gateway/test_busy_wake_admission.py tests/gateway/test_kanban_changes_requested_notifier.py tests/gateway/test_kanban_multiplex_served_profile_parity.py tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_apiserver_wake.py tests/gateway/test_kanban_notifier_served_apiserver_wake.py tests/gateway/test_kanban_notifier_wake_only_ordering.py` … +99 more; full list: `grep -rlE "hermes_cli\.kanban_db" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `hermes_cli/kanban.py`: 18 test files reference `hermes_cli.kanban` (`grep -rlE "hermes_cli\.kanban\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 170 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `hermes_cli.kanban_db.*` in tests; 111 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-011 · `gateway/run_turn.py` FILE_LINES 4386 (target 2000, cap 4386, 2386 over)
- **Where:** T1 core; function units in this file: A-026, A-046; existing siblings: `gateway/run_turn_followup_ack.py`, `gateway/run_turn_runner.py`, `gateway/run_turn_runner_approval_settle.py`, `gateway/run_turn_runner_clarify_delivery.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 7 top-level defs/classes) into a new or existing `run_turn_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_failed_turn_site_codes.py tests/agent/test_multiplex_base_url_scope.py tests/gateway/test_empty_sentinel_rewrite_copy.py tests/gateway/test_hygiene_no_commit_reason.py tests/gateway/test_interim_only_consumer_warning.py tests/gateway/test_pre_agent_fallback_notice.py tests/gateway/test_session_hygiene.py tests/gateway/test_stream_consumer.py tests/gateway/test_tool_log_mode.py tests/gateway/test_transient_failure_transcript.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 169 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_turn.*` in tests; 10 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.69): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-012 · `agent/agent_runtime_helpers.py` FILE_LINES 3936 (target 2000, cap 3936, 1936 over)
- **Where:** T1 core; function units in this file: A-038; existing siblings: `agent/agent_runtime_helpers_dead_connections.py`, `agent/agent_runtime_helpers_placeholders.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 122 top-level defs/classes) into a new or existing `agent_runtime_helpers_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_prompt_cache_policy.py tests/agent/test_billing_unverified_carrythrough.py tests/agent/test_cache_disabled_on_stubs.py tests/agent/test_codex_model_entitlement_rotation.py tests/agent/test_codex_soft_failure_pool_rotation.py tests/agent/test_codex_token_expired_replay_recovery.py tests/agent/test_context_compressor.py tests/agent/test_context_engine_message_uid.py tests/agent/test_create_openai_client_reuse.py tests/agent/test_credential_pool_routing.py tests/agent/test_cursor_optimizations_parity.py` … +64 more; full list: `grep -rlE "agent\.agent_runtime_helpers" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 274 commits touched this file on main in the 90 days to 2026-10-17; 11 string patch targets on `agent.agent_runtime_helpers.*` in tests; 76 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-013 · `cli.py` FILE_LINES 3866 (target 2000, cap 3866, 1866 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 42 top-level defs/classes) into a new or existing `cli_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_transport_a_registration.py tests/agent/lsp/test_workspace_release.py tests/agent/test_conversation_compression_manual.py tests/agent/test_credential_pool_routing.py tests/agent/test_curator.py tests/agent/test_display_cwidth_vs16.py tests/agent/test_env_loader_secret_sources.py tests/agent/test_fast_mode_auto.py tests/agent/test_grid_sweep_cli_tui_stream_diagnostics.py tests/agent/test_in_place_merged_user_rows.py tests/agent/test_in_place_preflight_rewind.py tests/agent/test_kanban_turn_recovery.py` … +337 more; full list: `grep -rlE "cli" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 457 commits touched this file on main in the 90 days to 2026-10-17; 281 string patch targets on `cli.*` in tests; 349 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.6): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-014 · `hermes_cli/main.py` FILE_LINES 3825 (target 2000, cap 3825, 1825 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `hermes_cli/main_agent_cmds.py`, `hermes_cli/main_container.py`, `hermes_cli/main_dashboard.py`, `hermes_cli/main_dep_hints.py`, `hermes_cli/main_desktop.py`, `hermes_cli/main_desktop_tcc.py`, `hermes_cli/main_install_repair.py`, `hermes_cli/main_platform_setup.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 111 top-level defs/classes) into a new or existing `main_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/_fixtures/live_system_guard.py tests/acp_adapter/test_entry.py tests/cron/test_atomic_paused_creation.py tests/cron/test_bot_chat_cli_home.py tests/cron/test_cron_bot_chat_delivery.py tests/cron/test_restart_safe_worker.py tests/gateway/test_cgroup_cleanup.py tests/gateway/test_gateway_command_line_matcher.py tests/gateway/test_gateway_inline_bootstrap_identity.py tests/gateway/test_gateway_liveness_home_guard.py tests/gateway/test_host_gateway_profile_env.py tests/gateway/test_multiplex_secondary_port_binding_env.py` … +234 more; full list: `grep -rlE "hermes_cli\.main" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 472 commits touched this file on main in the 90 days to 2026-10-17; 92 string patch targets on `hermes_cli.main.*` in tests; 246 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-015 · `hermes_cli/config_defaults.py` FILE_LINES 3339 (target 2000, cap 3339, 1339 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 4 top-level defs/classes) into a new or existing `config_defaults_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_background_review_input_budget.py tests/agent/test_curator.py tests/agent/test_empty_response_guard.py tests/agent/test_empty_response_transient_policy.py tests/agent/test_silence_guard.py tests/hermes_cli/test_config.py tests/hermes_cli/test_config_migration_45_connections.py tests/hermes_cli/test_config_migration_48_sandbox_image.py tests/hermes_cli/test_config_migration_49_vercel_image.py tests/hermes_cli/test_gateway_migrate_multiplex.py tests/hermes_cli/test_gateway_scm_feature_removed.py tests/hermes_cli/test_kanban_notify.py` … +9 more; full list: `grep -rlE "hermes_cli\.config_defaults" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `hermes_cli/config.py`: 580 test files reference `hermes_cli.config` (`grep -rlE "hermes_cli\.config\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 387 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.config_defaults.*` in tests; 21 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-016 · `agent/credential_pool.py` FILE_LINES 3179 (target 2000, cap 3179, 1179 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `agent/credential_pool_admin.py`, `agent/credential_pool_model_cooldowns.py`, `agent/credential_pool_plugin.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 61 top-level defs/classes) into a new or existing `credential_pool_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_account_usage.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_adapter.py tests/agent/test_anthropic_borrowed_row_authority.py tests/agent/test_anthropic_credential_persist_failure.py tests/agent/test_anthropic_external_login_optout.py tests/agent/test_anthropic_model_cooldowns.py tests/agent/test_anthropic_oauth_stress.py tests/agent/test_anthropic_spent_rotation_verdict.py tests/agent/test_auxiliary_client.py tests/agent/test_codex_model_entitlement_rotation.py tests/agent/test_codex_soft_failure_pool_rotation.py` … +78 more; full list: `grep -rlE "agent\.credential_pool" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 119 commits touched this file on main in the 90 days to 2026-10-17; 84 string patch targets on `agent.credential_pool.*` in tests; 90 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-017 · `hermes_cli/models.py` FILE_LINES 3100 (target 2000, cap 3100, 1100 over)
- **Where:** T1 core; function units in this file: A-062; existing siblings: `hermes_cli/models_catalog_static.py`, `hermes_cli/models_detect.py`, `hermes_cli/models_local.py`, `hermes_cli/models_pricing.py`, `hermes_cli/models_profile_cache.py`, `hermes_cli/models_reasoning_caps.py`, `hermes_cli/models_validate.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 154 top-level defs/classes) into a new or existing `models_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_named_provider_catalogs.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_reasoning_catalog_floor.py tests/agent/test_bedrock_integration.py tests/agent/test_capability_probe_credentials.py tests/agent/test_copilot_acp_deprecation.py tests/agent/test_copilot_initiator.py tests/agent/test_custom_provider_ca_probes.py tests/agent/test_fast_mode_auto.py tests/agent/test_nous_welcome_client_contract.py tests/agent/test_openai_ultrafast.py` … +136 more; full list: `grep -rlE "hermes_cli\.models" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 259 commits touched this file on main in the 90 days to 2026-10-17; 275 string patch targets on `hermes_cli.models.*` in tests; 148 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-018 · `tools/process_registry.py` FILE_LINES 3078 (target 2000, cap 3078, 1078 over)
- **Where:** T1 core; function units in this file: A-036; existing siblings: `tools/process_registry_checkpoint.py`, `tools/process_registry_env_log.py`, `tools/process_registry_notifications.py`, `tools/process_registry_results.py`, `tools/process_registry_termination.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 32 top-level defs/classes) into a new or existing `process_registry_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_owned_process_cleanup.py tests/agent/test_review_engine.py tests/cron/test_restart_safe_worker.py tests/cron/test_restart_safe_worker_death_evidence.py tests/gateway/test_abandoned_turn_process_cleanup.py tests/gateway/test_agents_command_delegations.py tests/gateway/test_api_server.py tests/gateway/test_api_server_active_work_drain.py tests/gateway/test_async_delivery_capability.py tests/gateway/test_background_process_notifications.py tests/gateway/test_child_notice_profile_admission.py tests/gateway/test_clean_shutdown_marker.py` … +83 more; full list: `grep -rlE "tools\.process_registry" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 168 commits touched this file on main in the 90 days to 2026-10-17; 107 string patch targets on `tools.process_registry.*` in tests; 95 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.92): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-019 · `hermes_cli/kanban_db_dispatch.py` FILE_LINES 3035 (target 2000, cap 3035, 1035 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 89 top-level defs/classes) into a new or existing `kanban_db_dispatch_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_run_agent.py tests/agent/test_secret_scope.py tests/agent/test_turn_finalizer_iteration_limit_exit.py tests/conftest.py tests/cron/test_cron_kanban_env_isolation.py tests/gateway/test_kanban_multiplex_served_profile_parity.py tests/gateway/test_kanban_reconcile_orphans.py tests/hermes_cli/test_kanban_block_classify.py tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_boards.py tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py tests/hermes_cli/test_kanban_complete_live_claim_guard.py` … +28 more; full list: `grep -rlE "hermes_cli\.kanban_db_dispatch" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `hermes_cli/kanban_db.py`: 111 test files reference `hermes_cli.kanban_db` (`grep -rlE "hermes_cli\.kanban_db\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 73 commits touched this file on main in the 90 days to 2026-10-17; 8 string patch targets on `hermes_cli.kanban_db_dispatch.*` in tests; 40 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-020 · `gateway/platforms/yuanbao.py` FILE_LINES 2820 (target 2000, cap 2820, 820 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/platforms/yuanbao_media.py`, `gateway/platforms/yuanbao_proto.py`, `gateway/platforms/yuanbao_sticker.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 49 top-level defs/classes) into a new or existing `yuanbao_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/platforms/test_yuanbao_state_cleanup.py tests/gateway/test_access_policy_mixin.py tests/gateway/test_adapter_session_key_seam.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_config_driven_access_policy.py tests/gateway/test_multiplex_process_global_sinks.py tests/gateway/test_platform_authz_scope.py tests/gateway/test_ws_dial_happy_eyeballs.py tests/gateway/test_yuanbao_forwarded_heartbeat.py tests/gateway/test_yuanbao_integration.py tests/gateway/test_yuanbao_markdown.py tests/gateway/test_yuanbao_pipeline.py` … +4 more; full list: `grep -rlE "gateway\.platforms\.yuanbao" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** medium — 59 commits touched this file on main in the 90 days to 2026-10-17; 7 string patch targets on `gateway.platforms.yuanbao.*` in tests; 16 test files reference it. Upstream (NousResearch main@18c525b6): fork-only file (absent upstream): no upstream-sync cost

#### B-021 · `hermes_cli/profiles.py` FILE_LINES 2734 (target 2000, cap 2734, 734 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `hermes_cli/profiles_service_cleanup.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 116 top-level defs/classes) into a new or existing `profiles_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_credential_pool_profile_oauth_fork.py tests/cron/test_bot_chat_pending_identity.py tests/cron/test_bot_chat_warning_replay.py tests/cron/test_cron_bot_chat_delivery.py tests/cron/test_cron_live_bot_delivery.py tests/cron/test_cron_manage_profile_scope.py tests/cron/test_cron_multiplex_desktop_ticker_scope.py tests/gateway/test_api_server.py tests/gateway/test_api_server_multiplex_secret_scope.py tests/gateway/test_api_server_profile_prefix_misdelivery.py tests/gateway/test_browser_control_artifacts.py tests/gateway/test_child_notice_profile_admission.py` … +140 more; full list: `grep -rlE "hermes_cli\.profiles" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 150 commits touched this file on main in the 90 days to 2026-10-17; 212 string patch targets on `hermes_cli.profiles.*` in tests; 152 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.93): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-022 · `agent/model_metadata.py` FILE_LINES 2691 (target 2000, cap 2691, 691 over)
- **Where:** T1 core; function units in this file: A-021; existing siblings: `agent/model_metadata_http.py`, `agent/model_metadata_llamacpp.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 146 top-level defs/classes) into a new or existing `model_metadata_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_server.py tests/agent/metadata_transport.py tests/agent/test_413_compression.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_astra_baseline_runtime.py tests/agent/test_aux_progress_streaming.py tests/agent/test_auxiliary_client.py tests/agent/test_background_review_input_budget.py tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_context_cache.py tests/agent/test_budget_reasoning_details_exclusion.py tests/agent/test_capability_probe_credentials.py` … +114 more; full list: `grep -rlE "agent\.model_metadata" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 187 commits touched this file on main in the 90 days to 2026-10-17; 328 string patch targets on `agent.model_metadata.*` in tests; 126 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-023 · `hermes_cli/cli_tui_mixin.py` FILE_LINES 2630 (target 2000, cap 2630, 630 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 16 top-level defs/classes) into a new or existing `cli_tui_mixin_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_cli_clarify_panel_height.py tests/hermes_cli/test_cli_ctrl_c_draft_priority.py tests/hermes_cli/test_cli_render_panel_width.py tests/hermes_cli/test_cli_tui_i18n_lazy_labels.py tests/hermes_cli/test_kitty_keypad_parity.py tests/hermes_cli/test_pt_chrome_no_vs16.py tests/hermes_cli/test_reasoning_picker.py tests/hermes_cli/test_scroll_list_panel_row_layout.py tests/hermes_cli/test_subagent_monitor.py tests/hermes_cli/test_todo_board_widget.py tests/hermes_cli/test_tui_rapid_enter_paste.py` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 45 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `hermes_cli.cli_tui_mixin.*` in tests; 11 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.93): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-024 · `agent/agent_init.py` FILE_LINES 2603 (target 2000, cap 2603, 603 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `agent/agent_init_fallback.py`, `agent/agent_init_params.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 72 top-level defs/classes) into a new or existing `agent_init_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_agent_init_fork.py tests/agent/test_anthropic_route_oauth_identity.py tests/agent/test_arcee_trinity_overrides.py tests/agent/test_codex_gpt55_autoraise_notice.py tests/agent/test_compression_config_defaults.py tests/agent/test_context_route_mismatch.py tests/agent/test_credential_pool_auto_detect.py tests/agent/test_custom_provider_extra_body.py tests/agent/test_custom_provider_extra_body_matching.py tests/agent/test_external_process_provider_init.py tests/agent/test_grid_sweep_cli_tui_stream_diagnostics.py tests/agent/test_init_exhausted_pool_billing_error.py` … +21 more; full list: `grep -rlE "agent\.agent_init" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 264 commits touched this file on main in the 90 days to 2026-10-17; 1 string patch targets on `agent.agent_init.*` in tests; 33 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-025 · `hermes_cli/auth.py` FILE_LINES 2491 (target 2000, cap 2491, 491 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `hermes_cli/auth_codex.py`, `hermes_cli/auth_codex_browser.py`, `hermes_cli/auth_commands.py`, `hermes_cli/auth_constants.py`, `hermes_cli/auth_device_flow.py`, `hermes_cli/auth_error_copy.py`, `hermes_cli/auth_external_process.py`, `hermes_cli/auth_minimax.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 121 top-level defs/classes) into a new or existing `auth_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_account_usage_pool_fetch.py tests/agent/test_actual_auxiliary_routing.py tests/agent/test_anthropic_borrowed_row_authority.py tests/agent/test_anthropic_credential_persist_failure.py tests/agent/test_anthropic_oauth_pkce.py tests/agent/test_anthropic_oauth_stress.py tests/agent/test_anthropic_spent_rotation_verdict.py tests/agent/test_auxiliary_client.py tests/agent/test_auxiliary_client_cache_follows_credential.py tests/agent/test_auxiliary_client_minimax_oauth.py tests/agent/test_auxiliary_client_resolve_dedup.py tests/agent/test_auxiliary_main_first.py` … +209 more; full list: `grep -rlE "hermes_cli\.auth" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 193 commits touched this file on main in the 90 days to 2026-10-17; 237 string patch targets on `hermes_cli.auth.*` in tests; 221 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-026 · `hermes_cli/cli_commands_mixin.py` FILE_LINES 2462 (target 2000, cap 2462, 462 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 26 top-level defs/classes) into a new or existing `cli_commands_mixin_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_refine_snapshot_isolation.py tests/agent/test_tool_executor_checkpoint_paths.py tests/cron/test_run_error_diagnostics.py tests/hermes_cli/test_backup.py tests/hermes_cli/test_cli_goal_kick_prompt.py tests/hermes_cli/test_cli_retire_agent.py tests/hermes_cli/test_cron.py tests/hermes_cli/test_debug.py tests/hermes_cli/test_diff_command.py tests/hermes_cli/test_focus_view.py tests/hermes_cli/test_goal_dispatch.py tests/hermes_cli/test_handoff_slow_dispatch_timeout.py` … +14 more; full list: `grep -rlE "hermes_cli\.cli_commands_mixin" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `cli.py`: 349 test files reference `cli` (`grep -rlE "(from cli import|^import cli\b|[\"']cli\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 153 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `hermes_cli.cli_commands_mixin.*` in tests; 26 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.96): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-027 · `gateway/relay/adapter.py` FILE_LINES 2410 (target 2000, cap 2410, 410 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 5 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/relay/test_live_cards_flow_trace.py tests/gateway/relay/test_relay_adapter.py tests/gateway/relay/test_relay_egress_declines.py tests/gateway/relay/test_relay_follow_up.py tests/gateway/relay/test_relay_format_hints.py tests/gateway/relay/test_relay_going_idle.py tests/gateway/relay/test_relay_inbound_dedupe.py tests/gateway/relay/test_relay_inchannel_continuable.py tests/gateway/relay/test_relay_interactive.py tests/gateway/relay/test_relay_interrupt.py tests/gateway/relay/test_relay_live_cards.py tests/gateway/relay/test_relay_media.py` … +19 more; full list: `grep -rlE "gateway\.relay\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** medium — 86 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.relay.adapter.*` in tests; 31 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.96): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-028 · `gateway/run_shutdown.py` FILE_LINES 2265 (target 2000, cap 2265, 265 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/run_shutdown_session_end.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 10 top-level defs/classes) into a new or existing `run_shutdown_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_interrupt_issuer_attribution.py tests/gateway/test_config.py tests/gateway/test_host_gateway_profile_env.py tests/gateway/test_scale_to_zero.py tests/gateway/test_shutdown_kill_off_loop.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 83 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_shutdown.*` in tests; 5 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-029 · `hermes_cli/plugins.py` FILE_LINES 2264 (target 2000, cap 2264, 264 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `hermes_cli/plugins_activation.py`, `hermes_cli/plugins_activation_live.py`, `hermes_cli/plugins_admission.py`, `hermes_cli/plugins_aux_tasks.py`, `hermes_cli/plugins_cadence.py`, `hermes_cli/plugins_cmd.py`, `hermes_cli/plugins_cmd_capabilities.py`, `hermes_cli/plugins_cmd_catalog.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 56 top-level defs/classes) into a new or existing `plugins_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_api_content_row_addressed_backfill.py tests/agent/test_api_content_sidecar.py tests/agent/test_auxiliary_hooks.py tests/agent/test_background_review_toolset_restriction.py tests/agent/test_compaction_prompt_rebuild.py tests/agent/test_context_engine.py tests/agent/test_context_engine_host_contract.py tests/agent/test_gateway_turn_sidecar.py tests/agent/test_i18n_layers.py tests/agent/test_local_tool_batches.py tests/agent/test_memory_provider.py tests/agent/test_outbound_webhooks.py` … +173 more; full list: `grep -rlE "hermes_cli\.plugins" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 178 commits touched this file on main in the 90 days to 2026-10-17; 262 string patch targets on `hermes_cli.plugins.*` in tests; 185 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-030 · `gateway/status.py` FILE_LINES 2151 (target 2000, cap 2151, 151 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/status_inline_source.py`, `gateway/status_phrases.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 121 top-level defs/classes) into a new or existing `status_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/_fixtures/live_system_guard.py tests/agent/test_conversation_compression_manual.py tests/cron/test_cron_multiplex_tick_ownership.py tests/cron/test_cron_tick_stale_yield.py tests/cron/test_cronjob_gateway_liveness.py tests/cron/test_owner_start_time_drift.py tests/cron/test_restart_safe_worker.py tests/gateway/test_adapter_connect_classification.py tests/gateway/test_api_server.py tests/gateway/test_api_server_active_work_drain.py tests/gateway/test_buzz_adapter.py tests/gateway/test_cgroup_cleanup.py` … +135 more; full list: `grep -rlE "gateway\.status" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 135 commits touched this file on main in the 90 days to 2026-10-17; 308 string patch targets on `gateway.status.*` in tests; 147 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-031 · `gateway/run_notifications.py` FILE_LINES 2126 (target 2000, cap 2126, 126 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/run_notifications_ownership.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 7 top-level defs/classes) into a new or existing `run_notifications_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_background_process_notifications.py tests/gateway/test_completion_delivery.py tests/gateway/test_decline_fallback_suppression.py tests/tools/test_async_batch_task_failure_notice.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 92 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_notifications.*` in tests; 4 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.84): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-032 · `agent/conversation_loop.py` FILE_LINES 2113 (target 2000, cap 2113, 113 over)
- **Where:** T1 core; function units in this file: A-044
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 68 top-level defs/classes) into a new or existing `conversation_loop_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_failed_turn_closure.py tests/agent/test_413_compression.py tests/agent/test_anthropic_billing_guidance.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_background_review.py tests/agent/test_background_review_input_budget.py tests/agent/test_bare_toolcall_recovery.py tests/agent/test_billing_unverified_carrythrough.py tests/agent/test_bot_chat_toolset_refresh.py tests/agent/test_canon_args_memo_parity.py tests/agent/test_codex_reasoning_only_streak.py tests/agent/test_compression_budget_rearm.py` … +57 more; full list: `grep -rlE "agent\.conversation_loop" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `run_agent.py`: 476 test files reference `run_agent` (`grep -rlE "(from run_agent import|^import run_agent\b|[\"']run_agent\.)" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** high — 337 commits touched this file on main in the 90 days to 2026-10-17; 41 string patch targets on `agent.conversation_loop.*` in tests; 69 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.93): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-033 · `hermes_cli/backup.py` FILE_LINES 2079 (target 2000, cap 2079, 79 over)
- **Where:** T1 core; function units in this file: A-007; existing siblings: `hermes_cli/backup_import.py`, `hermes_cli/backup_restore.py`, `hermes_cli/backup_sqlite.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 62 top-level defs/classes) into a new or existing `backup_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/cron/test_execution_ledger.py tests/hermes_cli/test_backup.py tests/hermes_cli/test_backup_all_profiles.py tests/hermes_cli/test_backup_path_errors.py tests/hermes_cli/test_backup_stability.py tests/hermes_cli/test_backup_zip_serialization.py tests/hermes_cli/test_old_updater_shims.py tests/hermes_cli/test_plugins_hub_live_catalog.py tests/hermes_cli/test_profile_export_credentials.py tests/hermes_cli/test_profile_pm_portability.py tests/hermes_cli/test_restore_source_integrity.py tests/hermes_cli/test_snapshot_restore_oauth.py` … +7 more; full list: `grep -rlE "hermes_cli\.backup" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 105 commits touched this file on main in the 90 days to 2026-10-17; 5 string patch targets on `hermes_cli.backup.*` in tests; 19 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.88): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-034 · `hermes_state.py` FILE_LINES 2066 (target 2000, cap 2066, 66 over)
- **Where:** T1 core; function units in this file: A-064; existing siblings: `hermes_state_common.py`, `hermes_state_compression.py`, `hermes_state_coverage.py`, `hermes_state_dbfile.py`, `hermes_state_errors.py`, `hermes_state_fts.py`, `hermes_state_gateway.py`, `hermes_state_guard.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 19 top-level defs/classes) into a new or existing `hermes_state_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/acp_adapter/test_empty_session_persistence.py tests/acp_adapter/test_failed_turn_closure.py tests/acp_adapter/test_session.py tests/acp_adapter/test_session_cwd_column.py tests/acp_adapter/test_session_db_private_access.py tests/acp_adapter/test_session_provenance.py tests/agent/test_413_compression.py tests/agent/test_860_dedup.py tests/agent/test_anthropic_preserved_thinking_replay.py tests/agent/test_anthropic_thinking_block_order.py tests/agent/test_api_content_row_addressed_backfill.py tests/agent/test_api_content_sidecar.py` … +524 more; full list: `grep -rlE "hermes_state" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 417 commits touched this file on main in the 90 days to 2026-10-17; 35 string patch targets on `hermes_state.*` in tests; 536 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.9): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-035 · `gateway/run_inbound.py` FILE_LINES 2046 (target 2000, cap 2046, 46 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/run_inbound_consumer.py`, `gateway/run_inbound_media.py`, `gateway/run_inbound_unauthorized.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 3 top-level defs/classes) into a new or existing `run_inbound_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_interrupt_issuer_attribution.py tests/gateway/test_discord_triggering_note_persistence.py tests/gateway/test_inbound_media_profile_scope.py tests/gateway/test_reaped_eviction_interrupts_run.py tests/gateway/test_skill_slash_native_boundary.py tests/tui_gateway/test_tui_gateway_server.py` Also reached through its facade `gateway/run.py`: 535 test files reference `gateway.run` (`grep -rlE "gateway\.run\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 80 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_inbound.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.91): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-036 · `agent/tool_executor.py` FILE_LINES 2042 (target 2000, cap 2042, 42 over)
- **Where:** T1 core; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 68 top-level defs/classes) into a new or existing `tool_executor_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_aux_stream_deadlines.py tests/agent/test_authorization_gate.py tests/agent/test_deadline.py tests/agent/test_future_timeout_worker_death.py tests/agent/test_image_generate_parallel.py tests/agent/test_interrupt_issuer_attribution.py tests/agent/test_iteration_budget_warning.py tests/agent/test_malformed_tool_arguments.py tests/agent/test_message_sequence_repair.py tests/agent/test_moa_loop_mode.py tests/agent/test_nt_namespace_guard.py tests/agent/test_plugin_live_notes_delivery.py` … +17 more; full list: `grep -rlE "agent\.tool_executor" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 138 commits touched this file on main in the 90 days to 2026-10-17; 15 string patch targets on `agent.tool_executor.*` in tests; 29 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-037 · `gateway/run_turn_runner.py` FILE_LINES 2001 (target 2000, cap 2001, 1 over)
- **Where:** T1 core; function units in this file: none; existing siblings: `gateway/run_turn_runner_approval_settle.py`, `gateway/run_turn_runner_clarify_delivery.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 4 top-level defs/classes) into a new or existing `run_turn_runner_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_cached_agent_history_guard.py tests/gateway/test_clarify_card_retire_on_timeout.py tests/gateway/test_clarify_delivery_fallback.py tests/gateway/test_decline_fallback_suppression.py tests/gateway/test_display_null_turn_wiring.py tests/gateway/test_exec_approval_template.py tests/gateway/test_exec_approval_timeout_notice.py tests/gateway/test_internal_event_pin_wiring.py tests/gateway/test_local_model_connection_reply.py tests/gateway/test_multiplex_session_db_profile_scope.py tests/gateway/test_native_stream_warning_composition.py tests/gateway/test_pre_agent_fallback_notice.py` … +10 more; full list: `grep -rlE "gateway\.run_turn_runner" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `gateway/run_turn.py`: 10 test files reference `gateway.run_turn` (`grep -rlE "gateway\.run_turn\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 59 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `gateway.run_turn_runner.*` in tests; 22 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.58): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-038 · `plugins/platforms/discord/adapter.py` FILE_LINES 7674 (target 2000, cap 7674, 5674 over)
- **Where:** T2 runtime edge; function units in this file: A-068, A-069, A-073, A-081; existing siblings: `plugins/platforms/discord/adapter_component_auth.py`, `plugins/platforms/discord/adapter_media.py`, `plugins/platforms/discord/adapter_slash_auth.py`, `plugins/platforms/discord/adapter_thread_titles.py`, `plugins/platforms/discord/adapter_voice_info.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 49 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_completion_admission.py tests/gateway/test_discord_admission_thread_exemption.py tests/gateway/test_discord_allow_bots_yaml_config.py tests/gateway/test_discord_allowed_mentions.py tests/gateway/test_discord_approval_mentions.py tests/gateway/test_discord_attachment_download.py tests/gateway/test_discord_attachment_receipts.py tests/gateway/test_discord_auto_thread_prompt_parity.py tests/gateway/test_discord_auto_thread_rename_pin.py` … +59 more; full list: `grep -rlE "plugins\.platforms\.discord\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 177 commits touched this file on main in the 90 days to 2026-10-17; 12 string patch targets on `plugins.platforms.discord.adapter.*` in tests; 71 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-039 · `plugins/platforms/telegram/adapter.py` FILE_LINES 7319 (target 2000, cap 7319, 5319 over)
- **Where:** T2 runtime edge; function units in this file: A-079
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 29 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/conftest.py tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_allowed_channels_widening.py tests/gateway/test_dm_topics.py tests/gateway/test_gateway_platform_event_hook.py tests/gateway/test_late_plugin_rewire.py tests/gateway/test_multiplex_endpoint_identity_scope.py tests/gateway/test_multiplex_interactive_auth.py tests/gateway/test_native_warning_coverage.py tests/gateway/test_platform_adapter_i18n.py tests/gateway/test_platform_plugin_handlers.py` … +79 more; full list: `grep -rlE "plugins\.platforms\.telegram\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 218 commits touched this file on main in the 90 days to 2026-10-17; 35 string patch targets on `plugins.platforms.telegram.adapter.*` in tests; 91 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-040 · `plugins/platforms/slack/adapter.py` FILE_LINES 6944 (target 2000, cap 6944, 4944 over)
- **Where:** T2 runtime edge; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 65 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_authz_secret_scope.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_handoff_thread_session_key.py tests/gateway/test_kanban_wake_scope.py tests/gateway/test_late_plugin_rewire.py tests/gateway/test_media_download_retry.py tests/gateway/test_multiplex_interactive_auth.py tests/gateway/test_native_producer_owner_matrix.py tests/gateway/test_send_image_file.py tests/gateway/test_send_multiple_images.py tests/gateway/test_send_retry_hooks.py` … +39 more; full list: `grep -rlE "plugins\.platforms\.slack\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 209 commits touched this file on main in the 90 days to 2026-10-17; 5 string patch targets on `plugins.platforms.slack.adapter.*` in tests; 51 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-041 · `plugins/platforms/feishu/adapter.py` FILE_LINES 4493 (target 2000, cap 4493, 2493 over)
- **Where:** T2 runtime edge; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 84 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/conftest.py tests/gateway/feishu_helpers.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_startup_secret_scope.py tests/gateway/test_feishu.py tests/gateway/test_feishu_approval_buttons.py tests/gateway/test_feishu_bot_admission.py tests/gateway/test_feishu_channel_prompts.py tests/gateway/test_feishu_group_policy_surfacing.py tests/gateway/test_feishu_lazy_import.py tests/gateway/test_feishu_meeting_invite.py tests/gateway/test_feishu_onboard.py` … +9 more; full list: `grep -rlE "plugins\.platforms\.feishu\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** medium — 82 commits touched this file on main in the 90 days to 2026-10-17; 54 string patch targets on `plugins.platforms.feishu.adapter.*` in tests; 21 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-042 · `plugins/platforms/matrix/adapter.py` FILE_LINES 3267 (target 2000, cap 3267, 1267 over)
- **Where:** T2 runtime edge; function units in this file: none; existing siblings: `plugins/platforms/matrix/adapter_crypto.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 49 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_secret_scope_tier1_migration.py tests/gateway/test_adapter_authz_secret_scope.py tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_handoff_thread_session_key.py tests/gateway/test_matrix.py tests/gateway/test_matrix_approval_reaction_fail_closed.py tests/gateway/test_matrix_crypto_store_per_profile.py tests/gateway/test_matrix_deps_rebind.py tests/gateway/test_matrix_dm_invite_recording.py tests/gateway/test_matrix_encrypted_drop_warning.py tests/gateway/test_matrix_exec_approval.py` … +23 more; full list: `grep -rlE "plugins\.platforms\.matrix\.adapter" tests --include=*.py | grep -v ^tests/e2e`
- **Merge risk:** high — 116 commits touched this file on main in the 90 days to 2026-10-17; 3 string patch targets on `plugins.platforms.matrix.adapter.*` in tests; 35 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-043 · `tui_gateway/methods_session.py` FILE_LINES 2357 (target 2000, cap 2357, 357 over)
- **Where:** T2 runtime edge; function units in this file: none; existing siblings: `tui_gateway/methods_session_branch.py`, `tui_gateway/methods_session_control.py`, `tui_gateway/methods_session_foreign.py`, `tui_gateway/methods_session_interrupt.py`, `tui_gateway/methods_session_model_guard.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 120 top-level defs/classes) into a new or existing `methods_session_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_handoff_slow_dispatch_timeout.py tests/tui_gateway/test_history_inline_images.py tests/tui_gateway/test_session_archive_rpc.py tests/tui_gateway/test_session_hidden_rpc.py tests/tui_gateway/test_session_list_lineage_rpc.py tests/tui_gateway/test_tui_gateway_server.py`
- **Merge risk:** high — 204 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tui_gateway.methods_session.*` in tests; 6 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-044 · `cron/scheduler_delivery.py` FILE_LINES 2131 (target 2000, cap 2131, 131 over)
- **Where:** T2 runtime edge; function units in this file: A-070; existing siblings: `cron/scheduler_delivery_origin.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 64 top-level defs/classes) into a new or existing `scheduler_delivery_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_exit_cleanup_interrupt.py tests/cron/test_bot_chat_cli_home.py tests/cron/test_bot_chat_delivery_child_env.py tests/cron/test_bot_chat_own_profile_multiplex_home.py tests/cron/test_bot_chat_pending.py tests/cron/test_bot_chat_pending_identity.py tests/cron/test_bot_chat_timeout_marker.py tests/cron/test_bot_chat_warning_replay.py tests/cron/test_cleanup_timeout.py tests/cron/test_cron_bot_chat_delivery.py tests/cron/test_cron_delivery_redaction.py tests/cron/test_cron_failure_deliver.py` … +37 more; full list: `grep -rlE "cron\.scheduler_delivery" tests --include=*.py | grep -v ^tests/e2e` Also reached through its facade `cron/scheduler.py`: 125 test files reference `cron.scheduler` (`grep -rlE "cron\.scheduler\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 52 string patch targets on `cron.scheduler_delivery.*` in tests; 49 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.91): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-045 · `plugins/platforms/buzz/adapter.py` FILE_LINES 2022 (target 2000, cap 2022, 22 over)
- **Where:** T2 runtime edge; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 48 top-level defs/classes) into a new or existing `adapter_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_adapter_settings_profile_parity.py tests/gateway/test_adapter_startup_secret_scope.py tests/gateway/test_buzz_forum_kinds.py tests/gateway/test_native_warning_coverage.py tests/gateway/test_shared_platform_boilerplate.py tests/plugins/platforms/buzz/test_buzz_unscoped_requirement_gate.py tests/plugins/platforms/test_interactive_setup_reconfigure_gate.py tests/tools/test_spawn_site_child_env.py`
- **Merge risk:** medium — 79 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `plugins.platforms.buzz.adapter.*` in tests; 8 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-046 · `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` FILE_LINES 3253 (target 2000, cap 3253, 1253 over)
- **Where:** T3 tooling; function units in this file: A-086, A-090
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 29 top-level defs/classes) into a new or existing `openclaw_to_hermes_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** no test file references this module directly — keep the change mechanical and add a differential check (see How to take a unit, step 5); also try `grep -rln "openclaw_to_hermes" tests`
- **Merge risk:** low — 17 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `optional-skills.migration.openclaw-migration.scripts.openclaw_to_hermes.*` in tests; 0 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-047 · `scripts/releases/authors_legacy.py` FILE_LINES 2043 (target 2000, cap 2043, 43 over)
- **Where:** T3 tooling; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: multi-session epic: each session moves ONE coherent topic (a group of the 0 top-level defs/classes) into a new or existing `authors_legacy_<topic>.py` sibling, fixes every importer in the same commit (no re-export shims for internal moves — root AGENTS.md), fixes docs that name the moved symbols, and lowers the cap. Pick the topic with the fewest test patch seams first. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/scripts/test_contributor_map.py` Also reached through its facade `scripts/releases/authors.py`: 2 test files reference `scripts.releases.authors` (`grep -rlE "scripts\.releases\.authors\b" tests --include=*.py | grep -v ^tests/e2e`) — run the ones exercising this behaviour.
- **Merge risk:** low — 1 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `scripts.releases.authors_legacy.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-048 · `tests/tui_gateway/test_tui_gateway_server.py` FILE_LINES 23551 (target 2000, cap 23551, 21551 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tui_gateway/test_tui_gateway_server_crash_history.py`, `tests/tui_gateway/test_tui_gateway_server_deep_truncate.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tui_gateway/test_tui_gateway_server.py`
- **Merge risk:** high — 115 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tui_gateway.test_tui_gateway_server.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-049 · `tests/agent/test_run_agent.py` FILE_LINES 7202 (target 2000, cap 7202, 5202 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_run_agent_codex_responses.py`, `tests/agent/test_run_agent_interrupt_hook.py`, `tests/agent/test_run_agent_multimodal_prologue.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_run_agent.py`
- **Merge risk:** medium — 46 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_run_agent.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-050 · `tests/hermes_state/test_hermes_state.py` FILE_LINES 7115 (target 2000, cap 7115, 5115 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_state/test_hermes_state_compression_busy_retry.py`, `tests/hermes_state/test_hermes_state_compression_locks.py`, `tests/hermes_state/test_hermes_state_ids.py`, `tests/hermes_state/test_hermes_state_readonly_preflight.py`, `tests/hermes_state/test_hermes_state_ux_messages.py`, `tests/hermes_state/test_hermes_state_wal_fallback.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_state/test_hermes_state.py`
- **Merge risk:** medium — 36 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_state.test_hermes_state.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-051 · `tests/hermes_cli/test_web_server.py` FILE_LINES 6706 (target 2000, cap 6706, 4706 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_web_server_approvals_broadcast.py`, `tests/hermes_cli/test_web_server_asset_mime.py`, `tests/hermes_cli/test_web_server_auto_archive_gateway_lock.py`, `tests/hermes_cli/test_web_server_auto_archive_profile_config.py`, `tests/hermes_cli/test_web_server_boot_handshake.py`, `tests/hermes_cli/test_web_server_chat_workspaces.py`, `tests/hermes_cli/test_web_server_config_offloop.py`, `tests/hermes_cli/test_web_server_console_ws.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_web_server.py`
- **Merge risk:** high — 171 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_web_server.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.89): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-052 · `tests/agent/test_auxiliary_client.py` FILE_LINES 5943 (target 2000, cap 5943, 3943 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_auxiliary_client_anthropic_custom.py`, `tests/agent/test_auxiliary_client_azure_foundry.py`, `tests/agent/test_auxiliary_client_base_url_host_validation.py`, `tests/agent/test_auxiliary_client_bootstrap_skew.py`, `tests/agent/test_auxiliary_client_cache_eviction.py`, `tests/agent/test_auxiliary_client_cache_follows_credential.py`, `tests/agent/test_auxiliary_client_minimax_oauth.py`, `tests/agent/test_auxiliary_client_nous_401_cache_key.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_auxiliary_client.py`
- **Merge risk:** high — 127 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_auxiliary_client.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-053 · `tests/gateway/test_slack.py` FILE_LINES 5748 (target 2000, cap 5748, 3748 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/gateway/test_slack_api_human_senders.py`, `tests/gateway/test_slack_approval_buttons.py`, `tests/gateway/test_slack_block_kit.py`, `tests/gateway/test_slack_block_kit_adapter.py`, `tests/gateway/test_slack_bot_auth_bypass.py`, `tests/gateway/test_slack_channel_session_scope.py`, `tests/gateway/test_slack_channel_skills.py`, `tests/gateway/test_slack_clarify_buttons.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_slack.py`
- **Merge risk:** medium — 93 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_slack.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-054 · `tests/tools/test_mcp_tool.py` FILE_LINES 4316 (target 2000, cap 4316, 2316 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_mcp_tool_401_handling.py`, `tests/tools/test_mcp_tool_errors.py`, `tests/tools/test_mcp_tool_issue_948.py`, `tests/tools/test_mcp_tool_session_expired.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_mcp_tool.py`
- **Merge risk:** medium — 63 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_mcp_tool.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.83): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-055 · `tests/agent/test_context_compressor.py` FILE_LINES 3824 (target 2000, cap 3824, 1824 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_context_compressor_bench_probe.py`, `tests/agent/test_context_compressor_cross_session_guard.py`, `tests/agent/test_context_compressor_custom_providers.py`, `tests/agent/test_context_compressor_reasoning_fallback.py`, `tests/agent/test_context_compressor_session_end_clears_state.py`, `tests/agent/test_context_compressor_structural_backoff.py`, `tests/agent/test_context_compressor_summary_continuity.py`, `tests/agent/test_context_compressor_summary_hook_compat.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_context_compressor.py`
- **Merge risk:** high — 121 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_context_compressor.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-056 · `tests/gateway/test_buzz_adapter.py` FILE_LINES 3554 (target 2000, cap 3554, 1554 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_buzz_adapter.py`
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_buzz_adapter.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-057 · `tests/tools/test_process_registry.py` FILE_LINES 3502 (target 2000, cap 3502, 1502 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_process_registry_completion_races.py`, `tests/tools/test_process_registry_failover_label.py`, `tests/tools/test_process_registry_lazy_restore.py`, `tests/tools/test_process_registry_list_exit.py`, `tests/tools/test_process_registry_owner_chain.py`, `tests/tools/test_process_registry_pty_kill.py`, `tests/tools/test_process_registry_windows_live.py`, `tests/tools/test_process_registry_write_stdin_surrogates.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_process_registry.py`
- **Merge risk:** medium — 78 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_process_registry.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-058 · `tests/agent/test_anthropic_adapter.py` FILE_LINES 3332 (target 2000, cap 3332, 1332 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_anthropic_adapter_extra_headers.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_anthropic_adapter.py`
- **Merge risk:** medium — 53 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_anthropic_adapter.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.71): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-059 · `tests/hermes_cli/test_gateway_service.py` FILE_LINES 3265 (target 2000, cap 3265, 1265 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_gateway_service_owner.py`, `tests/hermes_cli/test_gateway_service_paths.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_gateway_service.py`
- **Merge risk:** medium — 84 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_gateway_service.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-060 · `tests/gateway/test_matrix.py` FILE_LINES 3245 (target 2000, cap 3245, 1245 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/gateway/test_matrix_approval_reaction_fail_closed.py`, `tests/gateway/test_matrix_crypto_store_per_profile.py`, `tests/gateway/test_matrix_deps_rebind.py`, `tests/gateway/test_matrix_dm_invite_recording.py`, `tests/gateway/test_matrix_encrypted_drop_warning.py`, `tests/gateway/test_matrix_exec_approval.py`, `tests/gateway/test_matrix_handoff_thread.py`, `tests/gateway/test_matrix_latex_maths.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_matrix.py`
- **Merge risk:** medium — 30 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_matrix.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-061 · `tests/gateway/test_api_server.py` FILE_LINES 3169 (target 2000, cap 3169, 1169 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/gateway/test_api_server_active_work_drain.py`, `tests/gateway/test_api_server_artifact_store_async.py`, `tests/gateway/test_api_server_bind_guard.py`, `tests/gateway/test_api_server_compaction_projection.py`, `tests/gateway/test_api_server_declared_conversation.py`, `tests/gateway/test_api_server_jobs.py`, `tests/gateway/test_api_server_key_scope_regression.py`, `tests/gateway/test_api_server_media_data_urls.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server.py`
- **Merge risk:** medium — 57 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_api_server.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-062 · `tests/hermes_cli/test_relay_shared_metrics_runtime.py` FILE_LINES 3055 (target 2000, cap 3055, 1055 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_relay_shared_metrics_runtime.py`
- **Merge risk:** medium — 62 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_relay_shared_metrics_runtime.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-063 · `tests/hermes_cli/test_backup.py` FILE_LINES 2876 (target 2000, cap 2876, 876 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_backup_all_profiles.py`, `tests/hermes_cli/test_backup_import_deleted_holder.py`, `tests/hermes_cli/test_backup_path_errors.py`, `tests/hermes_cli/test_backup_preflight.py`, `tests/hermes_cli/test_backup_stability.py`, `tests/hermes_cli/test_backup_zip_serialization.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_backup.py`
- **Merge risk:** medium — 65 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_backup.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-064 · `tests/agent/test_run_agent_codex_responses.py` FILE_LINES 2731 (target 2000, cap 2731, 731 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_run_agent_codex_responses.py`
- **Merge risk:** low — 24 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_run_agent_codex_responses.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-065 · `tests/hermes_cli/test_plugins.py` FILE_LINES 2716 (target 2000, cap 2716, 716 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_plugins_admission_setter.py`, `tests/hermes_cli/test_plugins_cadence.py`, `tests/hermes_cli/test_plugins_check_updates.py`, `tests/hermes_cli/test_plugins_cmd.py`, `tests/hermes_cli/test_plugins_cmd_activation_keys.py`, `tests/hermes_cli/test_plugins_cmd_catalog.py`, `tests/hermes_cli/test_plugins_cmd_category_discovery.py`, `tests/hermes_cli/test_plugins_cmd_enable_disable_nested.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_plugins.py`
- **Merge risk:** medium — 61 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_plugins.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-066 · `tests/cron/test_scheduler.py` FILE_LINES 2646 (target 2000, cap 2646, 646 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/cron/test_scheduler_completion_verification.py`, `tests/cron/test_scheduler_cron_session_isolation.py`, `tests/cron/test_scheduler_provider.py`, `tests/cron/test_scheduler_shutdown_guard.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/cron/test_scheduler.py`
- **Merge risk:** medium — 66 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.cron.test_scheduler.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.96): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-067 · `tests/gateway/test_api_server_runs.py` FILE_LINES 2616 (target 2000, cap 2616, 616 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/gateway/test_api_server_runs_import_isolation.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_api_server_runs.py`
- **Merge risk:** medium — 48 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_api_server_runs.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.84): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-068 · `tests/tools/test_delegate.py` FILE_LINES 2595 (target 2000, cap 2595, 595 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_delegate_apiserver_background.py`, `tests/tools/test_delegate_auto_route_diagnostics.py`, `tests/tools/test_delegate_auto_route_gate.py`, `tests/tools/test_delegate_auto_route_visibility.py`, `tests/tools/test_delegate_batch_tag.py`, `tests/tools/test_delegate_batch_validation.py`, `tests/tools/test_delegate_by_provider_credentials.py`, `tests/tools/test_delegate_capability_inheritance.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_delegate.py`
- **Merge risk:** medium — 47 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_delegate.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.89): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-069 · `tests/gateway/test_feishu.py` FILE_LINES 2531 (target 2000, cap 2531, 531 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/gateway/test_feishu_approval_buttons.py`, `tests/gateway/test_feishu_bot_admission.py`, `tests/gateway/test_feishu_bot_auth_bypass.py`, `tests/gateway/test_feishu_channel_prompts.py`, `tests/gateway/test_feishu_comment.py`, `tests/gateway/test_feishu_comment_rules.py`, `tests/gateway/test_feishu_group_policy_surfacing.py`, `tests/gateway/test_feishu_lazy_import.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_feishu.py`
- **Merge risk:** medium — 44 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_feishu.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-070 · `tests/tools/test_computer_use.py` FILE_LINES 2457 (target 2000, cap 2457, 457 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_computer_use_approval_isolation.py`, `tests/tools/test_computer_use_ax_walk_bound.py`, `tests/tools/test_computer_use_backend_lifecycle.py`, `tests/tools/test_computer_use_backend_plugins.py`, `tests/tools/test_computer_use_backend_rebind.py`, `tests/tools/test_computer_use_capture_fence.py`, `tests/tools/test_computer_use_capture_routing.py`, `tests/tools/test_computer_use_capture_self_window.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_computer_use.py`
- **Merge risk:** medium — 56 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_computer_use.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-071 · `tests/hermes_cli/test_model_switch_custom_providers.py` FILE_LINES 2441 (target 2000, cap 2441, 441 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_model_switch_custom_providers.py`
- **Merge risk:** medium — 49 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_model_switch_custom_providers.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-072 · `tests/hermes_cli/test_profiles.py` FILE_LINES 2429 (target 2000, cap 2429, 429 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_profiles_s6_hooks.py`, `tests/hermes_cli/test_profiles_sidebar_cache.py`, `tests/hermes_cli/test_profiles_sidebar_failed_load.py`, `tests/hermes_cli/test_profiles_sidebar_scope.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_profiles.py`
- **Merge risk:** medium — 71 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_profiles.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.9): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-073 · `tests/hermes_cli/test_runtime_provider_resolution.py` FILE_LINES 2369 (target 2000, cap 2369, 369 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_runtime_provider_resolution.py`
- **Merge risk:** medium — 58 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_runtime_provider_resolution.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-074 · `tests/agent/test_error_classifier.py` FILE_LINES 2334 (target 2000, cap 2334, 334 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_error_classifier_content_policy.py`, `tests/agent/test_error_classifier_observed.py`, `tests/agent/test_error_classifier_upstream_blocked.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_error_classifier.py`
- **Merge risk:** medium — 70 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_error_classifier.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.96): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-075 · `tests/plugins/test_a2a_plugin.py` FILE_LINES 2269 (target 2000, cap 2269, 269 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/plugins/test_a2a_plugin.py`
- **Merge risk:** medium — 36 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.plugins.test_a2a_plugin.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.83): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-076 · `tests/tools/test_approval.py` FILE_LINES 2242 (target 2000, cap 2242, 242 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_approval_bun_deno_eval.py`, `tests/tools/test_approval_cancelled_attribution.py`, `tests/tools/test_approval_config_readonly.py`, `tests/tools/test_approval_deny_rules.py`, `tests/tools/test_approval_gateway_wait_late_choice.py`, `tests/tools/test_approval_global_flag_scan.py`, `tests/tools/test_approval_grep_in_substitution.py`, `tests/tools/test_approval_hook_session_id.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_approval.py`
- **Merge risk:** medium — 43 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_approval.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-077 · `tests/agent/test_credential_pool.py` FILE_LINES 2238 (target 2000, cap 2238, 238 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_credential_pool_anthropic_refresh_race.py`, `tests/agent/test_credential_pool_auto_detect.py`, `tests/agent/test_credential_pool_codex_singleton_isolation.py`, `tests/agent/test_credential_pool_connectors_guest.py`, `tests/agent/test_credential_pool_deferred_refresh.py`, `tests/agent/test_credential_pool_interrupt.py`, `tests/agent/test_credential_pool_key_rotation.py`, `tests/agent/test_credential_pool_lease_refresh_reselect.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_credential_pool.py`
- **Merge risk:** medium — 26 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_credential_pool.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.98): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-078 · `tests/hermes_cli/test_config.py` FILE_LINES 2227 (target 2000, cap 2227, 227 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_config_backups.py`, `tests/hermes_cli/test_config_cache_signature.py`, `tests/hermes_cli/test_config_check_diagnostics.py`, `tests/hermes_cli/test_config_display_language.py`, `tests/hermes_cli/test_config_dotted_key_names.py`, `tests/hermes_cli/test_config_edit_seed.py`, `tests/hermes_cli/test_config_effective.py`, `tests/hermes_cli/test_config_env_expansion.py` …
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_config.py`
- **Merge risk:** medium — 65 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_config.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-079 · `tests/tools/test_skills_hub.py` FILE_LINES 2186 (target 2000, cap 2186, 186 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_skills_hub_browse_sh.py`, `tests/tools/test_skills_hub_clawhub.py`, `tests/tools/test_skills_hub_generic_dir.py`, `tests/tools/test_skills_hub_github_fetch.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_skills_hub.py`
- **Merge risk:** medium — 52 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_skills_hub.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-080 · `tests/gateway/test_run_progress_topics.py` FILE_LINES 2179 (target 2000, cap 2179, 179 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/gateway/test_run_progress_topics.py`
- **Merge risk:** medium — 42 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.gateway.test_run_progress_topics.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-081 · `tests/tui_gateway/test_protocol.py` FILE_LINES 2136 (target 2000, cap 2136, 136 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tui_gateway/test_protocol.py`
- **Merge risk:** medium — 83 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tui_gateway.test_protocol.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-082 · `tests/agent/test_streaming.py` FILE_LINES 2107 (target 2000, cap 2107, 107 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_streaming_context_scrubber.py`, `tests/agent/test_streaming_tool_call_repair.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_streaming.py`
- **Merge risk:** low — 13 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_streaming.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.97): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-083 · `tests/hermes_cli/test_kanban_db.py` FILE_LINES 2097 (target 2000, cap 2097, 97 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/hermes_cli/test_kanban_db_init.py`, `tests/hermes_cli/test_kanban_db_repair.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py`
- **Merge risk:** medium — 61 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_kanban_db.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-084 · `tests/tui_gateway/test_hosted_room_service.py` FILE_LINES 2092 (target 2000, cap 2092, 92 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tui_gateway/test_hosted_room_service.py`
- **Merge risk:** low — 8 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tui_gateway.test_hosted_room_service.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-085 · `tests/tools/test_async_delegation.py` FILE_LINES 2091 (target 2000, cap 2091, 91 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/tools/test_async_delegation_failed_surface.py`, `tests/tools/test_async_delegation_fd_leak.py`, `tests/tools/test_async_delegation_orphan_sweep.py`, `tests/tools/test_async_delegation_requeue.py`, `tests/tools/test_async_delegation_retirement.py`, `tests/tools/test_async_delegation_stale_profile_scope.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tools/test_async_delegation.py`
- **Merge risk:** medium — 50 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tools.test_async_delegation.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.74): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-086 · `tests/agent/test_compression_concurrent_fork.py` FILE_LINES 2085 (target 2000, cap 2085, 85 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_compression_concurrent_fork.py`
- **Merge risk:** medium — 48 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_compression_concurrent_fork.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-087 · `tests/agent/test_model_metadata.py` FILE_LINES 2074 (target 2000, cap 2074, 74 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/test_model_metadata_fork.py`, `tests/agent/test_model_metadata_http.py`, `tests/agent/test_model_metadata_local_ctx.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/test_model_metadata.py`
- **Merge risk:** medium — 79 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.test_model_metadata.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.99): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-088 · `tests/hermes_cli/test_gui_command.py` FILE_LINES 2066 (target 2000, cap 2066, 66 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/hermes_cli/test_gui_command.py`
- **Merge risk:** medium — 67 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.hermes_cli.test_gui_command.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 0.95): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-089 · `tests/cron/test_jobs.py` FILE_LINES 2031 (target 2000, cap 2031, 31 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/cron/test_jobs_changed_notify.py`, `tests/cron/test_jobs_crossprocess_lock.py`, `tests/cron/test_jobs_file_ownership.py`, `tests/cron/test_jobs_shrink_merge.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/cron/test_jobs.py`
- **Merge risk:** medium — 49 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.cron.test_jobs.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-090 · `tests/agent/transports/test_codex_transport.py` FILE_LINES 2017 (target 2000, cap 2017, 17 over)
- **Where:** T4 tests; function units in this file: none; existing siblings: `tests/agent/transports/test_codex_transport_github_reasoning.py`
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/agent/transports/test_codex_transport.py`
- **Merge risk:** medium — 50 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.agent.transports.test_codex_transport.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

#### B-091 · `tests/tui_gateway/test_hosted_room_driver_runtime.py` FILE_LINES 2008 (target 2000, cap 2008, 8 over)
- **Where:** T4 tests; function units in this file: none
- **Remedy:** move an existing function or class into a topical sibling `<stem>_<topic>.py` (moved code keeps its cap); split a test file into `test_<stem>_<topic>.py`. Approach: split by topic into new `test_<stem>_<topic>.py` files next to it (move whole test classes / groups with their fixtures; shared fixtures go to the directory's conftest only if already shared); run the old and new files and compare collected test counts. Taking this file's A-units first usually lowers FILE_LINES as a side effect (their phases move to a sibling).
- **Tests:** `scripts/run_tests.sh tests/tui_gateway/test_hosted_room_driver_runtime.py`
- **Merge risk:** low — 7 commits touched this file on main in the 90 days to 2026-10-17; 0 string patch targets on `tests.tui_gateway.test_hosted_room_driver_runtime.*` in tests; 1 test files reference it. Upstream (NousResearch main@18c525b6): file shared with upstream (line similarity 1.0): moving code out changes where upstream hunks land — each slice adds manual-port work at the next upstream sync

## Queue C, D and advisory hits

- `queue-c-tier1.md` (1,040 units), `queue-c-tier2.md` (248), `queue-c-tier3.md` (241),
  `queue-c-tier4.md` (508): one row per (file, rule family), ordered tier → family → hit count;
  columns give the rules and counts, lines on main, churn, whether the file is identical to upstream,
  and the test files to run. Remedies: Rule-family playbooks above.
- `queue-d-js-and-out-of-lane.md`: not for this series (Owner decision 3).
- `advisory-hits.md`: the 277 advisory findings (HX012, HX001, PS-P06, HX003, HX010, PS-P05) — they
  do not fail the gate; listed for completeness.

## Progress log

- 2026-10-17 — session 1: measured; landed `build_anthropic_kwargs`, `run_import`,
  `_handle_warm_action` (branch `quality/ratchet-backlog-slice1`). Next: first row of Next pick.
- 2026-10-17 — session 3 (A-008): landed `agent/anthropic_adapter.py::_strip_unknown_tool_blocks`
  (branch `claude/strip-unknown-tool-blocks-health-i0uhc5`). CC 29 → 19, NESTING 7 → 4
  (FUNC_LINES 210 → 113; file 1987 → 1890 lines); new sibling `agent/anthropic_adapter_tool_blocks.py`
  (breadcrumb builders, result-text join, per-message reorder; max CC 4, NESTING 3; late-imports
  `logger` from the facade). Baselined BLE001 hits stay inline in the unit, in order. 55 test files
  referencing the adapter: 1307 passed, 0 failed, 17 skipped; differential old-vs-new over 20,002
  randomized message histories (every role/block type, unknown/known/unnamed/id-less tool_use,
  str-raising input and result text, thinking blocks; output, in-place mutation, exceptions and log
  records compared): 0 mismatches, sibling 100% line+branch coverage under the grid.
  `python scripts/check` (full) green, 12 checks ok; health gate 0 blocking/0 advisory.
  Next: next row of Next pick (A-011).
- 2026-10-17 — A-011 `tools/personas_sync.py::sync_personas`: CC 29 → 8, NESTING 8 → 2 (same-file
  helpers `_sync_new_persona`, `_sync_existing_persona`, `_update_from_bundled`,
  `_restore_from_backup`; max CC 7 / nesting 3). Tests 9/9; differential old-vs-new identical over
  366 fault-injected cases (branch `claude/sync-personas-code-health-8u1cn7`). Next: A-020.
- 2026-10-17 — A-020 `tools/delegation_router.py::route_task_models` CC 26 → 13 (same-file helpers
  `_gate_providers`, `_partition_tasks`, `_tier_roles`, `_classify_limits`, `_unpack_verdict`,
  `_advisory_entry`, `_full_route`, each CC ≤ 5); 128 tests + 24,192-case old-vs-new differential
  green (branch `claude/reduce-route-task-models-cc-8w5epf`). Next: A-025 (pick 4).
- 2026-10-17 — A-025 `tools/delegate_tool.py::_resolve_task_routes`: CC 25 → 12 (FUNC_LINES 292 → 174)
  via same-file helpers `_route_advisory`, `_resolve_role_route_creds`, `_warn_unrouted_task`,
  `_route_info` (CC 2/7/6/3); BLE001 hit kept inline (branch
  `claude/reduce-delegate-tool-complexity-pl169p`). Next: first still-over-cap row of Next pick.
- 2026-10-17 — A-046 done: `GatewayTurnMixin._resolve_session_agent_runtime` CC 22 → 12 (channel-override
  and empty-model safety-net phases → `gateway/run_turn_model_resolution.py`, CC 6/6; BLE001 hit kept
  inline). Next: A-048 `hermes_cli/config.py::convert_auxiliary_to_provider_first`.
- 2026-10-17 — A-048 `convert_auxiliary_to_provider_first`: CC 22 → 10 (per-task phase →
  `hermes_cli/config_aux_provider_first.py::accumulate_aux_task`, CC 13); config.py 4722 → 4674
  lines (branch `claude/ratchet-convert-auxiliary-provider-5xngey`). Next: next row of Next pick.
- 2026-10-17 — A-128 `tests/hermes_cli/test_diff_replay_no_stale_cells.py::replay`: CC 26 → 20
  (extracted `_skip_osc`, `_paint_run`; HX011 ladder left inline). Branch
  `claude/reduce-replay-complexity-jcfmyt`. Next: next fork-only row of Next pick.
- 2026-10-17 — C-1044 `plugins/model-providers/router/__init__.py` F821×2 (real bug): defined
  `_DISK_TTL_SECONDS = 24 * 3600` (mirrors `hermes_cli/models_reasoning_caps`'s disk-mirror TTL);
  an unparseable-`ts` mirror and a fresh/stale mirror now hydrate instead of NameError (line 228
  had broken disk hydration whenever a mirror parsed). New
  `tests/plugins/model_providers/test_router_disk_mirror.py` (4 tests, all red on base). Branch
  `claude/fix-disk-ttl-seconds-undefined-npwxbi`. Note: upstream-identical unit, upstream carries
  the same bug. Next: next row of Next pick.
- 2026-10-17 — review follow-up (slice 1 safety nets → committed tests): behavior locks
  `tests/agent/test_anthropic_kwargs_behavior_lock.py` (seeded 910-case grid over models ×
  endpoints × OAuth × tools × tool_choice × reasoning × caps × fast × cache, checked against the
  adapter's own predicates + 3 pinned requests; 6 tests, ~1.4s) and
  `tests/tools/test_memory_tool_warm_actions.py` (every `_WARM_ACTIONS` entry, session-pin trio and
  store gate through `_handle_warm_action` with recording fakes; exact JSON + call log; 75 tests).
  Both turn red on every one of 14 hand mutations of the units. The holographic round-trip in
  `tests/agent/test_memory_tool_warm_dispatch.py` now skips when the provider is not installed.
  Branch `claude/add-regression-tests-slice-1-9ypeqj`. Next: next row of Next pick.
- 2026-10-17 — A-070 `cron/scheduler_delivery.py::_deliver_to_bot_chat`: CC 28 → 20 (FUNC_LINES
  222 → 175; file 2131 → 2085 lines); delivery-key, pending-admission/outcome, live-receipt outcome
  and CLI-target pinning phases → new sibling `cron/scheduler_delivery_bot_chat.py` (5 functions, max
  CC 3 / nesting 1; late-imports `logger` from the facade). All four BLE001 hits stay inline, in
  order. 62 test files: 1245 passed, 0 failed, 6 skipped (4 gateway files need `aiohttp`, absent
  here); differential old-vs-new over 20,000 randomized fault-injected scenarios: 0 mismatches,
  sibling 100% line+branch (branch `claude/code-health-deliver-to-bot-chat-l1idyz`). Next: A-059.
- 2026-10-17 — A-032 `agent/anthropic_adapter.py::create_anthropic_message`: CC 22 → 11, NESTING 7 → 5
  (per-event callback + two-phase deadline loop → same-file `_drain_stream_with_deadlines`, CC 12 /
  NESTING 3; file 1890 → 1913 lines). 53 adapter-referencing test files: 1231 passed, 0 failed,
  17 skipped; differential old-vs-new over 20,000 randomized stream cases (fake clock, iterable /
  non-iterable streams, beta / plain clients, stream-unavailable / other errors, raising callbacks,
  ceiling / idle values incl. invalid; return, exceptions, call order, logs, kwargs mutation and
  clock reads compared): 0 mismatches, unit + helper 100% line+branch. `python scripts/check` green
  (branch `claude/reduce-create-anthropic-message-complexity-tj88w7`). Next: A-070 (pick 9).
- 2026-10-17 — A-059 `hermes_cli/cli_stream_mixin.py::CLIStreamMixin._emit_stream_text`: CC 21 → 15
  (FUNC_LINES 140 → 104); sentence-boundary early flush → same-file
  `_flush_stream_sentence_early(emit_one)` (CC 7, NESTING 4). BLE001 (466) and BLE001+S110 (566)
  hits kept inline, in order. 14 stream test files 82/82; differential old-vs-new over 20,000
  randomized chunk sequences: 0 mismatches, new method at 100% line+branch coverage (branch
  `claude/health-emit-stream-text-x9jjrs`). Next: A-004 (pick 11).
- 2026-10-17 — A-004 `gateway/platforms/api_server_openai_routes.py::OpenAICompatRoutesMixin._handle_responses`:
  CC 33 → 17 (FUNC_LINES 164 → 86, NESTING 4 → 2) via same-file helpers `_responses_input_messages`,
  `_responses_explicit_history` (module level, CC 8/7) and the `_stream_responses` / `_complete_responses`
  branches (CC 1/6); BLE001 hit kept inline; file 1336 → 1376 lines. 73 api_server test files: 1430
  passed, 1 failed, 27 skipped — identical on clean main (the failure is `test_model_validation.py::
  …test_non_nous_provider_does_not_consult_portal_feed`). Note: the api_server tests need aiohttp, so
  build the venv with `--extra messaging` too, or they fail to collect. Differential old-vs-new
  over 20,000 randomized requests (fake store/route/agent/SSE, response + call log + queued events
  compared): 0 mismatches, 100% line+branch coverage of the unit. Branch
  `claude/refactor-openai-handle-responses-yggrzc`. Next: A-051 (pick 12).
- 2026-10-17 — C-0003 `agent/fork/rate_limit_tracker.py` F821×1 → 0 (file hits 10 → 9): added the
  missing `from typing import Any` for the `headers: Any` annotation. No runtime change (the module
  has `from __future__ import annotations`), but `typing.get_type_hints()` on
  `capture_rate_limits_from_headers` raised `NameError` on base and resolves now. No other F821 in
  the file; the other 9 hits (swallow family) left in place. 5 rate-limit test files: 76 passed, 0
  failed. Branch `claude/fix-rate-limit-tracker-f821-ijyuhu`. Next: C-0004.
- 2026-10-17 — C-0004 `hermes_cli/cli_init_mixin.py` correctness F821×1 → 0: the
  `_present_store_warning` closure now reads a closure-safe `store_error` local instead of the
  `except … as e` name (details call stays inside the rich `try`, so a raising `__str__` still
  falls back to the plain warning). **Correction to Notable findings:** the claimed bug does not
  occur — `render_notification` calls the renderer synchronously inside the `except` block, while
  `e` is still bound, so the rich warning with details was already shown; the hit was a latent
  hazard (any deferral would NameError), not a live bug. New behaviour lock
  `test_store_failure_warning_carries_the_raw_details` in
  `tests/hermes_cli/test_cli_session_store_shared_registry.py` (2/2 pass; green on base too, as
  expected). No sibling instances (the file's other except-name, `exc` at ~177, is not captured).
  `python scripts/check` green, 12 checks. Branch `claude/fix-cli-init-mixin-f821-e6yqll`.
  Next: C-0003 (`agent/fork/rate_limit_tracker.py` F821, fork-only; still open), then C-0007.
- 2026-10-17 — C-0006 `hermes_cli/gateway_launchd.py` F811×1 → 0: removed the unused module-level
  `from xml.sax.saxutils import escape`; its only caller (`generate_launchd_plist`) already bound the
  function-local `html.escape`, so the plist output is unchanged. No other F811 in the file; the 3
  BLE001 hits untouched. Tests 125 passed, 1 failed (`test_remaps_default_home`, identical on main).
  Branch `claude/fix-gateway-launchd-f811-ctvz3j`. Next: C-0007.
- 2026-10-17 — C-0007 `hermes_cli/web_routers/display.py` (correctness): F821×1 → 0. `_open_rfb`'s
  `profile_home: Path` annotation named a `Path` only imported locally inside `_bridge` (latent:
  `typing.get_type_hints(_open_rfb)` raised `NameError` on main); `Path` is now a module-level import
  and `_bridge`'s redundant local import is gone. No other F-rule hits in the file. Display ws tests
  8/8; `scripts/check` ok. Branch `claude/fix-display-py-f821-0ikhxl`.
- 2026-10-17 — pre-existing red `tests/hermes_cli/test_width_engine_matrix.py::test_fixture_is_pinned_to_shipped_xterm_version`:
  not a version drift (fixture, `web/` + `apps/desktop/` pins and `package-lock.json` all say
  `@xterm/addon-unicode11` 0.9.0) — the test read the shipped version from an installed
  `node_modules/@xterm/addon-unicode11/package.json`, which the Python test env never has
  (FileNotFoundError on every clean checkout). It now reads the committed lockfile's resolution(s) and
  requires exactly the fixture's version; a lockfile bump to 0.10.0 still fails with the regenerate
  message. File 4/4 passed. Branch `claude/fix-xterm-version-fixture-j4vcmn`.
- 2026-10-17 — pre-existing red `test_commandcode_profile.py::…::test_resolve_provider_full` (env-dependent:
  red only when models.dev is reachable). Root cause: models.dev added a `commandcode` entry
  (2026-10-14, `env = ["CMD_API_KEY"]`), and `hermes_cli/providers.py::get_provider` let a bare
  models.dev hit shadow the plugin profile (key `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL` both
  dropped) — code bug, not a stale test: the runtime registry and the picker still use Hermes's names.
  Fix: an overlay-less models.dev hit now takes its overlay from the plugin profile
  (`_profile_overlay`: transport, auth_type, key vars, endpoint, base-URL var); explicit
  `HERMES_OVERLAYS` entries keep precedence. Same exposure closed for gemini/meta-ai/deepinfra/alibaba-cn.
  Test now pins both catalog states (absent / listed under `CMD_API_KEY`); `[True]` red on base with
  the reported assertion. 196 provider/model-switch test files: 4180 passed, 12 failed — the same 12
  on base (no `acp` module, exo needs egress, meta-ai id, firecrawl). `python scripts/check` green.
  Branch `claude/fix-commandcode-api-key-test-uot9md`. Next: next row of Next pick.
- 2026-10-17 — C-1053 `plugins/platforms/simplex/adapter.py` (async): ASYNC240×3 + HX008×1 → 0.
  `_send_command` creates its reply future on `asyncio.get_running_loop()`; the three
  `Path(...).exists()` guards in `send_image` / `send_document` / `send_voice` now run via
  `await asyncio.to_thread(...)` (same short-circuit, same error strings, same await order). Left:
  `send_image`'s synchronous `_prepare_image` (Pillow / `convert`) call — blocking, but not an
  ASYNC240 hit and moving it off-loop is its own change. Tests: 6 simplex-touching files 95 passed
  (5 `test_send_cmd` WhatsApp errors = missing `aiohttp`, identical on main); old-vs-new
  differential over existing/missing/empty paths + `_send_command` timeout identical;
  `scripts/check` ok. Branch `claude/c1053-async-adapter-ratchet-5o50p4`. Next: C-1054.
- 2026-10-17 — C-0017 `gateway/platforms/signal.py` async: ASYNC240 3 → 0. `_resolve_image_path`'s
  exists-then-stat moved off the loop into one `asyncio.to_thread(_existing_file_size, …)` (new
  module helper, same missing/oversize semantics); `_send_attachment` awaits
  `asyncio.to_thread(Path(file_path).stat)` (FileNotFoundError still caught). 8 signal/send test
  files: 137 passed, 1 skipped; old-vs-new differential over 9 inputs (empty, bare `file://`, small,
  oversize, directory, broken symlink, missing, URL download): 0 mismatches. `python scripts/check`
  green. Branch `claude/code-health-ratchet-c0017-uea4i3`. Next: C-0018.
- 2026-10-17 — C-1055 `plugins/platforms/buzz/adapter.py` (async): ASYNC240×3 → 0. The three local-media
  probes (`send_image`, `_send_file_attachment`, `send_image_file`) now run `Path(...).expanduser` and
  the sibling `is_file()` stat via `await asyncio.to_thread(...)`; `_send_file_attachment(probe=False)`
  uses the caller's already-expanded path as-is. No other ASYNC/HX007-9/RUF006 hits in the file
  (ASYNC109 is not a ratchet rule). File held at 2,022 lines (its cap). Buzz + adapter-parity tests:
  16 files, 521 passed, 0 failed. `python scripts/check` green, 12 checks.
  Branch `claude/buzz-adapter-async-fixes-fn9jmb`.
- 2026-10-17 — C-0020 `agent/context_references.py` async ASYNC240×2 → 0: the `cwd` / `allowed_root`
  `expanduser().resolve()` in `preprocess_context_references_async` now run via
  `await asyncio.to_thread(_expand_and_resolve, ...)` (new same-file sync helper; same values and
  exceptions, `to_thread` carries the caller's contextvars). No other ASYNC hits in the file. Left: the
  sync `_expand_path_reference` / `_expand_git_reference` still do filesystem/git work on the loop
  from `_expand_reference` (not flagged by the rule; a larger off-loop change, own unit if wanted).
  15 referencing test files: 880 passed, 0 failed, 1 skipped; `python scripts/check` green, 12 checks.
  Branch `claude/fix-context-references-async-e59xwr`. Next: C-0021.
- 2026-10-17 — C-1041 `plugins/platforms/feishu/adapter.py` correctness: F821 28 → 0 (other rules
  unchanged: ASYNC230 1, ASYNC240 5, BLE001 21, S110 5; FILE_LINES 4493 → 4493). One cause behind all
  28: the lazy-loaded lark_oapi SDK names were bound only by `globals().update(...)`, invisible to
  static analysis. Not latent NameErrors — the names did exist at runtime. Fix: the None placeholders
  are spelled out as plain module assignments; `_LARK_SDK_IMPORTS` and `_load_lark_oapi` are unchanged
  (table checked identical by AST; every table name has a placeholder; all are None before load).
  Lines kept flat by packing the placeholders and two table lines. 26 Feishu/adapter test files: 420
  passed, 2 failed — the same 2 on base (`test_feishu.py` webhook tests, no `aiohttp` in the test env).
  `python scripts/check` green. Branch `claude/fix-feishu-adapter-f821-vfjpql`.
- 2026-10-17 — C-0014 `gateway/platforms/whatsapp_cloud.py` async: ASYNC230×1 + ASYNC240×4 → 0
  (file hits 13 → 8; the 8 BLE001 hits untouched, in place and in order). `_upload_media`'s
  exists/getsize and the upload `open()` now hop through `asyncio.to_thread` (the handle still
  streams into the multipart upload; stale `# noqa: ASYNC230` removed), `send_voice`'s `.mp3`
  exists check likewise. Sibling instances of the same class, not flagged by ruff, fixed too: the
  inbound cache `write_bytes`, the transient `.ogg` `os.unlink`, and the ≤100KB document read
  `_inject_document_text` (now run via `to_thread` from `_build_message_event_from_cloud`). Exception
  paths unchanged (mkdir still raises, write still caught as `OSError`, open errors still logged
  and returned). 11 test files referencing the adapter: 226 passed, 0 failed; old-vs-new
  differential over 150 upload cases (no client / missing / directory / oversized / unreadable ×
  post ok / raise / 500 / no id), 4 download and 5 injection cases: 0 behavioural mismatches.
  Left: `rich_sent_store.lookup` (sync, called from the async event builder) is another module's
  API, not this unit. `python scripts/check` green. Branch
  `claude/fix-whatsapp-cloud-async-rules-4zw8mf`. Next: C-0017 (`gateway/platforms/signal.py`, fork-only).
- 2026-10-17 — C-1046 `plugins/platforms/line/adapter.py` (async): ASYNC240×6 + RUF006×1 → 0 (BLE001
  15 → 15, in place and in order; file 996 → 1014 lines). `_handle_media`'s `is_file` and both
  HERMES_HOME `resolve()`s now run via `await asyncio.to_thread(...)` (the `except Exception` fallback
  stays inline), and the request-path resolve + allowed-roots check moved to the sync helper
  `_resolve_in_media_roots` (one hop). `send_video`'s preview `is_file` is off-loop too. The DM
  typing-indicator task is now held in `_background_tasks` with a discard callback (`loading()`
  already swallows its errors). Unflagged siblings fixed: the three `_check_media_file` preflights
  (`is_file` + `stat`) run via `to_thread`, and the fallback-PNG temp write is `_write_fallback_preview`
  run via `to_thread` (unlink-on-failure kept on both sides). Left: `_register_media` / `_serve_file`
  still `resolve()` on the loop — they mutate the token table and evict temp files, so moving them to
  a worker thread would race the loop. 7 LINE-touching test files: 148 passed, 2 skipped; old-vs-new
  differential (184 cases: serve 200/403/404/410 incl. symlink-out and HERMES_HOME failure, send
  image/voice/video × connected/public-URL × 8 paths, preview write failure): only difference is the
  tracked loading task. `python scripts/check` green, 12 checks. Branch
  `claude/async-adapter-ratchet-k716n8`.
- 2026-10-17 — C-1050 `plugins/platforms/mattermost/adapter.py` async: ASYNC240×5 → 0 (no other
  ASYNC hits in the file). `_send_local_file` and `_load_batch_image` run `Path.exists`/`read_bytes`
  via `asyncio.to_thread`; `_standalone_send` runs its media `os.path.exists` probe the same way.
  Evaluation order and exception paths unchanged (a directory path still raises
  `IsADirectoryError` to the caller). 17 test files referencing mattermost (test env built with
  `--extra messaging`; without it aiohttp is missing and 23 tests fail on main too): 327 passed,
  3 failed — the Slack-only `TestSlackDownloadSlackFileBytes` cases in
  `test_media_download_retry.py`, identical on main (DNS blocked for files.slack.com). Old-vs-new
  differential over 23 cases (local send / batch `file://` / standalone media grid × existing,
  missing, directory, empty, %-encoded): 0 mismatches. `python scripts/check` green. Branch
  `claude/code-health-c-1050-async-9qggr3`.
- 2026-10-17 — C-1052 `plugins/platforms/photon/adapter.py` async: HX008×3 → 0, RUF006×1 → 0.
  `connect`, `_start_sidecar` and `_supervise_sidecar` (all coroutines) use
  `asyncio.get_running_loop()`; `_dispatch_fatal_notification` now holds the detached notifier in
  the base adapter's `_detached_fatal_tasks` set (discarded on completion) — not
  `_background_tasks`, which `cancel_background_tasks()` would cancel mid-handoff. Its exception was
  already handled by `_notify_fatal_error_logged`. Real latent bug: a notifier parked on an
  otherwise-unreferenced future was garbage-collected mid-handoff ("Task was destroyed but it is
  pending"); new `test_inflight_notification_is_strongly_referenced` in
  `tests/plugins/platforms/photon/test_fatal_notify_self_cancel.py` is red on base, green now.
  The 15 swallow hits (C-1104) untouched, in place. 41 test files (all photon tests + every test
  naming photon + the queue row's files): 949 passed, 0 failed, 32 skipped. `python scripts/check`
  green, 12 checks. Branch `claude/code-health-c1052-adapter-1fv0ye`. Next: next open fork-only or
  diverged Queue C unit.
- 2026-10-17 — C-0013 `gateway/platforms/weixin.py` async: ASYNC240×3 + RUF006×2 → 0 (file hits
  22 → 17; the 17 BLE001 hits untouched, in place and in order; FILE_LINES 1243 → 1261). RUF006: the
  inbound `_process_message_safe` dispatch in `_poll_loop` and the `_fetch_typing_ticket` prefetch in
  `_process_message` now go through `WeixinAdapter._spawn`, which keeps a strong ref in
  `_inflight_tasks` until done and has a done-callback (`_reap_task`) that logs anything that escapes
  (both coroutines already catch `Exception`, so in practice nothing does). Same scheduling as before:
  no new await, not cancelled at `disconnect()` (deliberately not put in base `_background_tasks`,
  which shutdown cancels). ASYNC240: `send_image`'s `os.path.abspath` and its temp-file
  exists/unlink cleanup (new module helper `_remove_file_quietly`, same `suppress(OSError)`) and
  `_send_file`'s `Path.read_bytes` now hop through `asyncio.to_thread`. 17 test files referencing
  weixin: 486 passed, 25 skipped, 1 failed — the same 1 on base
  (`test_poller_fd_lifecycle.py::test_recycle_closes_old_and_installs_fresh_session`, no `aiohttp`
  in the test env). Old-vs-new differential (send_image remote ok / raise / temp already gone, local
  relative / absolute / `file://`; poll-loop dispatch of 5 messages + refs released): 0 mismatches.
  Left (sync I/O in async paths that the rules don't flag, each adds an await point that would change
  cancellation shape — own unit if wanted): `_load_sync_buf` at `_poll_loop` start,
  `ContextTokenStore.restore` in `connect`/`send_weixin_direct`, `save_weixin_account` in `qr_login`,
  the temp-file write in `_download_remote_media`. `python scripts/check` green, 12 checks. Branch
  `claude/weixin-async-health-ratchet-cnbqya`.
- 2026-10-17 — C-0011 `gateway/platforms/qqbot/adapter.py` (async): ASYNC230×1 + ASYNC240×6 + RUF006×1
  → 0 (19 BLE001 untouched, in place and in order). `_create_task` now holds a strong ref to every
  task it spawns (`_dispatch_tasks`) and logs a failed one with its traceback; the dangling
  `asyncio.create_task(self._on_message(...))` goes through it (siblings: ws close, Identify/Resume,
  interactions). Blocking file/path I/O in async defs → `asyncio.to_thread`: the flagged lines plus
  the audio pipeline's temp write/unlink, pilk attempt (new `_pilk_to_wav`), raw-PCM write
  (`_write_raw_wav`), STT upload now reads the wav bytes off-loop (old `# noqa: ASYNC230` gone),
  upload path checks (`_resolve_upload_path`). Helpers sit *before* their callers so the
  SequenceMatcher line survival keeps the BLE001 `except` lines old. New
  `tests/gateway/test_qqbot_async_io.py` (3 tests; the task-ownership one red on base). 15 qqbot test
  files 430/430; differential old-vs-new over 720 audio-conversion cases (fake pilk/ffmpeg modes):
  0 mismatches. `scripts/check` green. Branch `claude/fix-qqbot-adapter-async-rules-l5wfo4`.
  Next: C-0012.
- 2026-10-17 — C-1056 `plugins/platforms/matrix/adapter.py` (async): ASYNC240×1 + HX009×1 + RUF006×1 → 0
  (file hits 55 → 52; the 51 BLE001 + 1 S110 untouched, in place and in order). `_send_local_file`'s
  expanduser/exists/stat and the upload `read_bytes` hop through `asyncio.to_thread` (same not-found
  warning, same OSError→0 size, read errors still propagate); `_dispatch_sync` checks `BaseException`
  so a cancelled handler is logged too; `_background_read_receipt` keeps its task in
  `_read_receipt_tasks` (discarded on done; `_send` already catches and logs). Sibling blocking path
  calls in async defs, not flagged by ruff, moved off-loop too: connect's store-dir `mkdir`, the
  legacy `crypto_store.pickle` exists/unlink, and `send_voice`'s temp-file `os.unlink`. Left: the
  sync recovery-key file helpers (`_write_matrix_recovery_key_output_file` etc.), called from a sync
  helper inside cross-signing bootstrap — one-shot and profile-scoped, own change. FILE_LINES held at
  3,267 (cap) by repacking `_send_local_file`'s signature. New invariant test
  `tests/gateway/test_matrix_sync_dispatch_cancel.py` (red on main). 35 matrix-referencing test files:
  same failure set as main in both the test env (17, all missing `aiohttp`) and a `--extra matrix`
  env (2, `test_matrix_deps_rebind.py` expects mautrix absent). Branch
  `claude/matrix-adapter-code-health-ratchet-e3vm4o`.
- 2026-10-17 — C-0029 `gateway/platforms/bluebubbles.py` (async): RUF006×1 → 0 (the 9 BLE001 untouched,
  in place and in order; FILE_LINES 610 → 612). `_handle_webhook`'s fire-and-forget read receipt now
  lives in `_read_receipt_tasks` (discarded on done) so it cannot be garbage-collected mid-run; its
  exceptions were already handled — `mark_read` → `_private_api_chat_call` suppresses and returns
  False. A dedicated set, not `_background_tasks`, so shutdown's `cancel_background_tasks` behaves as
  before. No other `create_task`/`ensure_future` in the file (the `handle_message` task beside it was
  already tracked). New invariant test `tests/gateway/test_bluebubbles_read_receipt_task.py` (red on
  main). Unit's 3 test files + new one: 100/100 in a `--extra sms` env (the plain test env lacks
  `aiohttp`, which the webhook handler imports). Branch `claude/async-bluebubbles-ratchet-6urbee`.
- 2026-10-17 — C-0023 `hermes_cli/web_routers/dashboard_ui.py` (async): ASYNC240×2 → 0 (file hits 5 → 3;
  the 3 BLE001 untouched, in place and in order). `post_agent_plugin_activate`'s `expanduser` plus
  the sibling `profile_name_for_home` (it resolves the path) move into `_served_profile_for_home`,
  run via `asyncio.to_thread` (same 400 for a non-profile home). `serve_plugin_asset`'s
  `resolve`/`is_relative_to(base.resolve())` plus the sibling `exists`/`is_file` stats move into
  `_resolve_plugin_asset`, run via `asyncio.to_thread` (same 403/404). Helpers sit before their
  callers. Left: `serve_plugin_asset`/`rescan_dashboard_plugins` still call `_get_dashboard_plugins()`
  and `_plugin_enable_sets()` on the loop (discovery cache + config read, not this rule; own change).
  New `tests/hermes_cli/test_dashboard_ui_offloop_paths.py` (2 tests, both red on main);
  `test_plugins_hub_perf_guard.py`, `test_web_server.py`, `test_dashboard_auth_middleware.py`,
  `test_plugin_runtime_disable_gate.py` 235 passed / 25 skipped; differential old-vs-new over 182
  activate/asset cases (bodies, homes, traversal, symlinks, missing, dirs): 0 mismatches.
  `scripts/check` green. Branch `claude/c-0023-dashboard-ui-async-6cb3zu`.
- 2026-10-17 — C-0024 `hermes_cli/web_routers/files.py` async: ASYNC240 2 → 0. `get_media`'s path
  resolve / suffix / media-root / `is_file` / size checks moved into a same-file sync
  `_media_target` run via `await asyncio.to_thread(...)` (same checks, order, status codes and
  details); `stream_upload_to_path`'s temp cleanup is `await asyncio.to_thread(tmp_path.unlink,
  missing_ok=True)` (cancellation-cleanup test still green). New invariant test (red on main):
  `get_media` path probes run off the loop. Left: unflagged sync I/O in other `async def` routes
  (`list_managed_files`, upload/mkdir/delete, `mkstemp`/`os.replace` in `stream_upload_to_path`) —
  not ratchet hits, separate change. Tests: unit's 5 files + content-filter scrub 266 passed / 31
  skipped; ops callers (credential pool, destructive scope, admin endpoints) 82 passed.
  `scripts/check` ok. FILE_LINES 992 → 997. Branch `claude/fix-c0024-files-async-30hdxi`. Next: C-0025.
