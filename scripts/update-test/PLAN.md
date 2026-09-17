# Manual update-rehearsal plan

> **This directory lives outside the `hermes-agent` checkout** (it is a
> hand-off kit for real users, not part of the repo or its CI). Paths written
> as `tests/install/...` or `hermes_cli/...` are relative to a `hermes-agent`
> checkout; anything without a directory prefix is a file in this directory.

Goal: prove, on a real machine with a real existing install, that an existing
user can move to **this branch's code** through each of the update surfaces
without losing anything — and do it in a way that can later be lifted into the
`install-e2e` job.

The four proofs:

| # | Proof | Surface |
|---|---|---|
| 1 | `install.sh` / `install.ps1` install → `hermes update` | CLI updater |
| 2 | desktop installer install → the app's own update mechanism | desktop app |
| 3 | an install **with a plugin installed** (mnemosyne) keeps the plugin | plugin survival |
| 4 | an install with the **photon sidecar** keeps the sidecar | sidecar survival |

Desktop app **bundles** (`Hermes-Setup.dmg`/`.exe` → packaged `.app`/MSIX and
electron-updater feeds) are explicitly out of scope. This plan is about source
installs and the source-install desktop app.

---

## 1. The load-bearing finding: the redirect must be transport-level

"Make the install see my fork's main as canonical **without** seeing it as a
fork" is not cosmetic. It is required for the update to work at all:

* `hermes update` on a source install resolves its update channel from R2
  (`hermes-assets.nousresearch.com/releases/channels/<channel>.json`) through
  `hermes_cli/source_releases.py::resolve_source_target`.
* That read validates the channel record's `repository` against
  `source_repository()`, which is derived from
  **`git config --get remote.origin.url`**.
* A channel record is bound to `NousResearch/hermes-agent`. If `origin` is
  literally a fork URL, resolution dies with *"Channel repository does not
  match this source installation"* / *"Could not resolve the main source
  channel"* and **no update is applied**.

So the fork code has to arrive over a channel whose *configured* origin URL is
still the official one. Two independent git readers are involved and they do
**not** agree about URL rewriting:

| Reader | Used for | `url.<x>.insteadOf` rewriting |
|---|---|---|
| `git config --get remote.origin.url` | `source_repository()` → channel resolution | **not** rewritten (raw value) |
| `git remote get-url origin` | `_get_origin_url()` → `_is_fork()` | **is** rewritten |

(git's `insteadOf` is a transport rewrite: `remote get-url` applies it,
`config --get` does not.)

Therefore:

* A **`url.<rehearsal-source>.insteadOf <official>`** entry in the tester's
  **global** git config sends all fetches to the rehearsal source while channel
  resolution still sees the official repository. The updater's own git calls
  run with the ambient environment (`update_cmd._git_run` → `_no_prompt_git_kwargs`
  copies `os.environ`), so global config is honoured. Hermes's *read-only*
  internal probes (`source_check.source_git_env`) set `GIT_CONFIG_GLOBAL` to
  `/dev/null`, so the redirect cannot corrupt them — and `config --get` is
  unaffected anyway.
* The one side effect is `_is_fork()` returning true (because `remote get-url`
  is rewritten), which prints an *"Updating from fork"* banner and offers the
  upstream-sync prompt. The scripts neutralise that with
  `$HERMES_HOME/.skip_upstream_prompt` (which `_should_skip_upstream_prompt()`
  honours), and offer an opt-in **git shim** (`--with-git-shim`) that makes
  `remote get-url origin` report the official URL — the exact trick the CI
  drivers use (`tests/install/e2e-assets/installer-common.sh::arm_source_redirect`,
  `tests/install/windows-e2e.ps1::Set-GitRedirect`).

This is why the scripts do not simply repoint `origin`.

### Where the code comes from

Two modes:

* **`--mode local` (default, recommended).** A bare clone of the install's own
  checkout, `serve.git`, with `main` parked at the target commit; the redirect
  points at `file://…/serve.git`. No fork mutation, no network, reproducible,
  and byte-for-byte the mechanism the e2e drivers already use. Advance
  `serve.git`'s `main` between runs to re-test "an update is available".
* **`--mode fork`.** The redirect points at the fork over the network and the
  fork's `main` must already be the code under test. Literal reading of "my
  fork's main", at the cost of requiring the push and the network.

---

## 2. The scripts

`hermes-update-rehearsal.sh` (macOS/POSIX) and `hermes-update-rehearsal.ps1`
(Windows) are mirrors of each other and share one subcommand set:

| Subcommand | Does |
|---|---|
| `pre` | Snapshot everything below into a timestamped dir outside `$HERMES_HOME`, then fetch the custom repo + ref and point the install's update source at it. Creates nothing else, changes nothing else, and judges nothing. |
| `post` | Remove the redirect, wipe, and put the backup back. Reports how exact the restore was (informational, never a failure). Destructive; needs `--yes`. |
| `status` | Print what is prepared and which backup root is in play. Read-only. |

There is no `verify` verb on purpose. This tool is for handing to a real user:
`pre` reports what it did and prints the next commands, and the user decides
whether the update worked. A script that graded their install would be wrong
about as often as it was right, and would invite arguing with it instead of
looking at Hermes. The assertions live in CI instead — see §8.

`pre` is deliberately the only place network is needed: it clones the rehearsal
source into `serve.git` inside the backup dir, so the update itself is served
from local disk.

### What `pre` captures

Everything lands in `~/hermes-update-rehearsal/<UTC timestamp>/` (override with
`--backup-root`). Keep it off the same volume if you can — the copy is the size
of the install, and `pre` reports how long it took.

1. **The entire `$HERMES_HOME`** → `hermes-home.tgz`. No excludes: the
   `hermes-agent/` checkout, the venv/PM store, `node_modules`, `.git` and all
   runtime state come with it, so a restore is a true rollback rather than a
   re-download. SQLite sidecars (`-wal`/`-shm`/`-journal`) travel **with** their
   `db` on purpose: a raw copy of db+wal+shm is a consistent SQLite state, and
   it is only pairing a *checkpointed* copy with a stale wal that tears
   (`hermes_cli/backup.py` excludes them because it snapshots via
   `sqlite3.backup()`; a plain copy does not). Expect the archive to be as large
   as the install and the copy to take minutes.
2. **The checkout's git facts** → `checkout.txt` (HEAD, branch, stash count),
   `remotes.txt` (`name<TAB>url`, from `config --get`, not `remote -v`), and the
   checkout's local `.git/config`. These are *assertion* inputs for `verify`,
   not a restore mechanism — the tar already carries `.git`.
3. **Electron userData** → `electron-userdata.tgz`.
   `HERMES_DESKTOP_USER_DATA_DIR` when set, else the platform default
   (`~/Library/Application Support/Hermes`, `%APPDATA%\Hermes`) plus
   `HERMES_DATA_DIR_SUFFIX`.
4. **The `hermes` shims on PATH** → `shims/` plus `shims.txt` and
   `shims-links.txt` (symlink targets preserved). POSIX: `$HOME/.local/bin`,
   `<root>/bin`, `/usr/local/bin`, and the checkout's `.hermes/bin`. Windows:
   the checkout's `.hermes\bin`, `<home>\bin` (where `install.ps1` publishes and
   wires the USER PATH), and `~\.local\bin`.
5. **The USER PATH value** (Windows) → `user-path-before.txt`, so a restore puts
   back the `PATH` entry that exposes `<home>\bin`. A blank record is left alone
   rather than written — an empty value would wipe the user's PATH.
6. **Global git config** → `gitconfig.bak`, plus the redirect entries `arm`
   adds, recorded for exact removal.
7. **Two fingerprints** the update must not move: `fingerprint-before.txt`
   (curated durable state: `config.yaml`, `.env`, `auth.json`, `state.db`,
   `memories/`, `skills/`, `cron/`, `plugins/`, `profiles/`, `photon/`,
   `desktop-plugins/`, `tui-widgets/`, `skins/`, `pets/`, `sessions/`) and
   `userdata-before.txt`. `state.db` is fingerprinted by **row counts**
   (`sessions`/`messages`/`usage`/`cron_jobs`), never by bytes — a byte hash of
   a live SQLite database changes for benign reasons and would make `verify`
   meaningless.
8. **Nothing else.** The tool does not seed fixtures, install plugins, or
   script any user action: everything it protects is whatever the user's own
   install already has.

The fingerprints exist for one purpose: after `post` restores, it re-computes
them and tells the user how exact the restore was. They are a *restore-fidelity*
report, never a verdict on the update.

---

## 3. Preconditions

* A machine you can afford to lose. `tests/install/README.md` says the CI
  drivers are destructive, and so is this: it will run a real update against a
  real install, then wipe and restore. A VM or a second user account is better
  than your daily driver.
* A **real** install that a user could plausibly have:
  * installed by `install.sh` / `install.ps1` (`~/.hermes/hermes-agent` or
    `%LOCALAPPDATA%\hermes\hermes-agent`), on a released tag, not a dev
    checkout;
  * the desktop app present (built by `--include-desktop` / `-IncludeDesktop`,
    or the packaged app) for scenario 2;
  * a plugin installed for scenario 3 (see below);
  * the photon sidecar installed for scenario 4.
* Quiesce first: no gateway, no desktop app, no `hermes` session running. On
  Windows, `hermes update` refuses to mutate a venv that other processes hold.
* Network access to `hermes-assets.nousresearch.com` for channel resolution.
  `preflight` checks it and warns (it cannot be faked without a hosts entry).

### Fixtures for scenarios 3 and 4

* **Plugin (mnemosyne).** Install it the way a user does — `memory.provider`
  from the mnemosyne distribution's documented path — or, for a cheaper
  rehearsal, seed the shape CI asserts and the plugin scanner recurses into:
  `$HERMES_HOME/plugins/mnemosyne-wrapper/{plugin.yaml,mnemosyne-wrapper.json}`
  plus a symlinked runtime and an externally-owned sidecar witness file
  outside the home. `tests/install/e2e-assets/verify-plugin-preservation.py seed
  --home <home> --external <path>` creates exactly that fixture.
* **Photon sidecar.** `hermes photon install-sidecar` (or just let it install
  on first use), then confirm `$HERMES_HOME/photon/sidecar/node_modules` and
  `node_modules/.package-lock.json` exist. If you use `PHOTON_SIDECAR_DIR`, also
  record that path — it is the override branch of `resolve_sidecar_dir`.

---

## 4. Scenarios

Each scenario is: `pre` → *update normally, the way a user would, and use
Hermes* → `post`.

### Scenario 1 — source install, `hermes update`

```bash
./hermes-update-rehearsal.sh pre --ref <branch>
# user path, from a plain shell:
cd "$HERMES_HOME/hermes-agent" && hermes update
# use Hermes, poke at whatever you need, then:
./hermes-update-rehearsal.sh post
```

`pre` prints the exact two commands to run — copy them rather than retyping.

Expected `hermes update` output: `→ Update channel: main`, `→ Fetching
updates...`, `→ Found N new commit(s)`, `✓ Code updated!`, dependency sync,
then the completion line with the old→new version transition. It may also print
`⚠ Updating from fork`, because the redirect is a transport-level rewrite that
`git remote get-url origin` sees but `git config --get` does not. That banner is
cosmetic: channel resolution (which reads the configured URL) still resolves
normally, which is the part that matters and the part with no workaround.

### Scenario 2 — the desktop app's update mechanism

Same, but let the **app** drive the update instead of the shell:

1. Launch the installed desktop app as a user does (the packaged/installed
   entry point, or `hermes desktop`).
2. Settings → About → **Update now**.
3. Wait for the app to report completion and relaunch.
4. Use Hermes, then `post`.

A GUI-launched app inherits the **global git config** — which is exactly why the
redirect lives there and not in a shell-only `PATH` shim. The
`.skip_upstream_prompt` marker is a file on disk, so it is inherited either way.
`post` reports separately whether the desktop app's userData came back
byte-for-byte, so you can see whether `connection.json` and window state
survived.

### Scenario 3 — plugin survival

Run scenario 1 or 2 on an install that has a plugin. There is no fixture to
install: if the plugin is on the install, whatever it wrote under
`$HERMES_HOME/plugins/**` or `profiles/<name>/plugins/**` is in the backup, and
`post`'s fingerprint report will show if anything under those trees did not come
back. (The strict delete/modify contract for plugin trees runs in CI, where the
baseline is reproducible — see §8.)

### Scenario 4 — photon sidecar survival

Same shape. Everything under `$HERMES_HOME/photon/` is inside the whole-home
backup, including the sidecar's `node_modules` and its
`node_modules/.package-lock.json` marker, so `post` restores it and reports
whether it matched. If `PHOTON_SIDECAR_DIR` points outside the home, that tree
is outside the backup too — back it up yourself before you start.

---

## 5. The preservation contract (enforced in CI)

The scripts verify nothing. `pre` captures and points; `post` restores and
reports how exact the restore was. The contract they exist to demonstrate is
asserted in CI, on every upgrade leg, by
`e2e-assets/verify-user-state.py` (user state) and
`e2e-assets/verify-plugin-preservation.py` (plugin trees):

1. `state.db` does not lose rows. Bytes are deliberately **not** compared — a
   live SQLite file changes for benign reasons, so a byte hash would either
   always fail or prove nothing.
2. No deletion or byte change under `.env`, `auth.json`, `gateway_state.json`,
   `memories/`, `cron/`, `sessions/`, `profiles/`, `photon/`,
   `desktop-plugins/`, `tui-widgets/`, `skins/`, `pets/`, or
   `skills/.archive/`. `config.yaml` may be rewritten (additive migration) and
   the bundled `skills/` tree may be re-synced by the product — both are
   reported, neither fails.
3. Plugin trees survive under the separate plugin contract (anything deleted or
   modified fails; added files are tolerated).
4. The redirect stayed transport-level: `git config --get remote.origin.url` is
   still the official URL while `git remote get-url origin` is the redirected
   one. If the configured URL ever looked like the rehearsal source, channel
   resolution would fail and the leg would not be testing the real user path.
5. The user-visible launcher still exists and still runs after the upgrade — and
   on Windows, `<home>\bin` is still on the USER PATH.

Two things the CI checker does **not** cover, so they stay manual here: the
desktop app's Electron userData (only meaningful where the app is installed and
`desktop-smoke.ts` treats that dir as its own sandbox), and
`PHOTON_SIDECAR_DIR` when it points outside `$HERMES_HOME` (outside the backup,
by definition).

---

## 6. Undo: `post`

`post` is one verb because the two halves must not be separable: removing the
redirect and putting the state back have to happen together or the install is
left in a state neither the user nor the script can reason about. Because the
backup is the whole home, the restore is a true rollback — the checkout, venv,
PM store and `node_modules` come back with it, not just the config.

`post` does:

1. Remove the redirect entries from the global git config and restore that file
   from `gitconfig.bak`.
2. Delete `$HERMES_HOME/.skip_upstream_prompt`.
3. Put the Windows USER PATH value back from `user-path-before.txt` (left
   untouched when the record is blank — an empty record must never wipe PATH),
   and restore the `hermes` launcher shims.
4. Stop the desktop app and the gateway.
5. Delete `$HERMES_HOME`, the Electron userData dir, and the recorded shim files.
6. Extract `hermes-home.tgz` into `$HERMES_HOME` and `electron-userdata.tgz`
   into the userData dir.
7. Restore the shims (files from `shims/`, symlinks from `shims-links.txt`).
8. Re-fingerprint and compare against the pre-`pre` fingerprints, then report
   how many entries matched. A non-empty diff is reported plainly and does not
   fail — it is information for the tester, not a verdict.

Then: open the desktop app once, run `hermes doctor`, confirm the gateway
starts, and keep the snapshot directory until that passes.

---

## 7. Testing the scripts themselves

Both scripts have a smoke test that builds a synthetic install in a temp tree
and drives every subcommand for real: `pre` (the whole-home backup, the
fingerprint, the fetch into `serve.git`, `main` parked at the right commit, the
two `insteadOf` entries, the marker, and that the redirect is visible to
`git remote get-url` while `git config --get` still reports the official URL),
then `post` (redirect gone, marker gone, and every artefact back). The bash one
also asserts `pre` left the checkout byte-identical, which is the mechanical
half of "`pre` asserts nothing and changes nothing".

```bash
bash smoke-test.sh                       # macOS/Linux
powershell -ExecutionPolicy Bypass -File smoke-test.ps1
```

The bash one also runs under git-bash on Windows (it normalises paths with
`cygpath` and, where GNU tar is in play, needs `TAR_OPTIONS=--force-local` — the
smoke test sets that itself). Two assertions self-skip on a host that cannot
create symlinks (Windows without developer mode).

Known tooling traps these hit, worth knowing before you edit either script:

* **`url.<base>.insteadOf` is multi-valued.** Writing it with plain
  `git config` replaces the existing value, so the second official URL silently
  drops the first and the redirect covers only one form. Use `--add`, and
  `--unset-all` to remove.
* **`file://C:/x` parses as host `C:`.** A drive-letter path needs
  `file:///C:/x`.
* **`git remote get-url` applies the rewrite; `git config --get` does not.** Use
  `config --get` for anything asserting "the remote did not change".
* **GNU tar reads `C:/x` as `host:path`.** Needs `--force-local` (macOS bsdtar
  does not, and does not need it).
* **`cmd | grep -q` under `pipefail` is a SIGPIPE race** — capture, then grep.
* **Windows PowerShell 5.1 reads a BOM-less `.ps1` as ANSI**, so a UTF-8 em-dash
  breaks string parsing. `hermes-update-rehearsal.ps1` is ASCII-only for that
  reason.
* **A PATH that points into a `WindowsApps` payload cannot be launched by
  Windows PowerShell** (`Access is denied`). The bundled Hermes app puts its
  whole toolchain there, so on such a host both the Store `python` alias and the
  payload `git` are unusable from PowerShell. The Windows script skips those
  candidates and prefers the install's own venv/PM-store interpreter; a host like
  that needs a normal `Git\cmd` and a real venv python ahead of the payload on
  PATH before either script will work.
* **`state.db` must be compared by row counts, not bytes.** A live SQLite file
  changes for benign reasons.

---

## 8. What was added to the CI drivers

The assertion layer moved; the harness did not. `wipe`/`restore` and the backup
tar have no home in CI (the runner is disposable), and `pre`/`post` stay manual.
What landed:

* `e2e-assets/verify-user-state.py` — the shared, read-only, stdlib-only
  snapshot/verify checker for the user's own durable state. It has **no** `seed`
  mode: the state it defends is produced by the product, never written by the
  harness. `state.db` is compared by row counts. The bundled `skills/` tree is
  recorded but never judged (the product re-syncs it); `skills/.archive/` is
  judged. `plugins/**` is deliberately **not** scanned — that contract belongs
  to `verify-plugin-preservation.py`, and double-owning a tree makes two
  verifiers disagree later.
* `e2e-assets/user-state-actions.sh` — produces that state through the ordinary
  CLI (`hermes chat -q`, `hermes auth add`, `hermes profile create`), probing
  `--help` for each flag per the harness's "probe, do not assume" rule, and
  asserting each action actually landed so a leg cannot pass while testing
  nothing.
* `e2e-assets/preserve-user-state.sh` — the POSIX/macOS hook pair, called at the
  same two points as `preserve-plugins.sh` (`before_upgrade` after the install
  phase's checks, `after_upgrade` right after the post-update checkout
  assertion and *before* the new-side desktop checkpoint, so the window
  contains only the upgrade).
* `tests/install/windows-e2e.ps1` — `Invoke-UserStateActions` in the install
  phase (reusing `Start-DesktopJourneyMock` so the leg has a genuinely
  configured provider), `Invoke-UserStateSnapshot` in the update phase
  immediately before `serve.git`'s `main` advances, and
  `Invoke-UserStateVerify` + `Assert-UserShims` after `Test-HermesRuns`.
* Two invariants in both drivers: `Assert-RedirectIsTransportOnly` (configured
  origin stays official while the transport URL is redirected) and
  `Assert-UserShims` (the launcher still runs; Windows: `<home>\bin` is still
  on the USER PATH).
* `tests/scripts/test_verify_user_state.py` — 16 tests over a real temp
  filesystem with real sqlite databases. These are the tests that keep the
  allowlist honest: `config.yaml` rewrites pass, a state.db size change is not
  loss, row loss is, a bundled-skill rewrite is advisory-only, and `plugins/**`
  is not judged here at all.

Deliberately **not** done: these scripts live outside the repo, so they cannot be
wired into `installer-tests.yml` from here. If that is ever wanted, they have to
move back under `tests/install/manual/` — which also means adding that path to
`_INSTALLER_PATHS` in `scripts/ci/classify_changes.py` (the lane is
installer-gated, so it would change which PRs spin up a Windows runner), plus
pwsh 5.1 / 7 and one `shell: bash` step in
`.github/workflows/installer-tests.yml`. Keeping them out is why they can stay a
plain hand-off kit instead of a maintained CI lane.

Also not covered: a real plugin install (`hermes plugins install …`, network) and
the photon sidecar (`npm ci`) — both need the network and a real package, and
the risk surface *is* the `node_modules` mirror, which a fixture would not
exercise honestly. They belong on a low-frequency leg when someone wants them.

---

## 9. Risks and gotchas

* **`hermes update` legitimately writes to `$HERMES_HOME`.** Config migration
  rewrites `config.yaml` (additively), pre-update backups land in `backups/`
  and `state-snapshots/`, `state.db` may be re-created and restored, and
  migrations can touch sibling profile configs. The CI checker treats those as
  allowed; its snapshot diff is what keeps "allowed" honest.
* **Desktop app rebuild.** The update rebuilds `apps/desktop`. That is slow and
  needs Node; a failed build is a real update failure, not a rehearsal artifact.
* **Windows venv holders.** Any process running from the install's venv blocks
  the dependency sync (`--force-venv` exists and is a trap). Close the app and
  the gateway first.
* **The `.skip_upstream_prompt` marker is our addition, not a user's.** Record
  it as prepared state so `post` removes it; leaving it behind would suppress a
  prompt a real fork user should see.
* **There is deliberately no `git` shim.** An earlier draft put a wrapper `git`
  on PATH that returned the rewritten remote for one subcommand; it was
  dropped. A second `git` on PATH is exactly the kind of thing that confuses
  the *next* thing you debug, and the redirect already makes the update pull
  the custom source. The `⚠ Updating from fork` banner is the price, and it is
  cosmetic — the code path that matters reads the configured URL.
* **R2 channel resolution is network-bound and out of our control.** If
  `main.json` is unreachable or its `repository`/delivery shape changes,
  `hermes update` fails before any git work. `pre` cannot pre-empt that — it
  never talks to the channel — so if the update dies that early, that is the
  first thing to check.
* **Nothing is served from the real fork, so nothing needs unwinding there.**
  `pre` clones `--source`/`--ref` into a bare `serve.git` inside the backup dir
  and redirects to that, so the update never reads or writes the fork itself.
  The only repair the fork could ever need is if someone pushed to it by hand.
