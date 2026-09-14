# Native bundled retirement acceptance

`install-e2e.yml` route `channel-retirement` runs the existing native bundled
entrypoints with a retirement phase. One shared lifecycle uses installed-chat,
update UI and process observers; there is no publication receipt/certification graph.

## Inputs and dispatch

Supply one digest-pinned journey per **fresh disposable authority**:

- `schema: 1`, `repository`, exact `controllerCommit`, native `platform: darwin|win32`,
  `arch: arm64|x64`, `route: direct|via-update`, `stable: absent|running`, `home: shared`.
- `minimumVersion` is the stable payload source-version floor.
- `A`, `S`, `T`, and `B` only for `via-update`: each `{head, manifest}` with the original
  immutable manifest bytes referenced by `{buildId, sequence, manifestKey, sha256}`.
- A/B have one persistent preview identity and increasing native versions. S/T use
  the official stable identity, increasing native versions, and manifest
  `receiverProtocol: 1`. Test S/T requests carry the existing scoped
  `receiverCandidate: true` marker; this is not native success evidence.
- Every request, artifact and descriptor belongs to the same
  `ci-disposable/<repository-id>/<allocation-run-attempt>/` authority. No tags or
  releases are fabricated. Normal signed candidate build/smoke gates produce S/T.

The source must be active at A or B for `via-update`; the driver installs pinned A
and clicks Update only after its first chat. If B is already advertised, the gate
reads it without rewriting the head. `direct` uses old A while its channel can be
newer. Stable must be at S; T is archived but not promoted. The controller never
rewinds a head or resets a retired channel. On the direct route, A stays closed
while retirement pins S and stable advances to T. A must then install pinned S,
not substitute T; S takes T through its ordinary stable updater.

```sh
gh workflow run install-e2e.yml --repo OWNER/hermes-agent --ref main \
  -f route=channel-retirement -f retirement-platform=darwin -f retirement-arch=arm64 \
  -f retirement-manifest=https://TRUSTED_HOST/journey.json \
  -f retirement-sha256=EXACT_SHA256 -f disposable-run=ALLOCATION_RUN_ID-ATTEMPT
```

This is an example, not a command executed locally. The signing environment must
supply the existing R2 credentials/root and GitHub must provide repository ID.
Default-branch hosted dispatch only; the exact default-branch SHA is checked.
Native children receive no publisher credentials. The scoped coordinator calls
`ChannelPublisher.retire()` directly and reads back channel/descriptor writes.
Windows descriptors use the existing App Installer writer with a scoped canonical
self URI and pinned immutable bundle URL. Stable S follows that feed to T.

## Observations and limits

Both native drivers route `A → B → S → T` and offline `A → S → T`, with absent or
running S and a shared persistent natural home. They reject emulation, existing
user state, missing artifacts, invalid signatures/identities, mismatched native
versions, stale PIDs, failed chats, and incomplete cleanup.

Automatic activation is verified **before any driver-owned reopen**: exact
installed bytes/signature/stamp, process executable/PID/birth, installed backend
listener and selected home; S also requires the authenticated completed private
journal and durable home/profile choice. Existing running S must retain its PID
and birth. After this observation, the ordinary harness pattern closes/reopens
the verified app for shared Playwright chat. These are explicitly post-transition
chat checks, not claims of chat in the OS-activated absent-S/T renderer. No HOME or
userData override is passed on reopen, so it also checks cold-launch adoption.

State checks retain real SQLite messages/integrity, marker bytes and the existing
plugin/profile/external-link fixtures. Preview removal roots and owned launchers
must disappear without test cleanup making them pass. Windows checks canonical
App Installer registration and Start-menu removal. Diagnostics contain selected
non-secret facts only, never the private journal/token or whole user home.

**Native execution has not been performed on Linux.** Real signed A/B/S/T and
native Actions runs remain necessary. Custom/distinct/removal-scoped homes,
remote connection migration, receiver cancellation/conflicts, retained newer
stable, interruption and cleanup-retry are not claimed by this shared-home journey;
those need their respective native/runtime acceptance cases. A test S/T build
must actually have stable receiver provenance and normal stable update ownership;
an official-looking identity on a preview-stamped candidate is not accepted.

Local checks: `node --test tests/install/e2e-assets/channel-retirement-manifest.test.cjs`
exercises real loopback downloads and rejects corruption, plus portable input
contracts for both platforms. Syntax, workflow lint and helper import checks do
not count as native installation or migration success.
