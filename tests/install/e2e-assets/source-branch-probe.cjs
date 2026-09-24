// Test-only preload for the unpublished Git main used by source-install E2E.
// The shipped source checker accepts --branch main, but Desktop's automatic
// check normally resolves the published main channel. Intercept only that
// subprocess invocation; the actual checker, UI, and updater still run.
const childProcess = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')
const { syncBuiltinESMExports } = require('node:module')

function sourceProbeGit(userData, realGit, stagedUrl, platform = process.platform) {
  if (!/^file:\/\/[^\s]+$/.test(stagedUrl)) throw new Error('source check must use a staged file:// Git origin')
  const file = path.join(userData, platform === 'win32' ? 'source-probe-git.cmd' : 'source-probe-git.sh')
  const args = [
    `url.${stagedUrl}.insteadOf=https://github.com/NousResearch/hermes-agent.git`,
    `url.${stagedUrl}.insteadOf=git@github.com:NousResearch/hermes-agent.git`,
  ]
  if (platform === 'win32') {
    const quote = value => `"${value.replace(/"/g, '""')}"`
    fs.writeFileSync(file, `@echo off\r\n${quote(realGit)} ${args.map(value => `-c ${quote(value)}`).join(' ')} %*\r\n`)
  } else {
    const quote = value => `'${value.replace(/'/g, "'\\''")}'`
    fs.writeFileSync(file, `#!/bin/sh\nexec ${quote(realGit)} ${args.map(value => `-c ${quote(value)}`).join(' ')} "$@"\n`, { mode: 0o700 })
  }
  return file
}

function prepareSourceBranchEnvironment(root, expectedSha, realGit, capturedEnv, launchEnv) {
  if (!realGit || !path.isAbsolute(realGit) || !fs.existsSync(realGit)) {
    throw new Error('source app-update requires HERMES_E2E_REAL_GIT for staged main')
  }
  const install = path.resolve(root)
  const staged = childProcess.execFileSync(realGit, ['-C', install, 'remote', 'get-url', 'origin'], {
    encoding: 'utf8', env: capturedEnv,
  }).trim()
  if (!/^file:\/\/[^\s]+$/.test(staged)) throw new Error('source check must use a staged file:// Git origin')
  const advertised = childProcess.execFileSync(realGit, ['ls-remote', '--heads', staged, 'refs/heads/main'], {
    encoding: 'utf8', env: capturedEnv,
  }).trim().split(/\s+/)[0]
  if (advertised !== expectedSha) {
    throw new Error(`staged Git main ${advertised} does not match expected ${expectedSha}`)
  }
  launchEnv.HERMES_E2E_SOURCE_ROOT = install
  launchEnv.HERMES_E2E_SOURCE_GIT = sourceProbeGit(launchEnv.HERMES_DESKTOP_USER_DATA_DIR, realGit, staged)
  launchEnv.NODE_OPTIONS = `--require=${JSON.stringify(__filename)}`
}

function branchProbeArgs(args, root, realGit) {
  if (!root || !realGit || !Array.isArray(args)) return args
  // PM .cmd launchers are passed to cmd.exe as one screened command string.
  // Limit the test-only rewrite to that exact shape; keep every other cmd call intact.
  if (args.length === 5 && args.slice(0, 4).join(' ') === '/d /v:off /s /c') {
    const command = args[4]
    if (
      typeof command !== 'string' ||
      /["%&|<>^\r\n]/.test(realGit) ||
      !/^""[^"\r\n]+\.cmd" "--run-module" "hermes_cli\.source_check" /i.test(command) ||
      !command.includes(`"--install-root" "${root}"`) ||
      command.includes('"--branch"') ||
      !command.endsWith('"') ||
      [...command.matchAll(/"--git" "[^"\r\n]+"/g)].length !== 1
    ) return args
    const selected = command.slice(0, -1).replace(/"--git" "[^"\r\n]+"/, `"--git" "${realGit}"`) + ' "--branch" "main""'
    return [...args.slice(0, 4), selected]
  }
  const legacy = args[0] === '-c' && args[1]?.includes('runpy.run_path(str(p))')
    && args[1]?.includes('hermes_cli/source_check.py')
  const managed = args[0] === '--run-module' && args[1] === 'hermes_cli.source_check'
  if (!(legacy || managed)
      || args[args.indexOf('--install-root') + 1] !== root
      || !args.includes('--git') || args.includes('--branch')) return args
  const selected = [...args]
  selected[selected.indexOf('--git') + 1] = realGit
  return [...selected, '--branch', 'main']
}

if (process.env.HERMES_E2E_SOURCE_ROOT && process.env.HERMES_E2E_SOURCE_GIT) {
  const original = childProcess.execFile
  const select = args => {
    const selected = branchProbeArgs(args, process.env.HERMES_E2E_SOURCE_ROOT, process.env.HERMES_E2E_SOURCE_GIT)
    if (selected !== args) process.stderr.write('[source-branch-probe] checking staged Git main explicitly\n')
    return selected
  }
  childProcess.execFile = function (file, args, options, callback) {
    return original.call(this, file, select(args), options, callback)
  }
  const custom = Symbol.for('nodejs.util.promisify.custom')
  childProcess.execFile[custom] = (file, args, options) => original[custom](file, select(args), options)
  syncBuiltinESMExports()
}

module.exports = { branchProbeArgs, prepareSourceBranchEnvironment, sourceProbeGit }