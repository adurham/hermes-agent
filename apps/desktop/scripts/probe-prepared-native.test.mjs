import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { test } from 'vitest'

test('the native admission child refuses ordinary Node instead of certifying an Electron ABI', () => {
  const child = spawnSync(process.execPath, [path.join(import.meta.dirname, 'probe-prepared-native.mjs'), '--child', import.meta.dirname], {
    encoding: 'utf8', timeout: 5000, env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
  })
  assert.notEqual(child.status, 0)
  assert.match(child.stderr, /Electron runtime required/)
})
