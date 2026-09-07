'use strict'

// apps/desktop/update-feed.cjs — the ONE source of truth for the Darwin
// (macOS) electron-updater feed layout, shared by the desktop runtime
// (generic provider base URL) and the release pipeline
// (scripts/r2-release.mjs finalize). Pure CJS: requireable from both the
// app bundle and ESM scripts via createRequire. No dependencies.
//
//   darwinFeed('stable')          → { directory: 'releases/darwin/stable',
//                                     channel: 'stable',
//                                     fileName: 'stable-mac.yml',
//                                     allowPrerelease: false }
//   darwinFeed('canary', true)    → { directory: 'releases/darwin/light/canary',
//                                     channel: 'canary',
//                                     fileName: 'canary-mac.yml',
//                                     allowPrerelease: true }
//
// A client composes its feed URL as PUBLIC_URL + '/' + feed.directory +
// '/' + feed.fileName; the producer publishes the manifest at exactly that
// key. There is no placeholder default URL — the caller supplies the base.

const CHANNELS = ['stable', 'canary']

function darwinFeed(channel, light = false) {
  if (!CHANNELS.includes(channel)) {
    throw new TypeError(`darwinFeed: unknown channel ${JSON.stringify(channel)} (expected stable|canary)`)
  }
  return {
    directory: light ? `releases/darwin/light/${channel}` : `releases/darwin/${channel}`,
    channel,
    fileName: `${channel}-mac.yml`,
    allowPrerelease: channel === 'canary',
  }
}

module.exports = { darwinFeed }
