import { describe, expect, it } from 'vitest'

import { sessionLabel } from './sessionLabel.js'

describe('sessionLabel', () => {
  it("shows a canonical durable id as main's 8-character label", () => {
    expect(sessionLabel(`local-${'ab12cd34'}${'0'.repeat(56)}`)).toBe('ab12cd34')
  })

  it('leaves every other id as it is', () => {
    expect(sessionLabel('a707d4c3')).toBe('a707d4c3')
    expect(sessionLabel('20261012_134501_abcdef')).toBe('20261012_134501_abcdef')
    expect(sessionLabel(null)).toBeNull()
  })
})
