import { mkdtempSync, mkdirSync, existsSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { expect, test, vi } from 'vitest'
import notarize, { runCommand } from '../apps/desktop/scripts/notarize.mjs'

const submissionId = '00000000-0000-4000-8000-000000000001'
const missingTicket = () => Object.assign(new Error('CloudKit query for Hermes.app failed due to "Record not found".\nThe staple and validate action failed! Error 65.'), { code: 65 })

function fixture() {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-notary-test-'))
  mkdirSync(path.join(root, 'Hermes.app'))
  const key = path.join(root, 'api-key.p8')
  writeFileSync(key, 'test credential path only')
  return {
    root,
    context: { electronPlatformName: 'darwin', appOutDir: root, packager: { appInfo: { productFilename: 'Hermes' } } },
    environments: [
      { APPLE_NOTARY_PROFILE: 'test-profile' },
      { APPLE_API_KEY: key, APPLE_API_KEY_ID: 'test-key', APPLE_API_ISSUER: 'test-issuer' }
    ],
    cleanup: () => rmSync(root, { recursive: true, force: true })
  }
}

test('accepted submissions retry only ticket propagation, without resubmitting or rebuilding', async () => {
  const f = fixture()
  try {
    for (const env of f.environments) {
      const calls = []
      let attempts = 0
      const run = vi.fn(async (command, args) => {
        calls.push([command, ...args])
        if (command === 'ditto') writeFileSync(args.at(-1), 'archive fixture')
        if (args[0] === 'notarytool') return { stdout: JSON.stringify({ id: submissionId, status: 'Accepted' }) }
        if (args[0] === 'stapler' && ++attempts <= 2) {
          // Exercise exit-code and output capture with a real child process.
          return runCommand(process.execPath, ['-e',
            'process.stdout.write(process.argv[1]); process.exitCode = 65', missingTicket().message])
        }
        return { stdout: '', stderr: '' }
      })
      const sleep = vi.fn(async () => {})
      const log = vi.fn()
      await notarize(f.context, { run, sleep, log, env })
      expect(calls.filter(c => c[0] === 'ditto')).toHaveLength(1)
      const submissions = calls.filter(c => c[1] === 'notarytool' && c[2] === 'submit')
      expect(submissions).toHaveLength(1)
      expect(submissions[0]).toEqual(expect.arrayContaining(['--wait', '--output-format', 'json']))
      expect(submissions[0]).toEqual(expect.arrayContaining(env.APPLE_NOTARY_PROFILE
        ? ['--keychain-profile', env.APPLE_NOTARY_PROFILE]
        : ['--key', env.APPLE_API_KEY, '--key-id', env.APPLE_API_KEY_ID, '--issuer', env.APPLE_API_ISSUER]))
      expect(attempts).toBe(3)
      expect(sleep.mock.calls.map(([ms]) => ms)).toEqual([15000, 30000])
      expect(log).toHaveBeenCalledWith(expect.stringContaining(`${submissionId}: Accepted`))
      expect(existsSync(path.join(f.root, 'Hermes.zip'))).toBe(false)
    }
  } finally {
    f.cleanup()
  }
})

test('rejections, unknown failures and exhausted propagation retries remain build failures', async () => {
  const f = fixture()
  try {
    for (const scenario of ['invalid-zero', 'invalid-nonzero', 'malformed', 'auth', 'permanent-staple', 'exhausted']) {
      const calls = []
      const run = vi.fn(async (command, args) => {
        calls.push([command, ...args])
        if (command === 'ditto') writeFileSync(args.at(-1), 'archive fixture')
        if (args[0] === 'notarytool' && args[1] === 'log') return { stdout: '{"issues":[{"message":"signature rejected"}]}' }
        if (args[0] === 'notarytool') {
          if (scenario === 'auth') throw new Error('authentication failed')
          if (scenario === 'malformed') return { stdout: 'not JSON' }
          const stdout = JSON.stringify({ id: submissionId, status: scenario.startsWith('invalid') ? 'Invalid' : 'Accepted' })
          if (scenario === 'invalid-nonzero') throw Object.assign(new Error('submit failed'), { code: 1, stdout })
          return { stdout }
        }
        if (args[0] === 'stapler') {
          if (scenario === 'permanent-staple') throw Object.assign(new Error('invalid bundle format'), { code: 65 })
          throw missingTicket()
        }
        return { stdout: '', stderr: '' }
      })
      const sleep = vi.fn(async () => {})
      const failure = await notarize(f.context, { run, sleep, log: vi.fn(), env: f.environments[0] }).catch(e => e)
      expect(failure).toBeInstanceOf(Error)
      expect(calls.filter(c => c[2] === 'submit')).toHaveLength(1)
      const staples = calls.filter(c => c[1] === 'stapler')
      if (scenario.startsWith('invalid')) {
        expect(failure.message).toContain('Invalid')
        expect(failure.message).toContain('signature rejected')
        expect(calls.filter(c => c[2] === 'log')).toHaveLength(1)
        expect(staples).toHaveLength(0)
      } else if (scenario === 'exhausted') {
        expect(failure.message).toContain('Record not found')
        expect(staples.length).toBe(sleep.mock.calls.length + 1)
        expect(staples.length).toBeGreaterThan(1)
        expect(staples.length).toBeLessThanOrEqual(6)
      } else {
        expect(staples).toHaveLength(scenario === 'permanent-staple' ? 1 : 0)
      }
      if (scenario !== 'exhausted') expect(sleep).not.toHaveBeenCalled()
      expect(existsSync(path.join(f.root, 'Hermes.zip'))).toBe(false)
    }
  } finally {
    f.cleanup()
  }
})
