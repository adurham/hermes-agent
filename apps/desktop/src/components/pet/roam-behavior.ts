/**
 * Pure decision helpers for the floating pet's wander — the "what to do & when"
 * layer, split out from the geometry (`roam-geometry.ts`) and the RAF/DOM loop
 * (`use-pet-roam.ts`) so the *rhythm* of the roam is tunable in one place and
 * unit testable (every function takes an injectable `rng`).
 *
 * The goal is a calm, believable critter rather than a fidgeting one. Two ideas
 * from ambient game-AI carry the weight (see GameAIPro ch.36 "Breathing Life
 * into Your Background Characters" + standard idle/wander state machines):
 *
 *  1. **Loaf, don't pace.** A background character that picks a new walk on
 *     every beat reads as nervous. Most decision beats just keep resting;
 *     movement is the exception, not the default (`REST_CHANCE`).
 *  2. **Memoryless dwell times.** Uniform pauses feel metronomic. An
 *     exponential dwell — the classic model for idle durations — gives mostly
 *     short rests with the occasional long loaf, so the cadence never reads as a
 *     fixed pattern (`dwellMs` / `PAUSE_DWELL`).
 */

import type { Ledge } from './roam-geometry'

export type Rng = () => number

/** What the pet does when a rest beat ends. */
export type RoamMove = 'rest' | 'stroll' | 'hop'

export interface DwellRange {
  /** Mean of the exponential draw — the "typical" rest length. */
  meanMs: number
  /** Floor, so a near-zero draw never produces a jittery micro-pause. */
  minMs: number
  /** Ceiling, so a fat-tail draw (or a throttled tab) can't freeze the pet. */
  maxMs: number
}

// Rest length between beats: mostly short loafs, the occasional long one.
export const PAUSE_DWELL: DwellRange = { maxMs: 13000, meanMs: 4200, minMs: 1500 }
// Most beats the pet just keeps loafing — a critter that re-walks every beat
// reads as nervous, not alive.
export const REST_CHANCE = 0.62
// When it *does* move, chance it hops to another ledge vs. strolling this one.
export const HOP_CHANCE = 0.2
// Strolls should cover ground, not shuffle: travel at least this fraction of the
// ledge (or this many px, whichever is larger), up to the room available.
const STROLL_MIN_FRACTION = 0.45
const STROLL_MIN_PX = 110
// Bias toward the roomier side so the pet crosses the app instead of pacing one
// spot; the long tail of the coin still lets it double back now and then.
const STROLL_TOWARD_ROOM = 0.85

/**
 * Exponential (memoryless) dwell time, clamped to `[minMs, maxMs]`. With rng→0
 * this returns `minMs`; with rng→1 it saturates at `maxMs`; in between it's
 * `-ln(u)·meanMs`, so short rests dominate and long loafs are rare but possible.
 */
export function dwellMs({ meanMs, minMs, maxMs }: DwellRange, rng: Rng = Math.random): number {
  const u = 1 - rng() // map [0,1) → (0,1] so the log stays finite

  return Math.min(maxMs, Math.max(minMs, -Math.log(u) * meanMs))
}

/**
 * Decide a beat: rest (the common case), or — when the pet is actually going to
 * move — hop to a reachable ledge if one exists and the dice say so, else stroll
 * the current ledge. `canHop` is false when no neighbouring surface overlaps, so
 * the pet never "hops" in place.
 */
export function chooseMove(canHop: boolean, rng: Rng = Math.random): RoamMove {
  if (rng() < REST_CHANCE) {
    return 'rest'
  }

  return canHop && rng() < HOP_CHANCE ? 'hop' : 'stroll'
}

/**
 * A stroll destination (absolute x) on `ledge` that actually goes somewhere:
 * lean toward the side with more room and guarantee a decent minimum travel, so
 * the pet crosses the app rather than shuffling in place.
 */
export function pickStrollTarget(ledge: Ledge, fromX: number, rng: Rng = Math.random): number {
  const span = ledge.right - ledge.left

  if (span <= 4) {
    return ledge.left
  }

  const roomLeft = fromX - ledge.left
  const roomRight = ledge.right - fromX
  // Usually head to the roomier side; the long tail of the coin doubles back.
  const goRight = rng() < STROLL_TOWARD_ROOM === roomRight >= roomLeft
  const room = Math.max(0, goRight ? roomRight : roomLeft)
  const minDist = Math.min(room, Math.max(span * STROLL_MIN_FRACTION, STROLL_MIN_PX))
  const dist = minDist + rng() * Math.max(0, room - minDist)

  return goRight ? fromX + dist : fromX - dist
}

// Spring-up hop duration as a fraction of the sprite's jump-loop cadence
// (`loopMs`), not a flat guess. `PetSprite` paces every state to complete its
// real frame count exactly once per `loopMs` (`stepMs = loopMs / frameCount`
// in pet-sprite.tsx), so a hop that finishes in a fixed 460ms — as this used
// to be — plays only ~2 of a ~5-frame jump row before `settleOn()` snaps the
// pose back to idle: the animation reads as a teleport, not a hop, and gets
// worse the slower a pet's `loopMs` is configured.
const JUMP_DUR_FRACTION = 0.75
// Floor: even a very fast (short-`loopMs`) pet still gets a readable spring,
// not an instant snap.
const JUMP_DUR_MIN_MS = 260
// Ceiling: a very slow pet's platforming hop still reads as a hop, not a
// sluggish float.
const JUMP_DUR_MAX_MS = 900

/**
 * Physical duration (ms) of a spring-up hop between ledges, paced to the
 * sprite's own `loopMs` so the jump pose has time to actually play out its
 * frames before the pet lands and the roam loop cuts back to idle.
 */
export function jumpDurationMs(loopMs: number): number {
  return Math.min(JUMP_DUR_MAX_MS, Math.max(JUMP_DUR_MIN_MS, loopMs * JUMP_DUR_FRACTION))
}

// Fraction of the pet's own on-screen height it hops for the STATIONARY jump
// reaction (idle fidget / click-to-pet / turn-end celebrate) — see
// `.pet-jump-bob` in styles.css. This is a CSS transform bob, not the roam
// loop's ledge physics, so it needs its own readable floor/ceiling rather
// than reusing GRAVITY_PX_S2/ledge geometry that only exists once roaming.
const JUMP_BOB_HEIGHT_FRACTION = 0.28
const JUMP_BOB_HEIGHT_MIN_PX = 10
const JUMP_BOB_HEIGHT_MAX_PX = 36

/**
 * How high (px) the stationary jump bob should lift the pet, scaled to its
 * on-screen height so small/large pet scales both read as a real hop rather
 * than a barely-there wobble or a wildly oversized pogo.
 */
export function jumpBobHeightPx(petH: number): number {
  return Math.min(JUMP_BOB_HEIGHT_MAX_PX, Math.max(JUMP_BOB_HEIGHT_MIN_PX, petH * JUMP_BOB_HEIGHT_FRACTION))
}
