import { useStore } from '@nanostores/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactElement, useState } from 'react'

import { $apiRequestScope } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import {
  getWebAccessStatus,
  restartWebAccess,
  startWebAccess,
  stopWebAccess,
  type WebAccessChange,
  type WebAccessMode,
  type WebAccessStatus
} from '@/hermes'
import { useI18n } from '@/i18n'
import type { WebAccessTranslations } from '@/i18n/types_gateway_host'
import { ExternalLink } from '@/lib/external-link'
import { Globe, Loader2, Network } from '@/lib/icons'
import { isBrowserHostedDesktop } from '@/lib/platform'
import { notify, notifyError } from '@/store/notifications'
import { $connection } from '@/store/session'

import { CONTROL_TEXT } from './constants'
import { ListRow, Pill, SectionHeading, SettingsContent, SettingsSection, SettingsSkeleton } from './primitives'

const START_POLL_MS = 1500
const START_TIMEOUT_MS = 90_000

/**
 * The config says what the gateway serves from its next start; the gateway says what it serves now.
 * `pendingOn`/`pendingOff`: they disagree until the gateway restarts (a refused restart, or one in flight).
 */
type Phase = 'off' | 'pendingOff' | 'pendingOn' | 'serving' | 'unavailable'

type Busy = 'restart' | 'start' | 'stop' | null

/** The gateway serves the configured web app: it applied this config, not an earlier one's port. */
function servesConfig(status: WebAccessStatus): boolean {
  const { state, url } = status.gateway

  return status.enabled && state === 'serving' && url !== null && new URL(url).port === String(status.port)
}

function phaseOf(status: WebAccessStatus): Phase {
  if (!status.enabled) {
    return status.gateway.state === 'serving' ? 'pendingOff' : 'off'
  }

  if (servesConfig(status)) {
    return 'serving'
  }

  return status.gateway.state === 'unavailable' ? 'unavailable' : 'pendingOn'
}

/** Poll until the restarted gateway serves the web app or reports why it can't. */
async function waitForWebApp(): Promise<WebAccessStatus> {
  for (const deadline = Date.now() + START_TIMEOUT_MS; Date.now() < deadline; ) {
    await new Promise(resolve => setTimeout(resolve, START_POLL_MS))
    // The backend answering this may be the gateway being restarted.
    const status = await getWebAccessStatus().catch(() => null)

    if (status && (servesConfig(status) || status.gateway.state === 'unavailable')) {
      return status
    }
  }

  throw new Error('timeout')
}

/** Web access serves THIS machine: the native app on its local connection only. */
export function useWebAccessAvailable(): boolean {
  const connection = useStore($connection)

  return !isBrowserHostedDesktop() && connection?.mode !== 'remote'
}

export function WebAccessSettings(): ReactElement | null {
  const { t } = useI18n()
  const w = t.settings.gateway.webAccess
  const key = ['web-access', JSON.stringify(useStore($apiRequestScope))]
  // A `?tab=web-access` link can reach this pane where the nav hides it: there it neither asks the
  // backend nor shows anything.
  const available = useWebAccessAvailable()

  const { data: status, error } = useQuery({
    enabled: available,
    queryFn: getWebAccessStatus,
    queryKey: key,
    retry: false
  })

  if (!available) {
    return null
  }

  if (!status) {
    return error ? <SettingsContent>{w.failed}</SettingsContent> : <SettingsSkeleton sections={[{ rows: 2 }]} />
  }

  return <WebAccessForm queryKey={key} status={status} w={w} />
}

type FormProps = { queryKey: readonly unknown[]; status: WebAccessStatus; w: WebAccessTranslations }

function WebAccessForm({ queryKey, status, w }: FormProps): ReactElement {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<Busy>(null)
  const [mode, setMode] = useState<WebAccessMode>(status.mode === 'public' ? 'public' : 'lan')
  const [publicUrl, setPublicUrl] = useState(status.public_url)
  const phase = phaseOf(status)
  const lanUrl = status.lan_address ? `http://${status.lan_address}:${status.port}` : null
  const localUrl = status.gateway.url ?? `http://127.0.0.1:${status.port}`

  const url =
    status.mode === 'public' ? status.public_url : status.mode === 'local' ? localUrl : (lanUrl ?? '')

  const ready = status.nous_signed_in && (mode === 'lan' ? Boolean(lanUrl) : publicUrl.trim().startsWith('https://'))

  const apply = (next: WebAccessStatus): void => {
    queryClient.setQueryData(queryKey, next)
  }

  // Saved either way; a refused restart (a chat is running) applies when the gateway next restarts.
  const run = async (kind: NonNullable<Busy>, change: () => Promise<WebAccessChange>) => {
    setBusy(kind)

    try {
      const result = await change()
      apply(result)

      if (!result.restart_started) {
        // Not a failure: the pane now shows the saved change waiting for the restart.
        notify({ kind: 'warning', message: result.restart_busy ? w.restartBusy : w.restartFailed, title: w.restartNeeded })

        return
      }

      if (result.enabled && kind !== 'stop') {
        apply(await waitForWebApp().catch(() => Promise.reject(new Error(w.startTimedOut))))
      }
    } catch (err) {
      notifyError(err, w.failed)
    } finally {
      setBusy(null)
      void queryClient.invalidateQueries({ queryKey })
    }
  }

  const start = () => void run('start', () => startWebAccess(mode, mode === 'public' ? publicUrl.trim() : undefined))
  const stop = () => void run('stop', stopWebAccess)
  // A saved change the gateway has not applied yet: restart it, and nothing else.
  const restart = () => void run('restart', restartWebAccess)

  return (
    <SettingsContent>
      <SectionHeading icon={Network} page title={w.title} />
      <p className="mb-4 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{w.intro}</p>
      <SettingsSection aside={<Pill tone={phase === 'serving' ? 'success' : 'muted'}>{pillLabel(phase, busy, w)}</Pill>} icon={Globe} title={w.title}>
        {/* A web app served on this computer only has no network address to choose until it stops. */}
        {status.mode !== 'local' && (
          <WhereRow
            lanUrl={lanUrl}
            locked={phase !== 'off' || busy !== null}
            mode={mode}
            onMode={setMode}
            onPublicUrl={setPublicUrl}
            port={status.port}
            publicUrl={publicUrl}
            w={w}
          />
        )}
        <ListRow
          action={
            <Button
              disabled={busy !== null || (phase === 'off' && !ready)}
              onClick={phase === 'off' ? start : phase.startsWith('pending') ? restart : stop}
              size="sm"
              variant={phase === 'off' ? 'default' : 'outline'}
            >
              {busy && <Loader2 className="animate-spin" />}
              {phase === 'off' ? w.start : phase.startsWith('pending') ? w.restartGateway : w.stop}
            </Button>
          }
          description={describe(phase, status, w)}
          title={phase === 'serving' ? <ExternalLink href={url} native showExternalIcon>{url}</ExternalLink> : url || w.title}
        />
      </SettingsSection>
    </SettingsContent>
  )
}

type WhereProps = {
  lanUrl: null | string
  locked: boolean
  mode: WebAccessMode
  onMode: (mode: WebAccessMode) => void
  onPublicUrl: (url: string) => void
  port: number
  publicUrl: string
  w: WebAccessTranslations
}

function WhereRow({ lanUrl, locked, mode, onMode, onPublicUrl, port, publicUrl, w }: WhereProps): ReactElement {
  return (
    <ListRow
      action={
        <SegmentedControl
          disabled={locked}
          onChange={onMode}
          options={[
            { id: 'lan', label: w.modeLan },
            { id: 'public', label: w.modePublic }
          ]}
          value={mode}
        />
      }
      below={
        mode === 'public' && (
          <Input
            className={`${CONTROL_TEXT} mt-3 max-w-sm`}
            disabled={locked}
            onChange={event => onPublicUrl(event.target.value)}
            placeholder="https://hermes.example.com"
            value={publicUrl}
          />
        )
      }
      description={mode === 'lan' ? (lanUrl ? w.modeLanDetail(lanUrl) : w.noLan) : w.modePublicDetail(port)}
      title={mode === 'lan' ? w.modeLan : w.modePublic}
    />
  )
}

function pillLabel(phase: Phase, busy: Busy, w: WebAccessTranslations): string {
  const labels: Record<Phase, string> = {
    off: w.stopped,
    pendingOff: w.restartNeeded,
    pendingOn: w.restartNeeded,
    serving: w.running,
    unavailable: w.unavailable
  }

  return busy === 'start' || (busy === 'restart' && phase === 'pendingOn') ? w.starting : labels[phase]
}

function describe(phase: Phase, status: WebAccessStatus, w: WebAccessTranslations): string {
  const descriptions: Record<Phase, string> = {
    off: status.nous_signed_in ? w.notRunningDetail : w.needsNous,
    pendingOff: w.pendingOff,
    pendingOn: w.pendingOn,
    serving: status.mode === 'public' ? w.runningPublic : status.mode === 'local' ? w.runningLocal : w.runningLan,
    unavailable: w.unavailableDetail(status.gateway.reason ?? '')
  }

  return descriptions[phase]
}
