import { describe, expect, it } from 'vitest'

import { splitDragHandleProps } from './session-row-state'

describe('splitDragHandleProps', () => {
  it('pulls onPointerDown out for the wide drag-from-name surface, leaving everything else for the keyboard handle', () => {
    const onPointerDown = () => undefined
    const onKeyDown = () => undefined

    const dragHandleProps = {
      'aria-label': 'Reorder session',
      onKeyDown,
      onPointerDown,
      role: 'button',
      tabIndex: 0
    }

    const { keyboardProps, pointerDown } = splitDragHandleProps(dragHandleProps)

    expect(pointerDown).toBe(onPointerDown)
    expect(keyboardProps).toEqual({
      'aria-label': 'Reorder session',
      onKeyDown,
      role: 'button',
      tabIndex: 0
    })
    expect(keyboardProps).not.toHaveProperty('onPointerDown')
  })

  it('handles an undefined dragHandleProps (non-reorderable rows) without throwing', () => {
    const { keyboardProps, pointerDown } = splitDragHandleProps(undefined)

    expect(pointerDown).toBeUndefined()
    expect(keyboardProps).toEqual({})
  })
})
