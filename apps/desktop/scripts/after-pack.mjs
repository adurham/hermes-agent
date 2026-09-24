/**
 * after-pack.mjs — electron-builder afterPack hook.
 *
 * Stamps the Hermes icon + identity onto the packed Windows Hermes.exe via
 * resedit (delegated to set-exe-identity.mjs). This runs for EVERY packed
 * build — first install, `hermes desktop`, the installer's --update rebuild,
 * and a dev's manual `npm run pack` — so the branded exe can never silently
 * revert to the stock "Electron" icon/name (the bug when the stamp lived only
 * in install.ps1, which the update path doesn't use).
 *
 * Windows-only: resedit edits PE resources, irrelevant on macOS/Linux where the
 * app identity comes from the bundle Info.plist / desktop entry. Best-effort:
 * a stamp failure must never fail an otherwise-good build (worst case is the
 * stock icon, not a broken app), so we log and resolve rather than throw.
 *
 * On macOS this hook ALSO restores the empty app-level localizations dropped
 * during Electron extraction (upstream v2026.9.24). Run after language
 * filtering and before signing; the markers are derived from the packaged
 * framework, not the host's Electron (which may be another version).
 *
 * electron-builder passes a context with:
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - appOutDir:            the unpacked app directory for this target
 *   - packager.appInfo.productFilename: the exe basename (e.g. 'Hermes')
 *   - packager.getResourcesDir / getMacOsElectronFrameworkResourcesDir
 */

import { mkdir, readdir } from 'node:fs/promises'
import path from 'node:path'

import { stampExeIdentity } from './set-exe-identity.mjs'

export default async function afterPack({ appOutDir, electronPlatformName, packager }) {
  if (electronPlatformName === 'win32') {
    const productName = packager?.appInfo?.productFilename || 'Hermes'
    const exe = path.join(appOutDir, `${productName}.exe`)
    const desktopRoot = path.resolve(import.meta.dirname, '..')

    try {
      await stampExeIdentity(exe, desktopRoot)
    } catch (err) {
      // Never fail the build over a cosmetic stamp.
      console.warn(`[after-pack] exe identity stamp failed (${err.message}); Hermes.exe keeps the stock Electron icon`)
    }

    return
  }

  if (electronPlatformName !== 'darwin') {
    return
  }

  try {
    const resources = packager.getResourcesDir(appOutDir)
    const framework = packager.getMacOsElectronFrameworkResourcesDir(appOutDir)
    const entries = await readdir(framework, { withFileTypes: true })
    // Chromium also ships grammatical-gender packs; these are not macOS locales.
    const locales = entries.filter(
      entry =>
        entry.isDirectory() && entry.name.endsWith('.lproj') && !/_(FEMININE|MASCULINE|NEUTER)\.lproj$/.test(entry.name)
    )
    await Promise.all(locales.map(entry => mkdir(path.join(resources, entry.name), { recursive: true })))
  } catch (error) {
    // Keep an otherwise usable package, but make failed locale restoration visible.
    console.warn(
      `[after-pack] macOS locale markers were not restored: ${error instanceof Error ? error.message : String(error)}`
    )
  }
}
