#!/usr/bin/env node
'use strict'
// mac-bundled-verify.mjs — verify an INSTALLED macOS bundle on the runner:
// real codesign validation, team identity, bundle version, and the baked
// install stamp, against a manifest side from bundle-inputs.json. Parses
// success receipts — file existence is never success.
//
// macOS-only (codesign/plutil). Pure assertion logic lives in
// mac-bundled-manifest.cjs and is covered by tests-js.
//
// Usage:
//   node mac-bundled-verify.mjs verify-app --app <path> \
//     --expect-version 1.2.3 --expect-commit <sha> --expect-tag vX.Y.Z \
//     --expect-identity <team-id> [--out <receipt.json>]

import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { codesignTeam, codesignValid, stampAssertions } = require('./mac-bundled-manifest.cjs')

const [, , command, ...rest] = process.argv

function fail(message) {
  console.error(`E2E ASSERTION FAILED: ${message}`)
  process.exit(1)
}

function verifyApp() {
  const { values } = parseArgs({
    strict: false,
    args: rest,
    options: {
      app: { type: 'string' },
      'expect-version': { type: 'string' },
      'expect-commit': { type: 'string' },
      'expect-tag': { type: 'string' },
      'expect-identity': { type: 'string' },
      out: { type: 'string' },
    },
  })
  const app = values.app
  if (!app || !fs.existsSync(path.join(app, 'Contents', 'Info.plist'))) {
    fail(`not a .app bundle: ${app}`)
  }

  // 1. The signature is valid, intact, and satisfies its own designated
  //    requirement — Squirrel's gate will re-run this at install time.
  try {
    execFileSync('codesign', ['--verify', '--deep', '--strict', '--verbose=2', app], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  } catch (error) {
    fail(`codesign --verify rejected ${app}: ${String(error.stderr || error)}`)
  }

  // 2. The signing team matches the manifest identity. Squirrel.Mac only
  //    accepts an update signed by the SAME team — this assertion is what
  //    makes the pair updatable at all.
  const displayOut = execFileSync('codesign', ['-dv', app], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  const team = codesignTeam(displayOut)
  if (!team) fail(`no TeamIdentifier in codesign -dv output for ${app}`)
  if (team !== values['expect-identity']) {
    fail(`codesign TeamIdentifier ${team} != manifest identity ${values['expect-identity']}`)
  }

  // 3. Bundle version and identifier from the real Info.plist.
  const plist = JSON.parse(execFileSync('plutil', ['-convert', 'json', '-o', '-', path.join(app, 'Contents', 'Info.plist')], { encoding: 'utf8' }))
  const receipt = {
    app,
    teamIdentifier: team,
    bundleVersion: plist.CFBundleShortVersionString,
    bundleIdentifier: plist.CFBundleIdentifier,
    verifiedAt: new Date().toISOString(),
  }
  if (receipt.bundleVersion !== values['expect-version']) {
    fail(`CFBundleShortVersionString ${receipt.bundleVersion} != expected ${values['expect-version']}`)
  }
  if (!String(receipt.bundleIdentifier).startsWith('com.nousresearch.')) {
    fail(`CFBundleIdentifier ${receipt.bundleIdentifier} is not a Hermes product bundle id`)
  }

  // 4. The baked install stamp (provenance: commit, tag, payload, mechanism).
  const stampPath = path.join(app, 'Contents', 'Resources', 'install-stamp.json')
  if (!fs.existsSync(stampPath)) fail(`no install stamp at ${stampPath}`)
  const stamp = JSON.parse(fs.readFileSync(stampPath, 'utf8'))
  receipt.stamp = stamp
  const problems = stampAssertions(stamp, {
    commit: values['expect-commit'],
    tag: values['expect-tag'],
  })
  if (problems.length) fail(`install stamp disagrees: ${problems.join('; ')}`)

  console.log(JSON.stringify(receipt, null, 2))
  if (values.out) fs.writeFileSync(values.out, JSON.stringify(receipt, null, 2) + '\n')
}

switch (command) {
  case 'verify-app': verifyApp(); break
  default:
    console.error(`unknown command: ${command}`)
    process.exit(3)
}
