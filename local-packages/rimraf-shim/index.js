'use strict'

// Shim for the deprecated rimraf@2.6.3 package. Same callable API
// (`rimraf(path, opts, cb)` and `rimraf.sync(path, opts)`), backed by Node's
// built-in fs.rm/fs.rmSync (stable since Node 14.14 -- this repo's engines
// floor is >=20.0.0) instead of the real rimraf + its glob@7/inflight
// dependency tree. Only consumer in this repo is `temp@0.9.4` (used by
// electron-winstaller for Windows Squirrel/NSIS temp-dir cleanup), which
// calls both entry points with a `maxBusyTries` option -- mapped here to
// fs.rm's equivalent `maxRetries`.
const fs = require('fs')

function rimraf(p, opts, cb) {
  if (typeof opts === 'function') {
    cb = opts
    opts = {}
  }
  opts = opts || {}
  fs.rm(p, { recursive: true, force: true, maxRetries: opts.maxBusyTries || 0 }, cb)
}

rimraf.sync = function rimrafSync(p, opts) {
  opts = opts || {}
  fs.rmSync(p, { recursive: true, force: true, maxRetries: opts.maxBusyTries || 0 })
}

module.exports = rimraf
