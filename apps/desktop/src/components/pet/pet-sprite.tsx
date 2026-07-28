import { memo, useEffect, useMemo, useRef } from 'react'

import { $petJumpBeat, $petRoamAirborne, $petState, type PetInfo, type PetState } from '@/store/pet'

import { jumpBobHeightPx, jumpDurationMs } from './roam-behavior'
import { subscribeWindowVisibility } from './use-window-visibility'

const DEFAULT_FRAME_W = 192
const DEFAULT_FRAME_H = 208
const DEFAULT_FRAMES = 6
const DEFAULT_LOOP_MS = 1100
// Mirrors agent.pet.constants.DEFAULT_SCALE — fallback only; the gateway sends
// the configured scale.
const DEFAULT_SCALE = 0.33

// Mirrors agent.pet.constants.CODEX_STATE_ROWS (Petdex current taxonomy).
export const DEFAULT_STATE_ROWS = [
  'idle',
  'running-right',
  'running-left',
  'waving',
  'jumping',
  'failed',
  'waiting',
  'running',
  'review'
]

const STATE_ALIASES: Record<PetState, string[]> = {
  idle: ['idle'],
  wave: ['wave', 'waving'],
  jump: ['jump', 'jumping'],
  run: ['run', 'running'],
  failed: ['failed'],
  review: ['review'],
  waiting: ['waiting']
}

const ROW_TO_STATE: Record<string, PetState> = {
  idle: 'idle',
  wave: 'wave',
  waving: 'wave',
  jump: 'jump',
  jumping: 'jump',
  run: 'run',
  running: 'run',
  'running-right': 'run',
  'running-left': 'run',
  failed: 'failed',
  review: 'review',
  waiting: 'waiting'
}

/**
 * Pick the running row + mirror for a horizontal travel direction.
 *
 * Codex sheets ship dedicated `running-left` / `running-right` locomotion rows
 * (already facing their way → no flip). Pets without them fall back to the
 * in-place `running`/`run` row, which faces left by convention, so rightward
 * travel is mirrored. Returns no `row` in that fallback case so the caller lets
 * `$petState` resolve it (and applies `mirror`).
 */
export function roamWalkRow(dir: -1 | 0 | 1, stateRows?: string[]): { row?: string; mirror: boolean } {
  if (dir === 0) {
    return { mirror: false }
  }

  const rows = stateRows ?? DEFAULT_STATE_ROWS
  const hasLeft = rows.includes('running-left')
  const hasRight = rows.includes('running-right')

  if (dir > 0) {
    if (hasRight) {
      return { mirror: false, row: 'running-right' }
    }

    if (hasLeft) {
      return { mirror: true, row: 'running-left' }
    }

    return { mirror: true }
  }

  if (hasLeft) {
    return { mirror: false, row: 'running-left' }
  }

  if (hasRight) {
    return { mirror: true, row: 'running-right' }
  }

  return { mirror: false }
}

interface PetSpriteProps {
  info: PetInfo
  /** On-screen scale multiplier applied on top of the pet's native scale. */
  zoom?: number
  /**
   * Force a specific animation state instead of reading the live `$petState`.
   * Used by the generate-flow preview to showcase every row without driving (or
   * being driven by) the real agent activity that moves the floating mascot.
   */
  stateOverride?: PetState
  /** Force a concrete row name from `info.stateRows` (e.g. `running-right`). */
  rowOverride?: string
}

/**
 * Canvas renderer for a petdex spritesheet — the one piece that must be
 * TypeScript (the engine's decode/encode is Python). Draws the row matching the
 * live `$petState`, stepping `framesPerState` frames across a `loopMs` loop.
 *
 * State is read from `$petState` via a ref + subscription rather than a prop,
 * so the frequent activity-driven state changes during an agent turn update the
 * canvas (inside its RAF loop) WITHOUT triggering a React re-render. Combined
 * with `memo`, this component effectively never re-renders after mount until
 * the pet itself changes.
 */
function PetSpriteImpl({ info, zoom = 1, stateOverride, rowOverride }: PetSpriteProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const stateRef = useRef<PetState>($petState.get())
  const overrideRef = useRef<PetState | undefined>(stateOverride)
  const rowOverrideRef = useRef<string | undefined>(rowOverride)

  // Keep the override current without re-running the RAF setup effect.
  useEffect(() => {
    overrideRef.current = stateOverride
  }, [stateOverride])

  useEffect(() => {
    rowOverrideRef.current = rowOverride
  }, [rowOverride])

  const frameW = info.frameW ?? DEFAULT_FRAME_W
  const frameH = info.frameH ?? DEFAULT_FRAME_H
  const frames = info.framesPerState ?? DEFAULT_FRAMES
  const framesByState = info.framesByState
  const framesByRow = info.framesByRow
  const loopMs = info.loopMs ?? DEFAULT_LOOP_MS
  const scale = (info.scale ?? DEFAULT_SCALE) * zoom
  const rows = info.stateRows ?? DEFAULT_STATE_ROWS

  const drawW = Math.round(frameW * scale)
  const drawH = Math.round(frameH * scale)

  const image = useMemo(() => {
    if (!info.spritesheetBase64) {
      return null
    }

    const img = new Image()
    img.src = `data:${info.mime ?? 'image/webp'};base64,${info.spritesheetBase64}`

    return img
  }, [info.spritesheetBase64, info.mime])

  useEffect(() => {
    const canvas = canvasRef.current

    if (!canvas || !image) {
      return
    }

    // willReadFrequently: the pop-out overlay samples this canvas's alpha under
    // the cursor (per-pixel click-through), so opt into the CPU-readback path.
    const ctx = canvas.getContext('2d', { willReadFrequently: true })

    if (!ctx) {
      return
    }

    // Size the backing store to real device pixels, not CSS pixels — mirrors
    // the same fix already applied to this pet's other canvases
    // (pixel-egg-sprite.tsx, pet-star-shower.tsx). Without this, on a HiDPI
    // (Retina) display the canvas draws at half (or less) the screen's actual
    // pixel density and the OS/browser has to upscale the whole element to
    // fill its CSS box on top of the drawImage scale-down already happening
    // from the 192×208 source frame — a second resampling pass whose
    // sub-pixel rounding shifts slightly differently per frame, reading as a
    // subtle shimmer/wobble on a fast, edge-heavy cycle like running. Capped
    // at 3x like the sibling components — no benefit beyond that, and it
    // caps the offscreen memory/fill cost on very high-density external
    // displays.
    const dpr = Math.min(window.devicePixelRatio || 1, 3)
    const backingW = Math.round(drawW * dpr)
    const backingH = Math.round(drawH * dpr)

    if (canvas.width !== backingW || canvas.height !== backingH) {
      canvas.width = backingW
      canvas.height = backingH
    }

    // Track state via subscription, not a prop — no re-render on activity ticks.
    stateRef.current = $petState.get()

    const unsubState = $petState.listen(next => {
      stateRef.current = next
    })

    let raf = 0
    let frame = 0
    let lastStep = performance.now()
    let drawnFrame = -1
    let drawnRow = -1
    let activeRow = -1
    let activeCount = -1

    // Freeze the loop while the window is truly hidden (minimized, on another
    // space, or occluded). electron/main.ts disables Chromium's normal
    // renderer-backgrounding throttle app-wide so a streaming reply never
    // stalls on refocus — but that also means an rAF loop with no gate of its
    // own spins at 60Hz forever with nobody looking at it. This loop is purely
    // cosmetic (idle sprite animation), so it can safely pause; resume + force
    // a fresh frame the instant it's visible again.
    //
    // Deliberately NOT document.hidden/visibilitychange, and NOT
    // document.hasFocus(): this app's occlusion-throttle switches make Page
    // Visibility unreliable here — a window created with `show: false` can
    // get stuck reporting `hidden` forever even once genuinely shown, because
    // the show→visible transition stops propagating correctly to Blink when
    // occlusion tracking is off (verified regression: froze the sprite on
    // frame 1 from launch — see FORK.md 2026-07-26 entries). Use the real
    // OS-level visibility pushed from the main process instead (see
    // use-window-visibility.ts). Also not hasFocus(): the pet (both the
    // in-window floating mascot and the popped-out overlay) is a background
    // companion meant to keep animating while glanced at, not a foreground
    // interactive surface, and the popped-out overlay window is
    // `focusable: false` by design so hasFocus() is permanently false there.
    let paused = false

    const schedule = () => {
      if (!paused && !raf) {
        raf = requestAnimationFrame(render)
      }
    }

    const onVisibilityChange = (visible: boolean) => {
      const next = !visible

      if (next === paused) {
        return
      }

      paused = next

      if (paused) {
        if (raf) {
          cancelAnimationFrame(raf)
          raf = 0
        }
      } else {
        lastStep = performance.now()
        drawnFrame = -1
        schedule()
      }
    }

    const rowIndexForState = (s: PetState): number => {
      for (const key of STATE_ALIASES[s] ?? [s]) {
        const idx = rows.indexOf(key)

        if (idx >= 0) {
          return idx
        }
      }

      return 0
    }

    // Resolve a state to the row it draws and its real frame count. A state
    // with no real frames (ragged sheet, empty row) falls back to idle rather
    // than flashing blank padding.
    const resolve = (s: PetState): { row: number; count: number } => {
      const real = framesByState?.[s] ?? frames

      if (real > 0) {
        return { row: rowIndexForState(s), count: real }
      }

      return { row: rowIndexForState('idle'), count: Math.max(1, framesByState?.idle ?? frames) }
    }

    const resolveRow = (rowName: string): { row: number; count: number } => {
      const row = rows.indexOf(rowName)
      const state = ROW_TO_STATE[rowName]

      const count = Math.max(
        1,
        framesByRow?.[rowName] ?? framesByState?.[rowName] ?? (state ? framesByState?.[state] : 0) ?? frames
      )

      return { row: row >= 0 ? row : rowIndexForState(state ?? 'idle'), count }
    }

    const render = (now: number) => {
      const forcedRow = rowOverrideRef.current
      const { row, count } = forcedRow ? resolveRow(forcedRow) : resolve(overrideRef.current ?? stateRef.current)

      if (row !== activeRow || count !== activeCount) {
        activeRow = row
        activeCount = count
        frame = 0
        lastStep = now
        drawnFrame = -1
      }

      // Per-state step keeps every state's loop ~loopMs even when frame counts
      // differ; counts vary per row so derive the cadence here, not once.
      const stepMs = loopMs / count

      if (now - lastStep >= stepMs) {
        frame += 1
        lastStep = now
      }

      frame %= count

      // Only touch the canvas when the visible cell actually changes. The RAF
      // ticks at ~60Hz but the sprite only steps ~5Hz, so this skips ~90% of
      // the clear+draw work and keeps the main thread free.
      if ((frame !== drawnFrame || row !== drawnRow) && image.complete && image.naturalWidth > 0) {
        const sx = frame * frameW
        const sy = row * frameH
        ctx.clearRect(0, 0, canvas.width, canvas.height)
        ctx.imageSmoothingEnabled = false
        ctx.drawImage(image, sx, sy, frameW, frameH, 0, 0, backingW, backingH)
        drawnFrame = frame
        drawnRow = row
      }

      raf = requestAnimationFrame(render)
    }

    schedule()

    const unsubVisibility = subscribeWindowVisibility(onVisibilityChange)

    return () => {
      cancelAnimationFrame(raf)
      unsubState()
      unsubVisibility()
    }
  }, [image, frameW, frameH, frames, framesByState, framesByRow, loopMs, drawW, drawH, rows])

  // Stationary jump bob: play a CSS vertical hop for as long as the pose
  // stays `jump` for a reason OTHER than the roam loop's own ledge-to-ledge
  // hop (idle fidget, click-to-pet, turn-end celebrate — see `.pet-jump-bob`
  // in styles.css). The roam loop already moves the container's real
  // top/left during an actual hop (`$petRoamAirborne`), so skip this there
  // or the two vertical motions would fight each other.
  //
  // The pose is held far longer than one hop: a turn-completion celebrate
  // holds `jump` for 2200ms — deliberately ~2x the sprite's own `loopMs` so
  // the frame animation gets to loop twice before settling (see the
  // "runs ~2 loops, then settles" comment at the celebrate call site) — but
  // a single bob capped at `jumpDurationMs()` (max 900ms) lands and goes flat
  // for the remaining ~1.3s while the leg frames keep cycling in place. That
  // reads exactly as "stuck repeating the same jump animation but not
  // changing height." Fix: re-trigger the bob every `jumpDurationMs(loopMs)`
  // for as long as the pose is actually held, so the physical hop always
  // tracks how long the jump pose is shown — not a duration hardcoded to one
  // cycle regardless of the caller's hold time.
  useEffect(() => {
    const wrap = wrapRef.current

    if (!wrap) {
      return
    }

    const playBob = () => {
      wrap.style.setProperty('--pet-jump-height', `${jumpBobHeightPx(drawH)}px`)
      wrap.style.setProperty('--pet-jump-ms', `${jumpDurationMs(loopMs)}ms`)
      // Force a reflow so re-triggering the same animation class (e.g. two
      // quick jump beats, or the next repeat in the cadence below) restarts
      // it instead of no-oping.
      wrap.classList.remove('pet-jump-bob')
      void wrap.offsetWidth
      wrap.classList.add('pet-jump-bob')
    }

    let repeatTimer: ReturnType<typeof setInterval> | undefined

    const startBobbing = () => {
      playBob()
      clearInterval(repeatTimer)
      repeatTimer = setInterval(playBob, jumpDurationMs(loopMs))
    }

    const stopBobbing = () => {
      clearInterval(repeatTimer)
      repeatTimer = undefined
    }

    const isJumping = () => (overrideRef.current ?? stateRef.current) === 'jump' && !$petRoamAirborne.get()

    let prevJumping = isJumping()

    if (prevJumping) {
      startBobbing()
    }

    const unsubState = $petState.listen(() => {
      const jumping = isJumping()

      if (jumping && !prevJumping) {
        startBobbing()
      } else if (!jumping && prevJumping) {
        stopBobbing()
      }

      prevJumping = jumping
    })

    // A plain `atom.listen()` (unlike `.subscribe()`) never calls the new
    // listener synchronously with the current value — only future `.set()`
    // calls — so every firing here is a real, new celebrate request. Restart
    // the cadence from a fresh hop rather than letting a stale interval's
    // next tick land mid-way through the new request's hold window.
    const unsubBeat = $petJumpBeat.listen(() => {
      if (isJumping()) {
        startBobbing()
      }
    })

    return () => {
      unsubState()
      unsubBeat()
      stopBobbing()
    }
  }, [drawH, loopMs])

  return (
    <div ref={wrapRef} style={{ height: drawH, lineHeight: 0, width: drawW }}>
      <canvas
        aria-label={info.displayName ? `${info.displayName} pet` : 'pet'}
        ref={canvasRef}
        style={{ height: drawH, imageRendering: 'pixelated', width: drawW }}
      />
    </div>
  )
}

/**
 * Memoized so a parent re-render (e.g. a position commit on drag-end) doesn't
 * re-run the canvas setup. Props change only when the pet itself changes.
 */
export const PetSprite = memo(PetSpriteImpl)
