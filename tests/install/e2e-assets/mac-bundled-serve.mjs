#!/usr/bin/env node
'use strict'
// mac-bundled-serve.mjs — loopback-only static file server for the macOS
// bundled-update E2E feed. Serves the directory produced by
// mac-bundled-feed.mjs over 127.0.0.1, logs every request to a JSONL file
// (the proof the app really fetched THIS feed), and stays up until killed.
//
// Usage: node mac-bundled-serve.mjs --dir <feed-dir> --port <port> \
//          --log <requests.jsonl> [--health /__healthz]

import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { parseArgs } from 'node:util'

const { values } = parseArgs({
  options: {
    dir: { type: 'string' },
    port: { type: 'string' },
    log: { type: 'string' },
    health: { type: 'string', default: '/__healthz' }
  }
})

const root = path.resolve(values.dir)
if (!fs.existsSync(root)) {
  console.error(`feed dir not found: ${root}`)
  process.exit(1)
}
const logStream = values.log ? fs.createWriteStream(values.log, { flags: 'a' }) : null

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${values.port}`)
  if (logStream) {
    logStream.write(JSON.stringify({ t: Date.now(), path: url.pathname, ua: req.headers['user-agent'] || '' }) + '\n')
  }
  if (url.pathname === values.health) {
    res.writeHead(200, { 'content-type': 'text/plain' })
    res.end('ok\n')
    return
  }
  const target = path.resolve(root, '.' + decodeURIComponent(url.pathname))
  if (!target.startsWith(root) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    res.writeHead(404, { 'content-type': 'text/plain' })
    res.end('not found\n')
    return
  }
  // no-store: the updater must always see the live feed, never a cache.
  res.writeHead(200, {
    'content-type': target.endsWith('.yml') ? 'text/yaml; charset=utf-8' : 'application/zip',
    'content-length': fs.statSync(target).size,
    'cache-control': 'no-store'
  })
  fs.createReadStream(target).pipe(res)
})

// Loopback only, by construction and by policy: the production updater
// accepts loopback HTTP overrides exclusively (mac-client.ts).
server.listen(Number(values.port), '127.0.0.1', () => {
  console.log(`serving ${root} on http://127.0.0.1:${values.port}`)
})
