import type { Dialog, MessageBoxOptions, MessageBoxReturnValue } from 'electron'

import type { RetirementWorkspaceConflict } from './retirement-receiver'
import type { RetirementWorkspaceChoice } from './retirement-state'

export interface RetirementDialogHost {
  ready: () => Promise<void>
  showMessageBox: Dialog['showMessageBox']
}

/** The destination confirms its current workspace, not a stale source-side guess. */
export async function confirmRetirementWorkspace(
  conflict: RetirementWorkspaceConflict,
  host: RetirementDialogHost
): Promise<RetirementWorkspaceChoice | null> {
  const choices: (RetirementWorkspaceChoice | null)[] = [null, 'keep-stable']
  const buttons: string[] = ['Cancel migration', 'Keep stable workspace']

  if (conflict.canOpenPreview) {
    choices.push('open-preview')
    buttons.push('Open preview workspace')
  }

  const options: MessageBoxOptions = {
    type: 'question',
    title: 'Choose the workspace for stable',
    message: 'Stable and the preview have different workspaces.',
    detail: [
      `Stable: ${conflict.current.home}\nProfile: ${conflict.current.profile}\nConnection: ${conflict.current.connectionId}`,
      `Preview: ${conflict.requested.home}\nProfile: ${conflict.requested.profile}\nConnection: ${conflict.requested.connectionId}`,
      'Both workspaces will be preserved. This does not merge or copy their data.',
      conflict.canOpenPreview ? '' : 'To open the preview workspace, close stable and remove any explicit home override, then retry.'
    ].filter(Boolean).join('\n\n'),
    buttons,
    defaultId: 0,
    cancelId: 0,
    noLink: true
  }
  await host.ready()

  const result: MessageBoxReturnValue = await host.showMessageBox(options)

  return choices[result.response] ?? null
}
