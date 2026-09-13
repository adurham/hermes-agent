import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'vitest'

// ─── fs-open-limit.cjs ─────────────────────────────────────────────
//
// The preload caps concurrent fs.open calls so @electron/osx-sign's
// unbounded walk cannot exhaust the descriptor table (EMFILE during mac
// signing). These tests run REAL node processes against REAL trees with a
// REAL low `ulimit -n`, because the whole contract is "does this survive
// a descriptor limit" — a mocked fs would prove nothing.

const PRELOAD = path.join(import.meta.dirname, 'fs-open-limit.cjs')
const POSIX = process.platform !== 'win32'
const require = createRequire(import.meta.url)
const supplierWalk = pathToFileURL(path.join(path.dirname(require.resolve('@electron/osx-sign')), 'util.js')).href

/** Run `body` in a child node process, optionally with the preload + a soft fd limit. */
function runNode(body, { preload = false, ulimit = null, env = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fsopen-test-'))
  const script = path.join(dir, 'body.mjs')
  fs.writeFileSync(script, body)
  const childEnv = { ...process.env, NODE_OPTIONS: '', ...env }
  delete childEnv.HERMES_FS_OPEN_LIMIT_DEBUG
  if (!Object.hasOwn(env, 'HERMES_FS_OPEN_LIMIT')) delete childEnv.HERMES_FS_OPEN_LIMIT
  const nodeArgs = preload ? ['--require', PRELOAD, script] : [script]
  // ulimit needs a shell; keep the arg vector explicit either way.
  const cmd = ulimit
    ? `ulimit -n ${ulimit} || exit; exec "$0" ${nodeArgs.map((a) => `'${a}'`).join(' ')}`
    : null
  const res = cmd
    ? spawnSync('/bin/sh', ['-c', cmd, process.execPath], { encoding: 'utf8', env: childEnv, timeout: 15000 })
    : spawnSync(process.execPath, nodeArgs, { encoding: 'utf8', env: childEnv, timeout: 15000 })
  fs.rmSync(dir, { recursive: true, force: true })
  return res
}

/** Build a tree big enough to exhaust a low fd limit, like the payload does. */
function mkTree(fileCount) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fsopen-tree-'))
  for (let i = 0; i < fileCount; i++) {
    const d = path.join(root, `pkg${i % 40}`, `mod${i % 15}`)
    fs.mkdirSync(d, { recursive: true })
    fs.writeFileSync(path.join(d, `f${i}.py`), '# python\n')
  }
  return root
}

// Import the installed supplier: a lookalike walk cannot prove that its
// isbinaryfile dependency's actual descriptor lifetime is protected.
const WALK_BODY = (root) => `
import fs from 'node:fs'
import path from 'node:path'
const root = ${JSON.stringify(root)}
const framework = path.join(root, 'Child.app/Contents/Demo.framework')
fs.mkdirSync(framework, { recursive: true })
fs.writeFileSync(path.join(framework, 'Demo'), Buffer.from([0xcf, 0xfa, 0xed, 0xfe, 0]))
fs.writeFileSync(path.join(root, 'old.cstemp'), 'temporary signature')
const link = path.join(root, 'framework-link')
if (!fs.existsSync(link)) fs.symlinkSync(framework, link)

let live = 0, peak = 0, opens = 0
const open = fs.open, close = fs.close
fs.open = function (...args) {
  const callback = args.pop()
  return open.call(fs, ...args, (error, fd) => {
    if (!error) { live++; opens++; peak = Math.max(peak, live) }
    callback(error, fd)
  })
}
fs.close = function (fd, callback) {
  return close.call(fs, fd, error => {
    if (!error) live--
    callback(error)
  })
}
const { walk } = await import(${JSON.stringify(supplierWalk)})
const selected = await walk(root)
// isbinaryfile resolves before its close callback; drain without a timed sleep.
while (live) await new Promise(resolve => setImmediate(resolve))
console.log(JSON.stringify({ selected: selected.map(p => path.relative(root, p)), opens, peak, live,
  tempRemoved: !fs.existsSync(path.join(root, 'old.cstemp')) }))
`

test.skipIf(!POSIX)('the real supplier walk survives a low fd limit without changing signing candidates', () => {
  const tree = mkTree(3000)
  try {
    const unbounded = runNode(WALK_BODY(tree), { ulimit: 64 })
    assert.notEqual(unbounded.status, 0)
    assert.match(unbounded.stderr, /EMFILE/)
    const reference = runNode(WALK_BODY(tree), { ulimit: 4096 })
    const limited = runNode(WALK_BODY(tree), { preload: true, ulimit: 64 })
    assert.equal(reference.status, 0, reference.error?.message || reference.stderr)
    assert.equal(limited.status, 0, limited.error?.message || limited.stderr)
    const before = JSON.parse(reference.stdout)
    const after = JSON.parse(limited.stdout)
    assert.deepEqual(after.selected, before.selected)
    assert.deepEqual(after.selected, [
      'Child.app/Contents/Demo.framework/Demo', 'Child.app/Contents/Demo.framework', 'Child.app',
    ])
    assert.equal(after.opens, 3001)
    assert.equal(after.live, 0)
    assert.equal(after.tempRemoved, true)
    assert.ok(after.peak < 64, JSON.stringify(after))
    assert.ok(before.peak > after.peak, JSON.stringify({ before, after }))
  } finally {
    fs.rmSync(tree, { recursive: true, force: true })
  }
})

test.skipIf(!POSIX)('both fs.open forms are queued, so a huge fan-out survives', () => {
  // isbinaryfile uses promisify(fs.open); other packaging callers may use
  // fs.promises.open. Both public interfaces remain covered by the preload.
  //
  // Each iteration opens AND CLOSES before the next needs a descriptor,
  // which is the shape the cap can actually satisfy. (Holding 400
  // descriptors live at once is impossible under a 64 fd limit no matter
  // how the opens are scheduled — an earlier version of this test asked
  // for exactly that and failed for its own reasons, not the patch's.)
  const body = `
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'peak-'))
for (let i = 0; i < 400; i++) fs.writeFileSync(path.join(dir, 'f' + i), 'x')

// promise form: 400 concurrent open+close cycles.
await Promise.all(Array.from({length: 400}, async (_, i) => {
  const fh = await fs.promises.open(path.join(dir, 'f' + i), 'r')
  await fh.close()
}))

// callback form: same fan-out, through promisify like isbinaryfile does.
import { promisify } from 'node:util'
const openAsync = promisify(fs.open)
const closeAsync = promisify(fs.close)
await Promise.all(Array.from({length: 400}, async (_, i) => {
  const fd = await openAsync(path.join(dir, 'f' + i), 'r')
  await closeAsync(fd)
}))

fs.rmSync(dir, { recursive: true, force: true })
console.log('BOTH_FORMS_OK')
`
  const res = runNode(body, { preload: true, ulimit: 64 })
  assert.equal(res.status, 0, `failed: ${res.stderr.slice(0, 400)}`)
  assert.match(res.stdout, /BOTH_FORMS_OK/)
})

test.skipIf(!POSIX)('the callback fan-out DOES fail without the preload', () => {
  // Guards the test above: if 400 open+close cycles were survivable
  // unpatched, it would prove nothing about the queue.
  const body = `
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'
const openAsync = promisify(fs.open)
const closeAsync = promisify(fs.close)
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'peak-raw-'))
for (let i = 0; i < 400; i++) fs.writeFileSync(path.join(dir, 'f' + i), 'x')
await Promise.all(Array.from({length: 400}, async (_, i) => {
  const fd = await openAsync(path.join(dir, 'f' + i), 'r')
  await closeAsync(fd)
}))
console.log('UNEXPECTEDLY_OK')
`
  const res = runNode(body, { ulimit: 64 })
  assert.notEqual(res.status, 0, 'unpatched fan-out should exhaust the fd table')
  assert.match(res.stderr, /EMFILE/)
})

test('open errors still reach the caller unchanged', () => {
  const body = `
import fs from 'node:fs'
let promiseErr = null, cbErr = null
try { await fs.promises.open('/definitely/not/here', 'r') } catch (e) { promiseErr = e }
await new Promise((res) => fs.open('/definitely/not/here', 'r', (e) => { cbErr = e; res() }))
if (promiseErr?.code !== 'ENOENT') throw new Error('promise form lost the error: ' + promiseErr?.code)
if (cbErr?.code !== 'ENOENT') throw new Error('callback form lost the error: ' + cbErr?.code)
console.log('ERRORS_PROPAGATE')
`
  const res = runNode(body, { preload: true })
  assert.equal(res.status, 0, res.stderr.slice(0, 400))
  assert.match(res.stdout, /ERRORS_PROPAGATE/)
})

test('a rejected open releases its slot instead of wedging the queue', () => {
  // If the failure path forgot to release, the queue drains to zero
  // permanently and this hangs rather than failing.
  const body = `
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rej-'))
fs.writeFileSync(path.join(dir, 'real'), 'x')
// 300 failures (more than the cap of 100) then a real open.
await Promise.all(Array.from({length: 300}, () =>
  fs.promises.open(path.join(dir, 'missing'), 'r').catch(() => null)))
const fh = await fs.promises.open(path.join(dir, 'real'), 'r')
await fh.close()
fs.rmSync(dir, { recursive: true, force: true })
console.log('QUEUE_STILL_LIVE')
`
  const res = runNode(body, { preload: true })
  assert.equal(res.status, 0, res.stderr.slice(0, 400))
  assert.match(res.stdout, /QUEUE_STILL_LIVE/)
})

test.skipIf(!POSIX)('holding descriptors while opening more cannot deadlock', () => {
  // The slot is released when open() SETTLES, not when the fd closes, so a
  // task holding many descriptors occupies no slots. Gating the fd lifetime
  // instead deadlocks — that is the design this asserts against.
  const body = `
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hold-'))
for (let i = 0; i < 600; i++) fs.writeFileSync(path.join(dir, 'f' + i), 'x')
// 200 tasks (> the cap of 100) each holding one fd, then asking for two more.
const held = []
await Promise.all(Array.from({length: 200}, (_, i) => (async () => {
  const a = await fs.promises.open(path.join(dir, 'f' + i), 'r')
  const b = await fs.promises.open(path.join(dir, 'f' + (i + 200)), 'r')
  const c = await fs.promises.open(path.join(dir, 'f' + (i + 400)), 'r')
  held.push(a, b, c)
})()))
for (const h of held) await h.close()
fs.rmSync(dir, { recursive: true, force: true })
console.log('NO_DEADLOCK')
`
  const res = runNode(body, { preload: true, ulimit: 1024 })
  assert.equal(res.status, 0, res.stderr.slice(0, 400))
  assert.match(res.stdout, /NO_DEADLOCK/)
})

test('HERMES_FS_OPEN_LIMIT=0 disables the patch', () => {
  // An escape hatch that must genuinely leave fs.open alone.
  const body = `
import fs from 'node:fs'
if (fs.open.name !== 'open') throw new Error('unexpected name')
if (globalThis.__hermesFsOpenLimited) throw new Error('patch installed despite limit 0')
console.log('DISABLED_OK')
`
  const res = runNode(body, { preload: true, env: { HERMES_FS_OPEN_LIMIT: '0' } })
  assert.equal(res.status, 0, res.stderr.slice(0, 400))
  assert.match(res.stdout, /DISABLED_OK/)
})
