/**
 * Headless driver: does a streamed growth re-pin the bottom over a reader who left it?
 *
 * Live evidence (desktop.log, the current build, 2.5h of real use): every deep reading position is
 * destroyed 4-28ms after it is taken, and every yank lands on `scrollHeight - 1 - clientHeight` —
 * use-stick-to-bottom's OWN target, passed through the app's `resolveThreadScrollTarget`, one pixel
 * short of the app's own bottom (`threadScrollTargetTop` = `scrollHeight - clientHeight`). No app leg
 * logs a write at that moment (no `ro`, no `settle`, no cancel), and the recording gate then files
 * `{kind:'bottom'}` — which is why the whole per-session store holds nothing but bottoms.
 *
 * The writer is therefore the library's sticky follow, which only runs while the library still
 * believes it is at the bottom (`if (!state.isAtBottom) return`). A wheel never loses that belief —
 * `handleWheel` escapes on `deltaY < 0` with no other condition. The moves that DO get yanked are
 * single scroll events of 400-2400px, which no wheel can produce (a notch is 50-160px): scrollbar
 * drags, keyboard, find-in-page. Those rely on the library's only other escape, `handleScroll`'s
 * 1ms-timeout `isScrollingUp` check — and that check BAILS OUT entirely when a content resize landed
 * in the same frame (`if (state.resizeDifference || scrollTop === ignoreScrollToTop) return`).
 * A live turn resizes the transcript continuously, so the window is easy to land in, and the next
 * growth re-pins the bottom over the reader.
 *
 * Each round here therefore: re-locks the library the way a reader does (sit at the bottom, let the
 * stream follow), then leaves the bottom with NO wheel in the same frame as a growth, then keeps
 * streaming. A control round does the identical move WITH a wheel event.
 *
 * Prereq: `npx vite --host 127.0.0.1 --port 5174` in apps/desktop, then
 *   node scripts/diag-scroll-yank.mjs
 *
 * RESULT (2026-10-30): the harness does NOT reproduce the live re-pin — 0/6 for the wheel-less move
 * and 0/6 for the wheel one, against a 10889px transcript with a run in flight. The live writer is
 * the app's own post-load anchor apply (`el.scrollTop = el.scrollHeight - restoreFromBottom`), which
 * the harness's flow never produces, so this rests on the live trace and on the DOM regression tests
 * in `../src/components/assistant-ui/thread/list-session-scroll.test.tsx`. The write-trace below is
 * still the tool that names a scrollTop writer when the harness does move one.
 */

import { chromium } from '@playwright/test'

const URL = process.env.HARNESS_URL ?? 'http://127.0.0.1:5174/scroll-harness.html'
const ROUNDS = Number(process.env.ROUNDS ?? 6)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })

page.on('pageerror', error => console.error('[pageerror]', String(error).slice(0, 300)))

// Every write to a transcript viewport's scrollTop, with the frame that made it.
await page.addInitScript(() => {
  window.__writes = []
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get() {
      return descriptor.get.call(this)
    },
    set(value) {
      if (this.getAttribute?.('data-slot') === 'aui_thread-viewport') {
        window.__writes.push({
          from: descriptor.get.call(this),
          to: value,
          stack: String(new Error().stack)
            .split('\n')
            .slice(1, 10)
            .map(line => line.trim())
            .join(' | ')
        })
      }
      return descriptor.set.call(this, value)
    }
  })
})

await page.goto(URL, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-viewport"]', { timeout: 30_000 })
await page.waitForTimeout(1200)

const geometry = () =>
  page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    return {
      fromBottom: Math.round(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight),
      height: viewport.scrollHeight,
      libraryBottom: viewport.scrollHeight - 1 - viewport.clientHeight,
      following: viewport.dataset.following,
      top: Math.round(viewport.scrollTop)
    }
  })

const drainWrites = async () =>
  page.evaluate(() => {
    const all = window.__writes
    window.__writes = []
    return all
  })

const writerOf = stack =>
  stack.includes('use-stick-to-bottom') ? 'LIBRARY' : stack.includes('list.tsx') ? 'app' : 'driver'

/** Sit at the bottom and let the stream re-lock the follow, exactly as a reader following a run does. */
const returnToBottom = async () => {
  await page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    viewport.scrollTop = viewport.scrollHeight
  })
  await page.waitForTimeout(500)
}

/**
 * Leave the bottom with ONE scroll event and no wheel, in the SAME frame as a transcript growth —
 * the move the live trace shows, and the one the library's resize-difference bail swallows.
 */
const leaveBottomInResizeFrame = (px, withWheel) =>
  page.evaluate(
    ([px, withWheel]) => {
      document.getElementById('grow')?.click()
      const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
      if (withWheel) {
        viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -px }))
      }
      viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - px)
      viewport.dispatchEvent(new Event('scroll'))
    },
    [px, withWheel]
  )

const stream = on =>
  page.evaluate(on => {
    if (window.__stream) {
      window.clearInterval(window.__stream)
      window.__stream = null
    }

    if (on) {
      window.__stream = window.setInterval(() => document.getElementById('grow')?.click(), 90)
    }
  }, on)

console.log('harness:', URL)

for (let i = 0; i < 4; i += 1) {
  await page.click('#deep')
  await page.waitForTimeout(400)
}

await page.click('#grow-20')
await page.waitForTimeout(600)
await page.click('#stream')
await page.waitForTimeout(300)
await drainWrites()

const start = await geometry()
console.log(`\ntranscript ${start.height}px, viewport ${Math.round(start.height - start.libraryBottom - 1)}px`)

const runRounds = async (label, withWheel) => {
  await stream(true)
  await page.waitForTimeout(300)
  await drainWrites()

  let yanked = 0

  for (let round = 0; round < ROUNDS; round += 1) {
    await returnToBottom()
    await drainWrites()

    await leaveBottomInResizeFrame(1400, withWheel)
    await page.waitForTimeout(120)
    const taken = await geometry()
    await page.waitForTimeout(400)
    const after = await geometry()
    const writes = await drainWrites()
    const follows = writes.filter(write => writerOf(write.stack) === 'LIBRARY' && write.to - write.from > 200)
    const back = after.fromBottom < 60

    if (back) {
      yanked += 1
    }

    console.log(
      `  ${label} round ${round + 1}: left at ${taken.fromBottom}px, then ${after.fromBottom}px ` +
        `${back ? `→ TAKEN BACK TO THE BOTTOM (${follows.length} library follow write(s))` : '→ held'}` +
        (follows.length ? `  e.g. ${Math.round(follows[0].from)} -> ${Math.round(follows[0].to)}` : '')
    )
  }

  await stream(false)
  await page.waitForTimeout(200)

  return yanked
}

const noWheel = await runRounds('no-wheel', false)
const wheel = await runRounds('wheel', true)

console.log('\n══ verdict ══')
console.log(`  no-wheel leave-the-bottom (scrollbar drag / keyboard / find-in-page): ${noWheel}/${ROUNDS} taken back`)
console.log(`  same move WITH a wheel event:                                        ${wheel}/${ROUNDS} taken back`)
console.log(
  `  library target = scrollHeight-1-clientHeight; the app's own bottom = scrollHeight-clientHeight (one px apart)`
)

await browser.close()
