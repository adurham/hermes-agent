// Shared installed-chat/update controls; never launch an unobserved destination.
import fs from 'node:fs'
import path from 'node:path'
import { _electron } from '@playwright/test'
import { runUpdateWindowChat } from './update-window-chat.mjs'
import { pickAppWindow, openAbout, waitForUpdate } from './update-ui.cjs'
import { prepareWindowForInput } from './window-input.cjs'
import { observeProcessClose } from './process-close.cjs'

/** @typedef {{exe: string, resources: string, userData: string, commit: string}} Installed */
/** @typedef {{app: import('@playwright/test').ElectronApplication, page: import('@playwright/test').Page,
 * closed: (timeout?: number) => Promise<void>, installed: Installed}} Window */

/** @param {Installed} installed @param {NodeJS.ProcessEnv} env @returns {Promise<Window>} */
export async function launch(installed, env) {
  const app = await _electron.launch({ executablePath: installed.exe, cwd: env.HOME, env, timeout: 120_000 })
  const closed = observeProcessClose(app.process())
  const page = await pickAppWindow(app, console.log)
  await prepareWindowForInput(app, page)
  return { app, page, closed, installed }
}

/** @param {Window} window @param {string} mockUrl @param {string} out */
export async function chat(window, mockUrl, out) {
  const { app, page, installed } = window
  const close = page.getByRole('button', { name: 'Close settings', exact: true })
  if (await close.isVisible()) await close.click()
  // Assertion input only: the launched app receives no userData/home override.
  process.env.HERMES_DESKTOP_USER_DATA_DIR = installed.userData
  await runUpdateWindowChat(app, page, { mockUrl, outDir: out, expectCommit: installed.commit,
    origin: 'bundled', executable: installed.exe, root: path.join(installed.resources, 'agent-payload') })
  return JSON.parse(fs.readFileSync(path.join(out, 'desktop-chat-old.json'), 'utf8')).prompt
}

/** @param {Window} window @param {'update'|'retire'} action @param {string} out */
export async function transition(window, action, out) {
  const { app, page } = window
  const controls = { log: console.log, shot: (page, name) => page.screenshot({ path: path.join(out, name + '.png') }),
    prepare: () => prepareWindowForInput(app, page) }
  await openAbout(page, controls)
  if (action === 'update') {
    const button = await waitForUpdate(page, controls)
    await button.click()
  } else {
    const consent = page.getByRole('checkbox', { name: 'Install or update stable and remove this preview only after stable is ready.', exact: true })
    const deadline = Date.now() + 180_000
    while (!await consent.isVisible()) {
      const move = page.getByRole('button', { name: 'Move to stable', exact: true })
      if (await move.isVisible()) await move.click()
      else {
        const check = page.getByRole('button', { name: 'Check now', exact: true })
        if (await check.isVisible()) await check.click()
      }
      if (Date.now() > deadline) throw new Error('Retirement consent unavailable')
      await page.waitForTimeout(500)
    }
    if (await consent.isChecked()) throw new Error('Consent unexpectedly preselected')
    await page.getByRole('radio', { name: 'Open this preview workspace in stable', exact: true }).check()
    await consent.check()
    await page.getByRole('button', { name: 'Move to stable', exact: true }).click()
  }
  await window.closed(900_000)
}

/** @param {Window} window */
export async function close(window) { await window.app.close(); await window.closed(60_000) }
