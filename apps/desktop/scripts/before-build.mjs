/**
 * Desktop bundles ship precompiled renderer assets. Returning false here tells
 * electron-builder to skip the node_modules collector/install step, which
 * avoids workspace dependency graph explosions and keeps packaging
 * deterministic across environments. The Hermes Agent Python payload is no
 * longer bundled; the Electron app fetches it at first launch via
 * `install.ps1`'s stage protocol (Windows). See `electron/main.ts`.
 *
 * Also syncs the desktop package.json version from the canonical source
 * (hermes_cli/__init__.py) so the installer DMG filename, Info.plist
 * CFBundleShortVersionString, and app.getVersion() always match the real
 * Hermes version — even when release.py's --bump step was skipped.
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(__dirname, '..', '..', '..')
const DESKTOP_PKG = resolve(__dirname, '..', 'package.json')
const VERSION_FILE = resolve(REPO_ROOT, 'hermes_cli', '__init__.py')

function syncVersionFromCanonicalSource() {
  try {
    const raw = readFileSync(VERSION_FILE, 'utf8')
    const match = raw.match(/__version__\s*=\s*"([^"]+)"/)
    if (!match) {
      console.warn('[before-build] could not parse version from hermes_cli/__init__.py')
      return
    }
    const canonicalVersion = match[1]
    const pkgRaw = readFileSync(DESKTOP_PKG, 'utf8')
    const pkg = JSON.parse(pkgRaw)
    if (pkg.version === canonicalVersion) {
      return // already in sync
    }
    pkg.version = canonicalVersion
    writeFileSync(DESKTOP_PKG, JSON.stringify(pkg, null, 2) + '\n', 'utf8')
    console.log(`[before-build] synced desktop package.json version: ${pkg.version} → ${canonicalVersion}`)
  } catch (err) {
    // Best-effort: a version sync failure must never block the build.
    console.warn(`[before-build] could not sync version from canonical source: ${err.message}`)
  }
}

export default async function beforeBuild() {
  syncVersionFromCanonicalSource()
  return false
}
