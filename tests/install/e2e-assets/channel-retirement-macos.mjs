// Native observations for the retirement journey. No destination launch or IPC apply.
import fs from 'node:fs'
import path from 'node:path'
import { execFileSync, spawnSync } from 'node:child_process'
import { createHash, createHmac } from 'node:crypto'
import assert from 'node:assert/strict'
import { codesignTeam, stampAssertions } from './mac-bundled-manifest.cjs'
import { readNativeProcesses } from '../../../tests-js/scripts/desktop-smoke-process.ts'

/** @returns {Promise<void>} */
export const pause = () => new Promise(resolve => setTimeout(resolve, 500))
/** @template T @param {() => T | Promise<T>} probe @param {string} description @param {number} [timeout] */
export async function until(probe, description, timeout = 900_000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const result = await probe()
    if (result) return result
    await pause()
  }
  throw new Error(`Timed out: ${description}`)
}
/** @param {string} command @param {string[]} args @returns {string} */
export function native(command, args) {
  return execFileSync(command, args, { encoding: 'utf8', timeout: 120_000, maxBuffer: 4 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] }).trim()
}
/** @param {string} file @param {object} data */
export function write(file, data) {
  assert.equal(fs.existsSync(file), false, 'Refusing to overwrite prior evidence')
  const temporary = file + '.partial'
  fs.writeFileSync(temporary, JSON.stringify(data, null, 2) + '\n', { flag: 'wx', mode: 0o600 })
  fs.renameSync(temporary, file)
}
/** @param {string} app @returns {string} */
export function executable(app) {
  const name = native('/usr/bin/plutil', ['-extract', 'CFBundleExecutable', 'raw', '-o', '-', path.join(app, 'Contents/Info.plist')])
  if (!name || /[/\\]/.test(name)) throw new Error('Unsafe bundle executable')
  return path.join(app, 'Contents/MacOS', name)
}
/** @param {string} app @param {import("./channel-retirement-manifest.cjs").Side} side @param {string} arch */
export function verifyApp(app, side, arch) {
  native('/usr/bin/codesign', ['--verify', '--deep', '--strict', app])
  native('/usr/sbin/spctl', ['-a', '-vv', '-t', 'exec', app])
  const signature = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', app], { encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] })
  if (signature.status !== 0) throw new Error('codesign display failed')
  assert.equal(codesignTeam(signature.stderr), side.package.teamId)
  assert.equal(/^Identifier=(.+)$/m.exec(signature.stderr)?.[1], side.package.identity)
  const plist = JSON.parse(native('/usr/bin/plutil', ['-convert', 'json', '-o', '-', path.join(app, 'Contents/Info.plist')]))
  assert.equal(plist.CFBundleIdentifier, side.package.identity)
  assert.equal(plist.CFBundleShortVersionString, side.package.version)
  const bin = executable(app)
  assert.equal(native('/usr/bin/lipo', ['-archs', bin]), arch === 'arm64' ? 'arm64' : 'x86_64')
  const stamp = JSON.parse(fs.readFileSync(path.join(app, 'Contents/Resources/install-stamp.json')))
  const request = side.manifest.request
  const expected = { commit: request.commit, tag: request.releaseTag || null }
  if (!request.releaseTag) expected.channelRequest = request
  assert.deepEqual(stampAssertions(stamp, expected), [])
  // Stable release manifests wrap original signed release bytes, not restamped channel builds.
  return { executable: bin, app, commit: stamp.commit, identity: plist.CFBundleIdentifier,
    version: plist.CFBundleShortVersionString, teamId: side.package.teamId }
}
/** @param {string} bin */
export function processes(bin) { return readNativeProcesses().filter(row => row.executable === bin) }
/** @param {import("../../../tests-js/scripts/desktop-smoke-process.ts").NativeProcess} row */
export function processIdentity(row) {
  return { pid: row.pid, executable: row.executable, birth: native('/bin/ps', ['-p', String(row.pid), '-o', 'lstart=']) }
}
/** @param {{pid: number, executable: string, birth: string}} observed */
export function assertProcess(observed) {
  const found = processes(observed.executable).find(row => row.pid === observed.pid)
  assert.ok(found, 'Observed native application exited')
  assert.equal(processIdentity(found).birth, observed.birth, 'Native PID was reused')
}
/** @param {string} bin @param {number} [exceptPid] */
export async function observe(bin, exceptPid) {
  return until(() => {
    const rows = processes(bin).filter(row => row.pid !== exceptPid)
    if (rows.length > 1) throw new Error('Ambiguous installed application process')
    return rows.length === 1 ? processIdentity(rows[0]) : null
  }, `automatic activation at ${bin}`)
}
/** @param {{pid: number, executable: string, birth: string}} observed */
export async function quit(observed) {
  assertProcess(observed)
  native('/usr/bin/osascript', ['-l', 'JavaScript', '-e',
    'ObjC.import("AppKit"); function run(args) { const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(args[0])); if (!app || !app.terminate) throw Error("Native Quit refused"); }', String(observed.pid)])
  await until(() => !processes(observed.executable).some(row => row.pid === observed.pid), 'normal native Quit', 60_000)
}
/** @param {string} root @param {string} source @param {string} destination
 * @param {string} manifestSha256 @param {string | undefined} sourcePackage */
export function completedJournal(root, source, destination, manifestSha256, sourcePackage) {
  if (!fs.existsSync(root)) return null
  const rows = fs.readdirSync(root).filter(name => /^[a-f0-9]{32}$/.test(name) && fs.existsSync(path.join(root, name, 'journal.json')))
  assert.ok(rows.length <= 1, 'Ambiguous retirement journals')
  if (!rows.length) return null
  const file = path.join(root, rows[0], 'journal.json')
  if (!fs.existsSync(file)) return null
  const record = JSON.parse(fs.readFileSync(file))
  if (record.stage !== 'complete') return null
  const normalize = value => sourcePackage ? path.win32.normalize(value).toLowerCase() : path.normalize(value)
  assert.equal(normalize(record.request.source.appPath), normalize(source))
  assert.equal(normalize(record.request.destination.appPath), normalize(destination))
  if (sourcePackage) assert.equal(record.request.source.packageFullName, sourcePackage)
  assert.equal(record.request.destinationManifestSha256, manifestSha256)
  const digest = createHash('sha256').update(JSON.stringify(record.request)).digest('hex')
  assert.equal(record.requestDigest, digest)
  const { mac, ...ready } = record.ready
  assert.equal(createHmac('sha256', Buffer.from(record.request.token, 'hex')).update(JSON.stringify(ready)).digest('hex'), mac)
  assert.equal(ready.requestDigest, digest)
  assert.equal(ready.id, record.request.id)
  assert.equal(ready.home, record.state.selectedHome)
  assert.equal(ready.commit, record.request.destination.commit)
  assert.equal(ready.connectionId, 'local')
  // Export only non-secret observed fields. Never persist the private request/token.
  return { id: ready.id, pid: ready.pid, executable: ready.executable, commit: ready.commit,
    home: ready.home, profile: ready.profile, stage: record.stage,
    removalRoots: record.request.source.removalRoots }
}
