import { describe, expect, it, vi } from 'vitest'

import { type CatalogRequest, lookupPluginCatalogEntry } from './plugin-catalog'

const SHA = 'a'.repeat(40)

const replying =
  (reply: unknown): CatalogRequest =>
  async <T>() =>
    reply as T

const noFetch = vi.fn(async () => {
  throw new Error('the renderer must not fetch the docs host when the backend answers')
}) as unknown as typeof fetch

describe('lookupPluginCatalogEntry', () => {
  it('asks the backend, not the docs host (its 301 has no CORS header)', async () => {
    const calls: [string, Record<string, unknown>][] = []
    const found = { name: 'aihubmix', repo: 'https://github.com/AIhubmix/mono', sha: SHA, subdir: 'aihubmix' }

    const request: CatalogRequest = async <T>(method: string, params: Record<string, unknown>) => {
      calls.push([method, params])

      return { status: 'found', entry: found } as T
    }

    await expect(lookupPluginCatalogEntry(' aihubmix ', { fetchImpl: noFetch, request })).resolves.toEqual({
      ok: true,
      entry: { name: 'aihubmix', repo: 'https://github.com/AIhubmix/mono', sha: SHA, subdir: 'aihubmix' }
    })
    expect(calls).toEqual([['plugins.catalog.lookup', { name: 'aihubmix' }]])
    expect(noFetch).not.toHaveBeenCalled()
  })

  it.each([
    [{ status: 'unknown' }, 'unknown'],
    [{ status: 'invalid_name' }, 'invalid_name'],
    [{ status: 'unavailable' }, 'unavailable'],
    [{ status: 'found', entry: { name: 'x' } }, 'unavailable'],
    [{ status: 'something-new' }, 'unavailable'],
    [null, 'unavailable'],
    [undefined, 'unavailable']
  ] as const)('backend %j → %s, with no fetch', async (reply, error) => {
    await expect(
      lookupPluginCatalogEntry('weather', { fetchImpl: noFetch, request: replying(reply) })
    ).resolves.toEqual({
      ok: false,
      error
    })
  })

  it('refuses a non-catalog name before asking anyone', async () => {
    const asked: string[] = []

    const request: CatalogRequest = async <T>(method: string) => {
      asked.push(method)

      return { status: 'found' } as T
    }

    await expect(lookupPluginCatalogEntry('../etc', { fetchImpl: noFetch, request })).resolves.toEqual({
      ok: false,
      error: 'invalid_name'
    })
    expect(asked).toEqual([])
  })

  it('falls back to the direct feed only when the backend cannot answer', async () => {
    const feed = [{ name: 'weather', repo: 'https://github.com/x/weather', sha: SHA, subdir: '' }]
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify(feed))) as unknown as typeof fetch

    const request: CatalogRequest = async () => {
      throw new Error('method not found: plugins.catalog.lookup')
    }

    await expect(lookupPluginCatalogEntry('weather', { fetchImpl, request })).resolves.toMatchObject({
      ok: true,
      entry: { name: 'weather', repo: 'https://github.com/x/weather' }
    })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
})
