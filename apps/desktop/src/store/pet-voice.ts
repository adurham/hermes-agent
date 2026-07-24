import { atom } from 'nanostores'

import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'

// Desktop-only pet TTS: mirrors `display.pet.voice_enabled` / `voice_provider`
// in config.yaml (see hermes_cli/config.py DEFAULT_CONFIG) so the Settings
// toggle and any other reader agree on one source of truth, same pattern as
// `voice-prefs.ts`'s `$autoSpeakReplies`.
export const $petVoiceEnabled = atom<boolean>(false)
export const $petVoiceProvider = atom<string>('')

/** Seed the atoms from a loaded config payload (mount / refresh). */
export function applyPetVoiceFromConfig(
  config: { display?: { pet?: { voice_enabled?: unknown; voice_provider?: unknown } | null } | null } | null | undefined
): void {
  $petVoiceEnabled.set(Boolean(config?.display?.pet?.voice_enabled))
  $petVoiceProvider.set(String(config?.display?.pet?.voice_provider ?? ''))
}

/**
 * Flip the pet-voice preference and persist it. Optimistic — the atom updates
 * instantly and reverts if the config write fails. Read-modify-writes the
 * whole record (there's no partial-update endpoint), preserving any other
 * `display.pet.*` keys (scale, slug, roam, etc.) already present.
 */
export async function setPetVoiceEnabled(enabled: boolean): Promise<void> {
  const previous = $petVoiceEnabled.get()

  if (previous === enabled) {
    return
  }

  $petVoiceEnabled.set(enabled)

  try {
    const record = await getHermesConfigRecord()
    const display = record.display && typeof record.display === 'object' ? (record.display as Record<string, unknown>) : {}
    const pet = display.pet && typeof display.pet === 'object' ? (display.pet as Record<string, unknown>) : {}

    await saveHermesConfig({
      ...record,
      display: { ...display, pet: { ...pet, voice_enabled: enabled } }
    })
  } catch (error) {
    $petVoiceEnabled.set(previous)
    throw error
  }
}
