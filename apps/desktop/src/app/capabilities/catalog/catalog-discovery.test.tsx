import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { CatalogEntry } from './catalog-data'
import { CatalogDiscovery } from './catalog-discovery'

const entry = (name: string, category: string, source = 'community') =>
  ({ id: name, name, source, category, categoryLabel: category, tags: [], search: name, stars: 1, imageUrl: null }) as unknown as CatalogEntry

const sections = (container: HTMLElement) =>
  [...container.querySelectorAll('[data-catalog-section]')].map(node => node.getAttribute('data-catalog-section')).filter(id => id !== 'developer')

const renderDiscovery = (entries: CatalogEntry[], byCategory: boolean) =>
  render(
    <CatalogDiscovery
      byCategory={byCategory}
      card={item => <article key={item.id}>{item.name}</article>}
      entries={entries}
      kind="plugins"
      narrowed={byCategory}
      onCategory={() => {}}
    />
  ).container

describe('multi-category discovery', () => {
  const picked = [entry('m1', 'memory'), entry('t1', 'tools'), entry('o1', 'tools', 'official'), entry('o2', 'memory', 'official')]

  it('gives every picked category its own shelf, small ones included, with no tier shelves', () => {
    expect(sections(renderDiscovery(picked, true)).sort()).toEqual(['memory', 'tools'])
  })

  it('keeps the default page folding small categories and splitting tiers', () => {
    // One official becomes the hero pick; the other keeps the Official shelf.
    expect(sections(renderDiscovery(picked, false))).toEqual(['featured', 'official', 'more'])
  })
})
