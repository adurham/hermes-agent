import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useMediaImage } from './use-media-image'

// Regression for #74564 (aperture half): the old preview clamp
// (--image-preview-height: clamp(16.25rem, …, 26.25rem), max-width 34rem)
// meant the reserved frame for a landscape image could never span the
// preview width — a 16:9 frame was at most 26.25rem × 16/9 ≈ 46.7rem, and
// on typical windows the viewport clamp pinned it nearer 16.25rem × 16/9 ≈
// 28.9rem, so stacked landscape images showed through a small aperture
// (the reporter measured ~10% of one image's height).
//
// The envelope lives in styles.css as two custom properties; both the
// useMediaImage frame width and the unframed max-h in markdown-text.tsx
// derive from them. Pin the contract at that seam.
describe('inline image preview envelope (#74564)', () => {
  const stylesheet = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../styles.css'), 'utf8')

  beforeEach(() => {
    vi.stubGlobal('window', { hermesDesktop: {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('guarantees a landscape frame can span the full preview width', () => {
    const maxWidth = stylesheet.match(/--image-preview-max-width:\s*([\d.]+)rem/)?.[1]
    const height = stylesheet.match(/--image-preview-height:\s*clamp\(([\d.]+)rem/)?.[1]

    expect(maxWidth).toBeDefined()
    expect(height).toBeDefined()

    // The height FLOOR must be at least maxWidth × 9/16: then a 16:9 frame
    // (width = height × 16/9) reaches the full preview width even on the
    // shortest viewport, instead of collapsing to the old 16.25rem floor.
    expect(Number(height)).toBeGreaterThanOrEqual((Number(maxWidth) * 9) / 16)
    // And the envelope actually grew from the reported 34rem/16.25rem pair.
    expect(Number(maxWidth)).toBeGreaterThan(34)
    expect(Number(height)).toBeGreaterThan(16.25)
  })

  it('keeps the natural-size cap so a small image is never upscaled', () => {
    const { result } = renderHook(() =>
      useMediaImage('/home/user/out/shot.png', 16 / 9, { width: 640, height: 360 })
    )

    expect(String(result.current.frameStyle?.width)).toContain('640px')
  })
})
