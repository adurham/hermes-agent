import assert from 'node:assert/strict'
import { test } from 'vitest'

import { nativeQuad } from '../../../scripts/msix-shared.mjs'

// 2026-09-22T00:14:03Z, worked by hand: day-of-year 264 (2026 is not a leap
// year; Jan 1 is hour 0), so hour-of-year is 264*24 = 6336. The 14th minute
// holds 14*60+3 = 843 seconds.
const EPOCH = Date.parse('2026-09-22T00:14:03Z') / 1000

test('a stable release and a canary built at the same instant share one quad', () => {
  assert.equal(nativeQuad('v0.21.5', EPOCH), '2026.6336.843.0')
  assert.equal(nativeQuad('v0.21.4+canary.20260922T001403Z', EPOCH), '2026.6336.843.0')
})

test('a later build sorts above an earlier one, stable or canary', () => {
  const earlier = nativeQuad('v0.21.5', EPOCH).split('.').map(Number)
  const later = nativeQuad('v0.21.4+canary.20260922T001404Z', EPOCH + 1).split('.').map(Number)
  const first = later.findIndex((value, index) => value !== earlier[index])
  assert.ok(first >= 0)
  assert.ok(later[first] > earlier[first])
})

test('every field stays inside 16 bits and the revision stays zero', () => {
  for (const stamp of ['2026-01-01T00:00:00Z', '2026-12-31T23:59:59Z', '2028-12-31T23:59:59Z']) {
    const parts = nativeQuad('v0.21.5', Date.parse(stamp) / 1000).split('.').map(Number)
    assert.equal(parts.length, 4)
    assert.ok(parts.every(value => value >= 0 && value <= 65535))
    assert.equal(parts[3], 0)
  }
})
