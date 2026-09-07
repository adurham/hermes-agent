import { describe, expect, it } from 'vitest'

import { fallbackModelLabel, fallbackModelLabelCompact } from './modelFallback.js'

describe('fallbackModelLabel', () => {
  it('renders the plain model name when not in fallback', () => {
    expect(fallbackModelLabel({ model: 'glm-5.3' })).toBe('glm-5.3')
  })

  it('renders the plain model name when fallbackActive is explicitly false', () => {
    expect(fallbackModelLabel({ fallbackActive: false, model: 'glm-5.3', primaryModel: 'glm-5.3' })).toBe('glm-5.3')
  })

  it('renders the glyph + verbose swap when fallbackActive is true', () => {
    expect(fallbackModelLabel({ fallbackActive: true, model: 'claude-opus-5', primaryModel: 'glm-5.3' })).toBe(
      '⚠ claude-opus-5 (fallback from glm-5.3)'
    )
  })

  it('prefers the backend-provided modelLabel over local composition', () => {
    expect(
      fallbackModelLabel({
        fallbackActive: true,
        model: 'claude-opus-5',
        modelLabel: '⚠ custom label from backend',
        primaryModel: 'glm-5.3'
      })
    ).toBe('⚠ custom label from backend')
  })

  it('does not render a stray glyph when fallbackActive is true but primaryModel is missing', () => {
    // An older/un-upgraded gateway may send partial fields; never guess.
    expect(fallbackModelLabel({ fallbackActive: true, model: 'claude-opus-5' })).toBe('claude-opus-5')
  })

  it('returns empty string when there is no model at all', () => {
    expect(fallbackModelLabel({})).toBe('')
  })
})

describe('fallbackModelLabelCompact', () => {
  it('renders the plain model name when not in fallback', () => {
    expect(fallbackModelLabelCompact({ model: 'glm-5.3' })).toBe('glm-5.3')
  })

  it('renders the glyph + compact arrow swap when fallbackActive is true', () => {
    expect(fallbackModelLabelCompact({ fallbackActive: true, model: 'claude-opus-5', primaryModel: 'glm-5.3' })).toBe(
      '⚠ glm-5.3→claude-opus-5'
    )
  })

  it('ignores modelLabel (compact form is always locally composed)', () => {
    expect(
      fallbackModelLabelCompact({
        fallbackActive: true,
        model: 'claude-opus-5',
        modelLabel: '⚠ verbose backend label',
        primaryModel: 'glm-5.3'
      })
    ).toBe('⚠ glm-5.3→claude-opus-5')
  })
})
