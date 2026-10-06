import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { mediaDisplayLabel, mediaKind, mediaMarkdownHref } from '@/lib/media'

import { MarkdownTextContent } from './markdown-text'

// Regression for #74564 (attachment-card half): gateway-local PDF/ZIP
// artifacts emitted with MEDIA: (or a plain filesystem link) must surface as
// a prominent named attachment card with a download action — not a bare
// inline "Open <name>" fallback link.
describe('document attachments delivered via MEDIA (#74564)', () => {
  afterEach(cleanup)

  it('classifies pdf/zip as document files (never as opaque non-file kinds)', () => {
    // Documents stay 'file' — but they must be DOCUMENTS, so the renderer
    // routes them to the attachment card rather than a bare open anchor.
    expect(mediaKind('/home/user/out/report.pdf')).toBe('file')
    expect(mediaKind('/home/user/out/bundle.zip')).toBe('file')
    // The display label keeps the basename for the card.
    expect(mediaDisplayLabel('/home/user/out/report.pdf')).toContain('report.pdf')
  })

  it('renders a MEDIA pdf as a prominent attachment card with a download action', async () => {
    const href = mediaMarkdownHref('/home/user/out/report.pdf')

    render(<MarkdownTextContent isRunning={false} text={`Wrote the deck: [report.pdf](${href})`} />)

    // PreviewAttachment paints the filename + Download/Open-preview buttons;
    // the degraded path was a bare "Open report.pdf" anchor with no card.
    expect((await screen.findAllByRole('button')).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Download')).toBeTruthy()
    expect(screen.getByText('report.pdf')).toBeTruthy()
    expect(screen.queryByText(/^Open report/)).toBeNull()
  })

  it('renders a MEDIA zip as a prominent attachment card too', async () => {
    const href = mediaMarkdownHref('/home/user/out/images.zip')

    render(<MarkdownTextContent isRunning={false} text={`[images.zip](${href})`} />)

    expect((await screen.findAllByRole('button')).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Download')).toBeTruthy()
    expect(screen.getByText('images.zip')).toBeTruthy()
    expect(screen.queryByText(/^Open images/)).toBeNull()
  })
})
