#!/usr/bin/env node
/**
 * Generate the icon assets on demand for the pipeline that needs them.
 *
 * Generated icons are NOT committed (see scripts/generate_icons.py) — every
 * consuming pipeline regenerates them before building:
 *   - website:  website/scripts/prebuild.mjs (docusaurus prebuild)
 *   - desktop:  apps/desktop/package.json prebuild + predev
 *   - installer: apps/bootstrap-installer/package.json prebuild
 *   - web:       web/package.json prebuild
 *
 * Finds the project venv python (resvg-py rides the dev extra) and runs the
 * generator from the repo root. Fails loudly if the toolchain is missing so
 * a build can never silently ship without icons.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function findPython() {
  const candidates = [
    path.join(repoRoot, '.venv', 'Scripts', 'python.exe'), // windows
    path.join(repoRoot, '.venv', 'bin', 'python'), // posix
    path.join(repoRoot, 'venv', 'Scripts', 'python.exe'),
    path.join(repoRoot, 'venv', 'bin', 'python'),
  ]
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  return null
}

function main() {
  const python = findPython()
  const generator = path.join(repoRoot, 'scripts', 'generate_icons.py')
  let result
  if (python) {
    result = spawnSync(python, [generator], { cwd: repoRoot, stdio: 'inherit' })
  } else if (process.env.VIRTUAL_ENV) {
    const py = process.platform === 'win32' ? 'python.exe' : 'python'
    result = spawnSync(py, [generator], { cwd: repoRoot, stdio: 'inherit', env: process.env })
  } else {
    // Fall back to uv-managed python (CI installs the dev extra).
    result = spawnSync('uv', ['run', 'python', generator], { cwd: repoRoot, stdio: 'inherit' })
  }
  if (result.error) {
    console.error('[generate-icons] failed to launch icon generator:', result.error.message)
    console.error('[generate-icons] install the dev extra with: uv sync --extra dev')
    process.exit(1)
  }
  process.exit(result.status ?? 1)
}

main()
