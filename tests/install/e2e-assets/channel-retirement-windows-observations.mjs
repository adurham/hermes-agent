// Portable checks over native observations; none of these create acceptance receipts.
import assert from 'node:assert/strict'
import path from 'node:path'
import { channelStampAssertions } from './mac-bundled-manifest.cjs'

/** @param {string} left @param {string} right */
export const samePath = (left, right) => path.win32.normalize(left).toLowerCase() === path.win32.normalize(right).toLowerCase()
/** @param {import("../../../tests-js/scripts/desktop-smoke-process.ts").NativeProcess[]} rows @param {string} executable */
export function mainProcesses(rows, executable) {
  return rows.filter(row => row.executable && samePath(row.executable, executable) &&
    !/(?:^|\s)--type(?:=|\s)/.test(row.command))
}
/** @param {{identity: string, publisher: string, version: string, arch: string, applicationId: string, stamp: object}} observed
 * @param {import("./channel-retirement-manifest.cjs").Side} side @param {string} arch */
export function assertInstalled(observed, side, arch) {
  assert.equal(observed.identity, side.package.identity)
  assert.equal(observed.publisher, side.package.publisher)
  assert.equal(observed.version, side.package.version)
  assert.equal(observed.arch.toLowerCase(), arch)
  assert.equal(observed.applicationId, side.manifest.request.identity.appNamePascal)
  const stamp = observed.stamp
  assert.equal(stamp.payload, 'bundled')
  assert.equal(stamp.distribution, 'desktop-app')
  assert.equal(stamp.updateMechanism, 'app-installer')
  assert.equal(stamp.dirty, false)
  assert.equal(stamp.commit, side.manifest.request.commit)
  assert.equal(stamp.tag, side.manifest.request.releaseTag || null)
  if (side.manifest.request.releaseTag) assert.equal(stamp.channelBuild == null, true)
  else assert.deepEqual(channelStampAssertions(stamp, side.manifest.request), [])
}
