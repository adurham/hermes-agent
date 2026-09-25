import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { NtExecutable, NtExecutableResource, Resource } from 'resedit'

import { stampExeIdentity } from './set-exe-identity.mjs'

// A 1x1 32-bit ICO — the smallest icon resedit's IconFile parser accepts.
// (A placeholder like the string "icon" is not a valid ICO and makes the
// stamp fail deep inside the icon parse instead of testing the stamp.)
const TINY_ICO_HEX =
  '0000010001000101000001002000300000001600000028000000010000000200000001002000' +
  '0000000004000000000000000000000000000000000000000000000000000000'

function makeDesktopRoot({ icon = true, exe = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-exe-identity-'))
  if (icon) {
    fs.mkdirSync(path.join(root, 'assets'))
    fs.writeFileSync(path.join(root, 'assets', 'icon.ico'), Buffer.from(TINY_ICO_HEX, 'hex'))
  }
  const exePath = path.join(root, 'Hermes.exe')
  if (exe) {
    // A real (minimal) PE image: the script parses and rewrites the PE, so a
    // bytes-shaped stub like "exe" is not a usable fixture.
    fs.writeFileSync(exePath, Buffer.from(NtExecutable.createEmpty().generate()))
  }
  return { exe: exePath, root }
}

function readIconIds(exePath) {
  const exe = NtExecutable.from(fs.readFileSync(exePath))
  const res = NtExecutableResource.from(exe)
  return Resource.IconGroupEntry.fromEntries(res.entries).flatMap(group =>
    group.icons.map(icon => icon.id)
  )
}

test('stamps the icon and identity strings onto the exe', async () => {
  const { exe, root } = makeDesktopRoot()
  try {
    await stampExeIdentity(exe, root)

    // The result must still be a parseable PE — the stamp rewrites the whole
    // image, so a botched rewrite would surface here as a parse failure.
    const stamped = NtExecutable.from(fs.readFileSync(exe))

    // The identity strings landed in a version resource.
    const res = NtExecutableResource.from(stamped)
    const [vi] = Resource.VersionInfo.fromEntries(res.entries)
    assert.ok(vi, 'expected a version resource after stamping')
    const langs = vi.getAllLanguagesForStringValues()
    assert.ok(langs.length > 0, 'expected at least one language entry')
    const strings = vi.getStringValues(langs[0])
    assert.equal(strings.ProductName, 'Hermes')
    assert.equal(strings.FileDescription, 'Hermes')
    assert.equal(strings.CompanyName, 'Nous Research')

    // And an icon group (id 1, per Resource.IconGroupEntry.replaceIconsForResource).
    assert.ok(readIconIds(exe).length > 0, 'expected an icon group after stamping')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('re-stamping an already-stamped exe is idempotent', async () => {
  const { exe, root } = makeDesktopRoot()
  try {
    await stampExeIdentity(exe, root)
    const first = fs.readFileSync(exe)
    await stampExeIdentity(exe, root)
    const second = fs.readFileSync(exe)

    // Same identity, so a repeat pass must not accumulate resource entries.
    const exe1 = NtExecutable.from(first)
    const exe2 = NtExecutable.from(second)
    const r1 = NtExecutableResource.from(exe1)
    const r2 = NtExecutableResource.from(exe2)
    assert.equal(r2.entries.length, r1.entries.length)
    assert.equal(readIconIds(exe).length, readIconIds(exe).length)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('fails loudly when the target exe is missing', async () => {
  const { root } = makeDesktopRoot({ exe: false })
  const missing = path.join(root, 'Nope.exe')
  try {
    await assert.rejects(stampExeIdentity(missing, root), /target exe not found/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('fails loudly when the icon asset is missing', async () => {
  const { exe, root } = makeDesktopRoot({ icon: false })
  try {
    await assert.rejects(stampExeIdentity(exe, root), /icon not found/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

// NOTE: this file used to exercise a bounded rcedit retry budget (transient
// "Unable to commit changes" / ENOENT spawn failures). That machinery is gone:
// 9d0d09564b replaced the deprecated rcedit dependency with resedit, which
// rebuilds the PE instead of patching it in place, so there is no commit step
// to retry and stampExeIdentity() takes no injection options. Those three
// tests were still driving the removed API long after production moved on,
// which is why they failed (RangeError: Invalid DataView length 64, from
// resedit parsing the "exe" string stub). They are replaced above with tests
// of the behavior production actually has now.
