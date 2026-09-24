import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { afterEach, expect, test, vi } from 'vitest'
import updateUi from '../tests/install/e2e-assets/update-ui.cjs'
import sourceBranchProbe from '../tests/install/e2e-assets/source-branch-probe.cjs'

const windows = []
afterEach(() => {
  windows.splice(0).forEach(window => window.close())
  vi.useRealTimers()
})

function fixture({ details = true, available = true, statusOverride } = {}) {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(0)
  const { window } = new JSDOM('<body></body>', { runScripts: 'outside-only' })
  windows.push(window)
  const { document } = window
  const clicks = []
  const addButton = (text, onClick) => {
    const button = document.createElement('button')
    button.textContent = text
    button.onclick = () => { clicks.push(text); onClick?.() }
    document.body.append(button)
    return button
  }
  const revealUpdate = () => addButton('Update now')
  const check = addButton('Check now', () => {
    check.disabled = true
    if (!available) return
    if (details) {
      const more = addButton("See what's new", () => { more.remove(); revealUpdate() })
    } else {
      revealUpdate()
    }
  })
  const status = statusOverride || { supported: true, behind: available ? 1 : 0 }
  window.hermesDesktop = { updates: { check: async () => status } }
  const page = {
    getByRole(role, { name }) {
      expect(role).toBe('button')
      const selected = () => [...document.querySelectorAll('button')].find(button => name.test(button.textContent))
      const locator = {
        first: () => locator,
        isVisible: async () => Boolean(selected()),
        click: async () => {
          const button = selected()
          if (!button || button.disabled) throw new Error('button not actionable')
          button.click()
        },
      }
      return locator
    },
    waitForTimeout: async ms => vi.setSystemTime(Date.now() + ms),
    evaluate: async fn => window.eval(`(${fn.toString()})()`),
  }
  return { page, clicks, log: vi.fn(), shot: vi.fn() }
}

test.each([true, false])('reveals the actual update button without applying (details=%s)', async details => {
  const f = fixture({ details })
  const update = await updateUi.waitForUpdate(f.page, f)
  expect(await update.isVisible()).toBe(true)
  expect(f.clicks).toEqual(details ? ['Check now', "See what's new"] : ['Check now'])
  expect(f.shot).toHaveBeenCalledWith(f.page, '04-update-available')
})

test('checks the live Desktop bridge against the staged target before opening About', async () => {
  const sha = 'a'.repeat(40)
  const f = fixture({ statusOverride: { supported: true, branch: 'main', targetSha: sha, updateAvailable: true } })
  await updateUi.assertStagedBranch(f.page, sha, f.log)
  expect(f.log).toHaveBeenCalledWith(expect.stringContaining('"targetSha"'))
  for (const statusOverride of [
    { supported: true, error: 'release-unavailable', branch: 'main' },
    { supported: true, branch: 'main', targetSha: 'b'.repeat(40), updateAvailable: true },
  ]) {
    const refused = fixture({ statusOverride })
    await expect(updateUi.assertStagedBranch(refused.page, sha, refused.log)).rejects.toThrow(/refusing to click/)
  }
})

test('test-only source probe pins staged main without changing other Python invocations', () => {
  const root = '/fixture/install'
  const probe = ['-c', 'from pathlib import Path; import runpy; p = Path("hermes_cli/source_check.py"); entry = runpy.run_path(str(p)).get("main") if p.is_file() else None; entry()', '--install-root', root, '--home', '/fixture/home', '--git', '/shim/git', '--force']
  const expected = [...probe]
  expected[expected.indexOf('--git') + 1] = '/real/git'
  expected.push('--branch', 'main')
  expect(sourceBranchProbe.branchProbeArgs(probe, root, '/real/git')).toEqual(expected)
  expect(sourceBranchProbe.branchProbeArgs(probe, '/other/install', '/real/git')).toBe(probe)
  expect(sourceBranchProbe.branchProbeArgs(['-c', 'print("hermes_cli/source_check.py")'], root, '/real/git')).toEqual(['-c', 'print("hermes_cli/source_check.py")'])
  expect(sourceBranchProbe.branchProbeArgs([...probe, '--branch', 'topic'], root, '/real/git')).toEqual([...probe, '--branch', 'topic'])
  expect(sourceBranchProbe.branchProbeArgs(probe, root, '')).toBe(probe)
})

test.skipIf(process.platform === 'win32')('probe Git reaches the staged main even with global Git config isolated', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-staged-git-'))
  const git = process.env.HERMES_E2E_REAL_GIT || process.env.PATH.split(path.delimiter)
    .map(dir => path.join(dir, process.platform === 'win32' ? 'git.exe' : 'git')).find(file => fs.existsSync(file))
  try {
    const checkout = path.join(root, 'checkout')
    const bare = path.join(root, 'serve.git')
    fs.mkdirSync(checkout)
    const run = (args, cwd = checkout, env = process.env) => execFileSync(git, args, { cwd, env, encoding: 'utf8' }).trim()
    run(['init', '-b', 'main'])
    run(['-c', 'user.name=Fixture', '-c', 'user.email=e2e@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'base'])
    const base = run(['rev-parse', 'HEAD'])
    run(['-c', 'user.name=Fixture', '-c', 'user.email=e2e@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'staged'])
    const sha = run(['rev-parse', 'HEAD'])
    run(['clone', '--bare', checkout, bare], root)
    run(['reset', '--hard', base])
    run(['remote', 'add', 'origin', 'https://github.com/NousResearch/hermes-agent.git'])
    const cfg = path.join(root, 'gitconfig')
    run(['config', '--file', cfg, '--add', `url.file://${bare}.insteadOf`, 'https://github.com/NousResearch/hermes-agent.git'])
    const capturedEnv = { ...process.env, GIT_CONFIG_GLOBAL: cfg }
    const launchEnv = { HERMES_DESKTOP_USER_DATA_DIR: root }
    sourceBranchProbe.prepareSourceBranchEnvironment(checkout, sha, git, capturedEnv, launchEnv)
    const env = { ...process.env, GIT_CONFIG_GLOBAL: process.platform === 'win32' ? 'NUL' : '/dev/null' }
    const shim = launchEnv.HERMES_E2E_SOURCE_GIT
    expect(execFileSync(shim, ['remote', 'get-url', 'origin'], { cwd: checkout, env, encoding: 'utf8' }).trim()).toBe(`file://${bare}`)
    expect(execFileSync(shim, ['ls-remote', '--heads', 'origin', 'refs/heads/main'], { cwd: checkout, env, encoding: 'utf8' }).split(/\s+/)[0]).toBe(sha)
    const python = process.env.HERMES_PYTHON || 'python3'
    const source = fileURLToPath(new URL('../', import.meta.url))
    const home = path.join(root, 'profile')
    fs.mkdirSync(home)
    const code = 'import json,sys; from pathlib import Path; from hermes_cli.source_check import check_for_updates; print(json.dumps(check_for_updates(install_root=Path(sys.argv[1]), home=Path(sys.argv[2]), branch="main", force=True, git=sys.argv[3])))'
    const status = JSON.parse(execFileSync(python, ['-c', code, checkout, home, shim], {
      cwd: checkout, encoding: 'utf8', env: { ...env, HERMES_HOME: home, GIT_ALLOW_PROTOCOL: 'file', PYTHONPATH: source },
    }))
    expect(status).toMatchObject({ supported: true, currentSha: base, branch: 'main', targetSha: sha, updateAvailable: true })
    expect(() => sourceBranchProbe.prepareSourceBranchEnvironment(checkout, '0'.repeat(40), git, capturedEnv, launchEnv)).toThrow(/does not match expected/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('preloaded Electron-style execFile transports explicit branch into the checker', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-branch-probe-'))
  try {
    fs.mkdirSync(path.join(root, 'hermes_cli'))
    fs.writeFileSync(path.join(root, 'hermes_cli', 'source_check.py'), 'import json, sys\ndef main(): print(json.dumps(sys.argv[1:]))\n')
    const probe = 'from pathlib import Path; import runpy; p = Path("hermes_cli/source_check.py"); entry = runpy.run_path(str(p)).get("main") if p.is_file() else None; entry() if callable(entry) else print("null")'
    const python = process.env.HERMES_PYTHON || 'python3'
    const script = 'const {promisify} = require("node:util"); const {execFile} = require("node:child_process"); promisify(execFile)(process.argv[1], ["-c", process.argv[2], "--install-root", process.argv[3], "--home", process.argv[3], "--git", "fixture-shim", "--force"], {cwd:process.argv[3]}).then(r=>console.log(r.stdout), e=>{console.error(e);process.exitCode=1})'
    const result = spawnSync(process.execPath, ['-e', script, python, probe, root], {
      encoding: 'utf8', env: { ...process.env, HERMES_E2E_SOURCE_ROOT: root, HERMES_E2E_SOURCE_GIT: '/real/git',
        NODE_OPTIONS: `--require=${JSON.stringify(fileURLToPath(new URL('../tests/install/e2e-assets/source-branch-probe.cjs', import.meta.url)))}` },
    })
    expect(result.status, result.stderr).toBe(0)
    expect(JSON.parse(result.stdout)).toEqual(['--install-root', root, '--home', root, '--git', '/real/git', '--force', '--branch', 'main'])
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('does not turn a completed check without an update into success', async () => {
  const f = fixture({ available: false })
  await expect(updateUi.waitForUpdate(f.page, f)).rejects.toThrow(/"Update now" never appeared/)
  expect(f.clicks).toEqual(['Check now'])
  expect(f.log).toHaveBeenCalledWith('[update-status] {"supported":true,"behind":0}')
  expect(f.shot).toHaveBeenCalledWith(f.page, 'ERROR-no-update-now')
})
