# PM audit remediation status

This change integrates repairs from the aggregate branch audit. It is not a
release certificate. The audit compared `49945b14029e09fef608db9ede899377cdb54e11`
with merge base `433f7196760e5d76f77ea6d5464ee7f1b602a4ee`.

## Implemented repairs

| Findings | Implementation |
| --- | --- |
| C01, C11, C12, C24 | Remove the deleted TLS import; fix ACP file decoding, proxy path ownership, and the monitoring SDK call. |
| C02 | Restore code-execution schema, registration, and child lifecycle handling. |
| C03, C04, C17 | Keep update metadata commands read-only; synchronize plugin updates; compare feed identities correctly. |
| C05, C27, C28 | Include managed Python in the install closure; repair installer stage dispatch and runtime selection. |
| C06, C07, C09 | Remove the unsupported SCM surface. Retain Scheduled Task supervision and protect unrelated runtimes on desktop quit. |
| C08, C19 | Correct the App Installer checker and restore meaningful update states. |
| C10 | Repair uninstall dispatch and owned-link removal. |
| C13, C14 | Prepare one complete dependency environment before publication. Preserve declared constraints and explicit pins; permit compatible transitive upgrades. Infrastructure failures do not disable plugins. |
| C15, C18 | Retain recoverable tool-store entries on failed publication. Use atomic facts/config writes and context-local receipts. |
| C16, C29 | Migrate removed dependency helpers and restore missing-dependency hints. Run the embedded Hindsight service from its separate environment. |
| C20, C21 | Correct platform-test selection and make the removed-import guard inspect actual source. |
| C22, C23 | Publish immutable release artifacts before feeds and resolve FFmpeg artifacts by target. |
| C25 | Preserve paused downloads, resume handles, and single-worker ownership. |
| C26 | Wire the boot and plugin-cadence paths to their existing owners. |

Implementation does not mean that every platform acceptance test is complete.
The updater, backup, setup, voice-text helpers, and several plugins now use
one implementation per reconciled concern. Runtime paths have a shared owner
in `hermes_cli/runtime_paths.py`.

## Closure implementation

| Contract | Owner and proof |
| --- | --- |
| Interrupted publication | `hermes_cli/runtime_state.py` journals the old config and the proposed config hash. Startup recovers before dependency activation. Recovery refuses to overwrite unrelated edits. Real subprocess tests terminate before and after facts publication. |
| Live-generation collection | Startup holds a generation lease under the publication lock. `pm gc` removes only unselected, lease-managed generations without live readers. Tests keep a real reader alive while collection runs. |
| Receipt correlation | PM completions carry the invoking update ID. The updater embeds that completion, including failed steps and refusal reasons. Nested commands and copied contexts cannot finalize an enclosing receipt. |
| Warning surfaces | Doctor uses the update checker's local provenance rules. The desktop reads sync status and distinguishes a healthy no-op from an embedded failure. |
| Updater ownership | `electron/updater/checkout.ts` owns checkout checks and handoffs. `main.ts` supplies its dependencies. |
| Windows relaunch | A detached PowerShell waiter snapshots package identity before quit, waits for the original process to exit, then waits for the installed version to change. A failed registration produces a manual-reopen warning. |
| Test integrity | TCC behavior uses actual host markers. Installer source-grep assertions were removed. Runtime and installer behavior tests remain. |

## Verified execution

- The latest complete native-Windows Python run reported 44,557 passed, one
  failed, and 1,404 skipped tests across 3,746 files. Python source hashes stayed
  unchanged throughout the run. It also reported one pass-on-retry HTTP test.
  The install-ID race and HTTP test were then fixed. Their two files passed
  35 tests without retries. This is not a complete final-tree suite pass.
- Root `npm run check` completed with exit code zero, including MSIX packaging.
  Desktop UI: 7,231 passed. Electron: 2,311 passed and 23 skipped. TUI:
  1,719 passed and eight skipped. Dashboard: 291 passed. Root JS: 77 passed.
  Type checks and lint had no errors. Existing lint warnings remain. This
  packaging check used the primary build directory's existing payload, not
  the separately verified fresh audit payload.
- The actual core project resolved and built through the workspace helper.
  Imports resolved from the generated workspace and the source lock was unchanged.
- A fresh native ARM64 PM bundle completed its pinned-tool checks and all-extras
  environment build. Its manifest records source tree
  `60c9fb444c93e8a79ae22a01677c3291385343a6`.
- The rebuilt thin desktop started its real backend twice in an isolated home,
  answered HTTP 200, and exited cleanly. Home entries survived relaunch.
- A real isolated API-server messaging gateway retained its PID and birth time
  after ordinary desktop quit. The desktop backend stopped; the gateway did not.
- A thin MSIX built with the real packaging toolchain. Windows Sandbox installed
  version `0.17.0.0`, updated to `0.17.0.1`, and verified absence after uninstall.
  Test-certificate creation and trust were confined to the disposable guest.

- A separate fresh bundled MSIX was produced from payload tree `60c9fb44...`.
  The unpacked artifact's own CLI ran, imports resolved inside its payload,
  and `hermes serve` answered HTTP 200. The Sandbox deployment attempt failed
  on an incorrect unpacked path. It does not prove bundled installation.
- Plugin checks now run from the first housekeeping tick, with the configured
  interval gate controlling network checks. A real isolated gateway wrote two
  successful plugin-check receipts one tick apart. Auto-apply was disabled.

These receipts cover different layers. Thin-package deployment and unpacked
runtime startup do not prove bundled installation or App Installer-triggered
relaunch. No tests sent an LLM request as evidence of this acceptance pass.

## Merge-ready closeout

The branch includes upstream `5e645791aca87dfe46844502fab9e77a42a2681f`.
The following limits from the earlier audit are resolved:

- Context-only homes share one dependency root across journal recovery,
  selected runtime state, and plugin unions. The regression batch passed
  71 tests with eight host skips.
- `tests/tools/test_subagent_steer.py` produced the relative `MagicMock`
  databases. Its mock parents now explicitly have no database. The delegate
  verification batch passed without new debris. Earlier debris is archived
  outside the repository.
- Docker bootstrap includes the stdlib runtime-path and locking owners that
  PM imports before third-party dependencies exist. Both architecture builds
  passed after this fix.
- Installer path probes no longer read a missing lockfile. PowerShell 5.1 and
  7 installer tests passed in CI.
- Service runtime selection uses the shared selected-environment resolver.
  PM facts supply managed Node paths. Native fixtures use disposable homes.
- The Windows updater uses its registered App Installer source unless an
  explicit feed override is configured. It downloads the descriptor before
  teardown and opens the local file. It does not require the disabled
  `ms-appinstaller:` protocol.

The complete Linux run on `f54141c07aa0f99dcd99170ea68663ef0fc1bf38`
reported 45,927 passed, zero failed, and 492 skipped across 3,776 files.
It reported one retry-only encoding-test flake. Commit
`6d9eefdab12d3555d763d57c4f643bfb4cfd259c` scopes that test's subprocess
receipt to its actual consumer. Its canonical targeted run passed without
retries. The final-head complete rerun remains separate evidence.

The same CI run passed JS/TS, macOS tests, Python E2E, installers, docs,
lockfile checks, and static gates. Windows verification remains pending.

A fresh Windows ARM64 PM bundle records
`07ee9299790e5635fd917da1769f969f14fc8030`. Later changes through
`6d9eefdab12d3555d763d57c4f643bfb4cfd259c` affect tests and CI only.
The corresponding desktop MSIX is being packaged. Microsoft App Installer
was installed and verified inside the offline Windows Sandbox. That setup
alone does not prove Hermes installation or automatic update/relaunch.

## Remaining acceptance work

- Obtain complete final-head native CI results without retry-only failures.
- Verify bundled MSIX deployment from the fresh payload.
- Exercise an actual App Installer-triggered update and automatic relaunch.
- Obtain explicit maintainer review for CI-sensitive changes. Manual CI runs
  skip PR-only review gates and do not satisfy that approval.

CI: [f54141c07a](https://github.com/NousResearch/hermes-agent/actions/runs/34050547009),
[6d9eefdab1](https://github.com/NousResearch/hermes-agent/actions/runs/34051022947).
The latter uses the same workflow from the same commit on an upstream
validation branch. GitHub intermittently rejected graph creation with
`resource_exhausted: gitmon refuses to schedule us: fail-fast:network`.

The external audit directory contains original findings, exact test selections,
per-run logs, source snapshots, and review adjudication. Host application,
certificate trust, and production services remain unchanged. No release has
been published.
