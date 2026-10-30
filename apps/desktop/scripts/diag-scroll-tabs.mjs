/**
 * Headless driver for the dev-only scroll harness (`scroll-harness.html`).
 *
 * Real Chromium, real layout, real `use-stick-to-bottom` — no Electron, no app window, no focus
 * stealing, and no LLM. Replays the owner's report step by step and prints the geometry at each
 * step as JSON so the numbers can be diffed:
 *
 *   1. read the transcript, scroll up 700px in tab A (a genuine reading position)
 *   2. switch to B, then back to A        -> does A come back to the reading position?
 *   3. grow A's transcript while reading  -> does the position survive streaming growth?
 *   4. switch to C (never visited)        -> does an unknown session land at the bottom?
 *
 * Prereq: `npx vite --host 127.0.0.1 --port 5174` in apps/desktop, then
 *   node scripts/diag-scroll-tabs.mjs
 */

import { chromium } from '@playwright/test'

const URL = process.env.HARNESS_URL ?? 'http://127.0.0.1:5174/scroll-harness.html'
const KEYS = ['a', 'b', 'c']
const STORE_KEY = 'hermes.desktop.threadScroll.v1.profile.default'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })

page.on('console', message => {
  if (message.type() === 'error') {
    console.error('[page console.error]', message.text().slice(0, 300))
  }
})
page.on('pageerror', error => console.error('[pageerror]', String(error).slice(0, 300)))

await page.goto(URL, { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-viewport"]', { timeout: 30_000 })
await page.waitForTimeout(1200)

const geometry = () =>
  page.evaluate(([keys, storeKey]) => {
    const panes = {}

    for (const key of keys) {
      const viewport = document.querySelector(`[data-key="${key}"] [data-slot="aui_thread-viewport"]`)

      if (!viewport) {
        continue
      }

      panes[key] = {
        fromBottom: Math.round(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight),
        height: viewport.scrollHeight,
        top: Math.round(viewport.scrollTop),
        visible: !document.querySelector(`[data-key="${key}"]`)?.hasAttribute('data-pane-hidden')
      }
    }

    return { panes, saved: window.localStorage.getItem(storeKey) }
  }, [KEYS, STORE_KEY])

const step = async label => {
  const data = await geometry()
  console.log(`\n── ${label}`)
  console.log(`   ${JSON.stringify(data.panes)}`)
  console.log(`   saved: ${data.saved}`)

  return data
}

const select = async key => {
  await page.click(`[data-tab="${key}"]`)
  await page.waitForTimeout(1200)
}

/** A reader scrolling up: real wheel input (breaks the bottom lock), then the resulting position. */
const scrollUp = async (key, px) => {
  await page.evaluate(
    ([key, px]) => {
      const viewport = document.querySelector(`[data-key="${key}"] [data-slot="aui_thread-viewport"]`)
      viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -px }))
      viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - px)
      viewport.dispatchEvent(new Event('scroll'))
    },
    [key, px]
  )
  await page.waitForTimeout(500)
}

console.log('harness:', URL)

await step('1. baseline (tab A active)')
await scrollUp('a', 700)
const reading = await step('2. scrolled up 700px in A')

await select('b')
const away = await step('3. switched to B (A is a hidden keep-alive pane)')

await select('a')
const back = await step('4. back to A')

await page.click('#grow-20')
await page.waitForTimeout(1500)
const grown = await step('5. A grew by 20 turns while the reader was 700px up')

// A running turn: the app's own live-follow path (status stack, composer dim, resize follow) is
// only armed while a run is active, so a streaming session is the case a static test misses.
await page.click('#stream')
await page.waitForTimeout(800)
await step('5b. run toggled ON while the reader is 700px up')

for (let chunk = 0; chunk < 3; chunk += 1) {
  await page.click('#grow')
  await page.waitForTimeout(700)
}

const streamed = await step('5c. three streamed chunks arrived while reading')

await page.click('#stream')
await page.click('#clamp')
await page.waitForTimeout(900)
await scrollUp('a', 700)
await step('5d. composer clamp ON, scrolled up again')

for (let chunk = 0; chunk < 3; chunk += 1) {
  await page.click('#grow')
  await page.waitForTimeout(700)
}

const clamped = await step('5e. chunks arrived while reading with the composer clamp')

// Deep history: the reader is far up a session whose runtime window had to pull older pages. On
// return the window is rebuilt at the tail, so a saved offset can out-reach what is mounted — the
// path that parks a clamped restore and grows pages back toward it.
await page.click('#clamp')
await page.click('[data-tab="a"]')
await page.waitForTimeout(900)

for (let pull = 0; pull < 4; pull += 1) {
  await page.click('#deep')
  await page.waitForTimeout(700)
}

await step('5f. A with the whole session materialized')
await scrollUp('a', 4000)
const deep = await step('5g. reader 4000px up a long transcript')
await select('b')
const deepAway = await step('5h. left that reading position')
await select('a')
const deepBack = await step('5i. returned to A')

await select('c')
const fresh = await step('6. switched to C, never visited (no saved position)')

console.log('\n══ verdict ══')
const readingFromBottom = reading.panes.a?.fromBottom ?? null
const backFromBottom = back.panes.a?.fromBottom ?? null
const grownFromBottom = grown.panes.a?.fromBottom ?? null
const freshFromBottom = fresh.panes.c?.fromBottom ?? null
const freshHeight = fresh.panes.c?.height ?? 0
const freshBottom = Math.max(0, freshHeight - (fresh.panes.c?.client ?? 0))

console.log(`  A left at ${readingFromBottom}px from bottom; after the round trip ${backFromBottom}px (saved ${away.saved})`)
console.log(`  A after 20 new turns: ${grownFromBottom}px from bottom  ${grownFromBottom !== null && grownFromBottom < 60 ? '→ YANKED TO THE BOTTOM' : '→ position held'}`)
const streamedFromBottom = streamed.panes.a?.fromBottom ?? null
const clampedFromBottom = clamped.panes.a?.fromBottom ?? null
console.log(`  A after streamed chunks (run active): ${streamedFromBottom}px from bottom  ${streamedFromBottom !== null && streamedFromBottom < 60 ? '→ YANKED TO THE BOTTOM' : '→ position held'}`)
console.log(`  A after chunks with the composer clamp: ${clampedFromBottom}px from bottom  ${clampedFromBottom !== null && clampedFromBottom < 60 ? '→ YANKED TO THE BOTTOM' : '→ position held'}`)
const deepReading = deep.panes.a?.fromBottom ?? null
const deepBackFromBottom = deepBack.panes.a?.fromBottom ?? null
console.log(`  A deep read left at ${deepReading}px (saved ${deepAway.saved}); returned to ${deepBackFromBottom}px ${Math.abs((deepBackFromBottom ?? -1) - (deepReading ?? 0)) <= 40 ? '→ restored ✓' : '→ NOT restored ✗'}`)
console.log(`  C (unknown) landed ${freshFromBottom}px from bottom (bottom is 0)  ${freshFromBottom !== null && Math.abs(freshFromBottom) <= 8 ? '→ defaulted to bottom ✓' : '→ NOT at the bottom ✗'}`)

await browser.close()
