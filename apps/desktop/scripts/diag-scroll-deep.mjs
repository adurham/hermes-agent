/**
 * Focused headless repro: a DEEP reading position (a session whose runtime window had to pull older
 * pages) left by a tab switch, then returned to — with the older pages working, and with them
 * failing to commit.
 *
 * Case 1 proves the parked-offset regrow works when the page pull commits.
 * Case 2 is the failure the app's own `expandWindow` contract allows (a remote page that resolves
 * false / never anchors): the saved offset can never be covered. Before the fallback fix the reader
 * was left at scrollTop 0 — the top of the loaded rows, neither their position nor the tail.
 *
 * Prereq: `npx vite --host 127.0.0.1 --port 5174` in apps/desktop, then
 *   node scripts/diag-scroll-deep.mjs
 */

import { chromium } from '@playwright/test'

const URL = process.env.HARNESS_URL ?? 'http://127.0.0.1:5174/scroll-harness.html'
const STORE_KEY = 'hermes.desktop.threadScroll.v1.profile.default'

const browser = await chromium.launch()

async function runCase({ failPages, label }) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })
  const traces = []

  page.on('console', message => {
    if (message.type() === 'error' && message.text().includes('[scroll-trace]')) {
      traces.push(message.text().replace('[scroll-trace] ', ''))
    }
  })

  await page.goto(URL, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-viewport"]', { timeout: 30_000 })
  await page.waitForTimeout(1200)

  const geometry = () =>
    page.evaluate(([storeKey]) => {
      const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')

      return {
        fromBottom: Math.round(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight),
        height: viewport.scrollHeight,
        saved: window.localStorage.getItem(storeKey),
        top: Math.round(viewport.scrollTop)
      }
    }, [STORE_KEY])

  if (failPages) {
    await page.click('#fail-pages')
  }

  for (let pull = 0; pull < 4; pull += 1) {
    await page.click('#deep')
    await page.waitForTimeout(700)
  }

  await page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -4000 }))
    viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - 4000)
    viewport.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(700)
  const reading = await geometry()

  await page.click('[data-tab="b"]')
  await page.waitForTimeout(1500)
  await page.click('[data-tab="a"]')
  await page.waitForTimeout(6000)

  const back = await geometry()

  console.log(`\n── ${label}`)
  console.log(`   left at      ${reading.fromBottom}px from bottom (saved ${reading.saved})`)
  console.log(`   returned to  ${back.fromBottom}px from bottom (top=${back.top}, height=${back.height})`)

  const settled = traces.filter(line => line.startsWith('settle') || line.startsWith('restore'))
  console.log(`   trace:       ${settled.join(' | ').slice(0, 300)}`)

  const verdict =
    Math.abs(back.fromBottom - reading.fromBottom) <= 40
      ? 'restored to the reading position ✓'
      : back.fromBottom <= 8
        ? 'defaulted to the BOTTOM (position unrecoverable, landed somewhere usable) ✓'
        : 'STRANDED mid-transcript — neither the position nor the bottom ✗'

  console.log(`   verdict:     ${verdict}`)

  await page.close()

  return { back, reading, verdict }
}

await runCase({ failPages: false, label: 'case 1: older pages commit (parked regrow works)' })
await runCase({ failPages: true, label: 'case 2: older pages never commit (remote page fails)' })

await browser.close()
