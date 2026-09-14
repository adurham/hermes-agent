#!/usr/bin/env node
// Retirement phase of the bundled native drivers. Observations are not release admission.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync, spawnSync } from 'node:child_process'
import { parseArgs } from 'node:util'
import { extractFile } from '@electron/asar'
import { validateJourney } from './e2e-assets/channel-retirement-manifest.cjs'
import * as mac from './e2e-assets/channel-retirement-macos.mjs'
import { samePath, mainProcesses, assertInstalled } from './e2e-assets/channel-retirement-windows-observations.mjs'
import { launch, chat, transition, close } from './e2e-assets/channel-retirement-window.mjs'
import { smokeEnvironment } from './e2e-assets/desktop-smoke.ts'
import { readNativeProcesses, descendants, localBackendProcess, assertBackendOrigin } from '../../tests-js/scripts/desktop-smoke-process.ts'
import { writeMockProviderConfig, writeEnvFile } from '../../tests-js/scripts/mock-provider-config.ts'
import { startMockServer } from '../../tests-js/scripts/mock-server.ts'

/** @typedef {import('./e2e-assets/channel-retirement-manifest.cjs').Side} Side */
/** @typedef {{pid: number, executable: string, birth: string}} Observed */
/** @typedef {import('./e2e-assets/channel-retirement-window.mjs').Installed & {
 * appPath: string, executable: string, packageFamilyName?: string, packageFullName?: string}} Installed */

const { values } = parseArgs({ options: { work: { type: 'string' }, arch: { type: 'string' } } })
assert.ok(['darwin', 'win32'].includes(process.platform), 'Native Windows or macOS required')
assert.equal(process.env.GITHUB_ACTIONS, 'true')
assert.equal(process.env.RUNNER_ENVIRONMENT, 'github-hosted')
assert.equal(process.arch, values.arch, 'Emulation is not native acceptance')
const work = fs.realpathSync(values.work)
assert.ok(work.startsWith(fs.realpathSync(process.env.RUNNER_TEMP) + path.sep))
const assets = path.join(import.meta.dirname, 'e2e-assets')
const windows = process.platform === 'win32'
const journey = validateJourney(JSON.parse(fs.readFileSync(path.join(work, 'inputs/journey-inputs.json'))), process.platform, process.arch)
assert.equal(journey.repository, process.env.GITHUB_REPOSITORY)
assert.equal(journey.controllerCommit, process.env.GITHUB_SHA)
const base = journey.A.manifest.request.publicBase
assert.match(base, /\/ci-disposable\/[1-9][0-9]*\/[1-9][0-9]*-[1-9][0-9]*$/)
const out = path.join(work, 'observations'); fs.mkdirSync(out)
const observations = {}
/** @param {string} operation @param {object} input */
function win(operation, input) {
  return JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(assets, 'channel-retirement-windows-native.ps1'), '-Operation', operation], {
    input: JSON.stringify(input), encoding: 'utf8', windowsHide: true, timeout: 600_000,
    stdio: ['pipe', 'pipe', 'pipe'], maxBuffer: 8 * 1024 * 1024,
  }).replace(/^\uFEFF/, ''))
}
const account = windows ? win('preflight', { arch: process.arch, identities: [journey.A.package.identity, journey.S.package.identity] })
  : { home: fs.realpathSync(os.userInfo().homedir), appData: path.join(os.userInfo().homedir, 'Library/Application Support') }
assert.equal(fs.realpathSync(os.homedir()), account.home, 'OS activation requires the natural account home')
const home = windows ? path.join(account.localAppData, 'hermes') : path.join(account.home, '.hermes')
const journal = path.join(account.home, '.hermes-desktop-retirement')
for (const file of [home, journal]) assert.equal(fs.existsSync(file), false, 'Existing user state refused: ' + file)
const apps = path.join(work, 'apps'); fs.mkdirSync(apps)
/** @param {Side} side */
const appPath = side => path.join(apps, side.manifest.request.identity.displayName + '.app')
const stableFeed = `${base}/releases/win32/stable/stable.appinstaller`
const python = windows ? 'python' : 'python3'
/** @param {string[]} args */
const run = args => execFileSync(python, args, { encoding: 'utf8', windowsHide: true, timeout: 60_000, stdio: ['ignore', 'pipe', 'pipe'] })
const plugin = path.join(assets, 'verify-plugin-preservation.py')
const marker = `retirement-${process.env.GITHUB_RUN_ID}-${process.env.GITHUB_RUN_ATTEMPT}`
const ownedLaunchers = new Set()

/** @param {Side} side @param {boolean} [install] @returns {Installed} */
function installed(side, install = false) {
  let result
  if (windows) {
    result = win(install ? 'install' : 'installed', { side, arch: process.arch,
      feedUri: side.manifest.request.channel === 'stable' ? stableFeed : `${base}/${side.package.feed.key}` })
    assertInstalled(result, side, process.arch)
  } else {
    const target = appPath(side)
    if (install) {
      const dir = path.join(work, 'extract-' + side.head.buildId); fs.mkdirSync(dir)
      for (const name of mac.native('/usr/bin/unzip', ['-Z1', side.artifactPath]).split('\n')) {
        assert.ok(!name.startsWith('/') && !name.includes('\\') && !name.split('/').includes('..'), 'Unsafe ZIP path')
      }
      mac.native('/usr/bin/ditto', ['-x', '-k', side.artifactPath, dir])
      const entries = fs.readdirSync(dir).filter(name => name.endsWith('.app'))
      assert.equal(entries.length, 1)
      assert.equal(fs.existsSync(target), false)
      fs.renameSync(path.join(dir, entries[0]), target)
    }
    const verified = mac.verifyApp(target, side, process.arch)
    result = { ...verified, appPath: target, resources: path.join(target, 'Contents/Resources') }
  }
  const pkg = JSON.parse(extractFile(path.join(result.resources, 'app.asar'), 'package.json'))
  const name = side.manifest.request.releaseTag ? pkg.productName || pkg.name : side.manifest.request.identity.appNamePascal
  return { ...result, exe: result.executable, userData: path.join(account.appData, name), commit: side.manifest.request.commit }
}
/** @param {string} exe */
function processes(exe) { return windows ? mainProcesses(readNativeProcesses(), exe) : mac.processes(exe) }
/** @param {Installed} app @param {Observed} [prior] @returns {Promise<Observed>} */
async function observe(app, prior) {
  const observed = await mac.until(() => {
    const rows = processes(app.exe).filter(row => row.pid !== prior?.pid)
    assert.ok(rows.length <= 1, 'Ambiguous installed main process')
    if (!rows.length) return null
    return windows ? win('process', rows[0]) : mac.processIdentity(rows[0])
  }, 'automatic native activation')
  if (prior) assert.notEqual(observed.birth, prior.birth, 'Process birth did not change')
  return observed
}
/** @param {Installed} app @param {Observed} observed */
async function healthy(app, observed) {
  const root = path.join(app.resources, 'agent-payload')
  const stamp = JSON.parse(fs.readFileSync(path.join(app.resources, 'install-stamp.json')))
  const site = fs.realpathSync(path.join(root, stamp.runtime.sitePackages))
  assert.ok(site.startsWith(fs.realpathSync(root) + path.sep), 'Observer dependencies escaped installed payload')
  return mac.until(async () => {
    const children = descendants(readNativeProcesses(), observed.pid).filter(row => row.executable.toLowerCase().startsWith(root.toLowerCase() + path.sep))
    let ports = []
    if (windows) ports = win('listeners', {}).filter(row => row.LocalAddress === '127.0.0.1' && children.some(child => child.pid === row.OwningProcess)).map(row => row.LocalPort)
    else for (const child of children) {
      const result = spawnSync('/usr/sbin/lsof', ['-nP', '-a', '-p', String(child.pid), '-iTCP', '-sTCP:LISTEN', '-Fn'], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] })
      if (result.error || ![0, 1].includes(result.status)) throw new Error('Native listener observation failed')
      ports.push(...[...result.stdout.matchAll(/^n127\.0\.0\.1:(\d+)$/gm)].map(match => Number(match[1])))
    }
    for (const port of ports) {
      const backend = localBackendProcess(port, observed.pid)
      assertBackendOrigin(backend, root, 'bundled')
      const selected = windows ? JSON.parse(execFileSync(backend.executable, ['-I', '-B', '-c',
        'import json,sys; sys.path.insert(0,sys.argv[2]); import psutil; print(json.dumps(psutil.Process(int(sys.argv[1])).environ().get("HERMES_HOME")))', String(backend.pid), site],
      { encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }))
        : /(?:^|\s)HERMES_HOME=(.*?)(?=\s+[A-Za-z_][A-Za-z_0-9]*=|$)/.exec(mac.native('/bin/ps', ['eww', '-p', String(backend.pid), '-o', 'args=']))?.[1]
      assert.equal(fs.realpathSync(selected), fs.realpathSync(home), 'Automatic backend selected a different home')
      const response = await fetch(`http://127.0.0.1:${port}/api/health`, { signal: AbortSignal.timeout(10_000) })
      assert.equal(response.status, 200)
      return { pid: backend.pid, executable: backend.executable, home: selected }
    }
    return null
  }, 'automatically activated bundled backend', 180_000)
}
/** @param {string} phase */
async function gate(phase) {
  mac.write(path.join(work, phase + '.request.json'), { phase, controllerCommit: journey.controllerCommit })
  const record = await mac.until(() => {
    const file = path.join(work, phase + '.ready.json')
    return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : null
  }, 'controller ' + phase)
  const response = await fetch(`${base}/releases/channels/${record.name}.json`, { cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(30_000) })
  assert.equal(response.status, 200); assert.deepEqual(await response.json(), record)
  if (phase === 'retire') { assert.deepEqual(record.destinationHead, journey.S.head); assert.equal(record.receiverProtocol, 1) }
  observations[phase] = record
}
const environment = smokeEnvironment(process.env, home, path.join(work, 'unused'))
for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HERMES_HOME', 'HERMES_DESKTOP_USER_DATA_DIR',
  'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'GITHUB_SHA', 'GITHUB_REF', 'GITHUB_REF_NAME']) delete environment[key]
environment.HOME = account.home
for (const key of ['USERPROFILE', 'APPDATA', 'LOCALAPPDATA']) if (process.env[key]) environment[key] = process.env[key]
const prompts = []
function preservation() {
  run([plugin, 'verify', '--home', home, '--snapshot', path.join(work, 'plugins.json'), '--report', path.join(out, 'plugins-' + prompts.length + '.json')])
  assert.equal(fs.readFileSync(path.join(home, 'retirement-marker.txt'), 'utf8'), marker)
  run(['-c', 'import sqlite3,sys,json,pathlib; c=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+"?mode=ro",uri=True); assert c.execute("pragma integrity_check").fetchone()[0]=="ok"; assert all(c.execute("select count(*) from messages where role=\'user\' and content like ?",("%"+p+"%",)).fetchone()[0]>0 for p in json.loads(sys.argv[2]))', path.join(home, 'state.db'), JSON.stringify(prompts)])
}
/** @param {Installed} app */
async function start(app) { assert.equal(processes(app.exe).length, 0); return launch(app, environment) }
/** @param {Installed} app @param {Observed} observed */
async function reopen(app, observed) {
  if (windows) win('quit', observed); else await mac.quit(observed)
  await mac.until(() => !processes(app.exe).length, 'verified app normal quit', 60_000)
  return start(app)
}
const mock = await startMockServer()
try {
  for (const side of [journey.A, ...(journey.B ? [journey.B] : []), journey.S, journey.T]) {
    assert.equal(fs.statSync(side.artifactPath).size, side.package.artifact.size)
    const hash = createHash('sha256')
    for await (const chunk of fs.createReadStream(side.artifactPath)) hash.update(chunk)
    assert.equal(hash.digest('hex'), side.package.artifact.sha256)
    if (windows) win('artifact', { side, arch: process.arch })
  }
  fs.mkdirSync(home, { mode: 0o700 })
  writeMockProviderConfig(home, mock.url, undefined, `updates:\n  desktop_feed_base_url: ${base}\n`)
  writeEnvFile(home); fs.writeFileSync(path.join(home, 'retirement-marker.txt'), marker, { flag: 'wx' })
  run([plugin, 'seed', '--home', home, '--external', path.join(work, 'external-plugin')])
  run([plugin, 'snapshot', '--home', home, '--out', path.join(work, 'plugins.json')])
  let sourceApp = installed(journey.A, true)
  assert.equal(fs.existsSync(sourceApp.userData), false, 'Existing desktop state refused')
  let existing
  if (journey.stable === 'running') {
    const stable = installed(journey.S, true)
    assert.equal(fs.existsSync(stable.userData), false)
    existing = await start(stable)
    await chat(existing, mock.url, path.join(out, 'preexisting'))
  }
  let source = await start(sourceApp)
  prompts.push(await chat(source, mock.url, path.join(out, 'A')))
  if (journey.B) {
    const before = await observe(sourceApp)
    await gate('preview-update'); await transition(source, 'update', out)
    if (windows) await mac.until(() => win('available', { side: journey.B, arch: process.arch }), 'B installed')
    const observed = await observe(windows ? installed(journey.B) : sourceApp, before)
    sourceApp = installed(journey.B)
    observations.B = { process: observed, backend: await healthy(sourceApp, observed) }
    mac.write(path.join(out, 'automatic-B.json'), observations.B)
    source = await reopen(sourceApp, observed)
    prompts.push(await chat(source, mock.url, path.join(out, 'B')))
  }
  preservation()
  if (!windows) {
    const dir = path.join(account.home, '.local/bin')
    if (fs.existsSync(dir)) for (const name of fs.readdirSync(dir)) {
      const file = path.join(dir, name)
      if (fs.lstatSync(file).isSymbolicLink() && path.resolve(dir, fs.readlinkSync(file)).startsWith(sourceApp.appPath + path.sep)) ownedLaunchers.add(file)
    }
  }
  if (journey.route === 'direct') await close(source)
  await gate('retire')
  if (journey.route === 'direct') {
    await gate('stable-update')
    source = await start(sourceApp)
    prompts.push(await chat(source, mock.url, path.join(out, 'offline-A')))
  }
  const before = await observe(sourceApp)
  const existingProcess = existing ? await observe(existing.installed) : null
  await transition(source, 'retire', out)
  const stable = await mac.until(() => {
    if (windows ? !win('available', { side: journey.S, arch: process.arch }) : !fs.existsSync(appPath(journey.S))) return null
    return installed(journey.S)
  }, 'stable installed')
  const ready = await mac.until(() => mac.completedJournal(journal, sourceApp.appPath, stable.appPath,
    journey.S.head.sha256, sourceApp.packageFullName), 'authenticated retirement completion')
  const observed = await observe(stable)
  assert.equal(observed.pid, ready.pid); assert.equal(ready.home, home)
  assert.ok(windows ? samePath(observed.executable, ready.executable) : observed.executable === ready.executable)
  if (existingProcess) assert.deepEqual(observed, existingProcess, 'Running stable instance was replaced')
  observations.S = { process: observed, backend: await healthy(stable, observed), retirement: ready }
  assert.equal(processes(before.executable).length, 0)
  assert.notEqual(stable.userData, sourceApp.userData)
  const preference = JSON.parse(fs.readFileSync(path.join(stable.userData, 'active-profile.json')))
  assert.equal(fs.realpathSync(preference.home), fs.realpathSync(home)); assert.equal(preference.profile, ready.profile)
  for (const removal of ready.removalRoots) assert.equal(fs.existsSync(removal), false, 'Preview root survived')
  for (const file of ownedLaunchers) assert.equal(fs.lstatSync(file, { throwIfNoEntry: false }), undefined, 'Owned CLI launcher survived')
  if (windows) {
    const removed = win('removed', { side: journey.A, arch: process.arch, family: sourceApp.packageFamilyName })
    assert.equal(removed.registered, false); assert.deepEqual(removed.startApps, [])
    assert.equal(win('registration', { side: journey.S, arch: process.arch }).uri, stableFeed)
  }
  mac.write(path.join(out, 'automatic-S.json'), observations.S)
  const stableWindow = existing ?? await reopen(stable, observed)
  prompts.push(await chat(stableWindow, mock.url, path.join(out, 'S'))); preservation()
  if (journey.route !== 'direct') await gate('stable-update')
  const prior = await observe(stable)
  await transition(stableWindow, 'update', out)
  if (windows) await mac.until(() => win('available', { side: journey.T, arch: process.arch }), 'T installed')
  const nextProcess = await observe(windows ? installed(journey.T) : stable, prior)
  const next = installed(journey.T)
  observations.T = { process: nextProcess, backend: await healthy(next, nextProcess) }
  if (windows) assert.equal(win('registration', { side: journey.T, arch: process.arch }).uri, stableFeed)
  mac.write(path.join(out, 'automatic-T.json'), observations.T)
  const cold = await reopen(next, nextProcess)
  prompts.push(await chat(cold, mock.url, path.join(out, 'T'))); preservation()
  await close(cold)
  mac.write(path.join(out, 'lifecycle.json'), observations)
} catch (error) {
  mac.write(path.join(out, 'failure.json'), { message: String(error.message), phases: Object.keys(observations) })
  process.exitCode = 1
} finally { await mock.close() }
// Do not invoke ElectronApplication.close on old update owners: inherited pipes
// can outlive the source, and Playwright cleanup must not kill native receivers.
process.exit(process.exitCode ?? 0)
