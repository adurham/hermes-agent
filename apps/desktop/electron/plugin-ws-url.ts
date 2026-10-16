import type { mintLocalGatewayTicket } from './local-gateway';
import { type GatewayEndpoint, localGatewayDial } from './local-gateway'

/** What the renderer's `pluginSocket` asks for: its plugin namespace, the path under it, and the
 *  (connectionId, profile) route every other plugin call resolves through. */
export interface PluginWsUrlRequest {
  connectionId?: unknown
  path?: unknown
  pluginId?: unknown
  profile?: unknown
}

interface ResolvedBackend {
  baseUrl: string
  gatewayEndpoint?: GatewayEndpoint
}

/** A path segment as the server reads it (decoded once), or null when it is not valid encoding. */
function decodedSegment(segment: string): null | string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return null
  }
}

/** `/api/plugins/<pluginId><path>` as the dial will carry it, or throw. The namespace is the
 *  boundary: the renderer's own check is not trusted here, so a traversal or another core route can
 *  never get a grant. Segments are checked decoded (`%2e%2e`, `%2f`, `%5c`, and `%25` double
 *  encoding are refused), and the result is what the URL parser makes of it. */
export function pluginWsPath(pluginId: unknown, path: unknown): string {
  if (typeof pluginId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(pluginId) || pluginId.includes('..')) {
    throw new Error('Invalid plugin id')
  }

  const prefix = `/api/plugins/${pluginId}/`

  const url = typeof path === 'string' && path.startsWith('/') && path.length <= 2048 && !/[#\\]/.test(path)
    && !path.split('?', 1)[0].split('/').some(segment => {
      const decoded = decodedSegment(segment)

      return decoded === null || decoded === '.' || decoded === '..' || /[/\\%]/.test(decoded)
    })
    ? new URL(`${prefix.slice(0, -1)}${path}`, 'ws://plugin.invalid')
    : null

  if (!url?.pathname.startsWith(prefix)) {
    throw new Error('Invalid plugin socket path')
  }

  return `${url.pathname}${url.search}`
}

/** A fresh one-use plugin socket URL for a local gateway, null for any other backend (those keep
 *  the renderer's token path). The grant is the `native-http` one the plugin's REST calls already
 *  carry, minted for the routed profile and attached by `prepare` to that one dial as a header.
 *  The route resolves exactly as `hermes:gateway:ws-url(-for)` does: registry-scoped when the
 *  renderer names a connection, else the profile-keyed local pool. */
export function createPluginWsUrlHandler(deps: {
  ensureBackend: (profile: unknown) => Promise<ResolvedBackend>
  ensureRegistryBackend: (connectionId: unknown, profile: unknown) => Promise<ResolvedBackend>
  forgetLocalGatewayDescriptor: (profile: unknown) => Promise<void> | void
  forgetRegistryLocalGatewayDescriptor: (connectionId: unknown, profile: unknown) => Promise<void> | void
  launchHome: string
  mint?: typeof mintLocalGatewayTicket
  prepare: (origin: string, path: string, ticket: string, webContentsId: number) => string
}) {
  return async (request: PluginWsUrlRequest, webContentsId: number): Promise<null | string> => {
    const path = pluginWsPath(request?.pluginId, request?.path)
    const { connectionId, profile } = request
    const ensure = () => (connectionId ? deps.ensureRegistryBackend(connectionId, profile) : deps.ensureBackend(profile))

    if (!(await ensure()).gatewayEndpoint) {
      return null
    }

    return localGatewayDial({
      ensure,
      forget: () => (connectionId ? deps.forgetRegistryLocalGatewayDescriptor(connectionId, profile) : deps.forgetLocalGatewayDescriptor(profile)),
      launchHome: deps.launchHome,
      mint: deps.mint,
      prepare: (baseUrl, ticket) => deps.prepare(baseUrl, path, ticket, webContentsId),
      profile: typeof profile === 'string' ? profile : '',
      purpose: 'native-http'
    })
  }
}
