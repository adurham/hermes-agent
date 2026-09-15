import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { GreetingPreviewText, greetingRevealFrames } from './greeting-preview'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('reveals the exact greeting in graphemes and cancels its pending frame on unmount', () => {
  const text = 'Hi 👩🏽‍💻.\n\n你好'
  const frames = greetingRevealFrames(text)
  const boundary = frames.find(frame => text.slice(0, frame.end).endsWith('👩🏽‍💻'))
  expect(boundary).toBeDefined()
  expect(frames.at(-1)?.end).toBe(text.length)
  expect(frames.every((frame, index) => index === 0 || frame.at > frames[index - 1].at)).toBe(true)

  let next: FrameRequestCallback | undefined
  const cancel = vi.fn()
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    next = callback

    return 1
  })
  vi.stubGlobal('cancelAnimationFrame', cancel)

  const { container, unmount } = render(
    <I18nProvider>
      <GreetingPreviewText text={text} />
    </I18nProvider>
  )

  expect(container.querySelector('.sr-only')?.textContent).toBe(text)
  act(() => next?.(performance.now() + 200))
  expect(container.querySelector('[aria-hidden]')?.textContent).not.toContain('你好')
  unmount()
  expect(cancel).toHaveBeenCalled()
})
