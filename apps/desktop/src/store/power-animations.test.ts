import { afterEach, describe, expect, it } from 'vitest'

import { $onBattery } from './power'
import './power-animations'

describe('battery animation gate', () => {
  afterEach(() => {
    $onBattery.set(false)
  })

  it('marks the document root while on battery', () => {
    $onBattery.set(true)
    expect(document.documentElement.dataset.onBattery).toBe('true')

    $onBattery.set(false)
    expect(document.documentElement.dataset.onBattery).toBe('false')
  })
})
