import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, it, vi } from 'vitest'

import { run } from '../.github/actions/setup-pm/prune/index.mjs'

const directories = []
afterEach(() => {
  for (const path of directories.splice(0)) rmSync(path, { recursive: true, force: true })
})

it('prunes the saved uv cache at teardown, never during registration', () => {
  const directory = mkdtempSync(join(tmpdir(), 'pm-post-'))
  directories.push(directory)
  const state = join(directory, 'state')
  const execute = vi.fn(() => ({ status: 0 }))
  const cache = join(directory, 'cache with spaces')
  const uv = join(directory, 'locked uv')
  run({ GITHUB_STATE: state, INPUT_UV: uv, INPUT_CACHE: cache }, execute)
  expect(execute).not.toHaveBeenCalled()
  const saved = Object.fromEntries(readFileSync(state, 'utf8').trim().split('\n').map(line => {
    const index = line.indexOf('=')
    return [`STATE_${line.slice(0, index)}`, line.slice(index + 1)]
  }))
  run({ ...saved, UV_CACHE_DIR: 'a later unrelated cache' }, execute)
  expect(execute).toHaveBeenCalledWith(uv, ['cache', 'prune', '--ci', '--force'], expect.objectContaining({
    env: expect.objectContaining({ UV_CACHE_DIR: cache }),
    stdio: 'inherit',
  }))
  execute.mockReturnValueOnce({ status: 1 })
  expect(() => run(saved, execute)).toThrow('uv cache prune failed')
})
