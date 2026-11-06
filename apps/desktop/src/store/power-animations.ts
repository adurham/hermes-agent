/**
 * Stops decorative compositor loops while the machine is on battery.
 *
 * Every `infinite` CSS animation is a composite at the panel's refresh rate for
 * as long as it runs, whether or not anything is happening. On a 120 Hz panel
 * that is 120 composite passes a second of pure decoration, and it keeps
 * ProMotion from idling down. On battery the root carries
 * `data-on-battery="true"` and styles.css settles those loops after one pass —
 * the app keeps drawing, it just stops animating for nobody.
 *
 * Spinners and progress indicators opt back in: a frozen spinner reads as a
 * hung app, and they only run while real work is in flight.
 *
 * Imported for its side effect from `main.tsx` (same pattern as store/power).
 */

import { $onBattery } from './power'

function apply(onBattery: boolean): void {
  if (typeof document === 'undefined') {
    return
  }

  document.documentElement.dataset.onBattery = onBattery ? 'true' : 'false'
}

if (typeof window !== 'undefined') {
  apply($onBattery.get())
  $onBattery.subscribe(apply)
}
