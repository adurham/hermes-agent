import { atom } from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { answerParkedCard, type ParkedCardAnswer, registerParkedCardAnswer } from './parked-card-answers'

const kind = (answer: ParkedCardAnswer) => ({ $requests: atom({}), answer, waiting: () => false })

describe('parked card answers', () => {
  const unregister: Array<() => void> = []

  afterEach(() => {
    unregister.splice(0).forEach(off => off())
  })

  it('stops at the first card that takes the text, and forgets a card once it unregisters', () => {
    const declines = vi.fn(() => false)
    const takes = vi.fn((sessionId: string) => sessionId === 'session-a')
    const later = vi.fn(() => true)

    unregister.push(registerParkedCardAnswer('test-declines', kind(declines)))
    const offTakes = registerParkedCardAnswer('test-takes', kind(takes))
    unregister.push(registerParkedCardAnswer('test-later', kind(later)))

    expect(answerParkedCard('session-a', 'hello')).toBe(true)
    expect(declines).toHaveBeenCalledWith('session-a', 'hello')
    expect(later).not.toHaveBeenCalled()

    offTakes()
    later.mockReturnValue(false)

    expect(answerParkedCard('session-a', 'hello')).toBe(false)
    expect(takes).toHaveBeenCalledTimes(1)
  })
})
