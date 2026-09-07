import { cn } from '@/lib/utils'
import { useTheme } from '@/themes'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: nous-girl mark on a fixed tile, theme-aware — light mode shows
// the black girl on white, dark mode the white girl on the nous dark surface.
// Fills the tile (softly rounded); size via className (default size-14).
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  const { renderedMode } = useTheme()
  const dark = renderedMode === 'dark'

  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white',
        dark && 'bg-[#0d1117]',
        className
      )}
      {...props}
    >
      <img alt="" className="size-full object-contain" src={assetPath(dark ? 'nous-girl-dark.png' : 'nous-girl.png')} />
    </span>
  )
}
