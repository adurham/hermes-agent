import { cn } from '../lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: nous-girl mark on a white tile (the light BrandMark variant).
// Ported from apps/desktop's BrandMark; asset lives in this app's public/.
// The installer has no theme system, so it always uses the light asset.
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span className={cn('inline-flex size-14 shrink-0 items-center justify-center bg-white', className)} {...props}>
      <img alt="" className="size-full object-contain" src={assetPath('nous-girl.png')} />
    </span>
  )
}
