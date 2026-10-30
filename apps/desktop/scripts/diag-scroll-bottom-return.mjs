/**
 * The user's exact report: "I've scrolled to the bottom there plenty of times, then navigate away and
 * back, and it's a few messages up in the scroll again — even though the agent is stopped."
 *
 * The live trace shows the ingredient this case needs: while a pane is HIDDEN its transcript is
 * pruned to a short tail (≈989px where the revealed one is 4385px), and the reveal lays the full
 * transcript back out. A reader parked at the bottom has to survive that regrow.
 *
 * Three variants, all from a reader who is genuinely at the bottom of a fully materialized session:
 *   a) nothing arrives while away
 *   b) output arrives while away (the PS5 repair card keeps emitting progress, per the trace)
 *   c) materialized deep, then away and back
 *
 * Prereq: `npx vite --host 127.0.0.1 --port 5174` in apps/desktop, then
 *   node scripts/diag-scroll-bottom-return.mjs
 */

import { chromium } from '@playwright/test'

const URL = process.env.HARNESS_URL ?? 'http://127.0.0.1:5174/scroll-harness.html'

const browser = await chromium.launch()

const geometry = (page, key) =>
  page.evaluate(key => {
    const viewport = document.querySelector(`[data-key="${key}"] [data-slot="aui_thread-viewport"]`)
    const panes = [...document.querySelectorAll('[data-key]')].map(pane => {
      const el = pane.querySelector('[data-slot="aui_thread-viewport"]')

      return { height: el?.scrollHeight ?? 0, key: pane.getAttribute('data-key') }
    })
    // The library's own "jump to bottom" affordance is what the user pointed at in the screenshot.
    const jump = [...document.querySelectorAll('button')]
      .map(button => button.textContent?.trim() ?? '')
      .find(text => /message/i.test(text) && text.length < 40)

    return {
      fromBottom: Math.round(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight),
      height: viewport.scrollHeight,
      jump: jump ?? null,
      panes,
      top: Math.round(viewport.scrollTop)
    }
  }, key)

async function runCase({ label, growWhileAway, pages }) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })

  await page.goto(URL, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-viewport"]', { timeout: 30_000 })
  await page.waitForTimeout(1200)

  for (let pull = 0; pull < pages; pull += 1) {
    await page.click('#deep')
    await page.waitForTimeout(700)
  }

  // The reader scrolls to the bottom themselves (this is what "I've scrolled to the bottom plenty of
  // times" means) — the library then treats them as following the tail.
  await page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 6000 }))
    viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight
    viewport.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(800)

  const parked = await geometry(page, 'a')

  await page.click('[data-tab="b"]')
  await page.waitForTimeout(1200)

  const hidden = await geometry(page, 'a')

  if (growWhileAway) {
    await page.click('#grow-20')
    await page.waitForTimeout(900)
  }

  await page.click('[data-tab="a"]')
  await page.waitForTimeout(700)
  const early = await geometry(page, 'a')

  await page.waitForTimeout(6000)
  const back = await geometry(page, 'a')

  const verdict = back.fromBottom <= 8 ? 'at the bottom ✓' : `${back.fromBottom}px up ✗ (a few messages)`

  console.log(`\n── ${label}`)
  console.log(`   parked at the bottom     fromBottom=${parked.fromBottom} height=${parked.height}`)
  console.log(`   while hidden             a=${hidden.panes.find(p => p.key === 'a')?.height}px  jump="${hidden.jump ?? '-'}"`)
  console.log(`   first paint on return    fromBottom=${early.fromBottom} height=${early.height} jump="${early.jump ?? '-'}"`)
  console.log(`   after settling           fromBottom=${back.fromBottom} height=${back.height} jump="${back.jump ?? '-'}"`)
  console.log(`   verdict                  ${verdict}`)

  await page.close()
}

await runCase({ label: 'a) nothing arrives while away', growWhileAway: false, pages: 0 })
await runCase({ label: 'b) output arrives while away (repair card)', growWhileAway: true, pages: 0 })
await runCase({ label: 'c) deep session, away and back', growWhileAway: false, pages: 4 })

// d) The reported shape: the reader is following the tail, and while the transcript is GROWING the
//    viewport sits a few dozen pixels off the bottom (the follow loop lags the growth by a frame).
//    That lag is what the app samples and saves on the way out. Restoring it as a deliberate
//    OFFSET lands the reader just outside the library's at-bottom zone, so the follow disarms and
//    every later re-render of the same content (the PS5 repair card emitting progress) drifts them
//    further up — "a few messages up, even though the agent is stopped."
{
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })

  await page.goto(URL, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-key="a"] [data-slot="aui_thread-viewport"]', { timeout: 30_000 })
  await page.waitForTimeout(1200)

  for (let pull = 0; pull < 3; pull += 1) {
    await page.click('#deep')
    await page.waitForTimeout(600)
  }

  await page.click('#stream')
  await page.waitForTimeout(300)

  // The reader is following the tail, and the follow loop lags the growth by a frame: at the instant
  // the tab is left (the moment the app samples and saves the position) the viewport sits ~24px off
  // the bottom. The nudge and the switch are one burst, because the library catches up about a frame
  // later — in the real app the same lag is produced continuously by the card re-rendering, and the
  // trace shows it being sampled as `{fromBottom: 16.9..24.2, kind: 'offset'}` with `settled: true`.
  await page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 6000 }))
    viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight
    viewport.dispatchEvent(new Event('scroll'))
  })
  await page.waitForTimeout(500)
  await page.evaluate(() => {
    const viewport = document.querySelector('[data-key="a"] [data-slot="aui_thread-viewport"]')
    viewport.scrollTop = viewport.scrollHeight - viewport.clientHeight - 24
    viewport.dispatchEvent(new Event('scroll'))
  })
  const lag = await geometry(page, 'a')

  await page.click('[data-tab="b"]')
  await page.waitForTimeout(1200)
  await page.click('[data-tab="a"]')
  await page.waitForTimeout(1500)
  const landed = await geometry(page, 'a')

  // The same content re-renders (progress lines on an unchanged message) and the transcript grows.
  await page.click('#grow-20')
  await page.waitForTimeout(4000)
  const drifted = await geometry(page, 'a')

  console.log('\n── d) saved while following the tail, 24px off (the growth lag)')
  console.log(`   saved state              fromBottom=${lag.fromBottom} (the app's 8px rule calls this an OFFSET)`)
  console.log(`   on return                fromBottom=${landed.fromBottom} height=${landed.height} jump="${landed.jump ?? '-'}"`)
  console.log(`   after the card grows     fromBottom=${drifted.fromBottom} height=${drifted.height} jump="${drifted.jump ?? '-'}"`)
  console.log(
    `   verdict                  ${drifted.fromBottom <= 8 ? 'still at the bottom ✓' : `drifted ${drifted.fromBottom}px up — the follow disarmed ✗`}`
  )

  await page.close()
}

await browser.close()
