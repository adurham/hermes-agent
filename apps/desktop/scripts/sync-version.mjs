/**
 * sync-version.mjs — Read the canonical version from hermes_cli/__init__.py
 * and write it into this package.json so electron-builder uses the real
 * Hermes version for the DMG filename, Info.plist, and app.getVersion().
 *
 * Called from the "prebuild" npm script. Best-effort: failures log a warning
 * and never block the build.
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PKG_PATH = resolve(__dirname, '..', 'package.json')
const VERSION_FILE = resolve(__dirname, '..', '..', '..', 'hermes_cli', '__init__.py')

try {
  const raw = readFileSync(VERSION_FILE, 'utf8')
  const match = raw.match(/__version__\s*=\s*"([^"]+)"/)
  if (!match) {
    console.warn('[sync-version] could not parse version from hermes_cli/__init__.py')
    process.exit(0)
  }
  const canonical = match[1]
  const pkg = JSON.parse(readFileSync(PKG_PATH, 'utf8'))
  if (pkg.version === canonical) {
    process.exit(0) // already in sync
  }
  pkg.version = canonical
  writeFileSync(PKG_PATH, JSON.stringify(pkg, null, 2) + '\n', 'utf8')
  console.log(`[sync-version] package.json version → ${canonical}`)
} catch (err) {
  console.warn(`[sync-version] could not sync version: ${err.message}`)
  process.exit(0)
}
