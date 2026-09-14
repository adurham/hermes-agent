import { closeSync, fsyncSync, openSync, readFileSync } from 'node:fs'
import path from 'node:path'

import {
  type ConnectionRegistry, migrateV1ToRegistry, normalizeConnectionInput, normalizeRegistry, type RegistryConnection,
  setPrimaryConnection, upsertConnection
} from '../connection-registry'
import { resolveDesktopRemoteRoute } from '../desktop-remote-route'
import { writeSecretFileAtomic } from '../hardening'

/** Explicit allowlist: credentials, proxy headers and opaque keychain envelopes never transfer. */
export interface RetirementConnection {
  kind: RegistryConnection['kind']
  label: string
  url?: string
  authMode?: 'oauth' | 'token'
  host?: string
  user?: string
  port?: number
  keyPath?: string
  remoteHermesPath?: string
  remoteProfile?: string
  org?: string
  name?: string
}

export interface RetirementConnectionSelection {
  id: string
  settings?: RetirementConnection
}

function retirementEndpointKey(connection: Pick<RetirementConnection, 'host' | 'kind' | 'port' | 'remoteProfile' | 'url' | 'user'>): string {
  if (connection.kind === 'ssh') {
    return `ssh:${(connection.user || '').toLowerCase()}@${(connection.host || '').toLowerCase()}:${connection.port ?? 22}::${(connection.remoteProfile || '').trim()}`
  }
  return `url:${String(connection.url || '').trim().replace(/\/+$/, '').toLowerCase()}`
}

export function retirementConnection(entry: RegistryConnection): RetirementConnection {
  if (entry.url) {
    const url: URL = new URL(entry.url)
    if (url.username || url.password) { throw new Error('Remove URL credentials and use saved authentication before migration') }
  }
  return { kind: entry.kind, label: entry.label, url: entry.url, authMode: entry.authMode,
    host: entry.host, user: entry.user, port: entry.port, keyPath: entry.keyPath,
    remoteHermesPath: entry.remoteHermesPath, remoteProfile: entry.remoteProfile, org: entry.org, name: entry.name }
}

interface StoredConnectionConfig {
  mode?: string
  profiles?: { [profile: string]: { mode?: string } }
}

function readConfig(file: string): StoredConnectionConfig | null {
  try { return JSON.parse(readFileSync(file, 'utf8')) }
  catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') { return null }
    throw error
  }
}

interface StoredConnections {
  registry: ConnectionRegistry
  config: StoredConnectionConfig
}

function readConnections(userData: string): StoredConnections {
  const config: StoredConnectionConfig = readConfig(path.join(userData, 'connection.json')) ?? {}
  const stored: StoredConnectionConfig | null = readConfig(path.join(userData, 'connections.json'))
  const registry: ConnectionRegistry = stored ? normalizeRegistry(stored) : migrateV1ToRegistry(config)
  if (registry.quarantined?.length) { throw new Error('Repair the saved connections before migration; original settings were preserved') }
  return { registry, config }
}

export function retirementSelectedConnection(userData: string, profile: string): string {
  const { registry, config } = readConnections(userData)
  const route = resolveDesktopRemoteRoute({ registry, config, profile })
  const id: string = route ? route.connectionId ?? '' : 'local'
  if (!id || (registry.launchMode === 'last-used' && registry.lastUsed !== id)) {
    throw new Error('Select a primary saved connection before migration; the launch route is ambiguous')
  }
  return id
}

export function assertRetirementConnection(userData: string, id: string, connection: RetirementConnection): void {
  const existing: RegistryConnection | undefined = readConnections(userData).registry.connections.find(entry => entry.id === id)
  if (!existing || JSON.stringify(retirementConnection(existing)) !== JSON.stringify(connection)) {
    throw new Error('Stable connection changed during migration; review the selection again')
  }
}

/** Resolve collisions before the receiver crosses its forward-only boundary. */
export function prepareRetirementConnection(userData: string, id: string, connection: RetirementConnection): RetirementConnectionSelection {
  // Reuse the allowlist guard so an embedded-credential URL can never start adoption.
  retirementConnection({ id, ...connection })
  if (connection.kind === 'local') { return { id: 'local' } }
  const { registry } = readConnections(userData)
  const key = retirementEndpointKey(connection)
  const same: RegistryConnection[] = registry.connections.filter((entry: RegistryConnection): boolean =>
    entry.kind !== 'local' && retirementEndpointKey(entry) === key)
  if (same.length > 1) { throw new Error('Multiple stable connections already point at the preview endpoint; select one in stable before migration') }
  if (same.length === 1) { return { id: same[0].id, settings: retirementConnection(same[0]) } }
  // The ordinary registry validator owns label and endpoint collision rules.
  normalizeConnectionInput({ ...connection, id }, registry)
  return { id, settings: connection }
}

/** Stable's existing credentials win when an identical endpoint is already registered. */
export function adoptRetirementConnection(userData: string, id: string, profile: string, connection?: RetirementConnection): void {
  const { registry, config } = readConnections(userData)
  const current: string = retirementSelectedConnection(userData, profile)

  if (current !== id && ((config.mode && config.mode !== 'local') ||
      (config.profiles?.[profile]?.mode && config.profiles[profile].mode !== 'local'))) {
    throw new Error('Stable has a legacy connection override. Keep its workspace, or select Local in stable and retry; saved connections remain intact')
  }
  let next: ConnectionRegistry = registry
  if (connection) {
    const existing: RegistryConnection | undefined = registry.connections.find(entry => entry.id === id)
    if (existing) {
      assertRetirementConnection(userData, id, connection)
    } else {
      next = upsertConnection(registry, normalizeConnectionInput({ ...connection, id }, registry))
    }
  }
  if (current === id) { return }
  next = { ...setPrimaryConnection(next, id), lastUsed: id, launchMode: 'primary' }
  const file: string = path.join(userData, 'connections.json')
  writeSecretFileAtomic(file, JSON.stringify(next, null, 2))
  const handle: number = openSync(file, 'r+')
  try { fsyncSync(handle) } finally { closeSync(handle) }
  if (process.platform !== 'win32') {
    const directory: number = openSync(userData, 'r')
    try { fsyncSync(directory) } finally { closeSync(directory) }
  }
  if (retirementSelectedConnection(userData, profile) !== id) { throw new Error('Connection adoption did not persist') }
}