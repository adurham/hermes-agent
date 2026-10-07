import { flattenMessageKeys } from '@hermes/shared/i18n'
import { expect, it } from 'vitest'

import { arOverrides } from './ar'
import { deOverrides } from './de'
import type { TranslationOverrides } from './define-locale'
import { en } from './en'
import { esOverrides } from './es'
import { frOverrides } from './fr'
import { jaOverrides } from './ja'
import { ruOverrides } from './ru'
import type { BundledLocale } from './types'
import { zhOverrides } from './zh'
import { zhHantOverrides } from './zh-hant'

// Overlays are partial, so a missing key renders English without failing typecheck
// or catalog-completeness.test.ts (which reads the merged catalog). This reads each
// locale's own overlay. overlay-gaps.json is the generated list of keys still
// missing per locale; regenerate with `npx vitest run src/i18n/overlay-completeness.test.ts -u`.
// A new English key must be translated in every locale, or it lands in that file's diff.
const OVERLAYS = {
  ar: arOverrides,
  de: deOverrides,
  es: esOverrides,
  fr: frOverrides,
  ja: jaOverrides,
  ru: ruOverrides,
  zh: zhOverrides,
  'zh-hant': zhHantOverrides
} satisfies Record<Exclude<BundledLocale, 'en'>, TranslationOverrides>

// `intro` is display-only; intro.test.tsx covers its translated rotation.
const translatable = (tree: TranslationOverrides) => flattenMessageKeys(tree).filter(key => !key.startsWith('intro.'))

it('lists exactly the English keys each locale overlay still lacks', async () => {
  const english = translatable(en)

  const gaps = Object.fromEntries(
    Object.entries(OVERLAYS).map(([locale, overlay]) => {
      const present = new Set(translatable(overlay))

      return [locale, english.filter(key => !present.has(key))]
    })
  )

  await expect(`${JSON.stringify(gaps, null, 2)}\n`).toMatchFileSnapshot('./overlay-gaps.json')
})
