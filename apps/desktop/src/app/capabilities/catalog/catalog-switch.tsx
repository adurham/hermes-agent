import { type ComponentProps, useId } from 'react'

import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/i18n'

/** Visible action copy stays attached to the actual controlled switch. */
export function CatalogSwitch(props: ComponentProps<typeof Switch>) {
  const id = useId()
  const { t } = useI18n()

  return <label className="catalog-switch relative inline-flex shrink-0 items-center gap-2 text-xs text-(--ui-text-secondary)" htmlFor={id}>
    <span>{props.checked ? t.settings.plugins.disable : t.settings.plugins.enable}</span>
    <Switch {...props} id={id} />
  </label>
}
