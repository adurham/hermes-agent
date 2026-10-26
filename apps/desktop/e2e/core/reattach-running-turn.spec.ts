/**
 * C2 core: a window that re-attaches to a turn that is still running.
 *
 * The turn persists its narrated tool round before the tool runs, so the
 * history a re-attaching window fetches already holds that round while the
 * live stream keeps drawing the same segment (#127288, #127665). Each step
 * leaves the chat mid-turn by a different road (switch away and back, reload,
 * a second switch during the running tool) and holds the settled turn to the
 * transcript oracle, whose in-page sampler also fails a transient double.
 */

import fs from 'node:fs'
import path from 'node:path'

import { type ElectronApplication, expect, type Page, test } from '@playwright/test'

import {
  coreAppEnv,
  type CoreSandbox,
  createCoreSandbox,
  currentSessionId,
  launchCoreApp,
  recordWebSockets,
  send,
  waitForInteractive,
  writeProviderHome,
  type WsRecorder
} from './harness'
import { assertTranscriptOracle, installDuplicateSampler, type OracleTarget } from './oracle'
import { shellQuote } from './process-census'
import { gate, type ScriptedProvider, startScriptedProvider } from './provider'

const nonce = Math.random()
  .toString(36)
  .slice(2, 8)
  .replace(/[^a-z0-9]/g, 'x')
  .padEnd(4, 'q')

const U = (n: number) => `U${n}-${nonce}`
const A = (n: number) => `A${n}-${nonce}`
const AI = (n: number) => `A${n}i-${nonce}`

const words = (marker: string, ...rest: string[]) => [
  `${marker} `,
  ...rest.map((w, i) => (i === rest.length - 1 ? w : `${w} `))
]

const echo = (tag: string) => ({ name: 'terminal', args: { command: `echo core-${tag}` } })

function viewport(page: Page) {
  return page.locator('[data-slot="aui_thread-viewport"]').filter({ visible: true }).first()
}

function stopButton(page: Page) {
  return page.locator('[data-slot="composer-root"] button[aria-label="Stop"]')
}

async function settled(page: Page, ws: WsRecorder, marker: string) {
  await expect
    .poll(() => ws.events.some(e => e.type === 'message.complete' && String(e.payload?.text ?? '').includes(marker)), {
      timeout: 120_000,
      message: `message.complete for ${marker}`
    })
    .toBe(true)
  await expect(stopButton(page)).toHaveCount(0, { timeout: 30_000 })
}

async function openSession(page: Page, sessionId: string, waitFor: string) {
  await page.evaluate(id => {
    window.location.hash = id ? `#/${id}` : '#/'
  }, sessionId)
  await expect.poll(() => currentSessionId(page)).toBe(sessionId)

  if (waitFor) {
    await expect(viewport(page)).toContainText(waitFor, { timeout: 30_000 })
  }
}

async function withApp(
  label: string,
  body: (ctx: {
    app: ElectronApplication
    page: Page
    provider: ScriptedProvider
    sandbox: CoreSandbox
    ws: WsRecorder
  }) => Promise<void>
) {
  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox(label)
  writeProviderHome(sandbox.hermesHome, provider.url)
  const { app, page } = await launchCoreApp(coreAppEnv(sandbox))
  const ws = recordWebSockets(page)

  try {
    await waitForInteractive(app, page)
    await installDuplicateSampler(page)
    await body({ app, page, provider, sandbox, ws })
  } finally {
    await app.close().catch(() => undefined)
    await provider.close()
    sandbox.cleanup()
  }
}

test('re-attaching to a running tool turn renders each segment once', async () => {
  await withApp('reattach', async ({ page, provider, sandbox, ws }) => {
    const session: OracleTarget = { sessionId: '', expectUserMarkers: [] }

    await test.step('switch away and back while the answer after a tool streams', async () => {
      const hold = gate()
      provider.script(U(1), [
        { text: words(AI(1), 'narrating', 'before', 'the', 'tool'), toolCalls: [echo('reattach-a')] },
        { text: words(A(1), 'answer', 'after', 'the', 'tool'), holdAfterFirstChunk: hold }
      ])
      await send(page, `${U(1)} narrate then answer`, 'Enter', ws)
      await provider.streamStarted(U(1), 1)
      await expect(viewport(page)).toContainText(A(1))
      session.sessionId = await currentSessionId(page)
      session.expectUserMarkers.push(U(1))
      await openSession(page, '', '')
      await openSession(page, session.sessionId, AI(1))
      hold.open()
      await settled(page, ws, A(1))
      await assertTranscriptOracle(page, ws, provider, session, 're-attach during the answer')
    })

    await test.step('switch away and back while the tool runs', async () => {
      const ready = path.join(sandbox.root, `reattach-running-${nonce}`)
      const release = path.join(sandbox.root, `reattach-release-${nonce}`)
      provider.script(U(2), [
        {
          text: words(AI(2), 'running', 'a', 'slow', 'tool'),
          toolCalls: [
            {
              name: 'terminal',
              args: {
                command: `touch ${shellQuote(ready)}; while [ ! -e ${shellQuote(release)} ]; do sleep 0.05; done; echo core-ran`
              }
            }
          ]
        },
        { text: words(AI(3), 'second', 'round'), toolCalls: [echo('reattach-b')] },
        { text: words(A(2), 'answer', 'after', 'two', 'rounds') }
      ])
      await send(page, `${U(2)} slow tool`, 'Enter', ws)
      await expect.poll(() => fs.existsSync(ready), { timeout: 60_000, message: 'tool started' }).toBe(true)
      session.expectUserMarkers.push(U(2))
      await openSession(page, '', '')
      await openSession(page, session.sessionId, AI(2))
      fs.writeFileSync(release, '')
      await settled(page, ws, A(2))
      await assertTranscriptOracle(page, ws, provider, session, 're-attach during the tool')
    })

    await test.step('reload while the answer after a tool streams', async () => {
      const hold = gate()
      provider.script(U(4), [
        { text: words(AI(4), 'narrating', 'again'), toolCalls: [echo('reattach-c')] },
        { text: words(A(4), 'answer', 'across', 'a', 'reload'), holdAfterFirstChunk: hold }
      ])
      await send(page, `${U(4)} reload mid answer`, 'Enter', ws)
      await provider.streamStarted(U(4), 1)
      await expect(viewport(page)).toContainText(A(4))
      session.expectUserMarkers.push(U(4))
      await page.reload()
      await expect(viewport(page)).toContainText(AI(4), { timeout: 60_000 })
      await installDuplicateSampler(page)
      hold.open()
      await settled(page, ws, A(4))
      await assertTranscriptOracle(page, ws, provider, session, 're-attach after a reload')
    })
  })
})
