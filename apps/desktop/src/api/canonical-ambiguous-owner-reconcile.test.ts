import { JsonRpcGatewayClient } from '@hermes/shared'
import { afterEach, expect, test, vi } from 'vitest'

import { HermesGateway } from './client'

afterEach(() => vi.restoreAllMocks())

// Equal stored session IDs under two profiles: a frame with an epoch neither snapshot named
// cannot be routed until an authoritative owner probe answers.
async function equalIdClient(resume: (profile: string) => Promise<unknown>) {
  vi.spyOn(JsonRpcGatewayClient.prototype, 'connect').mockResolvedValue()
  const calls: string[] = []

  vi.spyOn(JsonRpcGatewayClient.prototype, 'request').mockImplementation(async (method, params) => {
    calls.push(`${method}:${params?.profile}`)

    return (method === 'session.resume' ? resume(String(params?.profile)) : { status: 'resolved' }) as never
  })
  const client = new HermesGateway()
  await client.connect('ws://localhost/api/ws?native_dial=fixture&ticket=first')

  return { calls, client, emit: (event: Record<string, unknown>) => (client as any).handleEvent(event) }
}

const snapshot = (profile: string, epoch: string) => ({ session_id: 'same', revision: 1, execution_generation: 2, replay_epoch: epoch, prompts: [], info: { profile_name: profile } })

test('a prompt declined locally is shown again while the authority still lists it', async () => {
  const prompt = { kind: 'approval', prompt_id: 'pending', execution_generation: 2, command: 'build', choices: ['once'] }
  const { calls, client } = await equalIdClient(async () => ({ session_id: 's', revision: 1, execution_generation: 2, prompts: [prompt] }))
  const shown: any[] = []

  client.onRequest(request => {
    shown.push(request)

    if (shown.length === 1) { request.fail(-32603, 'session interrupted') }
  })
  await client.request('session.resume', { session_id: 's' })
  await client.request('session.resume', { session_id: 's' })
  expect(shown).toHaveLength(2)
  expect(calls.filter(call => call.startsWith('approval.respond'))).toHaveLength(0)
  client.close()
})
