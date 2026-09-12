import { execFileSync } from 'node:child_process'

import { hiddenWindowsChildOptions } from '../windows-child-options'

interface StateDbPreflight {
  python: string | null
  script: string
  home: string
  log: (message: string) => void
}

// Synchronous by design: the caller must not stop the backend before the snapshot.
export function preflightStateDb({ python, script, home, log }: StateDbPreflight): void {
  if (!python) {
    log('[updates] state.db pre-flight unavailable: Python not found')

    return
  }

  try {
    const result: string = execFileSync(
      python,
      ['-I', '-S', script, home],
      hiddenWindowsChildOptions({ encoding: 'utf8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] })
    )

    log(`[updates] state.db pre-flight: ${result.trim()}`)
  } catch (error: unknown) {
    log(`[updates] state.db pre-flight failed: ${error instanceof Error ? error.message : String(error)}`)
  }
}
