/**
 * Measures transcript row heights in the real layout — the evidence for the "near the bottom" band in
 * `thread-scroll.ts` (THREAD_SCROLL_STICKY_THRESHOLD_PX). The band must cover the follow loop's
 * one-frame lag (17-80px on live traces, see the store's comment) while staying well inside a single
 * row, so a reader who stopped just shy of the bottom edge is not visibly yanked when it is snapped
 * back to the bottom.
 *
 * Prereq: `npx vite --host 127.0.0.1 --port 5174` in apps/desktop, then
 *   node scripts/diag-transcript-row-height.mjs
 */

import { chromium } from '@playwright/test'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })

await page.goto(process.env.HARNESS_URL ?? 'http://127.0.0.1:5174/scroll-harness.html', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-content"]', { timeout: 30_000 })
await page.waitForTimeout(1500)

const rows = await page.evaluate(() => {
  const content = document.querySelector('[data-key="a"] [data-slot="aui_thread-content"]')
  const measured = [...content.children].map(child => ({
    height: Math.round(child.getBoundingClientRect().height),
    slot: child.getAttribute('data-slot') ?? child.tagName.toLowerCase()
  }))
  // Message rows only: the content wrapper also holds clearance/status elements, which are not rows a
  // reader can park inside.
  const messageRows = measured.filter(row => /message|turn|row/i.test(row.slot)).map(row => row.height).sort((a, b) => a - b)
  const heights = (messageRows.length ? messageRows : measured.map(row => row.height)).filter(height => height > 0).sort((a, b) => a - b)

  return {
    children: measured,
    count: heights.length,
    median: heights[Math.floor(heights.length / 2)],
    shortest: heights[0],
    tallest: heights.at(-1)
  }
})

console.log(`transcript content children: ${JSON.stringify(rows.children)}`)
console.log(`rows measured: ${rows.count}, shortest ${rows.shortest}px, median ${rows.median}px, tallest ${rows.tallest}px`)
console.log(`near-bottom band (96px) vs shortest row (${rows.shortest}px): ${96 < rows.shortest ? 'inside one row ✓' : 'wider than a row ✗'}`)

await browser.close()
