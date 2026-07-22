import { useStore } from '@nanostores/react'
import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { useOnProfileSwitch } from '@/app/hooks/use-on-profile-switch'
import { useRouteOverlayActive } from '@/app/hooks/use-route-overlay-active'
import { burstVibeHearts, PetHeartField } from '@/components/chat/vibe-hearts'
import { persistString, storedString } from '@/lib/storage'
import {
  $petAtRest,
  $petCanRoam,
  $petInfo,
  $petMotion,
  $petRoam,
  $petRoamDir,
  clearPetUnread,
  type PetInfo,
  petProfile,
  type PetState,
  setPetInfo
} from '@/store/pet'
import { resetPetGallery, setPetScale } from '@/store/pet-gallery'
import { $petOverlayActive, initPetOverlayBridge, popOutPet, restorePetOverlay } from '@/store/pet-overlay'
import { $gatewayState } from '@/store/session'
import { isSecondaryWindow } from '@/store/windows'
import { useTheme } from '@/themes/context'

import { PetBubble } from './pet-bubble'
import { PetSprite, roamWalkRow } from './pet-sprite'
import { dwellMs, type DwellRange } from './roam-behavior'
import { usePetRoam } from './use-pet-roam'
import { type PetZoomAnchor, usePetZoomGesture } from './use-pet-zoom-gesture'

// v2: positions are now top/left anchored (v1 stored bottom-anchored values,
// which dragged inverted). Bumping the key discards stale v1 coordinates.
const POSITION_KEY = 'hermes.desktop.pet-position.v2'

// Stand-in pet size for the pre-load clamp (real size flows in with `info`).
const NOMINAL_PET_PX = 96

// A pointer that moves less than this before release reads as a click/pet,
// not a drag — mirrors the pop-out overlay's own CLICK_SLOP_PX.
const CLICK_SLOP_PX = 4

// Idle fidget: an occasional "still here" beat (wave/jump) while the pet is at
// rest, so a long idle stretch doesn't read as frozen. Rare and irregular —
// same exponential-dwell technique as the roam loop's PAUSE_DWELL, just with a
// much longer mean so it reads as an occasional glance, not a tic.
const FIDGET_DWELL: DwellRange = { maxMs: 150000, meanMs: 50000, minMs: 20000 }

// Minimum room above the sprite (in the zone's local coordinate space) for
// the status bubble to fit without clipping against the zone's clipped top
// edge — a generous estimate for the bubble's own height + its 6px margin.
const BUBBLE_CLEARANCE_PX = 40

interface Point {
  x: number
  y: number
}

interface PetInfoMeta {
  enabled: boolean
  slug?: string
  displayName?: string
  scale?: number
  spritesheetRevision?: string
}

function samePetRevision(info: PetInfo, meta: PetInfoMeta): boolean {
  return (
    info.enabled &&
    Boolean(info.spritesheetBase64) &&
    info.slug === meta.slug &&
    info.displayName === meta.displayName &&
    info.scale === meta.scale &&
    info.spritesheetRevision === meta.spritesheetRevision
  )
}

// Keep a w×h box fully inside the viewport (or zone container, when confined).
// Pre-pet-load callers pass a nominal size; the live size flows in once `info` arrives.
function clampPoint(x: number, y: number, w: number, h: number, zone?: DOMRect | null): Point {
  const maxX = zone ? Math.max(0, zone.width - w) : Math.max(0, (window.innerWidth || 800) - w)
  const maxY = zone ? Math.max(0, zone.height - h) : Math.max(0, (window.innerHeight || 600) - h)

  return {
    x: Math.min(Math.max(0, x), maxX),
    y: Math.min(Math.max(0, y), maxY)
  }
}

// The sprite art faces left by default, so mirror it when the pet's center sits
// on the left half of the window (or zone container, when confined) — it always
// faces inward, toward the content.
function facing(leftX: number, petW: number, zone?: DOMRect | null): string {
  const mid = zone ? zone.width / 2 : (window.innerWidth || 800) / 2

  return leftX + petW / 2 < mid ? 'scaleX(-1)' : 'none'
}

// Horizontal anchor for the zone status bubble: centers on the pet by default,
// but pins to the pet's near edge instead when the pet sits in the outer third
// of a narrow zone — a strictly-centered bubble would otherwise overhang past
// the zone's clipped left/right edge and get cut off, same failure mode the
// vertical flip (BUBBLE_CLEARANCE_PX) fixes for the top edge.
function bubbleHorizontalStyle(petX: number, petW: number, zoneWidth: number): CSSProperties {
  const petCenter = petX + petW / 2
  const third = zoneWidth / 3

  if (petCenter < third) {
    return { left: 0, transform: 'none' }
  }

  if (petCenter > zoneWidth - third) {
    return { right: 0, transform: 'none' }
  }

  return { left: '50%', transform: 'translateX(-50%)' }
}

function loadPosition(zone?: DOMRect | null): Point {
  // When confined to a zone, default to the top-left corner of the zone
  // (0,0 relative to the container). The full-window default doesn't make
  // sense for absolute positioning inside a pane.
  if (zone) {
    return { x: 0, y: 0 }
  }

  try {
    const raw = storedString(POSITION_KEY)

    if (raw) {
      const parsed = JSON.parse(raw) as Point

      if (typeof parsed.x === 'number' && typeof parsed.y === 'number') {
        return clampPoint(parsed.x, parsed.y, NOMINAL_PET_PX, NOMINAL_PET_PX)
      }
    }
  } catch {
    // fall through to default
  }

  // Default: lower-left corner (top/left anchored).
  return clampPoint(24, (window.innerHeight || 600) - 220, NOMINAL_PET_PX, NOMINAL_PET_PX)
}

/**
 * In-window floating petdex mascot. Always-on-top within the app, draggable,
 * and reactive to agent activity via `$petState`. Fetches the active pet via
 * the shared `pet.info` RPC; renders nothing until a pet is installed +
 * enabled.
 *
 * Adopting a pet is fully in-app: type `/pet boba` in the composer. That
 * writes `display.pet.*` from the slash worker, so we keep polling `pet.info`
 * while no pet is active and the mascot pops in within a few seconds — no
 * reload, no CLI. Once a pet is live we still refresh more slowly so generated
 * pets rewritten on disk (or renamed/rebuilt by the hatch flow) repaint without
 * restarting the app.
 *
 * Promotion to a separate frameless OS-level window is a follow-up — the
 * sprite + state logic here is reused as-is, only the host changes.
 */
const PET_POLL_MS = 3000
const PET_ACTIVE_REFRESH_MS = 15000

export function FloatingPet({ zoneContainer }: { zoneContainer?: React.RefObject<HTMLDivElement | null> }) {
  const { requestGateway } = useGatewayRequest()
  const { resolvedMode } = useTheme()
  const gatewayState = useStore($gatewayState)
  const info = useStore($petInfo)
  const overlayActive = useStore($petOverlayActive)
  const roamEnabled = useStore($petRoam)
  const canRoam = useStore($petCanRoam)
  const roamDir = useStore($petRoamDir)
  const routeOverlayOpen = useRouteOverlayActive()

  const [position, setPosition] = useState<Point>(() =>
    zoneContainer ? { x: 0, y: 0 } : loadPosition()
  )

  const containerRef = useRef<HTMLDivElement | null>(null)
  // The facing mirror lives on the sprite wrapper, not the container, so the
  // speech bubble (a container child) never renders flipped/backwards.
  const spriteWrapRef = useRef<HTMLDivElement | null>(null)
  const petW = (info.frameW ?? 192) * (info.scale ?? 0.33)
  const petH = (info.frameH ?? 208) * (info.scale ?? 0.33)
  // Soft contact shadow, sized off the pet so every scale/species grounds the
  // same way (cf. lairp's per-actor feet ellipse). Lighter on light backgrounds.
  const shadowW = Math.round(petW * 0.55)
  const shadowH = Math.max(3, Math.round(shadowW * 0.28))
  const shadowAlpha = resolvedMode === 'light' ? 0.2 : 0.55

  // Live drag offset (pointer → element top-left). Drag updates the DOM
  // directly to avoid a React re-render (and canvas reflow) per pointermove —
  // state is only committed on release. `moved` distinguishes a real drag
  // from a click/pet: a pointerup with `moved` still false triggers the pet
  // reaction instead of just settling a (zero-distance) drag.
  const dragRef = useRef<{
    dx: number
    dy: number
    x: number
    y: number
    startClientX: number
    startClientY: number
    moved: boolean
  } | null>(null)

  // Keep the *whole* pet on-screen at its current size, so growing it near an
  // edge can't leave the window cropping it. Shared by drag + the reclamp effect.
  const clamp = useCallback(
    ({ x, y }: Point): Point => {
      const zone = zoneContainer?.current?.getBoundingClientRect()

      return clampPoint(x, y, petW, petH, zone)
    },
    [petW, petH, zoneContainer]
  )

  // Fetch pet.info on connect. Poll quickly while inactive so an in-app
  // `/pet <slug>` appears, then slowly while active so regenerated spritesheets
  // and row-count metadata replace the cached base64 payload.
  const active = info.enabled && Boolean(info.spritesheetBase64)
  useEffect(() => {
    if (gatewayState !== 'open') {
      return
    }

    let cancelled = false

    const pull = async () => {
      try {
        if (active) {
          try {
            const meta = await requestGateway<PetInfoMeta>('pet.info.meta', { profile: petProfile() })

            if (cancelled || !meta) {
              return
            }

            if (!meta.enabled) {
              setPetInfo({ enabled: false })

              return
            }

            if (samePetRevision($petInfo.get(), meta)) {
              return
            }
          } catch {
            // Older gateways may not have pet.info.meta yet; fall back to pet.info.
          }
        }

        const next = await requestGateway<PetInfo>('pet.info', { profile: petProfile() })

        if (!cancelled && next) {
          const current = $petInfo.get()

          if (
            next.enabled &&
            current.enabled &&
            current.slug === next.slug &&
            current.displayName === next.displayName &&
            current.scale === next.scale &&
            current.spritesheetRevision &&
            current.spritesheetRevision === next.spritesheetRevision
          ) {
            return
          }

          setPetInfo(next)
        }
      } catch {
        // cosmetic feature — never surface gateway errors
      }
    }

    void pull()
    const timer = window.setInterval(() => void pull(), active ? PET_ACTIVE_REFRESH_MS : PET_POLL_MS)
    window.addEventListener('focus', pull)

    return () => {
      cancelled = true
      window.removeEventListener('focus', pull)
      window.clearInterval(timer)
    }
  }, [gatewayState, active, requestGateway])

  // Pets are per-profile. When the active profile changes, drop the previous
  // profile's mascot + gallery cache so the poll above refetches the new
  // profile's pet (its config + pets dir resolve per-profile on the backend).
  useOnProfileSwitch(() => {
    setPetInfo({ enabled: false })
    resetPetGallery()
  })

  // Wire the overlay control channel once, only in the primary window — the
  // pop-out overlay belongs to it (main.ts positions it against the main
  // window and routes control messages back to it).
  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    return initPetOverlayBridge()
  }, [])

  // Returning to the app (by any route, not just the mail icon) clears the pet's
  // "new message" hint — you've seen it now.
  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    const onFocus = () => clearPetUnread()
    window.addEventListener('focus', onFocus)

    return () => window.removeEventListener('focus', onFocus)
  }, [])

  // Restore a popped-out pet on boot, once the pet has loaded (so we never spawn
  // an empty overlay window). Primary window only; runs at most once.
  const restoredRef = useRef(false)
  useEffect(() => {
    if (isSecondaryWindow() || restoredRef.current || !active) {
      return
    }

    restoredRef.current = true
    restorePetOverlay()
  }, [active])

  // Never strand or crop the pet: re-clamp (and persist) whenever the viewport
  // shrinks or the pet's own size changes (wheel/slider). `clamp` carries the
  // current size, so depending on it covers both triggers. Zone-mode positions
  // are container-local and never persisted — POSITION_KEY belongs to the
  // full-window pet's coordinate space.
  //
  // In zone mode the zone pane is a layout-tree track the user drags, which
  // never fires `window.resize` — only a ResizeObserver on the container
  // itself sees it. Without this, shrinking the zone left the pet clamped to
  // its OLD (now stale) bounds until some unrelated window resize happened to
  // trigger a recheck.
  useEffect(() => {
    const reclamp = () =>
      setPosition(prev => {
        const next = clamp(prev)

        if (next.x === prev.x && next.y === prev.y) {
          return prev
        }

        if (!zoneContainer) {
          persistString(POSITION_KEY, JSON.stringify(next))
        }

        return next
      })

    reclamp()
    window.addEventListener('resize', reclamp)

    const zoneEl = zoneContainer?.current
    const zoneObserver = zoneEl ? new ResizeObserver(reclamp) : undefined
    zoneObserver?.observe(zoneEl!)

    return () => {
      window.removeEventListener('resize', reclamp)
      zoneObserver?.disconnect()
    }
  }, [clamp, zoneContainer])

  // Viewport→container-local conversion. In zone mode style.left/top are
  // relative to the zone container; in full-window mode (position:fixed)
  // viewport coords ARE the style coords, so the origin is (0,0).
  const zoneOrigin = useCallback((): Point => {
    const z = zoneContainer?.current?.getBoundingClientRect()

    return z ? { x: z.left, y: z.top } : { x: 0, y: 0 }
  }, [zoneContainer])

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      const el = containerRef.current

      if (!el) {
        return
      }

      const rect = el.getBoundingClientRect()

      // Shift-click pops the pet out into a free-floating desktop overlay (it can
      // leave the window and stays visible while Hermes is minimized) instead of
      // starting an in-window drag. Primary window only — the overlay is anchored
      // to it.
      if (e.shiftKey && !isSecondaryWindow()) {
        popOutPet({ height: rect.height, width: rect.width, x: rect.left, y: rect.top })

        return
      }

      const origin = zoneOrigin()
      dragRef.current = {
        dx: e.clientX - rect.left,
        dy: e.clientY - rect.top,
        x: rect.left - origin.x,
        y: rect.top - origin.y,
        startClientX: e.clientX,
        startClientY: e.clientY,
        moved: false
      }
      el.setPointerCapture(e.pointerId)
      el.style.cursor = 'grabbing'
    },
    [zoneOrigin]
  )

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current
      const el = containerRef.current

      if (!drag || !el) {
        return
      }

      if (
        !drag.moved &&
        Math.hypot(e.clientX - drag.startClientX, e.clientY - drag.startClientY) > CLICK_SLOP_PX
      ) {
        drag.moved = true
      }

      // clientX/Y are viewport coords; convert the drag target into the pet's
      // positioning space (container-local in zone mode) before clamping.
      const origin = zoneOrigin()
      const next = clamp({ x: e.clientX - drag.dx - origin.x, y: e.clientY - drag.dy - origin.y })
      drag.x = next.x
      drag.y = next.y
      // Mutate the DOM directly — no setState, so no re-render while dragging. The
      // mirror follows the pointer across the midline for the same reason; it
      // rides the sprite wrapper so the bubble stays upright.
      el.style.left = `${next.x}px`
      el.style.top = `${next.y}px`

      if (spriteWrapRef.current) {
        const zone = zoneContainer?.current?.getBoundingClientRect()
        spriteWrapRef.current.style.transform = facing(next.x, petW, zone)
      }
    },
    [clamp, petW, zoneOrigin, zoneContainer]
  )

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current

      if (drag) {
        dragRef.current = null
        const committed = { x: drag.x, y: drag.y }
        setPosition(committed)

        if (!zoneContainer) {
          persistString(POSITION_KEY, JSON.stringify(committed))
        }

        // Pet the pet: a plain click (no real movement, not the shift-click
        // pop-out) triggers the same reaction the composer's affection detector
        // fires — a heart puff + a celebrate/wave beat — without needing an
        // agent turn to say something nice first.
        if (!drag.moved && !e.shiftKey) {
          burstVibeHearts()
        }
      }

      const el = containerRef.current

      if (el) {
        el.style.cursor = 'grab'
        el.releasePointerCapture?.(e.pointerId)
      }
    },
    [zoneContainer]
  )

  // Alt+wheel over the pet resizes it (persisted via the same path as the
  // settings slider). Zoom toward the cursor — shift the top-left so the pixel
  // under the pointer stays put — so the pet grows in place instead of running
  // off. The reclamp effect (via `clamp`) still guarantees it stays on-screen.
  const onScale = useCallback(
    (next: number, { clientX, clientY, ratio }: PetZoomAnchor) => {
      setPetScale(requestGateway, next)
      setPosition(prev => {
        // clientX/Y are viewport coords; prev is in the pet's positioning
        // space (container-local in zone mode) — convert before anchoring.
        const origin = zoneOrigin()
        const localX = clientX - origin.x
        const localY = clientY - origin.y

        const at = clampPoint(
          localX - (localX - prev.x) * ratio,
          localY - (localY - prev.y) * ratio,
          (info.frameW ?? 192) * next,
          (info.frameH ?? 208) * next,
          zoneContainer?.current?.getBoundingClientRect()
        )

        if (!zoneContainer) {
          persistString(POSITION_KEY, JSON.stringify(at))
        }

        return at
      })
    },
    [requestGateway, info.frameW, info.frameH, zoneOrigin, zoneContainer]
  )

  usePetZoomGesture(containerRef, onScale, active && !overlayActive)

  // Commit a roamed-to position back to React state + storage when the wander
  // loop settles, so the inline style matches the DOM once the loop stops
  // driving it imperatively. Stable identity keeps the roam effect from
  // restarting every render. Zone-mode positions are container-local — never
  // persisted to the full-window POSITION_KEY.
  const commitRoamPosition = useCallback(
    (point: Point) => {
      setPosition(point)

      if (!zoneContainer) {
        persistString(POSITION_KEY, JSON.stringify(point))
      }
    },
    [zoneContainer]
  )

  const isDragging = useCallback(() => dragRef.current !== null, [])

  // Roam the in-window pet whenever roaming is opted in and the agent isn't in
  // a state that wants a distinct stationary pose ($petCanRoam allows ordinary
  // idle AND active work — see its doc comment — so the pet keeps pacing while
  // the agent is thinking/running a tool, not just at true idle).
  usePetRoam({
    commit: commitRoamPosition,
    containerRef,
    enabled: roamEnabled && active && !overlayActive && canRoam,
    isInteracting: isDragging,
    loopMs: info.loopMs ?? 1100,
    overlayOpen: routeOverlayOpen,
    petH,
    petW,
    zoneContainer
  })

  // Idle fidget: while the pet is genuinely at rest, occasionally flash a
  // wave/jump beat so a long idle stretch doesn't read as frozen. Drives
  // `$petMotion` directly — the same silent "pose" channel the roam loop
  // uses for its own walk/hop animations — instead of `$petActivity`/
  // `flashPetActivity`, which is the REAL agent-status channel PetBubble
  // reads. That distinction matters: a fidget is purely decorative and must
  // never surface as a status line ("working…" etc.) the way genuine
  // activity does.
  //
  // Only runs while roam is off: with roam on, the wander loop already
  // provides continuous life (it loafs, then occasionally strolls/hops) by
  // writing this same atom, and a second writer on top would fight it.
  useEffect(() => {
    if (!active || overlayActive || roamEnabled) {
      return
    }

    let timer: ReturnType<typeof setTimeout> | undefined
    let decay: ReturnType<typeof setTimeout> | undefined

    const schedule = () => {
      timer = setTimeout(() => {
        // Only fidget while genuinely idle and nothing else is already
        // driving a pose (never interrupt a real turn or a stray beat).
        if ($petAtRest.get() && $petMotion.get() === null) {
          const beat: PetState = Math.random() < 0.5 ? 'jump' : 'wave'
          $petMotion.set(beat)
          decay = setTimeout(() => {
            // Don't clobber a pose something else set in the meantime.
            if ($petMotion.get() === beat) {
              $petMotion.set(null)
            }
          }, 1600)
        }

        schedule()
      }, dwellMs(FIDGET_DWELL))
    }

    schedule()

    return () => {
      clearTimeout(timer)
      clearTimeout(decay)
    }
  }, [active, overlayActive, roamEnabled])

  // While roaming, drive the directional run row + mirror from the travel
  // direction; at rest, fall back to the inward-facing static mascot.
  const walk = roamWalkRow(roamDir, info.stateRows)

  // While popped out, the desktop overlay window owns the mascot — hide the
  // in-window one so there aren't two.
  if (!info.enabled || !info.spritesheetBase64 || overlayActive) {
    return null
  }

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      ref={containerRef}
      style={{
        cursor: 'grab',
        left: position.x,
        pointerEvents: 'auto',
        position: zoneContainer ? 'absolute' : 'fixed',
        top: position.y,
        touchAction: 'none',
        userSelect: 'none',
        zIndex: zoneContainer ? 10 : 60
      }}
    >
      <div
        aria-hidden
        style={{
          background: `radial-gradient(ellipse at center, rgba(0,0,0,${shadowAlpha}) 0%, rgba(0,0,0,0) 70%)`,
          bottom: -shadowH * 0.4,
          height: shadowH,
          left: '50%',
          pointerEvents: 'none',
          position: 'absolute',
          transform: 'translateX(-50%)',
          width: shadowW,
          zIndex: 0
        }}
      />
      {/* Status bubble ("working…"/"your turn"/etc.) — only in the dedicated
          zone. The full-window pet skips it (the app itself is the surface,
          per the pop-out overlay's own rationale), but the zone is a small
          fixed box where a glanceable status line earns its keep.

          Flips below the sprite when there isn't enough headroom above (the
          zone clips with overflow:hidden, so a bubble that assumes it always
          has room above gets cut off whenever the pet is near the zone's top
          edge — from roaming there, or just being dragged there). */}
      {zoneContainer &&
        (() => {
          const aboveFits = position.y >= BUBBLE_CLEARANCE_PX
          const zoneWidth = zoneContainer.current?.getBoundingClientRect().width ?? 0
          const horizontal = zoneWidth ? bubbleHorizontalStyle(position.x, petW, zoneWidth) : {}

          return (
            <div
              style={{
                [aboveFits ? 'bottom' : 'top']: '100%',
                [aboveFits ? 'marginBottom' : 'marginTop']: 6,
                pointerEvents: 'none',
                position: 'absolute',
                whiteSpace: 'nowrap',
                zIndex: 2,
                ...horizontal
              }}
            >
              <PetBubble />
            </div>
          )
        })()}
      <div
        ref={spriteWrapRef}
        style={{
          lineHeight: 0,
          position: 'relative',
          transform: roamDir !== 0 ? (walk.mirror ? 'scaleX(-1)' : 'none') : facing(position.x, petW, zoneContainer?.current?.getBoundingClientRect()),
          zIndex: 1
        }}
      >
        <PetSprite info={info} rowOverride={walk.row} />
      </div>
      {/* Hearts puff off the pet; its celebrate ("yay"/jump) pose is driven by
          burstVibeHearts's router. */}
      <PetHeartField petH={petH} petW={petW} />
    </div>
  )
}
