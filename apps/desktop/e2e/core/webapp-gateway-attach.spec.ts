/**
 * The browser-hosted Desktop is served by the profile gateway's own listener, exactly like local
 * Electron Desktop talks to that gateway: one process runs every turn and owns the profile's store.
 *
 * `webapp.enabled` in the home's config.yaml turns it on; `hermes webapp` starts the gateway and prints
 * a one-use launch link it got over the gateway's control socket. Proof, end to end with a real browser:
 *  - exactly one gateway serves the sandbox, and the browser's socket is that gateway's own;
 *  - a turn's terminal tool runs under the gateway process (its ancestry);
 *  - the gateway admitted the turn for the browser's sign-in, not the host's native grant;
 *  - reload resumes the chat;
 *  - after the gateway is killed and started again, the same tab is still signed in (the browser
 *    credential is kept in the home) and the next turn works;
 *  - `hermes webapp --sign-out` cuts that tab off.
 *
 * Linux (the /proc ancestry walk); needs the Webapp renderer (`npm run build:webapp`) and a
 * Chromium Playwright can launch (`npx playwright install chromium`, or HERMES_E2E_CHROMIUM).
 */

import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import { createServer } from 'node:net'
import * as path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import { type Browser, chromium, expect, type Page, test } from '@playwright/test'

import {
  backendProcesses,
  composer,
  coreAppEnv,
  type CoreSandbox,
  createCoreSandbox,
  currentSessionId,
  DESKTOP_ROOT,
  recordWebSockets,
  REPO_ROOT,
  sandboxProcesses,
  send,
  storedSessionForMarker,
  writeProviderHome
} from './harness'
import { assertTranscriptOracle, installDuplicateSampler } from './oracle'
import { startScriptedProvider } from './provider'

const nonce = Math.random().toString(36).slice(2, 8).replace(/[^a-z0-9]/g, 'x').padEnd(4, 'q')
const U = (n: number) => `U${n}-webapp${nonce}`
const A = (n: number) => `A${n}-webapp${nonce}`
const AI = (n: number) => `A${n}i-webapp${nonce}`

const PYTHON = process.env.HERMES_E2E_PYTHON || 'python3'
/** The browser's sign-in on a loopback Webapp: the gateway's session credential. */
const BROWSER_LOGIN = 'auth:v1:["session-token","","legacy-token-owner"]'

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      server.close(() => resolve(typeof address === 'object' && address ? address.port : 0))
    })
  })
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0)

    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM'
  }
}

/** `hermes webapp <args>` in the sandbox, as an operator runs it. */
function hermesWebapp(sandbox: CoreSandbox, ...args: string[]): { code: number | null; output: string } {
  const result = spawnSync(PYTHON, ['-m', 'hermes_cli.main', 'webapp', ...args], {
    cwd: REPO_ROOT,
    env: coreAppEnv(sandbox),
    encoding: 'utf8',
    timeout: 180_000
  })

  return { code: result.status, output: `${result.stdout ?? ''}${result.stderr ?? ''}` }
}

/** Starts the gateway if needed and returns a one-use launch link for it. */
function launchLink(sandbox: CoreSandbox): string {
  const { code, output } = hermesWebapp(sandbox, '--no-open', '--skip-build')
  const link = /^\s+(http:\/\/127\.0\.0\.1:\d+\/webapp\/launch#\S+)$/m.exec(output)?.[1]
  expect(code, `hermes webapp --no-open\n${output.replace(/ticket=\S+/g, 'ticket=<redacted>')}`).toBe(0)
  expect(link, 'hermes webapp printed a one-use launch link').toBeTruthy()

  return link!
}

function admissionPrincipals(sandbox: CoreSandbox, sessionId: string): string[] {
  const db = new DatabaseSync(path.join(sandbox.hermesHome, 'state.db'), { readOnly: true })

  try {
    return (
      db.prepare('SELECT DISTINCT principal_id FROM session_admissions WHERE target_session_id = ?').all(sessionId) as {
        principal_id: string
      }[]
    ).map(row => row.principal_id)
  } finally {
    db.close()
  }
}

/** The pids from the tool's own shell up to init, as the tool wrote them. */
function toolAncestry(file: string): number[] {
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8').split(/\s+/).filter(Boolean).map(Number) : []
}

test('the gateway serves the Webapp and runs every turn its browser sends', async () => {
  test.skip(process.platform !== 'linux', 'walks /proc to prove which process ran the tool')
  expect(
    fs.existsSync(path.join(DESKTOP_ROOT, 'dist-webapp', 'index.html')),
    'the Webapp renderer is built (npm run build:webapp)'
  ).toBe(true)

  const provider = await startScriptedProvider()
  const sandbox = createCoreSandbox('webapp')
  writeProviderHome(sandbox.hermesHome, provider.url)
  const port = await freePort()
  fs.appendFileSync(path.join(sandbox.hermesHome, 'config.yaml'), `\nwebapp:\n  enabled: true\n  port: ${port}\n`)
  const ancestry = path.join(sandbox.root, 'tool-ancestry.txt')
  let browser: Browser | null = null

  try {
    const link = launchLink(sandbox)
    expect(new URL(link).port).toBe(String(port))
    browser = await chromium.launch({ executablePath: process.env.HERMES_E2E_CHROMIUM || undefined })
    const page: Page = await (await browser.newContext({ viewport: { width: 1280, height: 860 } })).newPage()
    const ws = recordWebSockets(page)
    await page.goto(link)
    await expect(composer(page)).toBeVisible({ timeout: 180_000 })
    expect(page.url()).not.toContain('ticket=')
    expect(page.url()).not.toContain('hermes-session=')
    await installDuplicateSampler(page)

    // The browser's socket is the gateway's own, on the Webapp's origin.
    await expect
      .poll(() => ws.sockets.some(socket => socket.url.startsWith(`ws://127.0.0.1:${port}/api/ws?native_dial=browser`)))
      .toBe(true)
    await expect.poll(() => backendProcesses(sandbox).length, { message: 'one gateway serves the sandbox' }).toBe(1)
    const [gateway] = backendProcesses(sandbox)

    provider.script(U(1), [
      {
        text: [`${AI(1)} `, 'checking'],
        toolCalls: [
          {
            name: 'terminal',
            args: {
              command:
                `sh -c 'p=$$; while [ "$p" -gt 1 ]; do echo $p; ` +
                `p=$(sed -n "s/^PPid:[[:space:]]*//p" /proc/$p/status); done' > ${ancestry}`
            }
          }
        ]
      },
      { text: [`${A(1)} `, 'ran ', 'on ', 'the ', 'gateway'] }
    ])
    await send(page, `${U(1)} where do you run`, 'Enter', ws)
    await expect
      .poll(() => provider.completions.some(c => c.marker === U(1) && c.step === 1 && c.finished), {
        timeout: 120_000,
        message: `provider finished ${U(1)}`
      })
      .toBe(true)
    await expect.poll(() => currentSessionId(page)).not.toBe('')
    const sessionId = await currentSessionId(page)
    await expect.poll(() => storedSessionForMarker(sandbox, 'default', U(1))).toBe(sessionId)

    // The tool ran inside the gateway, admitted for the browser's sign-in.
    await expect.poll(() => toolAncestry(ancestry)).toContain(gateway.pid)
    expect(admissionPrincipals(sandbox, sessionId)).toEqual([BROWSER_LOGIN])
    await assertTranscriptOracle(page, ws, provider, { sessionId, expectUserMarkers: [U(1)] }, 'webapp turn')

    // Reload resumes the chat on the same gateway.
    await page.reload()
    await expect(composer(page)).toBeVisible({ timeout: 120_000 })
    await expect(page.getByText(A(1)).first()).toBeVisible({ timeout: 60_000 })
    expect(backendProcesses(sandbox).map(proc => proc.pid)).toEqual([gateway.pid])

    // A gateway killed and started again (here by `hermes webapp`, whose new link this tab never
    // uses): the same tab is still signed in and the next turn runs on the new gateway.
    process.kill(gateway.pid, 'SIGKILL')
    await expect.poll(() => alive(gateway.pid), { timeout: 30_000 }).toBe(false)
    launchLink(sandbox)
    await page.reload()
    await expect(composer(page)).toBeVisible({ timeout: 120_000 })
    provider.script(U(2), [{ text: [`${A(2)} `, 'after ', 'the ', 'gateway ', 'came ', 'back'] }])
    await send(page, `${U(2)} still there`, 'Enter', ws)
    await expect
      .poll(() => provider.completions.some(c => c.marker === U(2) && c.finished), {
        timeout: 150_000,
        message: `provider finished ${U(2)} on the restarted gateway`
      })
      .toBe(true)
    const [replacement] = backendProcesses(sandbox)
    expect(backendProcesses(sandbox)).toHaveLength(1)
    expect(replacement.pid).not.toBe(gateway.pid)

    // Signing out replaces the browser credential: the tab loses access.
    const signedOut = hermesWebapp(sandbox, '--sign-out')
    expect(signedOut.code, signedOut.output).toBe(0)
    await page.reload()
    await expect(composer(page)).toBeHidden({ timeout: 60_000 })
  } finally {
    await browser?.close().catch(() => undefined)

    for (const proc of sandboxProcesses(sandbox)) {
      try {
        process.kill(proc.pid, 'SIGKILL')
      } catch {
        /* already gone */
      }
    }

    await provider.close()
    sandbox.cleanup()
  }
})
