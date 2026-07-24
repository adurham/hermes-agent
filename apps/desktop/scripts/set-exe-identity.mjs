#!/usr/bin/env node
// set-exe-identity.mjs — stamp the Hermes icon + version metadata onto the
// built Hermes.exe using resedit, completely decoupled from electron-builder's
// signing path.
//
// WHY THIS EXISTS
// ---------------
// apps/desktop/package.json sets build.win.signAndEditExecutable=false. That
// flag is load-bearing: turning electron-builder's own exe-editing ON also
// re-enables its signtool step, which fetches winCodeSign-2.6.0.7z, whose
// macOS symlinks crash 7-Zip on non-admin Windows (no Developer Mode = no
// SeCreateSymbolicLinkPrivilege). That is an unfixable dead end — we do NOT
// try to extract winCodeSign.
//
// The cost of disabling signAndEditExecutable is that electron-builder also
// skips its resource-editing step, so the unpacked Hermes.exe keeps the stock
// Electron icon and "Electron" taskbar name. This script restores the icon +
// identity by editing the PE resources directly with `resedit`. resedit is a
// pure-JS PE resource editor (no native binary, no wine/signtool, no certs) —
// it's the same library electron-builder's own app-builder-lib uses
// internally for this exact job (see node_modules/app-builder-lib/out/util/
// resEdit.js), so this script mirrors that implementation rather than
// inventing a new one. It replaced `rcedit` (deprecated upstream, shipped a
// native rcedit.exe run under Wine on macOS/Linux) with zero new footprint —
// resedit is already pulled in transitively via electron-builder.
//
// HOW IT RUNS
// -----------
// Primarily as an electron-builder `afterPack` hook (scripts/after-pack.mjs),
// so EVERY packed build — first install, `hermes desktop`, the installer's
// --update rebuild, or a dev's manual `npm run pack` — gets a branded exe from
// one place. Previously this stamp lived only in install.ps1, so the update
// path (which rebuilds via `hermes desktop --build-only`, never install.ps1)
// shipped a stock "Electron" exe. Keeping it in afterPack closes that gap.
//
// Also runnable standalone for ad-hoc re-stamping:
//   node scripts/set-exe-identity.mjs <path-to-Hermes.exe>
//
// Exits 0 on success, non-zero on failure when run as a CLI. As a hook,
// stampExeIdentity() resolves on success and rejects on failure; the caller
// (after-pack.mjs) swallows the rejection so a stamp failure never fails an
// otherwise-good build (worst case: stock icon, not a broken app).

import { resolve, join } from 'node:path'
import { existsSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'

import { NtExecutable, NtExecutableResource, Resource, Data } from 'resedit'

import { isMain } from './utils.mjs'

// Stamp the Hermes icon + identity onto `exe`. Resolves on success, throws on
// failure. `desktopRoot` defaults to this script's package root so the icon
// resolves regardless of cwd.
async function stampExeIdentity(exe, desktopRoot = resolve(import.meta.dirname, '..')) {
  if (!exe || !existsSync(exe)) {
    throw new Error(`target exe not found: ${exe}`)
  }

  // Icon lives at apps/desktop/assets/icon.ico
  const icon = join(desktopRoot, 'assets', 'icon.ico')
  if (!existsSync(icon)) {
    throw new Error(`icon not found: ${icon}`)
  }

  console.log(`[set-exe-identity] stamping ${exe}`)
  console.log(`[set-exe-identity] icon: ${icon}`)

  const buffer = await readFile(exe)
  const executable = NtExecutable.from(buffer)
  const res = NtExecutableResource.from(executable)

  // Use the exe's existing version resource if present (preserves the numeric
  // file/product version Electron already stamped); only create one from
  // scratch if none exists at all. We deliberately do NOT touch
  // setFileVersion/setProductVersion — this stamp only overlays display
  // strings + icon, matching what the old rcedit call did.
  const viList = Resource.VersionInfo.fromEntries(res.entries)
  const vi = viList.length > 0 ? viList[0] : Resource.VersionInfo.createEmpty()

  // Default to en-US (1033) / codepage 1200 if no language entries exist yet,
  // mirroring rcedit's own fallback behavior.
  const languages = vi.getAllLanguagesForStringValues()
  const lang = languages.length > 0 ? languages[0] : { lang: 1033, codepage: 1200 }

  vi.setStringValues(lang, {
    ProductName: 'Hermes',
    FileDescription: 'Hermes',
    CompanyName: 'Nous Research',
    LegalCopyright: 'Copyright (c) 2026 Nous Research'
  })
  vi.outputToResourceEntries(res.entries)

  const iconBuf = await readFile(icon)
  const iconFile = Data.IconFile.from(iconBuf)
  Resource.IconGroupEntry.replaceIconsForResource(
    res.entries,
    1,
    lang.lang,
    iconFile.icons.map(item => item.data)
  )

  res.outputResource(executable)
  await writeFile(exe, Buffer.from(executable.generate()))

  console.log('[set-exe-identity] done — Hermes icon + identity stamped')
}

export { stampExeIdentity }

// CLI entry point: `node scripts/set-exe-identity.mjs <exe>`.
if (isMain(import.meta.url)) {
  const exe = process.argv[2]
  if (!exe) {
    console.error('[set-exe-identity] usage: set-exe-identity.mjs <path-to-exe>')
    process.exit(2)
  }
  stampExeIdentity(exe).catch(err => {
    console.error(`[set-exe-identity] ${err.message}`)
    process.exit(1)
  })
}
