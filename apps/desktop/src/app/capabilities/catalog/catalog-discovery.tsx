import './catalog-discovery.css'

import { groupCatalogPlugins, PLUGIN_CATEGORIES } from '@hermes/shared'
import { type ReactNode, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { openExternalLink } from '@/lib/external-link'

import type { CatalogCardVariant } from './catalog-card'
import { type CatalogEntry, type CatalogKind, catalogLabel } from './catalog-data'
import { catalogCategories } from './catalog-query'

// Reviewed catalog identifiers, not an inferred recommendation or download metric.
const FEATURED = { plugins: 'hindsight', skills: 'claude-code' } as const

export function CatalogDiscovery({ entries, kind, card, onCategory, actions }: {
  entries: CatalogEntry[]
  kind: CatalogKind
  card: (entry: CatalogEntry, index: number, variant: CatalogCardVariant) => ReactNode
  onCategory: (category: string) => void
  actions?: ReactNode
}) {
  const { t } = useI18n()
  const [categoryLimit, setCategoryLimit] = useState(8)
  const title = kind === 'plugins' ? t.skills.tabPlugins : t.skills.tabSkills

  const featured = entries.find(entry => entry.identifier === FEATURED[kind])
    ?? entries.find(entry => entry.name === FEATURED[kind] && ['bundled', 'official', 'builtin'].includes(entry.source))

  const remaining = entries.filter(entry => entry !== featured)

  // Plugins with an unlisted category land in `general`, as in every other plugin grouping.
  const sections: [string, string, CatalogEntry[]][] =
    kind === 'plugins'
      ? groupCatalogPlugins(remaining).map(([key, items]) => [key, PLUGIN_CATEGORIES[key].label, items])
      : catalogCategories(remaining, kind).map(([key, meta]) => [
          key,
          meta.label,
          remaining.filter(entry => entry.category === key)
        ])

  return (
    <div className="catalog-discovery">
      {featured && <section data-catalog-section="featured">
        <header className="catalog-section-heading"><h2>{t.catalog.discover} {title}</h2></header>
        {card(featured, 0, 'hero')}
      </section>}
      {sections.slice(0, categoryLimit).map(([category, label, items], sectionIndex) => {
        const variant: CatalogCardVariant = items.slice(0, 4).some(entry => entry.imageUrl) || sectionIndex % 3 === 0 ? 'showcase' : sectionIndex % 3 === 2 ? 'horizontal' : 'compact'

        return <section data-catalog-section={category} key={category}>
          <header className="catalog-section-heading">
            <div><h2>{catalogLabel(label)}</h2><p>{kind === 'plugins' ? PLUGIN_CATEGORIES[category]?.blurb : `Skills for ${catalogLabel(label).toLowerCase()}.`}</p></div>
            <Button onClick={() => onCategory(category)} size="inline" variant="text">{t.catalog.seeAll}<Codicon name="arrow-right" /></Button>
          </header>
          <div className={`catalog-collection catalog-collection-${variant}`} data-catalog-hover-group>
            {items.slice(0, variant === 'showcase' ? 4 : variant === 'horizontal' ? 6 : kind === 'plugins' ? 12 : 8).map((entry, index) => card(entry, sectionIndex + index, variant))}
          </div>
        </section>
      })}
      {sections.length > categoryLimit && <Button onClick={() => setCategoryLimit(value => value + 8)} size="sm" variant="text">{t.catalog.more}</Button>}
      <section data-catalog-section="developer">
        <header className="catalog-section-heading"><h2>Developer Mode</h2></header>
        <div className="catalog-developer">
          <div aria-hidden className="catalog-developer-art" />
          <div className="catalog-developer-content">
            <span aria-hidden className="catalog-developer-mark" />
            <h3>Build for Hermes</h3>
            <p className="text-sm text-(--ui-text-secondary)">Built a tool, connector, plugin, or Mod?<br />Want to but don’t know how?</p>
            <div className="catalog-developer-ctas">
              <Button onClick={() => void openExternalLink('https://github.com/NousResearch/hermes-agent/compare')} size="sm" variant="default">
                Submit Mod<Codicon name="arrow-up-right" />
              </Button>
              <Button onClick={() => void openExternalLink(kind === 'plugins' ? 'https://hermes-agent.nousresearch.com/docs/user-guide/features/plugin-catalog' : 'https://hermes-agent.nousresearch.com/docs/skills/')} size="sm" variant="secondary">
                Developer Docs<Codicon name="arrow-up-right" />
              </Button>
            </div>
          </div>
        </div>
        {actions && <div className="catalog-developer-management">{actions}</div>}
      </section>
    </div>
  )
}
