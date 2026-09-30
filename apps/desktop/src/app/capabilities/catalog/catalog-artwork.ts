import artwork from '../../../../../shared/src/catalog-artwork.json'

const images = import.meta.glob<string>('../../../../../../website/static/img/catalog/*.jpg', {
  eager: true,
  import: 'default',
  query: '?url'
})

const artworkUrl = (filename: string | undefined) =>
  filename ? images[`../../../../../../website/static/img/catalog/${filename}`] ?? null : null

/** Only first-party catalog identities receive editorial artwork. */
export function officialCatalogArtwork(kind: 'skills' | 'plugins', row: {
  name?: string
  category?: string
  identifier?: string
  source?: string
  tier?: string
}): string | null {
  const source = kind === 'skills' ? row.source : row.tier

  if (kind === 'skills') {
    if (!['built-in', 'bundled', 'optional', 'official'].includes(source ?? '')) {return null}
    const key = `${row.category}/${row.name}`

    return artworkUrl((artwork.skills as Record<string, string>)[key])
  }

  if (source !== 'bundled') {return null}

  return artworkUrl((artwork.plugins as Record<string, string>)[row.identifier ?? ''])
}
