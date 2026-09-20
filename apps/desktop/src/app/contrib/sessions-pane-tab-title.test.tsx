/**
 * The Sessions sidebar pane registers at module import, before `I18nProvider`
 * has fetched `display.language`, so its string `title` is whatever English
 * `translateNow` sampled then. The strip label must come from `tabTitle`,
 * which subscribes to the live locale.
 */
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'

const { registry } = await import('@/contrib/registry')

await import('./controller')

describe('the Sessions pane tab label', () => {
  it('follows the live locale instead of the register-time title', () => {
    const sessions = registry.getArea('panes').find(c => c.id === 'sessions')!
    const tabTitle = (sessions.data as { tabTitle: () => ReactNode }).tabTitle
    const inLocale = (locale: string) =>
      renderToStaticMarkup(
        <I18nProvider configClient={null} initialLocale={locale}>
          {tabTitle()}
        </I18nProvider>
      )

    expect(inLocale('ru')).toBe('Сеансы')
    expect(inLocale('en')).toBe('Sessions')
  })
})
