import { describe, expect, it } from 'vitest'

import { fallbackModelLabel, fallbackModelLabelCompact } from './model-fallback-label'

describe('fallbackModelLabel', () => {
  it('renders the normalized display name when not in fallback', () => {
    expect(fallbackModelLabel({ model: 'anthropic/claude-opus-5' })).toBe('Opus 5')
  })

  it('renders the normalized display name when fallbackActive is explicitly false', () => {
    expect(
      fallbackModelLabel({ fallbackActive: false, model: 'anthropic/claude-opus-5', primaryModel: 'glm-5.3' })
    ).toBe('Opus 5')
  })

  it('renders the glyph + verbose swap when fallbackActive is true, through displayModelName', () => {
    expect(
      fallbackModelLabel({ fallbackActive: true, model: 'anthropic/claude-opus-5', primaryModel: 'glm-5.3' })
    ).toBe('⚠ Opus 5 (fallback from Glm 5.3)')
  })

  it('prefers the backend-provided modelLabel over local composition', () => {
    expect(
      fallbackModelLabel({
        fallbackActive: true,
        model: 'anthropic/claude-opus-5',
        modelLabel: '⚠ custom label from backend',
        primaryModel: 'glm-5.3'
      })
    ).toBe('⚠ custom label from backend')
  })

  it('does not render a stray glyph when fallbackActive is true but primaryModel is missing', () => {
    expect(fallbackModelLabel({ fallbackActive: true, model: 'anthropic/claude-opus-5' })).toBe('Opus 5')
  })

  it('returns empty string when there is no model at all', () => {
    expect(fallbackModelLabel({})).toBe('')
  })
})

describe('fallbackModelLabelCompact', () => {
  it('renders the normalized display name when not in fallback', () => {
    expect(fallbackModelLabelCompact({ model: 'anthropic/claude-opus-5' })).toBe('Opus 5')
  })

  it('renders the glyph + compact arrow swap when fallbackActive is true', () => {
    expect(
      fallbackModelLabelCompact({ fallbackActive: true, model: 'anthropic/claude-opus-5', primaryModel: 'glm-5.3' })
    ).toBe('⚠ Glm 5.3→Opus 5')
  })
})
