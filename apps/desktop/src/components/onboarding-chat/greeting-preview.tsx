import './greeting-preview.css'

import { useStore } from '@nanostores/react'
import { useReducedMotion } from 'motion/react'
import { useEffect, useMemo, useState } from 'react'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { $onboardingGreeting } from '@/components/onboarding-chat/assembly'
import { $onboardingGate } from '@/store/onboarding-gate'

/** Grapheme boundaries keep translated greetings and combined characters intact. */
export function greetingRevealFrames(text: string): { end: number; at: number }[] {
  const frames = []
  let at = 0

  for (const { index, segment } of new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text)) {
    frames.push({ end: index + segment.length, at })
    at += 1000 / 18

    if (/[.!?。！？]$/u.test(segment)) {
      at += 220
    } else if (segment === '\n' && text[index - 1] === '\n') {
      at += 650
    }
  }

  return frames
}

export function GreetingPreviewText({ text }: { text: string }) {
  const reducedMotion = useReducedMotion()
  const frames = useMemo(() => greetingRevealFrames(text), [text])
  const [end, setEnd] = useState(0)

  useEffect(() => {
    if (reducedMotion || !frames.length) {
      return
    }

    let frame = 0
    let index = 0
    const start = performance.now()

    const reveal = (now: number) => {
      while (index < frames.length && frames[index].at <= now - start) {
        index += 1
      }

      setEnd(index ? frames[index - 1].end : 0)

      if (index < frames.length) {
        frame = requestAnimationFrame(reveal)
      }
    }

    frame = requestAnimationFrame(reveal)

    return () => cancelAnimationFrame(frame)
  }, [frames, reducedMotion])

  const visible = reducedMotion ? text : text.slice(0, end)

  return (
    <div data-role="assistant" data-slot="guide-greeting-preview">
      <span className="sr-only">{text}</span>
      <div
        aria-hidden
        className="wrap-anywhere min-w-0 max-w-full overflow-hidden text-pretty text-[length:var(--conversation-text-font-size)] leading-(--dt-line-height) text-foreground"
        data-slot="aui_assistant-message-content"
      >
        <MarkdownTextContent caret={reducedMotion ? undefined : 'block'} isRunning={visible !== text} text={visible} />
      </div>
    </div>
  )
}

/** A local presentation of the exact greeting later persisted by session.create. */
export function GuideGreetingPreview() {
  const greeting = useStore($onboardingGreeting)
  const gate = useStore($onboardingGate)

  return (
    <div className="h-full overflow-y-auto" data-slot="guide-opening">
      <div className="mx-auto w-full max-w-(--composer-width) min-w-0 px-6 pt-[calc(var(--titlebar-height)-0.5rem)]">
        {gate.phase === 'cinematic' && greeting && <GreetingPreviewText key={greeting} text={greeting} />}
      </div>
    </div>
  )
}
